"""Shared fixtures: a small house with two rooms and a fake LLM agent."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    AbstractConversationAgent,
    ConversationInput,
    ConversationResult,
)
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.helpers import (
    area_registry as ar,
    chat_session,
    device_registry as dr,
    entity_registry as er,
    intent,
    llm,
)
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.assist_learner.const import CONF_THRESHOLD, DOMAIN, IDLE_TIMEOUT


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


@pytest.fixture(autouse=True)
def isolated_config_dir(hass: HomeAssistant, tmp_path):
    """Keep custom_sentences writes out of the shared testing config."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("")
    return tmp_path


@dataclass
class House:
    """Handles into the test house."""

    kitchen: ar.AreaEntry
    bedroom: ar.AreaEntry
    kitchen_satellite: str
    bedroom_satellite: str
    roaming_satellite: str
    turn_on_calls: list[ServiceCall]
    turn_off_calls: list[ServiceCall]
    extra: dict[str, Any] = field(default_factory=dict)

    def turned_on(self) -> set[str]:
        """Entity ids passed to light.turn_on so far."""
        ids: set[str] = set()
        for call in self.turn_on_calls:
            value = call.data.get("entity_id")
            ids.update([value] if isinstance(value, str) else value or [])
        return ids


@pytest.fixture
async def house(hass: HomeAssistant) -> House:
    """Two rooms, a satellite in each, a light in each, and one satellite with no area."""
    for component in ("homeassistant", "conversation", "intent", "llm"):
        assert await async_setup_component(hass, component, {})

    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen")
    bedroom = area_reg.async_create("Bedroom")

    host = MockConfigEntry(domain="test")
    host.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    ent_reg = er.async_get(hass)

    def device(name: str, area_id: str | None) -> str:
        entry = dev_reg.async_get_or_create(
            config_entry_id=host.entry_id, identifiers={("test", name)}, name=name
        )
        if area_id:
            dev_reg.async_update_device(entry.id, area_id=area_id)
        ent_reg.async_get_or_create(
            "assist_satellite",
            "test",
            name,
            device_id=entry.id,
            suggested_object_id=name.replace(" ", "_"),
        )
        return entry.id

    def light(object_id: str, name: str, area_id: str) -> None:
        entity = ent_reg.async_get_or_create(
            "light", "test", object_id, suggested_object_id=object_id, original_name=name
        )
        ent_reg.async_update_entity(entity.entity_id, area_id=area_id)
        hass.states.async_set(entity.entity_id, "off", {"friendly_name": name})

    light("kitchen_ceiling", "Kitchen Ceiling", kitchen.id)
    light("bedroom_lamp", "Bedroom Lamp", bedroom.id)

    house = House(
        kitchen=kitchen,
        bedroom=bedroom,
        kitchen_satellite=device("kitchen satellite", kitchen.id),
        bedroom_satellite=device("bedroom satellite", bedroom.id),
        roaming_satellite=device("roaming satellite", None),
        turn_on_calls=async_mock_service(hass, "light", "turn_on"),
        turn_off_calls=async_mock_service(hass, "light", "turn_off"),
    )
    await hass.async_block_till_done()
    return house


type Script = Callable[[ConversationInput], list[llm.ToolInput]]


class FakeLLMAgent(AbstractConversationAgent):
    """Behaves like an LLM agent: Assist API, real tool execution, final speech."""

    def __init__(self, hass: HomeAssistant, agent_id: str) -> None:
        self.hass = hass
        self.agent_id = agent_id
        self.script: Script = lambda _: []
        self.reply = "Done."
        self.calls = 0

    @property
    def supported_languages(self) -> list[str]:
        return ["en"]

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        self.calls += 1
        with (
            chat_session.async_get_chat_session(
                self.hass, user_input.conversation_id
            ) as session,
            conversation.async_get_chat_log(self.hass, session, user_input) as chat_log,
        ):
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context("fake_llm"), llm.LLM_API_ASSIST
            )
            tool_calls = self.script(user_input)

            async def stream():
                if tool_calls:
                    yield {"role": "assistant", "tool_calls": tool_calls}
                yield {"role": "assistant", "content": self.reply}

            async for _ in chat_log.async_add_delta_content_stream(self.agent_id, stream()):
                pass
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_speech(self.reply)
            return ConversationResult(
                response=response, conversation_id=chat_log.conversation_id
            )


@pytest.fixture
async def fake_llm(hass: HomeAssistant, house: House) -> FakeLLMAgent:
    """Register the fake LLM agent and a conversation entity state for config-flow checks."""
    entry = MockConfigEntry(domain="fake_llm")
    entry.add_to_hass(hass)
    agent = FakeLLMAgent(hass, entry.entry_id)
    conversation.async_set_agent(hass, entry, agent)
    hass.states.async_set("conversation.fake_llm", "unknown")
    return agent


@pytest.fixture
async def learner_entry(hass: HomeAssistant, fake_llm: FakeLLMAgent) -> MockConfigEntry:
    """Set up Assist Learner with threshold 2."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_THRESHOLD: 2})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def converse(
    hass: HomeAssistant, text: str, agent_id: str, device_id: str | None
) -> ConversationResult:
    """Send one command as a brand-new conversation, as a satellite would, and let it finalize."""
    satellite_id = None
    if device_id:
        satellites = er.async_entries_for_device(er.async_get(hass), device_id)
        satellite_id = next(e.entity_id for e in satellites if e.domain == "assist_satellite")
    result = await conversation.async_converse(
        hass,
        text,
        None,
        Context(),
        language="en",
        agent_id=agent_id,
        device_id=device_id,
        satellite_id=satellite_id,
    )
    await flush_turns(hass)
    return result


async def flush_turns(hass: HomeAssistant) -> None:
    """Advance past the idle timeout so pending turns finalize."""
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=IDLE_TIMEOUT + 1)
    )
    await hass.async_block_till_done()


async def learn(
    hass: HomeAssistant, agent: FakeLLMAgent, text: str, script: Script, device_id: str
) -> dict[str, Any]:
    """Have the LLM handle a command twice so it's proposed; return the entry."""
    agent.script = script
    learner = hass.config_entries.async_loaded_entries(DOMAIN)[0].runtime_data
    before = set(learner.store.entries)
    await converse(hass, text, agent.agent_id, device_id)
    await converse(hass, text, agent.agent_id, device_id)
    new = [e for i, e in learner.store.entries.items() if i not in before]
    assert len(new) == 1, learner.last_rejection
    assert new[0]["status"] == "proposed"
    return new[0]


def sentences_file(hass: HomeAssistant):
    """Path of the exported English sentences file."""
    from pathlib import Path  # noqa: PLC0415

    return Path(hass.config.path("custom_sentences", "en", "assist_learner.yaml"))


def turn_on_area(area: str) -> Script:
    """LLM script: turn on the lights in an area."""
    return lambda _: [
        llm.ToolInput(tool_name="intent__HassTurnOn", tool_args={"area": area, "domain": ["light"]})
    ]
