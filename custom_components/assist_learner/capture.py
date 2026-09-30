"""Assemble finalized exchanges from chat-log events.

Events are only triggers: they mark turn boundaries and give us a moment,
inside the agent's task, to grab the ChatLog object. The turn's contents are
read from that object when the turn finalizes.
"""

from collections.abc import Callable
from datetime import datetime
import json
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .context_adapter import ContextAdapterError, active_chat_log, snapshot_turn
from .models import Exchange

_LOGGER = logging.getLogger(__name__)

EVENT_CREATED = "created"
EVENT_UPDATED = "updated"
EVENT_DELETED = "deleted"
EVENT_CONTENT_ADDED = "content_added"


class ChatLogCapture:
    """Turns the chat-log event stream into one Exchange per user turn."""

    def __init__(
        self,
        hass: HomeAssistant,
        on_exchange: Callable[[Exchange], None],
        on_adapter_error: Callable[[str], None],
        idle_timeout: float,
    ) -> None:
        """Initialize the capture."""
        self._hass = hass
        self._on_exchange = on_exchange
        self._on_adapter_error = on_adapter_error
        self._idle_timeout = idle_timeout
        self._pending: dict[str, Exchange] = {}
        self._chat_logs: dict[str, Any] = {}
        self._saw_tool_result: set[str] = set()
        self._timers: dict[str, CALLBACK_TYPE] = {}
        # User turns seen per conversation; absent means the log predates us.
        self._turn_counts: dict[str, int] = {}
        self.last_event: datetime | None = None
        self.last_exchange: datetime | None = None
        self.last_error: str | None = None

    @callback
    def handle_event(
        self, conversation_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        """Chat-log subscriber. Core doesn't guard subscribers, so this must never raise."""
        try:
            self._handle_event(conversation_id, str(event_type), data)
        except Exception as err:  # noqa: BLE001
            self.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.exception("Assist Learner failed to process a chat-log event")

    @callback
    def async_shutdown(self) -> None:
        """Drop pending turns and timers."""
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
        self._pending.clear()
        self._chat_logs.clear()
        self._saw_tool_result.clear()
        self._turn_counts.clear()

    def _handle_event(
        self, conversation_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        self.last_event = dt_util.utcnow()

        if event_type == EVENT_CREATED:
            self._turn_counts[conversation_id] = 0
            return
        if event_type == EVENT_DELETED:
            self._finalize(conversation_id)
            self._turn_counts.pop(conversation_id, None)
            return
        if event_type not in (EVENT_CONTENT_ADDED, EVENT_UPDATED):
            return

        content = data.get("content") or {}
        if event_type == EVENT_CONTENT_ADDED and content.get("role") == "user":
            if conversation_id in self._pending:
                self._pending[conversation_id].superseded = True
                self._finalize(conversation_id)
            count = self._turn_counts.get(conversation_id)
            self._turn_counts[conversation_id] = (count or 0) + 1
            self._pending[conversation_id] = Exchange(
                conversation_id=conversation_id,
                text=str(content.get("content") or ""),
                turn_index=count,
            )

        if conversation_id not in self._pending:
            return
        if content.get("role") == "tool_result":
            self._saw_tool_result.add(conversation_id)
        if conversation_id not in self._chat_logs:
            try:
                chat_log = active_chat_log(conversation_id)
            except ContextAdapterError as err:
                self._adapter_error(self._pending[conversation_id], str(err))
                chat_log = None
            if chat_log is not None:
                self._chat_logs[conversation_id] = chat_log
        self._schedule_finalize(conversation_id)

    def _adapter_error(self, exchange: Exchange, reason: str) -> None:
        exchange.adapter_error = reason
        self.last_error = reason
        self._on_adapter_error(reason)

    def _schedule_finalize(self, conversation_id: str) -> None:
        if (cancel := self._timers.pop(conversation_id, None)) is not None:
            cancel()

        @callback
        def _fire(_now: datetime) -> None:
            self._timers.pop(conversation_id, None)
            self._finalize(conversation_id)

        self._timers[conversation_id] = async_call_later(
            self._hass, self._idle_timeout, _fire
        )

    def _finalize(self, conversation_id: str) -> None:
        if (cancel := self._timers.pop(conversation_id, None)) is not None:
            cancel()
        chat_log = self._chat_logs.pop(conversation_id, None)
        saw_tool_result = conversation_id in self._saw_tool_result
        self._saw_tool_result.discard(conversation_id)
        if (exchange := self._pending.pop(conversation_id, None)) is None:
            return

        if chat_log is not None and not exchange.adapter_error:
            try:
                snapshot = snapshot_turn(self._hass, chat_log, exchange.text)
            except ContextAdapterError as err:
                self._adapter_error(exchange, str(err))
            else:
                exchange.context = snapshot.context
                exchange.calls = snapshot.calls
                exchange.agent_ids = snapshot.agent_ids
                exchange.final_speech = snapshot.final_speech
                exchange.system_prompt = snapshot.system_prompt
                exchange.tool_names = snapshot.tool_names
        elif chat_log is None and saw_tool_result and not exchange.adapter_error:
            self._adapter_error(exchange, "tools ran but the active chat log was not available")

        if _LOGGER.isEnabledFor(logging.DEBUG):
            try:
                self._log_turn(exchange, chat_log is not None)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Assist Learner failed to log a turn")
        self.last_exchange = dt_util.utcnow()
        try:
            self._on_exchange(exchange)
        except Exception as err:  # noqa: BLE001
            self.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.exception("Assist Learner failed to process an exchange")

    def _log_turn(self, exchange: Exchange, had_chat_log: bool) -> None:
        context = exchange.context
        device_name = area_name = None
        if context and context.device_id:
            if device := dr.async_get(self._hass).async_get(context.device_id):
                device_name = device.name_by_user or device.name
        if context and context.area_id:
            if area := ar.async_get(self._hass).async_get_area(context.area_id):
                area_name = area.name
        trace = {
            "conversation_id": exchange.conversation_id,
            "turn_index": exchange.turn_index,
            "text": exchange.text,
            "language": context.language if context else None,
            "device_id": context.device_id if context else None,
            "device": device_name,
            "area_id": context.area_id if context else None,
            "area": area_name,
            "agent_ids": sorted(exchange.agent_ids),
            "calls": [
                {
                    "name": call.name,
                    "args": call.args,
                    "external": call.external,
                    "result": call.result,
                }
                for call in exchange.calls
            ],
            "final_speech": exchange.final_speech,
            "superseded": exchange.superseded,
            "adapter_error": exchange.adapter_error,
            "had_chat_log": had_chat_log,
            "tools": exchange.tool_names,
        }
        _LOGGER.debug("Turn: %s", json.dumps(trace, ensure_ascii=False, default=str))
        if exchange.system_prompt is not None:
            _LOGGER.debug(
                "System prompt for conversation %s:\n%s",
                exchange.conversation_id,
                exchange.system_prompt,
            )
