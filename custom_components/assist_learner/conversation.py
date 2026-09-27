"""Optional replay agent for approved commands that can't be sentences.

Set it as the pipeline's conversation agent. Approved replay-only entries are
replayed through a fresh Assist API instance built from the current request;
everything else is passed to the configured fallback agent unchanged.
"""

from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, intent, llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AssistLearnerConfigEntry
from .const import CONF_EXTRA_DENYLIST, CONF_FALLBACK_AGENT, DOMAIN, STATUS_APPROVED
from .denylist import blocked_entities
from .learner import Learner
from .normalize import normalize_text


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AssistLearnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the replay agent."""
    async_add_entities([ReplayAgent(entry.runtime_data, entry.entry_id)])


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


class ReplayAgent(conversation.ConversationEntity):
    """Replays approved commands, delegates everything else."""

    _attr_has_entity_name = True
    _attr_translation_key = "replay"

    def __init__(self, learner: Learner, entry_id: str) -> None:
        """Initialize."""
        self._learner = learner
        self._attr_unique_id = f"{entry_id}_replay"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Assist Learner",
            "entry_type": "service",
        }

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Any language; matching is on stored wording."""
        return "*"

    async def async_added_to_hass(self) -> None:
        """Tell the learner not to learn from its own replays."""
        await super().async_added_to_hass()
        self._learner.ignored_agent_ids.add(self.entity_id)

    async def async_will_remove_from_hass(self) -> None:
        """Forget the ignore."""
        self._learner.ignored_agent_ids.discard(self.entity_id)

    def _find(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_text(text)
        for entry in self._learner.store.entries.values():
            if (
                entry["status"] == STATUS_APPROVED
                and entry.get("replay_only")
                and not entry.get("stale")
                and "{" not in entry["sentence"]
                and entry["sentence"] == normalized
            ):
                return entry
        return None

    def _plan_calls(
        self, entry: dict[str, Any], user_input: conversation.ConversationInput
    ) -> list[llm.ToolInput] | None:
        area = _device_area(self.hass, user_input.device_id)
        extra = self._learner.options.get(CONF_EXTRA_DENYLIST, [])
        calls: list[llm.ToolInput] = []
        for call in entry["calls"]:
            if "slots" in call:
                args = dict(call["slots"])
                if entry.get("room_relative"):
                    if area is None:
                        return None
                    args["area"] = area.name
                targets = _preview_targets(self.hass, args)
                if not targets or blocked_entities(self.hass, targets, extra):
                    return None
            else:
                args = dict(call["args"])
                script_entity = f"script.{call['name'].partition('__')[2]}"
                if blocked_entities(self.hass, [script_entity], extra):
                    return None
            calls.append(llm.ToolInput(tool_name=call["name"], tool_args=args))
        return calls

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Replay or delegate."""
        entry = self._find(user_input.text)
        calls = self._plan_calls(entry, user_input) if entry else None
        if calls:
            # A fresh API instance from this request, never a stored one.
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN), llm.LLM_API_ASSIST
            )
            failed = False
            async for result in chat_log.async_add_assistant_content(
                conversation.AssistantContent(
                    agent_id=user_input.agent_id, content=None, tool_calls=calls
                )
            ):
                tool_result = result.tool_result
                if "error" in tool_result or (
                    tool_result.get("response_type") == "error"
                ):
                    failed = True
            response = intent.IntentResponse(language=user_input.language)
            speech = "Sorry, that didn't work." if failed else "Done."
            response.async_set_speech(speech)
            chat_log.async_add_assistant_content_without_tools(
                conversation.AssistantContent(agent_id=user_input.agent_id, content=speech)
            )
            return conversation.ConversationResult(
                response=response, conversation_id=chat_log.conversation_id
            )

        return await conversation.async_converse(
            self.hass,
            user_input.text,
            chat_log.conversation_id,
            user_input.context,
            language=user_input.language,
            agent_id=self._learner.options[CONF_FALLBACK_AGENT],
            device_id=user_input.device_id,
            satellite_id=user_input.satellite_id,
            extra_system_prompt=user_input.extra_system_prompt,
        )
