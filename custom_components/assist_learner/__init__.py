"""Assist Learner: turn repeated LLM fallbacks into reviewed local sentences."""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CANDIDATE_ID,
    ATTR_SENTENCE,
    CONF_REPLAY,
    DOMAIN,
    SERVICE_APPROVE,
    SERVICE_FORGET,
    SERVICE_LEARN_LAST,
    SERVICE_REJECT,
)
from .learner import Learner

type AssistLearnerConfigEntry = ConfigEntry[Learner]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_ID_SCHEMA = vol.Schema({vol.Required(ATTR_CANDIDATE_ID): cv.string})
_APPROVE_SCHEMA = _ID_SCHEMA.extend({vol.Optional(ATTR_SENTENCE): cv.string})


def _platforms(entry: ConfigEntry) -> list[Platform]:
    platforms = [Platform.SENSOR]
    if {**entry.data, **entry.options}.get(CONF_REPLAY):
        platforms.append(Platform.CONVERSATION)
    return platforms


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


async def async_setup_entry(
    hass: HomeAssistant, entry: AssistLearnerConfigEntry
) -> bool:
    """Start learning."""
    learner = Learner(hass, entry)
    await learner.async_start()
    entry.runtime_data = learner
    await hass.config_entries.async_forward_entry_setups(entry, _platforms(entry))
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
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, _platforms(entry)
    )
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
