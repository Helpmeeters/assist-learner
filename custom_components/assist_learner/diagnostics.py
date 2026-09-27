"""Diagnostics download. Utterances and entity names are redacted."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AssistLearnerConfigEntry

TO_REDACT = {"text", "raw_text", "sentence", "entities", "slots", "args", "area_id", "key"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AssistLearnerConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics."""
    learner = entry.runtime_data
    result = learner.last_export_result
    return {
        "options": {**entry.data, **entry.options},
        "capture_paused": learner.capture_paused,
        "last_error": learner.last_error,
        "last_event": learner.capture.last_event,
        "last_tool_exchange": learner.last_tool_exchange,
        "last_export": learner.last_export,
        "last_export_errors": dict(result.errors) if result else None,
        "counts": learner.store.counts(),
        "entries": [
            async_redact_data(entry_data, TO_REDACT)
            for entry_data in learner.store.entries.values()
        ],
    }
