"""Security-sensitive targets that are never exported or replayed."""

from collections.abc import Iterable

from homeassistant.core import HomeAssistant, split_entity_id

# Users can add to these but never remove them.
FLOOR_DOMAINS = frozenset({"lock", "alarm_control_panel", "valve"})
FLOOR_COVER_DEVICE_CLASSES = frozenset({"garage", "gate"})


def blocked_entities(
    hass: HomeAssistant,
    entity_ids: Iterable[str],
    extra_domains: Iterable[str] = (),
) -> list[str]:
    """Return the resolved entities that the denylist forbids."""
    domains = FLOOR_DOMAINS | frozenset(extra_domains)
    blocked: list[str] = []
    for entity_id in entity_ids:
        domain, _ = split_entity_id(entity_id)
        if domain in domains:
            blocked.append(entity_id)
            continue
        if domain == "cover":
            state = hass.states.get(entity_id)
            device_class = state.attributes.get("device_class") if state else None
            # An unknown cover can't be proven safe.
            if state is None or device_class in FLOOR_COVER_DEVICE_CLASSES:
                blocked.append(entity_id)
    return blocked
