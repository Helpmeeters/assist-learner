"""Dashboard card: websocket review API, and serving the card itself."""

from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.assist_learner.const import CARD_URL, DOMAIN, ISSUE_REVIEW

from .conftest import House, learn, sentences_file, turn_on_area


async def _until_result(client, msg_id: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect subscription events until the result for msg_id arrives."""
    events = []
    while True:
        msg = await client.receive_json()
        if msg["type"] == "result" and msg["id"] == msg_id:
            return events, msg
        events.append(msg["event"])


async def test_subscribe_and_bulk_review(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    house: House,
    fake_llm,
    learner_entry,
) -> None:
    """The card sees every proposal, and one review call approves and defers several."""
    dark = await learn(
        hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite
    )
    bright = await learn(
        hass, fake_llm, "brighten the bedroom up", turn_on_area("Bedroom"), house.kitchen_satellite
    )
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/subscribe"})
    msg = await client.receive_json()
    assert msg["success"]
    snapshot = (await client.receive_json())["event"]
    assert snapshot["loaded"] is True
    assert snapshot["counts"]["proposed"] == 2
    assert snapshot["counts"]["captured_total"] == 2
    first = snapshot["pending"][0]
    assert first["id"] == dark["id"]
    assert first["heard"] == "it's too dark in here"
    assert first["count"] == 2
    assert "Kitchen Ceiling (light.kitchen_ceiling)" in first["entities"]
    assert "whichever area the speaking device is in" in first["action"]

    await client.send_json(
        {
            "id": 99,
            "type": f"{DOMAIN}/review",
            "decisions": [
                {
                    "candidate_id": dark["id"],
                    "decision": "approve",
                    "sentence": "it's (too|so) dark in here",
                },
                {"candidate_id": bright["id"], "decision": "defer"},
            ],
        }
    )
    events, result = await _until_result(client, 99)
    assert result["success"]
    assert result["result"] == {"failed": []}
    assert events[-1]["pending"] == []
    assert events[-1]["counts"]["approved"] == 1
    assert events[-1]["counts"]["deferred"] == 1

    assert dark["status"] == "approved"
    assert bright["status"] == "deferred"
    assert "it's (too|so) dark in here" in sentences_file(hass).read_text()
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_REVIEW) is None
    state = hass.states.get("sensor.assist_learner_status")
    assert state.attributes["captured_total"] == 2
    assert state.attributes["proposed"] == 0


async def test_review_reports_ids_it_could_not_apply(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    house: House,
    fake_llm,
    learner_entry,
) -> None:
    """Rejecting works in bulk; unknown ids are reported rather than failing the batch."""
    entry = await learn(
        hass, fake_llm, "it's too dark in here", turn_on_area("Kitchen"), house.kitchen_satellite
    )
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": f"{DOMAIN}/review",
            "decisions": [
                {"candidate_id": entry["id"], "decision": "reject"},
                {"candidate_id": "nope", "decision": "defer"},
            ],
        }
    )
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"] == {"failed": ["nope"]}
    assert entry["status"] == "rejected"


async def test_review_requires_admin(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    learner_entry,
) -> None:
    """Non-admins can't read utterances or change what's learned."""
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": f"{DOMAIN}/subscribe"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"
    await client.send_json_auto_id({"type": f"{DOMAIN}/review", "decisions": []})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "unauthorized"


async def test_card_is_served_and_loaded_with_the_frontend(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, fake_llm
) -> None:
    """With the frontend running, the card's script is served and added to every page."""
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_extra_js_url:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    url = add_extra_js_url.call_args.args[1]
    assert url.startswith(f"{CARD_URL}?v=")
    response = await (await hass_client()).get(url)
    assert response.status == 200
    assert "customElements.define" in await response.text()
