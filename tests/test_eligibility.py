"""Eligibility rules, denylist, and abstraction into sentences."""

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.assist_learner.denylist import blocked_entities
from custom_components.assist_learner.eligibility import evaluate
from custom_components.assist_learner.models import CapturedCall, Exchange, TurnContext

from .conftest import House


def ok(*entities: str) -> dict:
    return {
        "response_type": "action_done",
        "data": {"success": [{"type": "entity", "id": e, "name": e} for e in entities], "failed": []},
    }


def call(name="intent__HassTurnOn", args=None, result=None, external=False, agent="agent", id_="1"):
    return CapturedCall(
        id=id_,
        name=name,
        args=args if args is not None else {"area": "Kitchen", "domain": ["light"]},
        external=external,
        agent_id=agent,
        result=result if result is not None else ok("light.kitchen_ceiling"),
    )


def exchange(text="it's too dark in here", calls=None, area_id="kitchen", **kwargs) -> Exchange:
    calls = calls if calls is not None else [call()]
    return Exchange(
        conversation_id="c",
        text=text,
        turn_index=kwargs.pop("turn_index", 0),
        context=TurnContext(language="en", device_id="d", area_id=area_id),
        calls=calls,
        agent_ids=kwargs.pop("agent_ids", {c.agent_id for c in calls} or {"agent"}),
        final_speech=kwargs.pop("final_speech", "Done."),
        **kwargs,
    )


async def test_room_relative(hass: HomeAssistant, house: House) -> None:
    """Area equal to the satellite's and not spoken becomes requires_context."""
    result = evaluate(hass, exchange())
    assert result.eligible, result.reason
    assert result.room_relative
    assert result.calls == [{"name": "intent__HassTurnOn", "slots": {"domain": "light"}}]
    assert result.sentence == "it's too dark in here"
    assert not result.replay_only


async def test_named_area_stays_explicit(hass: HomeAssistant, house: House) -> None:
    """If the user said the area, the sentence keeps it as a fixed slot."""
    result = evaluate(hass, exchange(text="make the kitchen bright please"))
    assert result.eligible, result.reason
    assert not result.room_relative
    assert result.calls[0]["slots"] == {"area": "Kitchen", "domain": "light"}


async def test_satellite_without_area_keeps_area(hass: HomeAssistant, house: House) -> None:
    """No satellite area means it can't be room-relative."""
    result = evaluate(hass, exchange(area_id=None))
    assert result.eligible
    assert not result.room_relative
    assert result.calls[0]["slots"]["area"] == "Kitchen"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"calls": [call(external=True)]}, "handled by the local matcher"),
        ({"calls": [call(result={"response_type": "action_done", "data": {"success": [{"type": "entity", "id": "light.a"}], "failed": [{"type": "entity", "id": "light.b"}]}})]}, "partially failed"),
        ({"calls": [call(result={"error": "MatchFailedError"})]}, "failed"),
        ({"final_speech": "Which lights do you mean?"}, "clarifying question"),
        ({"calls": [call(agent="a", id_="1"), call(agent="b", id_="2")]}, "more than one agent"),
        ({"calls": [call(name="homeassistant__GetLiveContext", args={}, result={"success": True, "result": "..."})]}, "read or unsupported"),
        ({"calls": [call(name="intent__HassTurnOn", result={"response_type": "query_answer", "data": {"success": [], "failed": []}})]}, "did not perform an action"),
        ({"calls": [call(), call(name="llm__GetDateTime", args={}, result={"success": True}, id_="2")]}, "read or unsupported"),
        ({"calls": []}, "no actions"),
        ({"turn_index": 1}, "follow-up"),
        ({"turn_index": None}, "follow-up"),
        ({"superseded": True}, "another turn"),
        ({"adapter_error": "boom"}, "context unavailable"),
        ({"calls": [call(name="HassTurnOn")]}, "no domain prefix"),
    ],
)
async def test_rejections(hass: HomeAssistant, house: House, kwargs, reason) -> None:
    """Each unsafe or unlearnable exchange is rejected with a reason."""
    result = evaluate(hass, exchange(**kwargs))
    assert not result.eligible
    assert reason in result.reason


