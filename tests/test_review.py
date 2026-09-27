"""Repair fix flow and services."""

import pytest

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from custom_components.assist_learner.const import DOMAIN, ISSUE_REVIEW

from .conftest import House, converse, learn, sentences_file, turn_on_area


async def _start_flow(hass: HomeAssistant) -> dict:
    return await repairs_flow_manager(hass).async_init(DOMAIN, data={"issue_id": ISSUE_REVIEW})


async def test_fix_flow_approve_and_reject(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """The flow shows resolved entities and steps through every proposal."""
    first = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    second = await learn(hass, fake_llm, "brighten the bedroom up", turn_on_area("Bedroom"), house.kitchen_satellite)

    manager = repairs_flow_manager(hass)
    result = await _start_flow(hass)
    assert result["step_id"] == "review"
    placeholders = result["description_placeholders"]
    assert "Kitchen Ceiling (light.kitchen_ceiling)" in placeholders["entities"]
    assert "whichever area the speaking device is in" in placeholders["action"]

    result = await manager.async_configure(result["flow_id"], {"decision": "approve"})
    assert result["step_id"] == "review"
    assert "Bedroom Lamp" in result["description_placeholders"]["entities"]
    result = await manager.async_configure(result["flow_id"], {"decision": "reject"})
    assert result["type"] == "create_entry"

    assert first["status"] == "approved"
    assert second["status"] == "rejected"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REVIEW) is None
    assert sentences_file(hass).exists()


async def test_fix_flow_skip_keeps_issue(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """Skipping leaves the proposal and the issue in place."""
    entry = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    result = await _start_flow(hass)
    result = await repairs_flow_manager(hass).async_configure(result["flow_id"], {"decision": "skip"})
    assert result["type"] == "abort"
    assert result["reason"] == "skipped"
    assert entry["status"] == "proposed"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REVIEW) is not None


async def test_fix_flow_edit_sentence(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """Editing the sentence in the flow exports the edited wording."""
    entry = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    result = await _start_flow(hass)
    await repairs_flow_manager(hass).async_configure(
        result["flow_id"], {"decision": "approve", "sentence": "it's (too|so) dark in here"}
    )
    assert entry["user_sentence"] is True
    assert "it's (too|so) dark in here" in sentences_file(hass).read_text()


async def test_learn_last_service(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """learn_last proposes after a single run, skipping the threshold."""
    fake_llm.script = turn_on_area("Kitchen")
    await converse(hass, "it's too dark in here", fake_llm.agent_id, house.kitchen_satellite)
    response = await hass.services.async_call(
        DOMAIN, "learn_last", {}, blocking=True, return_response=True
    )
    entry = learner_entry.runtime_data.store.get(response["candidate_id"])
    assert entry["status"] == "proposed"
    assert entry["count"] == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REVIEW) is not None


async def test_services(hass: HomeAssistant, house: House, fake_llm, learner_entry) -> None:
    """approve, forget, and reject work by id; unknown ids raise."""
    entry = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    await hass.services.async_call(DOMAIN, "approve", {"candidate_id": entry["id"]}, blocking=True)
    assert entry["status"] == "approved"
    await hass.services.async_call(DOMAIN, "forget", {"candidate_id": entry["id"]}, blocking=True)
    assert learner_entry.runtime_data.store.get(entry["id"]) is None

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "reject", {"candidate_id": "nope"}, blocking=True)


async def test_learn_last_with_nothing(hass: HomeAssistant, learner_entry) -> None:
    """learn_last explains when there's nothing to learn."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "learn_last", {}, blocking=True, return_response=True)
