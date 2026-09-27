"""Isolates Assist Learner's reliance on conversation internals.

Chat-log subscriber events are not enough on their own: they carry no
language, device, or area, and assistant messages added through
``ChatLog.async_add_assistant_content`` (the path streaming LLM agents use)
are appended without an event, so tool-call arguments and the final reply
never reach subscribers. The ChatLog object does hold all of it, and the
internal ``current_chat_log`` ContextVar exposes that object while the agent
runs. Everything touching that private surface lives here so a core change
breaks one module, loudly.
"""

from dataclasses import dataclass, field
import json
from typing import Any

from awesomeversion import AwesomeVersion

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import MIN_HA_VERSION
from .models import CapturedCall, TurnContext


class ContextAdapterError(Exception):
    """The internal chat-log shape no longer matches what this adapter expects."""


@dataclass(slots=True)
class TurnSnapshot:
    """Everything the agent did after one user message."""

    context: TurnContext | None
    calls: list[CapturedCall] = field(default_factory=list)
    agent_ids: set[str] = field(default_factory=set)
    final_speech: str | None = None


def unsupported_reason() -> str | None:
    """Return why capture can't run on this Home Assistant, or None if it can."""
    if AwesomeVersion(HA_VERSION) < AwesomeVersion(MIN_HA_VERSION):
        return f"Home Assistant {HA_VERSION} is older than {MIN_HA_VERSION}"
    try:
        from homeassistant.components.conversation import (  # noqa: PLC0415
            chat_log,
        )
    except ImportError as err:
        return f"conversation.chat_log is not importable: {err}"
    for name in ("current_chat_log", "async_subscribe_chat_logs"):
        if not hasattr(chat_log, name):
            return f"conversation.chat_log.{name} is missing"
    return None


def active_chat_log(conversation_id: str) -> Any | None:
    """Return the ChatLog the running agent is using, if it belongs to this conversation.

    Must be called synchronously from the chat-log callback.
    """
    from homeassistant.components.conversation.chat_log import (  # noqa: PLC0415
        current_chat_log,
    )

    chat_log = current_chat_log.get()
    if chat_log is None or getattr(chat_log, "conversation_id", None) != conversation_id:
        return None
    if not isinstance(getattr(chat_log, "content", None), list):
        raise ContextAdapterError("ChatLog.content is no longer a list")
    return chat_log


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _turn_context(hass: HomeAssistant, chat_log: Any) -> TurnContext | None:
    llm_api = getattr(chat_log, "llm_api", None)
    if llm_api is None:
        return None
    llm_context = getattr(llm_api, "llm_context", None)
    try:
        language = llm_context.language
        device_id = llm_context.device_id
    except AttributeError as err:
        raise ContextAdapterError(f"llm_context shape changed: {err}") from err

    area_id: str | None = None
    if device_id and (device := dr.async_get(hass).async_get(device_id)):
        area_id = dr.async_get_effective_area_id(hass, device)
    return TurnContext(language=language, device_id=device_id, area_id=area_id)


def snapshot_turn(hass: HomeAssistant, chat_log: Any, user_text: str) -> TurnSnapshot:
    """Copy what happened after the last user message matching user_text."""
    content = list(chat_log.content)
    start: int | None = None
    for index in range(len(content) - 1, -1, -1):
        item = content[index]
        if getattr(item, "role", None) == "user" and getattr(item, "content", None) == user_text:
            start = index
            break
    if start is None:
        raise ContextAdapterError("the user message is not in the chat log")

    snapshot = TurnSnapshot(context=_turn_context(hass, chat_log))
    calls_by_id: dict[str, CapturedCall] = {}
    try:
        for item in content[start + 1 :]:
            role = item.role
            if role == "assistant":
                snapshot.agent_ids.add(item.agent_id)
                if item.content:
                    snapshot.final_speech = item.content
                for tool_call in item.tool_calls or []:
                    call = CapturedCall(
                        id=str(tool_call.id),
                        name=str(tool_call.tool_name),
                        args=_json_copy(tool_call.tool_args or {}),
                        external=bool(tool_call.external),
                        agent_id=item.agent_id,
                    )
                    snapshot.calls.append(call)
                    calls_by_id[call.id] = call
            elif role == "tool_result":
                snapshot.agent_ids.add(item.agent_id)
                if (call := calls_by_id.get(str(item.tool_call_id))) is not None:
                    call.result = _json_copy(dict(item.tool_result or {}))
            elif role == "user":
                break
    except AttributeError as err:
        raise ContextAdapterError(f"chat log content shape changed: {err}") from err
    return snapshot
