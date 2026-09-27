"""Config and options flow for Assist Learner."""

from typing import Any, override

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_EXTRA_DENYLIST,
    CONF_FALLBACK_AGENT,
    CONF_LANGUAGE,
    CONF_REPLAY,
    CONF_THRESHOLD,
    DEFAULT_THRESHOLD,
    DOMAIN,
)
from .context_adapter import unsupported_reason
from .denylist import FLOOR_DOMAINS

HOME_ASSISTANT_AGENT = "conversation.home_assistant"


def _llm_agents(hass: HomeAssistant) -> list[str]:
    return [
        state.entity_id
        for state in hass.states.async_all("conversation")
        if state.entity_id != HOME_ASSISTANT_AGENT
    ]


def _prefer_local_enabled(hass: HomeAssistant) -> bool | None:
    """True if any pipeline with a non-default agent prefers local intents; None if unknown."""
    if "assist_pipeline" not in hass.config.components:
        return None
    try:
        from homeassistant.components.assist_pipeline import (  # noqa: PLC0415
            async_get_pipelines,
        )
    except ImportError:
        return None
    return any(
        pipeline.prefer_local_intents
        and pipeline.conversation_engine not in (None, HOME_ASSISTANT_AGENT, "homeassistant")
        for pipeline in async_get_pipelines(hass)
    )


def _schema(hass: HomeAssistant, defaults: dict[str, Any]) -> vol.Schema:
    agents = _llm_agents(hass)
    fallback_selector: Any = (
        selector.EntitySelector(
            selector.EntitySelectorConfig(domain="conversation", include_entities=agents)
        )
        if agents
        else selector.TextSelector()
    )
    schema: dict[Any, Any] = {
        vol.Required(
            CONF_THRESHOLD, default=defaults.get(CONF_THRESHOLD, DEFAULT_THRESHOLD)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=10, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(
            CONF_EXTRA_DENYLIST, default=defaults.get(CONF_EXTRA_DENYLIST, [])
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["cover", "climate", "media_player", "switch", "script", "siren", "water_heater"],
                multiple=True,
                custom_value=True,
            )
        ),
        vol.Optional(
            CONF_LANGUAGE, default=defaults.get(CONF_LANGUAGE, hass.config.language)
        ): selector.LanguageSelector(),
        vol.Optional(CONF_REPLAY, default=defaults.get(CONF_REPLAY, False)): bool,
    }
    fallback_key = (
        vol.Optional(CONF_FALLBACK_AGENT, default=defaults[CONF_FALLBACK_AGENT])
        if defaults.get(CONF_FALLBACK_AGENT)
        else vol.Optional(CONF_FALLBACK_AGENT)
    )
    schema[fallback_key] = fallback_selector
    return vol.Schema(schema)


def _placeholders(hass: HomeAssistant) -> dict[str, str]:
    prefer_local = _prefer_local_enabled(hass)
    if prefer_local is False:
        note = (
            "No Assist pipeline with an LLM agent has \"Prefer handling commands "
            "locally\" turned on. Learned sentences only take effect when it is on."
        )
    else:
        note = ""
    return {"prefer_local_note": note, "denylist_floor": ", ".join(sorted(FLOOR_DOMAINS))}


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    if user_input.get(CONF_REPLAY) and not user_input.get(CONF_FALLBACK_AGENT):
        return {CONF_FALLBACK_AGENT: "fallback_required"}
    return {}


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    data[CONF_THRESHOLD] = int(data[CONF_THRESHOLD])
    return data


class AssistLearnerConfigFlow(ConfigFlow, domain=DOMAIN):
    """One-screen setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle setup."""
        if reason := unsupported_reason():
            return self.async_abort(
                reason="unsupported", description_placeholders={"reason": reason}
            )
        if not _llm_agents(self.hass):
            return self.async_abort(reason="no_llm_agent")

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(title="Assist Learner", data=_clean(user_input))

        return self.async_show_form(
            step_id="user",
            data_schema=_schema(self.hass, user_input or {}),
            errors=errors,
            description_placeholders=_placeholders(self.hass),
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return AssistLearnerOptionsFlow()


class AssistLearnerOptionsFlow(OptionsFlow):
    """Change settings after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=_clean(user_input))
        defaults = user_input or {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(self.hass, defaults),
            errors=errors,
            description_placeholders=_placeholders(self.hass),
        )
