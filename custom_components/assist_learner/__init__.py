"""Assist Learner: turn repeated LLM fallbacks into reviewed local sentences."""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    intent,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CANDIDATE_ID,
    ATTR_SENTENCE,
    CONF_FALLBACK_AGENT,
    DOMAIN,
    ISSUE_REPLAY_AGENT_REMOVED,
    REPLAY_INTENT,
    SERVICE_APPROVE,
    SERVICE_FORGET,
    SERVICE_LEARN_LAST,
    SERVICE_REJECT,
)
from .learner import Learner
from .replay import ReplayIntentHandler

type AssistLearnerConfigEntry = ConfigEntry[Learner]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [Platform.SENSOR]

_ID_SCHEMA = vol.Schema({vol.Required(ATTR_CANDIDATE_ID): cv.string})
_APPROVE_SCHEMA = _ID_SCHEMA.extend({vol.Optional(ATTR_SENTENCE): cv.string})


def _learner(hass: HomeAssistant) -> Learner:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    return entries[0].runtime_data


def _unknown(candidate_id: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_candidate",
        translation_placeholders={"candidate_id": candidate_id},
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services."""

    async def approve(call: ServiceCall) -> None:
        candidate_id = call.data[ATTR_CANDIDATE_ID]
        if not await _learner(hass).async_approve(
            candidate_id, call.data.get(ATTR_SENTENCE)
        ):
            raise _unknown(candidate_id)

    async def reject(call: ServiceCall) -> None:
        candidate_id = call.data[ATTR_CANDIDATE_ID]
        if not await _learner(hass).async_reject(candidate_id):
            raise _unknown(candidate_id)

    async def forget(call: ServiceCall) -> None:
        candidate_id = call.data[ATTR_CANDIDATE_ID]
        if not await _learner(hass).async_forget(candidate_id):
            raise _unknown(candidate_id)

    async def learn_last(call: ServiceCall) -> ServiceResponse:
        learner = _learner(hass)
        entry_id = learner.async_learn_last()
        if entry_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="nothing_to_learn"
            )
        entry = learner.store.get(entry_id) or {}
        return {"candidate_id": entry_id, "sentence": entry.get("sentence")}

    hass.services.async_register(DOMAIN, SERVICE_APPROVE, approve, _APPROVE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REJECT, reject, _ID_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_FORGET, forget, _ID_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_LEARN_LAST,
        learn_last,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _pipelines_using(hass: HomeAssistant, agent_id: str) -> list[str]:
    if "assist_pipeline" not in hass.config.components:
        return []
    from homeassistant.components.assist_pipeline import (  # noqa: PLC0415
        async_get_pipelines,
    )

    return [
        pipeline.name
        for pipeline in async_get_pipelines(hass)
        if pipeline.conversation_engine == agent_id
    ]


@callback
def _async_retire_replay_agent(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the old replay conversation agent, or say which pipelines still use it."""
    ent_reg = er.async_get(hass)
    agent_id = ent_reg.async_get_entity_id("conversation", DOMAIN, f"{entry.entry_id}_replay")
    pipelines = _pipelines_using(hass, agent_id) if agent_id else []
    if not pipelines:
        if agent_id:
            ent_reg.async_remove(agent_id)
        ir.async_delete_issue(hass, DOMAIN, ISSUE_REPLAY_AGENT_REMOVED)
        return
    fallback = {**entry.data, **entry.options}.get(CONF_FALLBACK_AGENT)
    state = hass.states.get(fallback) if fallback else None
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_REPLAY_AGENT_REMOVED,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_REPLAY_AGENT_REMOVED,
        translation_placeholders={
            "pipelines": ", ".join(pipelines),
            "agent": state.name if state else fallback or "your LLM agent",
        },
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: AssistLearnerConfigEntry
) -> bool:
    """Start learning."""
    learner = Learner(hass, entry)
    if learner.replay:
        intent.async_register(hass, ReplayIntentHandler(learner))
    await learner.async_start()
    entry.runtime_data = learner
    _async_retire_replay_agent(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def _async_reload_on_update(
    hass: HomeAssistant, entry: AssistLearnerConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: AssistLearnerConfigEntry
) -> bool:
    """Stop learning. The exported sentences file is left in place."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        intent.async_remove(hass, REPLAY_INTENT)
        await entry.runtime_data.async_stop()
    return unloaded
