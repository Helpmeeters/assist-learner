"""Optional replay agent."""

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, llm

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.assist_learner.const import (
    CONF_EXTRA_DENYLIST,
    CONF_FALLBACK_AGENT,
    CONF_REPLAY,
    CONF_THRESHOLD,
    DOMAIN,
)

from .conftest import House, converse, learn


def movie_time(_):
    return [
        llm.ToolInput(tool_name="intent__HassTurnOff", tool_args={"area": "Kitchen", "domain": ["light"]}),
        llm.ToolInput(tool_name="intent__HassTurnOn", tool_args={"name": "Bedroom Lamp"}),
    ]


@pytest.fixture
async def replay_entry(hass: HomeAssistant, fake_llm) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_THRESHOLD: 2, CONF_REPLAY: True, CONF_FALLBACK_AGENT: fake_llm.agent_id},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _replay_agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    return er.async_get(hass).async_get_entity_id("conversation", DOMAIN, f"{entry.entry_id}_replay")


async def _learn_movie_time(hass, house, fake_llm, entry) -> dict:
    learned = await learn(hass, fake_llm, "time for a movie", movie_time, house.kitchen_satellite)
    assert learned["replay_only"]
    await entry.runtime_data.async_approve(learned["id"])
    return learned


async def test_replays_in_current_area(
    hass: HomeAssistant, house: House, fake_llm, replay_entry
) -> None:
    """An approved multi-action command replays using the current request's area, not the LLM."""
    await _learn_movie_time(hass, house, fake_llm, replay_entry)
    agent_id = _replay_agent_id(hass, replay_entry)

    calls_before = fake_llm.calls
    house.turn_on_calls.clear()
    house.turn_off_calls.clear()
    result = await converse(hass, "Time for a movie!", agent_id, house.bedroom_satellite)

    assert fake_llm.calls == calls_before
    assert result.response.speech["plain"]["speech"] == "Done."
    turned_off = {e for c in house.turn_off_calls for e in c.data["entity_id"]}
    assert turned_off == {"light.bedroom_lamp"}
    assert house.turned_on() == {"light.bedroom_lamp"}


async def test_delegates_unknown_and_still_learns(
    hass: HomeAssistant, house: House, fake_llm, replay_entry
) -> None:
    """Anything not approved goes to the fallback agent, and learning continues."""
    agent_id = _replay_agent_id(hass, replay_entry)
    fake_llm.script = movie_time
    calls_before = fake_llm.calls
    await converse(hass, "time for a movie", agent_id, house.kitchen_satellite)
    assert fake_llm.calls == calls_before + 1
    (entry,) = replay_entry.runtime_data.store.entries.values()
    assert entry["count"] == 1


async def test_room_relative_without_area_delegates(
    hass: HomeAssistant, house: House, fake_llm, replay_entry
) -> None:
    """A room-relative replay from a device with no area falls back to the LLM."""
    await _learn_movie_time(hass, house, fake_llm, replay_entry)
    calls_before = fake_llm.calls
    await converse(hass, "time for a movie", _replay_agent_id(hass, replay_entry), house.roaming_satellite)
    assert fake_llm.calls == calls_before + 1


async def test_denylist_blocks_replay(
    hass: HomeAssistant, house: House, fake_llm, replay_entry
) -> None:
    """If the current targets become denylisted, replay refuses and delegates."""
    await _learn_movie_time(hass, house, fake_llm, replay_entry)
    hass.config_entries.async_update_entry(
        replay_entry, options={**replay_entry.data, CONF_EXTRA_DENYLIST: ["light"]}
    )
    await hass.async_block_till_done()

    calls_before = fake_llm.calls
    fake_llm.script = lambda _: []
    await converse(hass, "time for a movie", _replay_agent_id(hass, replay_entry), house.bedroom_satellite)
    assert fake_llm.calls == calls_before + 1
