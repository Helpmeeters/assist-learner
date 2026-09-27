"""Learn from a real chat log, approve, and run the result through the real default agent."""

import yaml

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.assist_learner.const import (
    DOMAIN,
    ISSUE_REVIEW,
    STATUS_APPROVED,
    STATUS_CANDIDATE,
    STATUS_PROPOSED,
)

from .conftest import House, converse, turn_on_area

PHRASE = "It's too dark in here"


async def _learn_and_approve(hass, house, fake_llm, learner_entry) -> dict:
    learner = learner_entry.runtime_data
    fake_llm.script = turn_on_area("Kitchen")

    await converse(hass, PHRASE, fake_llm.agent_id, house.kitchen_satellite)
    (entry,) = learner.store.entries.values()
    assert entry["status"] == STATUS_CANDIDATE
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REVIEW) is None

    await converse(hass, PHRASE, fake_llm.agent_id, house.kitchen_satellite)
    assert entry["status"] == STATUS_PROPOSED
    assert entry["count"] == 2
    assert entry["room_relative"] is True
    assert entry["calls"] == [
        {"name": "intent__HassTurnOn", "slots": {"domain": "light"}}
    ]
    assert entry["entities"] == ["light.kitchen_ceiling"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REVIEW) is not None

    assert await learner.async_approve(entry["id"])
    await hass.async_block_till_done()
    assert entry["status"] == STATUS_APPROVED
    assert entry["export_error"] is None, entry["export_error"]
    return entry


async def test_learned_sentence_stays_in_the_speaking_room(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """A kitchen-learned sentence turns on only the speaking room's lights, never the whole house."""
    entry = await _learn_and_approve(hass, house, fake_llm, learner_entry)

    path = hass.config.path("custom_sentences", "en", "assist_learner.yaml")
    with open(path, encoding="utf-8") as file:
        doc = yaml.safe_load(file)
    (block,) = doc["intents"]["HassTurnOn"]["data"]
    assert block["sentences"] == ["it's too dark in here"]
    assert block["requires_context"] == {"area": {"slot": True}}
    assert "area" not in block.get("slots", {})
    assert block["metadata"]["assist_learner_id"] == entry["id"]

    house.turn_on_calls.clear()
    await converse(hass, PHRASE, conversation.HOME_ASSISTANT_AGENT, house.bedroom_satellite)
    assert house.turned_on() == {"light.bedroom_lamp"}

    house.turn_on_calls.clear()
    await converse(hass, PHRASE, conversation.HOME_ASSISTANT_AGENT, house.kitchen_satellite)
    assert house.turned_on() == {"light.kitchen_ceiling"}

    house.turn_on_calls.clear()
    result = await converse(
        hass, PHRASE, conversation.HOME_ASSISTANT_AGENT, house.roaming_satellite
    )
    assert house.turned_on() == set()
    assert result.response.response_type.value == "error"
