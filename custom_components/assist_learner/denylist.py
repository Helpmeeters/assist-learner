"""User-configured domains that are never exported or replayed."""

from collections.abc import Iterable

from homeassistant.core import HomeAssistant, split_entity_id


def blocked_entities(
    hass: HomeAssistant,
    entity_ids: Iterable[str],
    extra_domains: Iterable[str] = (),
) -> list[str]:
    """Return the resolved entities in a domain the user chose to block."""
    domains = frozenset(extra_domains)
    return [e for e in entity_ids if split_entity_id(e)[0] in domains]
