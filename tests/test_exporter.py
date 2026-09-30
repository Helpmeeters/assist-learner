"""Exporter safety: validation, shadowing, atomic writes, and hand edits."""

from unittest.mock import patch

import yaml

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er, issue_registry as ir, llm

from custom_components.assist_learner.const import DOMAIN, ISSUE_EXPORT_FAILED
from custom_components.assist_learner.exporter import (
    ExportValidationError,
    resolve_language_variant,
)

from .conftest import House, converse, learn, sentences_file, turn_on_area


def turn_on_name(name: str):
    return lambda _: [llm.ToolInput(tool_name="intent__HassTurnOn", tool_args={"name": name})]


async def test_language_variant() -> None:
    """en-US resolves to the en directory the default agent reads."""
    assert resolve_language_variant("en-US") == "en"
    assert resolve_language_variant("en") == "en"


async def test_malformed_sentence_never_written(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """A broken edited sentence is refused; the file and local intents keep working."""
    learner = learner_entry.runtime_data
    good = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    await learner.async_approve(good["id"])
    before = sentences_file(hass).read_text()

    bad = await learn(hass, fake_llm, "brighten the bedroom up", turn_on_area("Bedroom"), house.kitchen_satellite)
    await learner.async_approve(bad["id"], sentence="brighten [the bedroom")
    assert bad["export_error"] and "parse" in bad["export_error"]
    assert sentences_file(hass).read_text() == before
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_EXPORT_FAILED) is not None

    house.turn_on_calls.clear()
    await converse(hass, "it's too dark in here", conversation.HOME_ASSISTANT_AGENT, house.kitchen_satellite)
    assert house.turned_on() == {"light.kitchen_ceiling"}
    house.turn_on_calls.clear()
    await converse(hass, "turn on the bedroom lamp", conversation.HOME_ASSISTANT_AGENT, house.kitchen_satellite)
    assert house.turned_on() == {"light.bedroom_lamp"}


async def test_file_validation_failure_keeps_old_file(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """If the merged file fails validation, nothing is swapped in."""
    learner = learner_entry.runtime_data
    first = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    await learner.async_approve(first["id"])
    before = sentences_file(hass).read_text()

    second = await learn(hass, fake_llm, "brighten the bedroom up", turn_on_area("Bedroom"), house.kitchen_satellite)
    with patch(
        "custom_components.assist_learner.exporter._Validator.check_file",
        side_effect=ExportValidationError("simulated"),
    ):
        await learner.async_approve(second["id"])
    assert sentences_file(hass).read_text() == before
    assert learner.last_export_result.file_error
    assert hass.states.get("sensor.assist_learner_status").state == "error"
    assert not [p for p in sentences_file(hass).parent.iterdir() if p.suffix == ".tmp"]


async def test_names_with_template_syntax(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """Entity and area names containing hassil template characters don't break export."""
    ent_reg = er.async_get(hass)
    entity = ent_reg.async_get_or_create(
        "light", "test", "odd", suggested_object_id="odd", original_name="Lamp (old"
    )
    ent_reg.async_update_entity(entity.entity_id, area_id=house.kitchen.id)
    hass.states.async_set(entity.entity_id, "off", {"friendly_name": "Lamp (old"})
    ar.async_get(hass).async_create("Den [2")

    learner = learner_entry.runtime_data
    entry = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    await learner.async_approve(entry["id"])
    assert not entry["export_error"]
    assert "it's too dark in here" in sentences_file(hass).read_text()


async def test_shadowing_rejected(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """A phrase built-in sentences already handle is not exported over them."""
    learner = learner_entry.runtime_data
    entry = await learn(
        hass, fake_llm, "turn on the bedroom lamp", turn_on_name("Bedroom Lamp"), house.kitchen_satellite
    )
    await learner.async_approve(entry["id"])
    assert "already handles" in entry["export_error"]
    assert not sentences_file(hass).exists()


async def test_hand_edits_preserved(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """A block the user edited is never rewritten, even when the file is regenerated."""
    learner = learner_entry.runtime_data
    first = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    await learner.async_approve(first["id"])

    path = sentences_file(hass)
    doc = yaml.safe_load(path.read_text())
    doc["intents"]["HassTurnOn"]["data"][0]["sentences"] = ["it's way too dark in here"]
    path.write_text(yaml.safe_dump(doc))

    second = await learn(hass, fake_llm, "brighten the bedroom up", turn_on_area("Bedroom"), house.kitchen_satellite)
    await learner.async_approve(second["id"])

    sentences = [
        s
        for block in yaml.safe_load(path.read_text())["intents"]["HassTurnOn"]["data"]
        for s in block["sentences"]
    ]
    assert "it's way too dark in here" in sentences
    assert "brighten the bedroom up" in sentences


async def test_forget_removes_block(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """Forgetting an exported entry removes its sentence and reloads conversation."""
    learner = learner_entry.runtime_data
    entry = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    await learner.async_approve(entry["id"])
    await learner.async_forget(entry["id"])
    assert yaml.safe_load(sentences_file(hass).read_text())["intents"] == {}

    house.turn_on_calls.clear()
    await converse(hass, "it's too dark in here", conversation.HOME_ASSISTANT_AGENT, house.kitchen_satellite)
    assert house.turned_on() == set()


async def test_foreign_blocks_kept(
    hass: HomeAssistant, house: House, fake_llm, learner_entry
) -> None:
    """Blocks without our metadata (added by hand) survive regeneration."""
    learner = learner_entry.runtime_data
    path = sentences_file(hass)
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({
        "language": "en",
        "intents": {"HassTurnOff": {"data": [{"sentences": ["lights out everybody"], "slots": {"domain": "light"}}]}},
    }))
    entry = await learn(hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite)
    await learner.async_approve(entry["id"])
    doc = yaml.safe_load(path.read_text())
    assert doc["intents"]["HassTurnOff"]["data"][0]["sentences"] == ["lights out everybody"]
    assert "HassTurnOn" in doc["intents"]
