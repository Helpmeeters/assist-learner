"""Replaying multi-step commands and scripts through the local matcher."""

from unittest.mock import patch

import pytest
import yaml

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir, llm

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.assist_learner.const import (
    CONF_EXTRA_DENYLIST,
    CONF_FALLBACK_AGENT,
    CONF_REPLAY,
    CONF_THRESHOLD,
    DOMAIN,
    ISSUE_REPLAY_AGENT_REMOVED,
    REPLAY_INTENT,
    REPLAY_SLOT,
)

from .conftest import House, converse, learn, sentences_file

LOCAL = conversation.HOME_ASSISTANT_AGENT


def movie_time(_):
    return [
        llm.ToolInput(tool_name="intent__HassTurnOff", tool_args={"area": "Kitchen", "domain": ["light"]}),
        llm.ToolInput(tool_name="intent__HassTurnOn", tool_args={"name": "Bedroom Lamp"}),
    ]


async def _setup(hass: HomeAssistant, **data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_THRESHOLD: 2, **data})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def replay_entry(hass: HomeAssistant, fake_llm) -> MockConfigEntry:
    return await _setup(hass, **{CONF_REPLAY: True})


async def _learn_movie_time(hass, house, fake_llm, entry) -> dict:
    learned = await learn(hass, fake_llm, "time for a movie", movie_time, house.kitchen_satellite)
    assert learned["replay_only"]
    await entry.runtime_data.async_approve(learned["id"])
    await hass.async_block_till_done()
    assert learned["export_error"] is None, learned["export_error"]
    return learned


def _replay_blocks(hass: HomeAssistant) -> list[dict]:
    if not sentences_file(hass).is_file():
        return []
    doc = yaml.safe_load(sentences_file(hass).read_text())
    return (doc["intents"].get(REPLAY_INTENT) or {}).get("data") or []


async def test_replays_locally_in_current_area(
    hass: HomeAssistant, house: House, fake_llm, replay_entry
) -> None:
    """The local matcher replays an approved multi-action command in the speaking room."""
    learned = await _learn_movie_time(hass, house, fake_llm, replay_entry)
    (block,) = _replay_blocks(hass)
    assert block["sentences"] == ["time for a movie"]
    assert block["slots"] == {REPLAY_SLOT: learned["id"]}
    assert block["requires_context"] == {"area": {"slot": True}}

    calls_before = fake_llm.calls
    house.turn_on_calls.clear()
    house.turn_off_calls.clear()
    result = await converse(hass, "Time for a movie!", LOCAL, house.bedroom_satellite)

    assert fake_llm.calls == calls_before
    assert result.response.speech["plain"]["speech"] == "Done."
    turned_off = {e for c in house.turn_off_calls for e in c.data["entity_id"]}
    assert turned_off == {"light.bedroom_lamp"}
    assert house.turned_on() == {"light.bedroom_lamp"}
    assert learned["count"] == 2
    assert len(replay_entry.runtime_data.store.entries) == 1


async def test_llm_still_answers_and_learns_unknown(
    hass: HomeAssistant, house: House, fake_llm, replay_entry
) -> None:
    """Nothing is inserted in front of the pipeline's agent; learning continues."""
    fake_llm.script = movie_time
    await converse(hass, "time for a movie", fake_llm.agent_id, house.kitchen_satellite)
    (entry,) = replay_entry.runtime_data.store.entries.values()
    assert entry["count"] == 1
    assert er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, f"{replay_entry.entry_id}_replay"
    ) is None


async def test_replay_off_keeps_it_out_of_the_file(
    hass: HomeAssistant, house: House, fake_llm
) -> None:
    """With replay off, approved multi-step commands aren't written; turning it on writes them."""
    entry = await _setup(hass)
    learned = await learn(hass, fake_llm, "time for a movie", movie_time, house.kitchen_satellite)
    await entry.runtime_data.async_approve(learned["id"])
    assert _replay_blocks(hass) == []

    hass.config_entries.async_update_entry(entry, options={**entry.data, CONF_REPLAY: True})
    await hass.async_block_till_done()
    assert len(_replay_blocks(hass)) == 1

    hass.config_entries.async_update_entry(entry, options={**entry.data, CONF_REPLAY: False})
    await hass.async_block_till_done()
    assert _replay_blocks(hass) == []


async def test_room_relative_without_area_does_not_match(
    hass: HomeAssistant, house: House, fake_llm, replay_entry
) -> None:
    """From a device with no area the local matcher passes, so the pipeline's LLM would answer."""
    await _learn_movie_time(hass, house, fake_llm, replay_entry)
    house.turn_on_calls.clear()
    house.turn_off_calls.clear()
    result = await converse(hass, "time for a movie", LOCAL, house.roaming_satellite)
    assert result.response.response_type.value == "error"
    assert house.turn_off_calls == []
    assert house.turned_on() == set()


async def test_denylist_blocks_replay(
    hass: HomeAssistant, house: House, fake_llm, replay_entry
) -> None:
    """If the current targets become denylisted, replay refuses."""
    await _learn_movie_time(hass, house, fake_llm, replay_entry)
    hass.config_entries.async_update_entry(
        replay_entry, options={**replay_entry.data, CONF_EXTRA_DENYLIST: ["light"]}
    )
    await hass.async_block_till_done()

    house.turn_on_calls.clear()
    house.turn_off_calls.clear()
    result = await converse(hass, "time for a movie", LOCAL, house.bedroom_satellite)
    assert result.response.response_type.value == "error"
    assert house.turn_off_calls == []
    assert house.turned_on() == set()


async def test_unused_old_replay_agent_is_removed(
    hass: HomeAssistant, house: House, fake_llm
) -> None:
    """The old conversation entity is cleaned up when no pipeline uses it."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_THRESHOLD: 2, CONF_REPLAY: True})
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    old = ent_reg.async_get_or_create(
        "conversation", DOMAIN, f"{entry.entry_id}_replay", config_entry=entry
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ent_reg.async_get(old.entity_id) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REPLAY_AGENT_REMOVED) is None


async def test_pipeline_on_old_replay_agent_raises_issue(
    hass: HomeAssistant, house: House, fake_llm
) -> None:
    """A pipeline still pointing at the old agent gets a repair naming the agent to switch back to."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_THRESHOLD: 2, CONF_REPLAY: True, CONF_FALLBACK_AGENT: "conversation.fake_llm"},
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "conversation", DOMAIN, f"{entry.entry_id}_replay", config_entry=entry
    )
    with patch(
        "custom_components.assist_learner._pipelines_using", return_value=["Home Assistant"]
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REPLAY_AGENT_REMOVED)
    assert issue.translation_placeholders == {
        "pipelines": "Home Assistant",
        "agent": "fake llm",
    }
