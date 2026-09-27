"""Store: agreement threshold, rejection, persistence, and schema migration."""

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.assist_learner.eligibility import Evaluation
from custom_components.assist_learner.store import (
    STORAGE_KEY,
    LearnerStore,
    migrate_tool_name,
)


def evaluation(key: str = "k1") -> Evaluation:
    return Evaluation(
        eligible=True,
        text="it's too dark in here",
        sentence="it's too dark in here",
        calls=[{"name": "intent__HassTurnOn", "slots": {"domain": "light"}}],
        entities=["light.kitchen_ceiling"],
        room_relative=True,
        language="en",
        area_id="kitchen",
        agent_id="agent",
        key=key,
    )


async def test_threshold_and_rejection(hass: HomeAssistant) -> None:
    """Entries are proposed at N agreeing runs; rejected keys never come back."""
    store = LearnerStore(hass)
    entry = store.async_record(evaluation(), "It's too dark in here", threshold=2)
    assert entry["status"] == "candidate"
    store.async_record(evaluation(), "It's too dark in here", threshold=2)
    assert entry["status"] == "proposed"

    store.async_reject(entry["id"])
    assert store.async_record(evaluation(), "It's too dark in here", threshold=2) is None


async def test_defer_reproposes_on_next_run(hass: HomeAssistant) -> None:
    """A deferred entry returns to review the next time it's heard, regardless of threshold."""
    store = LearnerStore(hass)
    entry = store.async_record(evaluation(), "x", threshold=1)
    assert store.async_defer(entry["id"])
    assert entry["status"] == "deferred"
    assert store.counts()["deferred"] == 1
    store.async_record(evaluation(), "x", threshold=5)
    assert entry["status"] == "proposed"
    assert entry["count"] == 2


async def test_forget_allows_relearning(hass: HomeAssistant) -> None:
    """Forget deletes the entry but doesn't block the phrase."""
    store = LearnerStore(hass)
    entry = store.async_record(evaluation(), "x", threshold=2)
    store.async_forget(entry["id"])
    assert store.async_record(evaluation(), "x", threshold=2) is not None


async def test_persists_across_restart(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """Entries survive a reload from disk."""
    store = LearnerStore(hass)
    entry = store.async_record(evaluation(), "x", threshold=1)
    store.async_approve(entry["id"])
    await store.async_save_now()

    reloaded = LearnerStore(hass)
    await reloaded.async_load()
    assert reloaded.get(entry["id"])["status"] == "approved"
    assert hass_storage[STORAGE_KEY]["version"] == 2


async def test_migrates_pre_2026_9_tool_names(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Version 1 stored unprefixed tool names; unknown ones are quarantined."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "entries": {
                "a": {"id": "a", "key": "ka", "status": "approved", "created": "1", "stale": False,
                      "calls": [{"name": "HassLightSet", "slots": {}}]},
                "b": {"id": "b", "key": "kb", "status": "approved", "created": "2", "stale": False,
                      "calls": [{"name": "good_night", "args": {}}]},
            },
            "rejected_keys": [],
            "last_eligible": None,
        },
    }
    store = LearnerStore(hass)
    await store.async_load()
    assert store.get("a")["calls"][0]["name"] == "light__HassLightSet"
    assert store.get("a")["stale"] is False
    assert store.get("b")["stale"] is True


def test_migrate_tool_name() -> None:
    """Known names gain their owning domain; already-prefixed names pass through."""
    assert migrate_tool_name("HassTurnOn") == "intent__HassTurnOn"
    assert migrate_tool_name("GetLiveContext") == "homeassistant__GetLiveContext"
    assert migrate_tool_name("intent__HassTurnOn") == "intent__HassTurnOn"
    assert migrate_tool_name("mystery") is None
