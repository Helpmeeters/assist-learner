"""Replays approved commands that can't be written as ordinary sentences.

The exporter writes them as sentences for REPLAY_INTENT that carry their entry
id, so the built-in local matcher recognizes them before the pipeline's own
agent runs. This handler then runs the stored calls through a fresh Assist API
instance built from the current request.
"""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, intent, llm

from .const import (
    CONF_EXTRA_DENYLIST,
    DOMAIN,
    REPLAY_INTENT,
    REPLAY_SLOT,
    STATUS_APPROVED,
)
from .denylist import blocked_entities
from .learner import Learner

_LOGGER = logging.getLogger(__name__)


def _device_area(hass: HomeAssistant, device_id: str | None) -> ar.AreaEntry | None:
    if not device_id or not (device := dr.async_get(hass).async_get(device_id)):
        return None
    area_id = dr.async_get_effective_area_id(hass, device)
    return ar.async_get(hass).async_get_area(area_id) if area_id else None


def _preview_targets(hass: HomeAssistant, slots: dict[str, Any]) -> list[str] | None:
    """Entities an intent call would hit right now, or None if it can't be resolved."""
    def _as_set(value: Any) -> set[str] | None:
        if value is None:
            return None
        return set(value) if isinstance(value, list) else {value}

    result = intent.async_match_targets(
        hass,
        intent.MatchTargetsConstraints(
            name=slots.get("name"),
            area_name=slots.get("area"),
            floor_name=slots.get("floor"),
            domains=_as_set(slots.get("domain")),
            device_classes=_as_set(slots.get("device_class")),
            assistant=conversation.DOMAIN,
            allow_duplicate_names=True,
        ),
    )
    return [state.entity_id for state in result.states] if result.is_match else None


class ReplayIntentHandler(intent.IntentHandler):
    """Runs the stored calls of an approved replay-only entry."""

    intent_type = REPLAY_INTENT

    def __init__(self, learner: Learner) -> None:
        """Initialize."""
        self._learner = learner

    @property
    def slot_schema(self) -> dict | None:
        """The entry id is fixed by the sentence; area comes from the request."""
        return {vol.Required(REPLAY_SLOT): str, vol.Optional("area"): str}

    def _plan_calls(
        self, entry: dict[str, Any], area_name: str | None
    ) -> list[llm.ToolInput] | None:
        extra = self._learner.options.get(CONF_EXTRA_DENYLIST, [])
        calls: list[llm.ToolInput] = []
        for call in entry["calls"]:
            if "slots" in call:
                args = dict(call["slots"])
                if entry.get("room_relative"):
                    if area_name is None:
                        return None
                    args["area"] = area_name
                targets = _preview_targets(self._learner.hass, args)
                if not targets or blocked_entities(self._learner.hass, targets, extra):
                    return None
            else:
                args = dict(call["args"])
                script_entity = f"script.{call['name'].partition('__')[2]}"
                if blocked_entities(self._learner.hass, [script_entity], extra):
                    return None
            calls.append(llm.ToolInput(tool_name=call["name"], tool_args=args))
        return calls

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Replay, or explain why it can't run here."""
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)
        entry_id = slots[REPLAY_SLOT]["value"]
        response = intent_obj.create_response()

        entry = self._learner.store.get(entry_id)
        if (
            entry is None
            or entry["status"] != STATUS_APPROVED
            or not entry.get("replay_only")
            or entry.get("stale")
        ):
            _LOGGER.debug("Replay %s for %r is no longer approved", entry_id, intent_obj.text_input)
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE,
                "That command is no longer learned.",
            )
            return response

        area_name = (slots.get("area") or {}).get("value")
        if area_name is None and (area := _device_area(hass, intent_obj.device_id)):
            area_name = area.name
        calls = self._plan_calls(entry, area_name)
        if not calls:
            _LOGGER.debug(
                "Approved replay %s for %r can't run here (no area, no targets, or "
                "denylisted)",
                entry_id,
                intent_obj.text_input,
            )
            response.async_set_error(
                intent.IntentResponseErrorCode.NO_VALID_TARGETS,
                "Sorry, I can't do that here.",
            )
            return response

        _LOGGER.debug(
            "Replaying %s for %r: %s",
            entry_id,
            intent_obj.text_input,
            [(call.tool_name, call.tool_args) for call in calls],
        )
        # A fresh API instance from this request, never a stored one.
        api = await llm.async_get_api(
            hass,
            llm.LLM_API_ASSIST,
            llm.LLMContext(
                platform=DOMAIN,
                context=intent_obj.context,
                language=intent_obj.language,
                assistant=conversation.DOMAIN,
                device_id=intent_obj.device_id,
            ),
        )
        failed = False
        for call in calls:
            try:
                result = await api.async_call_tool(call)
            except (HomeAssistantError, vol.Invalid) as err:
                _LOGGER.debug("Replay %s call %s failed: %s", entry_id, call.tool_name, err)
                failed = True
                continue
            if "error" in result or result.get("response_type") == "error":
                failed = True
        if failed:
            response.async_set_error(
                intent.IntentResponseErrorCode.FAILED_TO_HANDLE, "Sorry, that didn't work."
            )
        else:
            response.async_set_speech("Done.")
        return response
