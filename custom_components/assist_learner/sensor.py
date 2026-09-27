"""Learning status sensor."""

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AssistLearnerConfigEntry
from .const import DOMAIN, SIGNAL_UPDATED
from .learner import Learner


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AssistLearnerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the status sensor."""
    async_add_entities([LearnerStatusSensor(entry.runtime_data, entry.entry_id)])


class LearnerStatusSensor(SensorEntity):
    """Listening, paused, or error, with counts as attributes."""

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["listening", "paused", "error"]

    def __init__(self, learner: Learner, entry_id: str) -> None:
        """Initialize."""
        self._learner = learner
        self._attr_unique_id = f"{entry_id}_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry_id)},
            "name": "Assist Learner",
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        """Follow learner updates."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._updated)
        )

    @callback
    def _updated(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """Current status."""
        if self._learner.capture_paused:
            return "paused"
        result = self._learner.last_export_result
        if result is not None and result.file_error:
            return "error"
        return "listening"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Counts and recent activity."""
        learner = self._learner
        rejection = learner.last_rejection or {}
        return {
            **learner.store.counts(),
            "last_export": learner.last_export.isoformat() if learner.last_export else None,
            "last_error": learner.last_error,
            "last_skip_reason": rejection.get("reason"),
            "threshold": learner.threshold,
        }
