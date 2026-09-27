"""Review flow: step through proposed sentences from the Repairs dashboard."""

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import area_registry as ar, selector

from .const import ATTR_SENTENCE, DOMAIN, ISSUE_REVIEW, STATUS_PROPOSED
from .learner import Learner

DECISION = "decision"


def _learner(hass: HomeAssistant) -> Learner | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def describe_action(hass: HomeAssistant, entry: dict[str, Any]) -> str:
    """Human-readable summary of what approving an entry will do."""
    parts = []
    for call in entry["calls"]:
        name = call["name"].partition("__")[2] or call["name"]
        args = call.get("slots", call.get("args", {}))
        rendered = ", ".join(f"{k}={v}" for k, v in args.items())
        parts.append(f"{name}({rendered})")
    action = "; then ".join(parts)
    if entry.get("room_relative"):
        area = ar.async_get(hass).async_get_area(entry.get("area_id") or "")
        where = area.name if area else "unknown area"
        action += f" in whichever area the speaking device is in (learned in {where})"
    return action


def describe_entities(hass: HomeAssistant, entry: dict[str, Any]) -> str:
    """Resolved entities the command affected when it was learned."""
    names = []
    for entity_id in entry.get("entities") or []:
        state = hass.states.get(entity_id)
        names.append(f"{state.name} ({entity_id})" if state else entity_id)
    return ", ".join(names) or "none"


class ReviewCandidatesFlow(RepairsFlow):
    """Approve, reject, edit, or skip each proposed sentence."""

    def __init__(self) -> None:
        """Initialize."""
        self._skipped: set[str] = set()
        self._current: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start reviewing."""
        return await self.async_step_review()

    async def async_step_review(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show one candidate at a time."""
        learner = _learner(self.hass)
        if learner is None:
            return self.async_abort(reason="not_loaded")

        if user_input is not None and self._current is not None:
            decision = user_input[DECISION]
            if decision == "approve":
                await learner.async_approve(self._current, user_input.get(ATTR_SENTENCE))
            elif decision == "reject":
                await learner.async_reject(self._current)
            else:
                self._skipped.add(self._current)

        proposed = learner.store.by_status(STATUS_PROPOSED)
        pending = [e for e in proposed if e["id"] not in self._skipped]
        if not pending:
            if proposed:
                # Aborting keeps the issue open for the skipped ones.
                return self.async_abort(reason="skipped")
            return self.async_create_entry(data={})

        entry = pending[0]
        self._current = entry["id"]
        notes = []
        if entry.get("replay_only"):
            notes.append(
                f"This can't be a local sentence ({entry['replay_only_reason']}); "
                "approving it only enables the optional replay agent."
            )
        if entry.get("export_error"):
            notes.append(f"Last export problem: {entry['export_error']}")
        return self.async_show_form(
            step_id="review",
            data_schema=vol.Schema(
                {
                    vol.Required(DECISION, default="approve"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["approve", "reject", "skip"],
                            translation_key=DECISION,
                        )
                    ),
                    vol.Optional(ATTR_SENTENCE, default=entry["sentence"]): str,
                }
            ),
            description_placeholders={
                "heard": entry.get("raw_text") or entry["text"],
                "sentence": entry["sentence"],
                "action": describe_action(self.hass, entry),
                "entities": describe_entities(self.hass, entry),
                "count": str(entry["count"]),
                "remaining": str(len(pending)),
                "notes": " ".join(notes),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create a fix flow."""
    if issue_id == ISSUE_REVIEW:
        return ReviewCandidatesFlow()
    return ConfirmRepairFlow()
