"""Setup, unload, staleness, watchdog, config flow, and diagnostics."""

from datetime import timedelta
from unittest.mock import patch

import yaml

from homeassistant import config_entries
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.assist_learner.const import (
    CONF_REPLAY,
    CONF_THRESHOLD,
    DOMAIN,
    ISSUE_CAPTURE_PAUSED,
    ISSUE_WATCHDOG,
)
from custom_components.assist_learner.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import House, flush_turns, learn, sentences_file, turn_on_area


async def test_unload_mid_exchange(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """Unloading with a turn pending drops it cleanly; the store survives a reload."""
    learner = learner_entry.runtime_data
    entry = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)

    fake_llm.script = turn_on_area("Bedroom")
    await conversation.async_converse(
        hass, "brighten the bedroom up", None, Context(), language="en",
        agent_id=fake_llm.agent_id, device_id=house.kitchen_satellite,
    )
    assert learner.capture._pending

    assert await hass.config_entries.async_unload(learner_entry.entry_id)
    await flush_turns(hass)
    assert not learner.capture._pending

    assert await hass.config_entries.async_setup(learner_entry.entry_id)
    await hass.async_block_till_done()
    reloaded = learner_entry.runtime_data
    assert reloaded is not learner
    assert reloaded.store.get(entry["id"])["status"] == "proposed"
    assert len(reloaded.store.entries) == 1


async def test_unsupported_core_pauses(hass: HomeAssistant, house: House, fake_llm) -> None:
    """If the version gate fails, capture pauses with a repair issue instead of guessing."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_THRESHOLD: 2})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.assist_learner.learner.unsupported_reason",
        return_value="simulated core change",
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CAPTURE_PAUSED)
    assert issue.translation_placeholders == {"reason": "simulated core change"}
    assert hass.states.get("sensor.assist_learner_status").state == "paused"


async def test_removed_entity_marks_stale_and_unexports(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """Removing a target entity flags the entry stale and drops it from the file."""
    learner = learner_entry.runtime_data
    entry = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    await learner.async_approve(entry["id"])

    er.async_get(hass).async_remove("light.kitchen_ceiling")
    await hass.async_block_till_done()
    assert entry["stale"] is True
    assert yaml.safe_load(sentences_file(hass).read_text())["intents"] == {}
    assert hass.states.get("sensor.assist_learner_status").attributes["stale"] == 1


async def test_renamed_area_marks_fixed_area_entry_stale(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """A fixed area slot that no longer resolves is flagged stale."""
    entry = await learn(
        hass, fake_llm, "make the kitchen bright please", turn_on_area("Kitchen"), house.bedroom_satellite
    )
    assert entry["calls"][0]["slots"]["area"] == "Kitchen"
    ar.async_get(hass).async_update(house.kitchen.id, name="Galley")
    await hass.async_block_till_done()
    assert entry["stale"] is True


async def test_watchdog(hass: HomeAssistant, house: House, fake_llm, learner_entry) -> None:
    """Conversations without any parsed actions for a week raise a watchdog issue."""
    learner = learner_entry.runtime_data
    now = dt_util.utcnow()
    learner.started = now - timedelta(days=8)
    learner.capture.last_event = now - timedelta(hours=1)
    learner.last_tool_exchange = None
    learner._watchdog(now)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_WATCHDOG) is not None

    learner.last_tool_exchange = now - timedelta(hours=2)
    learner._watchdog(now)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_WATCHDOG) is None


async def test_config_flow_requires_llm_agent(hass: HomeAssistant, house: House) -> None:
    """Without an LLM agent there's nothing to learn from."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_llm_agent"


async def test_config_flow_creates_entry(hass: HomeAssistant, house: House, fake_llm) -> None:
    """One screen; replay needs a fallback agent."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_THRESHOLD: 3, CONF_REPLAY: True}
    )
    assert result["errors"] == {"fallback_agent_id": "fallback_required"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_THRESHOLD: 3, CONF_REPLAY: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_THRESHOLD] == 3
    await hass.async_block_till_done()


async def test_options_flow(hass: HomeAssistant, house: House, fake_llm, learner_entry) -> None:
    """Options change the threshold and reload."""
    result = await hass.config_entries.options.async_init(learner_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_THRESHOLD: 4, CONF_REPLAY: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert learner_entry.runtime_data.threshold == 4


async def test_diagnostics_redacted(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """Diagnostics never include what was said."""
    await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    diagnostics = await async_get_config_entry_diagnostics(hass, learner_entry)
    rendered = repr(diagnostics)
    assert "dark" not in rendered
    assert "light.kitchen_ceiling" not in rendered
    assert diagnostics["counts"]["proposed"] == 1
