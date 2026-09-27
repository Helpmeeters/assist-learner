"""Chat-log event sequencing, callback isolation, and the context adapter."""

from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.components.conversation.chat_log import (
    AssistantContent,
    ChatLog,
    ToolResultContent,
    UserContent,
    current_chat_log,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

from custom_components.assist_learner.capture import ChatLogCapture
from custom_components.assist_learner.models import Exchange

from .conftest import converse, flush_turns, turn_on_area

CONV = "conv-1"


def _chat_log(hass: HomeAssistant, conversation_id: str = CONV) -> ChatLog:
    chat_log = ChatLog(hass, conversation_id)
    chat_log.llm_api = SimpleNamespace(
        llm_context=SimpleNamespace(language="en", device_id=None)
    )
    return chat_log


def _capture(hass: HomeAssistant):
    exchanges: list[Exchange] = []
    errors: list[str] = []
    capture = ChatLogCapture(hass, exchanges.append, errors.append, idle_timeout=15)
    return capture, exchanges, errors


def _run_turn(capture: ChatLogCapture, chat_log: ChatLog, text: str, created: bool) -> None:
    """Replay the event sequence a streaming LLM agent produces."""
    if created:
        capture.handle_event(CONV, "created", {"chat_log": {}})
    user = UserContent(content=text)
    chat_log.content.append(user)
    capture.handle_event(CONV, "content_added", {"content": user.as_dict()})

    token = current_chat_log.set(chat_log)
    try:
        capture.handle_event(CONV, "updated", {"chat_log": {}})
        tool = llm.ToolInput(tool_name="intent__HassTurnOn", tool_args={"name": "Lamp"})
        # Streaming agents append this without firing an event.
        chat_log.content.append(AssistantContent(agent_id="agent", tool_calls=[tool]))
        result = ToolResultContent(
            agent_id="agent",
            tool_call_id=tool.id,
            tool_name=tool.tool_name,
            tool_result={"response_type": "action_done", "data": {"success": [], "failed": []}},
        )
        chat_log.content.append(result)
        capture.handle_event(CONV, "content_added", {"content": result.as_dict()})
        chat_log.content.append(AssistantContent(agent_id="agent", content="Done."))
    finally:
        current_chat_log.reset(token)


async def test_new_conversation(hass: HomeAssistant) -> None:
    """A new conversation yields one first-turn exchange read from the chat log."""
    capture, exchanges, errors = _capture(hass)
    _run_turn(capture, _chat_log(hass), "turn on the lamp please", created=True)
    assert exchanges == []
    await flush_turns(hass)

    (exchange,) = exchanges
    assert exchange.turn_index == 0
    assert exchange.text == "turn on the lamp please"
    assert [c.name for c in exchange.calls] == ["intent__HassTurnOn"]
    assert exchange.calls[0].args == {"name": "Lamp"}
    assert exchange.calls[0].result["response_type"] == "action_done"
    assert exchange.final_speech == "Done."
    assert exchange.agent_ids == {"agent"}
    assert exchange.context.language == "en"
    assert errors == []


async def test_existing_conversation_is_not_first_turn(hass: HomeAssistant) -> None:
    """A log that existed before we subscribed can't prove the turn stands alone."""
    capture, exchanges, _ = _capture(hass)
    _run_turn(capture, _chat_log(hass), "turn on the lamp please", created=False)
    await flush_turns(hass)
    (exchange,) = exchanges
    assert exchange.turn_index is None


async def test_deleted_with_no_content(hass: HomeAssistant) -> None:
    """CREATED then DELETED with nothing in between is ignored."""
    capture, exchanges, errors = _capture(hass)
    capture.handle_event(CONV, "created", {"chat_log": {}})
    capture.handle_event(CONV, "deleted", {})
    await flush_turns(hass)
    assert exchanges == []
    assert errors == []


async def test_deleted_finalizes_pending_turn(hass: HomeAssistant) -> None:
    """DELETED finalizes without waiting for the idle timeout."""
    capture, exchanges, _ = _capture(hass)
    _run_turn(capture, _chat_log(hass), "turn on the lamp please", created=True)
    capture.handle_event(CONV, "deleted", {})
    assert len(exchanges) == 1


async def test_second_turn_before_finalize_supersedes_first(hass: HomeAssistant) -> None:
    """A quick second turn marks the first as a correction."""
    capture, exchanges, _ = _capture(hass)
    chat_log = _chat_log(hass)
    _run_turn(capture, chat_log, "turn on the lamp please", created=True)
    _run_turn(capture, chat_log, "no the other lamp", created=False)
    await flush_turns(hass)

    first, second = exchanges
    assert first.superseded is True
    assert second.turn_index == 1


async def test_callback_never_raises(hass: HomeAssistant) -> None:
    """Malformed events and failing consumers are contained."""
    def boom(_exchange):
        raise RuntimeError("consumer failed")

    capture = ChatLogCapture(hass, boom, lambda _: None, idle_timeout=15)
    capture.handle_event(CONV, "content_added", {"content": "not a dict"})
    assert capture.last_error is not None

    _run_turn(capture, _chat_log(hass), "turn on the lamp please", created=True)
    await flush_turns(hass)
    assert "consumer failed" in capture.last_error


async def test_users_turn_completes_when_learning_fails(
    hass: HomeAssistant, house, fake_llm, learner_entry
) -> None:
    """If evaluation blows up, the user still gets the agent's answer."""
    fake_llm.script = turn_on_area("Kitchen")
    with patch(
        "custom_components.assist_learner.learner.evaluate",
        side_effect=RuntimeError("bug"),
    ):
        result = await converse(hass, "it's too dark in here", fake_llm.agent_id, house.kitchen_satellite)
    assert result.response.speech["plain"]["speech"] == "Done."
    assert house.turned_on() == {"light.kitchen_ceiling"}


async def test_adapter_missing_context(hass: HomeAssistant) -> None:
    """Tools ran but the ContextVar never exposed the chat log: capture reports it."""
    capture, exchanges, errors = _capture(hass)
    capture.handle_event(CONV, "created", {"chat_log": {}})
    capture.handle_event(CONV, "content_added", {"content": {"role": "user", "content": "turn it on now"}})
    capture.handle_event(
        CONV,
        "content_added",
        {"content": {"role": "tool_result", "tool_call_id": "x", "tool_name": "intent__HassTurnOn", "tool_result": {}}},
    )
    await flush_turns(hass)
    (exchange,) = exchanges
    assert exchange.adapter_error
    assert errors


async def test_adapter_shape_changed(hass: HomeAssistant) -> None:
    """If llm_context loses its fields, capture reports it instead of guessing."""
    capture, exchanges, errors = _capture(hass)
    chat_log = _chat_log(hass)
    chat_log.llm_api = SimpleNamespace(llm_context=SimpleNamespace())
    _run_turn(capture, chat_log, "turn on the lamp please", created=True)
    await flush_turns(hass)
    (exchange,) = exchanges
    assert "llm_context shape changed" in exchange.adapter_error
    assert errors


async def test_adapter_error_pauses_learner(
    hass: HomeAssistant, house, fake_llm, learner_entry
) -> None:
    """An adapter failure raises the capture-paused repair issue."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.assist_learner.const import DOMAIN, ISSUE_CAPTURE_PAUSED

    fake_llm.script = turn_on_area("Kitchen")
    with patch(
        "custom_components.assist_learner.capture.active_chat_log", return_value=None
    ):
        await converse(hass, "it's too dark in here", fake_llm.agent_id, house.kitchen_satellite)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CAPTURE_PAUSED) is not None
    assert hass.states.get("sensor.assist_learner_status").state == "paused"
