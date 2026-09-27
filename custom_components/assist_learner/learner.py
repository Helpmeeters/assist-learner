"""Coordinates capture, the candidate store, review, and export."""

import asyncio
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    entity_registry as er,
    intent,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .capture import ChatLogCapture
from .const import (
    CONF_EXTRA_DENYLIST,
    CONF_LANGUAGE,
    CONF_THRESHOLD,
    DEFAULT_THRESHOLD,
    DOMAIN,
    IDLE_TIMEOUT,
    ISSUE_CAPTURE_PAUSED,
    ISSUE_EXPORT_FAILED,
    ISSUE_REVIEW,
    ISSUE_WATCHDOG,
    SIGNAL_UPDATED,
    STATUS_APPROVED,
    STATUS_PROPOSED,
    WATCHDOG_INTERVAL,
    WATCHDOG_WINDOW,
)
from .context_adapter import unsupported_reason
from .eligibility import evaluate
from .exporter import ExportResult, async_export
from .models import Exchange
from .store import LearnerStore

_LOGGER = logging.getLogger(__name__)

_ENTITY_CHANGES_THAT_STALE = frozenset(
    {"entity_id", "name", "aliases", "area_id", "device_id", "disabled_by"}
)


class Learner:
    """Owns everything that runs while the config entry is loaded."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.store = LearnerStore(hass)
        self.capture = ChatLogCapture(
            hass, self._on_exchange, self._on_adapter_error, IDLE_TIMEOUT
        )
        self.started = dt_util.utcnow()
        self.capture_paused: str | None = None
        self.last_export: datetime | None = None
        self.last_export_result: ExportResult | None = None
        self.last_tool_exchange: datetime | None = None
        self.last_rejection: dict[str, Any] | None = None
        self.ignored_agent_ids: set[str] = set()
        self._export_lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []

    @property
    def options(self) -> dict[str, Any]:
        """Effective options."""
        return {**self.entry.data, **self.entry.options}

    @property
    def threshold(self) -> int:
        """Agreeing runs needed before a proposal."""
        return int(self.options.get(CONF_THRESHOLD, DEFAULT_THRESHOLD))

    @property
    def language(self) -> str:
        """Default language for export."""
        return self.options.get(CONF_LANGUAGE) or self.hass.config.language

    @property
    def last_error(self) -> str | None:
        """Most recent error from any stage."""
        if self.capture_paused:
            return self.capture_paused
        if self.last_export_result and self.last_export_result.file_error:
            return self.last_export_result.file_error
        return self.capture.last_error

    async def async_start(self) -> None:
        """Load state and begin capturing."""
        await self.store.async_load()

        if reason := unsupported_reason():
            self._pause(reason)
        else:
            from homeassistant.components.conversation.chat_log import (  # noqa: PLC0415
                async_subscribe_chat_logs,
            )

            self._unsubs.append(
                async_subscribe_chat_logs(self.hass, self.capture.handle_event)
            )
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CAPTURE_PAUSED)

        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_entity_registry_updated
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                ar.EVENT_AREA_REGISTRY_UPDATED, self._on_area_registry_updated
            )
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._watchdog, WATCHDOG_INTERVAL)
        )
        self.async_update_review_issue()
        if any(e["status"] == STATUS_APPROVED for e in self.store.entries.values()):
            self.hass.async_create_task(self.async_export(), eager_start=False)

    async def async_stop(self) -> None:
        """Stop capturing and flush state."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.capture.async_shutdown()
        await self.store.async_save_now()

    @callback
    def _pause(self, reason: str) -> None:
        self.capture_paused = reason
        _LOGGER.warning("Assist Learner capture paused: %s", reason)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_CAPTURE_PAUSED,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_CAPTURE_PAUSED,
            translation_placeholders={"reason": reason},
        )
        self._notify()

    @callback
    def _on_adapter_error(self, reason: str) -> None:
        if self.capture_paused != reason:
            self._pause(reason)

    @callback
    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATED)

    @callback
    def _on_exchange(self, exchange: Exchange) -> None:
        if exchange.agent_ids & self.ignored_agent_ids:
            return
        if any(not call.external for call in exchange.calls):
            self.last_tool_exchange = dt_util.utcnow()
        evaluation = evaluate(
            self.hass, exchange, self.options.get(CONF_EXTRA_DENYLIST, [])
        )
        if not evaluation.eligible:
            _LOGGER.debug("Not learning %r: %s", exchange.text, evaluation.reason)
            self.last_rejection = {"reason": evaluation.reason, "at": dt_util.utcnow()}
            self._notify()
            return
        entry = self.store.async_record(evaluation, exchange.text, self.threshold)
        if entry is not None:
            _LOGGER.debug(
                "Learned %r (%s, count %s)", entry["sentence"], entry["status"], entry["count"]
            )
        self.async_update_review_issue()
        self._notify()

    @callback
    def async_update_review_issue(self) -> None:
        """Create or clear the grouped review issue."""
        proposed = self.store.by_status(STATUS_PROPOSED)
        if not proposed:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_REVIEW)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_REVIEW,
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_REVIEW,
            translation_placeholders={"count": str(len(proposed))},
        )

    async def async_approve(self, entry_id: str, sentence: str | None = None) -> bool:
        """Approve and export."""
        if not self.store.async_approve(entry_id, sentence):
            return False
        self.async_update_review_issue()
        await self.async_export()
        return True

    async def async_reject(self, entry_id: str) -> bool:
        """Reject permanently and remove from the file if exported."""
        if not self.store.async_reject(entry_id):
            return False
        self.async_update_review_issue()
        await self.async_export()
        return True

    async def async_forget(self, entry_id: str) -> bool:
        """Delete an entry and remove it from the file."""
        if not self.store.async_forget(entry_id):
            return False
        self.async_update_review_issue()
        await self.async_export()
        return True

    @callback
    def async_learn_last(self) -> str | None:
        """Send the most recent eligible command straight to review."""
        entry_id = self.store.data.get("last_eligible")
        if not entry_id or not self.store.async_propose(entry_id):
            return None
        self.async_update_review_issue()
        self._notify()
        return entry_id

    async def async_export(self) -> ExportResult:
        """Export approved entries and surface failures as a repair issue."""
        async with self._export_lock:
            result = await async_export(self.hass, self.store, self.language)
        self.last_export = dt_util.utcnow()
        self.last_export_result = result
        if result.file_error or result.errors:
            details = result.file_error or "; ".join(
                f"{self._describe(entry_id)}: {error}"
                for entry_id, error in result.errors.items()
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_EXPORT_FAILED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_EXPORT_FAILED,
                translation_placeholders={"details": details},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_EXPORT_FAILED)
        self._notify()
        return result

    def _describe(self, entry_id: str) -> str:
        entry = self.store.get(entry_id)
        return f'"{entry["sentence"]}"' if entry else entry_id

    @callback
    def _watchdog(self, now: datetime) -> None:
        window_start = now - WATCHDOG_WINDOW
        silent = (
            self.capture_paused is None
            and self.started < window_start
            and self.capture.last_event is not None
            and self.capture.last_event > window_start
            and (self.last_tool_exchange is None or self.last_tool_exchange < window_start)
        )
        if silent:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_WATCHDOG,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_WATCHDOG,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_WATCHDOG)

    @callback
    def _on_entity_registry_updated(self, event: Event) -> None:
        data = event.data
        action = data.get("action")
        if action == "remove":
            ids = {data.get("entity_id")}
            reason = "a target entity was removed"
        elif action == "update" and _ENTITY_CHANGES_THAT_STALE & set(
            data.get("changes") or {}
        ):
            ids = {data.get("entity_id"), data.get("old_entity_id")}
            reason = "a target entity was renamed or moved"
        else:
            return
        if self.store.async_mark_stale(
            lambda e: bool(ids & set(e.get("entities") or [])), reason
        ):
            self._schedule_export()

    @callback
    def _on_area_registry_updated(self, event: Event) -> None:
        if event.data.get("action") not in ("remove", "update"):
            return
        area_id = event.data.get("area_id")
        area_reg = ar.async_get(self.hass)

        def _affected(entry: dict[str, Any]) -> bool:
            if entry.get("room_relative") and entry.get("area_id") == area_id:
                return event.data["action"] == "remove"
            for call in entry.get("calls") or []:
                area_name = (call.get("slots") or {}).get("area")
                if isinstance(area_name, str) and not list(
                    intent.find_areas(area_name, area_reg)
                ):
                    return True
            return False

        if self.store.async_mark_stale(_affected, "a target area was renamed or removed"):
            self._schedule_export()

    @callback
    def _schedule_export(self) -> None:
        self._notify()
        self.hass.async_create_task(self.async_export(), eager_start=False)
