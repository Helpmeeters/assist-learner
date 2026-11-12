"""Websocket API behind the dashboard review card."""

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    ATTR_CANDIDATE_ID,
    ATTR_SENTENCE,
    DECISIONS,
    DOMAIN,
    SIGNAL_UPDATED,
    STATUS_PROPOSED,
)
from .learner import Learner
from .repairs import describe_action, describe_entities, describe_notes


def _learner(hass: HomeAssistant) -> Learner | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the card's commands."""
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_review)


def review_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    """Counts and every proposal waiting for review, oldest first."""
    learner = _learner(hass)
    if learner is None:
        return {"loaded": False, "counts": {}, "pending": []}
    return {
        "loaded": True,
        "counts": learner.store.counts(),
        "pending": [
            {
                "id": entry["id"],
                "heard": entry.get("raw_text") or entry["text"],
                "sentence": entry["sentence"],
                "action": describe_action(hass, entry),
                "entities": describe_entities(hass, entry),
                "count": entry["count"],
                "notes": describe_notes(learner, entry),
                "updated": entry.get("updated"),
            }
            for entry in learner.store.by_status(STATUS_PROPOSED)
        ],
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@websocket_api.require_admin
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the review snapshot now and whenever it changes."""

    @callback
    def send() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], review_snapshot(hass)))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_UPDATED, send)
    connection.send_result(msg["id"])
    send()


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/review",
        vol.Required("decisions"): [
            {
                vol.Required(ATTR_CANDIDATE_ID): str,
                vol.Required("decision"): vol.In(DECISIONS),
                vol.Optional(ATTR_SENTENCE): str,
            }
        ],
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_review(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Approve, reject, or defer several proposals at once."""
    learner = _learner(hass)
    if learner is None:
        connection.send_error(msg["id"], "not_loaded", "Assist Learner isn't loaded.")
        return
    failed = await learner.async_review(
        [
            (item[ATTR_CANDIDATE_ID], item["decision"], item.get(ATTR_SENTENCE))
            for item in msg["decisions"]
        ]
    )
    connection.send_result(msg["id"], {"failed": failed})