async def test_script_is_replay_only(hass: HomeAssistant, house: House) -> None:
    """Scripts can't become sentences."""
    result = evaluate(
        hass,
        exchange(
            text="get the house ready for bed",
            calls=[call(name="script__good_night", args={}, result={"success": True, "result": {}})],
        ),
    )
    assert result.eligible
    assert result.replay_only
    assert result.entities == ["script.good_night"]


async def test_multi_call_is_replay_only(hass: HomeAssistant, house: House) -> None:
    """Several actions in one command can't be one sentence."""
    result = evaluate(
        hass,
        exchange(
            calls=[
                call(id_="1"),
                call(id_="2", args={"name": "Bedroom Lamp"}, result=ok("light.bedroom_lamp")),
            ]
        ),
    )
    assert result.eligible
    assert result.replay_only


async def test_number_becomes_range_slot(hass: HomeAssistant, house: House) -> None:
    """A spoken number tied to a range slot becomes {brightness}."""
    assert await async_setup_component(hass, "light", {})
    await hass.async_block_till_done()
    result = evaluate(
        hass,
        exchange(
            text="set the mood lighting to 40%",
            calls=[call(name="light__HassLightSet", args={"area": "Kitchen", "brightness": 40})],
        ),
    )
    assert result.eligible, result.reason
    assert result.sentence == "set the mood lighting to {brightness}[%| percent]"
    assert "brightness" not in result.calls[0]["slots"]


async def test_unmapped_number_rejected(hass: HomeAssistant, house: House) -> None:
    """A literal number that isn't a slot is useless in a sentence."""
    result = evaluate(hass, exchange(text="turn on lamp number 3 please"))
    assert not result.eligible
    assert "number" in result.reason


async def test_same_command_in_two_rooms_agrees(hass: HomeAssistant, house: House) -> None:
    """Room-relative runs from different rooms share one agreement key."""
    kitchen = evaluate(hass, exchange())
    bedroom = evaluate(
        hass,
        exchange(
            area_id="bedroom",
            calls=[call(args={"area": "Bedroom", "domain": ["light"]}, result=ok("light.bedroom_lamp"))],
        ),
    )
    assert kitchen.key == bedroom.key


@pytest.mark.parametrize(
    ("entity_id", "attributes", "blocked"),
    [
        ("lock.front_door", {}, True),
        ("alarm_control_panel.home", {}, True),
        ("valve.main_water", {}, True),
        ("cover.garage", {"device_class": "garage"}, True),
        ("cover.driveway", {"device_class": "gate"}, True),
        ("cover.blinds", {"device_class": "blind"}, False),
        ("light.kitchen_ceiling", {}, False),
    ],
)
async def test_denylist(hass: HomeAssistant, entity_id, attributes, blocked) -> None:
    """The floor blocks locks, alarms, valves, and garage or gate covers."""
    hass.states.async_set(entity_id, "closed", attributes)
    assert bool(blocked_entities(hass, [entity_id])) is blocked


async def test_denylist_extra_domains(hass: HomeAssistant) -> None:
    """Users can add domains."""
    assert blocked_entities(hass, ["switch.heater"], ["switch"]) == ["switch.heater"]


async def test_denylist_checks_resolved_entities(hass: HomeAssistant, house: House) -> None:
    """HassTurnOff on 'front door' is harmless-looking but unlocks a lock."""
    hass.states.async_set("lock.front_door", "locked")
    result = evaluate(
        hass,
        exchange(
            text="open up the front for me",
            calls=[call(name="intent__HassTurnOff", args={"name": "front door"}, result=ok("lock.front_door"))],
        ),
    )
    assert not result.eligible
    assert "denylisted" in result.reason
