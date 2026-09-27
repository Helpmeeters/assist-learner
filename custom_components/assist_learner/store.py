"""Persistent candidate store."""

from typing import Any, override
import uuid

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    STATUS_APPROVED,
    STATUS_CANDIDATE,
    STATUS_DEFERRED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
)
from .eligibility import Evaluation

STORAGE_VERSION = 2
STORAGE_KEY = DOMAIN
SAVE_DELAY = 10

# Pre-2026.9 LLM tool names had no owning-domain prefix.
_LEGACY_TOOL_DOMAINS: dict[str, str] = {
    "HassTurnOn": "intent",
    "HassTurnOff": "intent",
    "HassSetPosition": "intent",
    "HassStopMoving": "intent",
    "HassCancelAllTimers": "intent",
    "HassStartTimer": "intent",
    "HassCancelTimer": "intent",
    "HassIncreaseTimer": "intent",
    "HassDecreaseTimer": "intent",
    "HassPauseTimer": "intent",
    "HassUnpauseTimer": "intent",
    "HassTimerStatus": "intent",
    "HassLightSet": "light",
    "HassClimateSetTemperature": "climate",
    "HassClimateGetTemperature": "climate",
    "HassFanSetSpeed": "fan",
    "HassHumidifierSetpoint": "humidifier",
    "HassHumidifierMode": "humidifier",
    "HassVacuumStart": "vacuum",
    "HassVacuumReturnToBase": "vacuum",
    "HassLawnMowerStartMowing": "lawn_mower",
    "HassLawnMowerDock": "lawn_mower",
    "HassMediaPause": "media_player",
    "HassMediaUnpause": "media_player",
    "HassMediaNext": "media_player",
    "HassMediaPrevious": "media_player",
    "HassSetVolume": "media_player",
    "HassSetVolumeRelative": "media_player",
    "HassMediaSearchAndPlay": "media_player",
    "HassListAddItem": "todo",
    "HassListCompleteItem": "todo",
    "GetLiveContext": "homeassistant",
    "GetDateTime": "llm",
}


def migrate_tool_name(name: str) -> str | None:
    """Return the 2026.9+ name for a stored tool name, or None if unknown."""
    if "__" in name:
        return name
    if domain := _LEGACY_TOOL_DOMAINS.get(name):
        return f"{domain}__{name}"
    return None


def _migrate_v1(data: dict[str, Any]) -> dict[str, Any]:
    for entry in data.get("entries", {}).values():
        for call in entry.get("calls", []):
            migrated = migrate_tool_name(call.get("name", ""))
            if migrated is None:
                # Unknown tool: quarantine rather than guess.
                entry["stale"] = True
                entry["stale_reason"] = f"unknown legacy tool {call.get('name')}"
            else:
                call["name"] = migrated
    return data


class _VersionedStore(Store[dict[str, Any]]):
    @override
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version < 2:
            old_data = _migrate_v1(old_data)
        return old_data


def _empty() -> dict[str, Any]:
    return {"entries": {}, "rejected_keys": [], "last_eligible": None}


class LearnerStore:
    """Candidates, approvals, and permanent rejections."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        self._store = _VersionedStore(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = _empty()

    async def async_load(self) -> None:
        """Load from disk."""
        if (loaded := await self._store.async_load()) is not None:
            self.data = {**_empty(), **loaded}

    @callback
    def async_schedule_save(self) -> None:
        """Save soon."""
        self._store.async_delay_save(lambda: self.data, SAVE_DELAY)

    async def async_save_now(self) -> None:
        """Save immediately."""
        await self._store.async_save(self.data)

    @property
    def entries(self) -> dict[str, dict[str, Any]]:
        """All entries by id."""
        return self.data["entries"]

    def get(self, entry_id: str) -> dict[str, Any] | None:
        """Return one entry."""
        return self.entries.get(entry_id)

    def by_status(self, status: str) -> list[dict[str, Any]]:
        """Return entries with a status, oldest first."""
        return sorted(
            (e for e in self.entries.values() if e["status"] == status),
            key=lambda e: e["created"],
        )

    def _by_key(self, key: str) -> dict[str, Any] | None:
        return next((e for e in self.entries.values() if e["key"] == key), None)

    @callback
    def async_record(
        self, evaluation: Evaluation, raw_text: str, threshold: int
    ) -> dict[str, Any] | None:
        """Count one eligible run. Returns None if the phrase was rejected before."""
        if evaluation.key in self.data["rejected_keys"]:
            return None
        now = dt_util.utcnow().isoformat()
        entry = self._by_key(evaluation.key)
        if entry is None:
            entry = {
                "id": uuid.uuid4().hex[:12],
                "key": evaluation.key,
                "status": STATUS_CANDIDATE,
                "count": 0,
                "created": now,
                "stale": False,
                "stale_reason": None,
                "export_hash": None,
                "export_error": None,
                "user_sentence": False,
            }
            self.entries[entry["id"]] = entry
        entry.update(
            {
                "sentence": entry.get("sentence") if entry["user_sentence"] else evaluation.sentence,
                "text": evaluation.text,
                "raw_text": raw_text,
                "calls": evaluation.calls,
                "entities": evaluation.entities,
                "room_relative": evaluation.room_relative,
                "replay_only": evaluation.replay_only,
                "replay_only_reason": evaluation.replay_only_reason,
                "language": evaluation.language,
                "area_id": evaluation.area_id,
                "agent_id": evaluation.agent_id,
                "updated": now,
                # A fresh successful run proves the entry works again.
                "stale": False,
                "stale_reason": None,
            }
        )
        entry["count"] += 1
        if (
            entry["status"] == STATUS_CANDIDATE and entry["count"] >= threshold
        ) or entry["status"] == STATUS_DEFERRED:
            entry["status"] = STATUS_PROPOSED
        self.data["last_eligible"] = entry["id"]
        self.async_schedule_save()
        return entry

    @callback
    def async_propose(self, entry_id: str) -> bool:
        """Move a candidate straight to review."""
        entry = self.get(entry_id)
        if entry is None or entry["status"] not in (STATUS_CANDIDATE, STATUS_DEFERRED):
            return False
        entry["status"] = STATUS_PROPOSED
        self.async_schedule_save()
        return True

    @callback
    def async_defer(self, entry_id: str) -> bool:
        """Hide a proposal until its phrase is heard again."""
        entry = self.get(entry_id)
        if entry is None or entry["status"] != STATUS_PROPOSED:
            return False
        entry["status"] = STATUS_DEFERRED
        self.async_schedule_save()
        return True

    @callback
    def async_approve(self, entry_id: str, sentence: str | None = None) -> bool:
        """Approve an entry, optionally with an edited sentence."""
        entry = self.get(entry_id)
        if entry is None or entry["status"] == STATUS_REJECTED:
            return False
        if sentence and sentence.strip() != entry["sentence"]:
            entry["sentence"] = sentence.strip()
            entry["user_sentence"] = True
        entry["status"] = STATUS_APPROVED
        entry["export_error"] = None
        self.async_schedule_save()
        return True

    @callback
    def async_reject(self, entry_id: str) -> bool:
        """Reject an entry; its phrase is never proposed again."""
        entry = self.get(entry_id)
        if entry is None:
            return False
        entry["status"] = STATUS_REJECTED
        if entry["key"] not in self.data["rejected_keys"]:
            self.data["rejected_keys"].append(entry["key"])
        self.async_schedule_save()
        return True

    @callback
    def async_forget(self, entry_id: str) -> bool:
        """Delete an entry. Unlike reject, the phrase may be learned again."""
        if self.entries.pop(entry_id, None) is None:
            return False
        if self.data["last_eligible"] == entry_id:
            self.data["last_eligible"] = None
        self.async_schedule_save()
        return True

    @callback
    def async_mark_stale(self, predicate, reason: str) -> int:
        """Flag entries matching predicate as stale."""
        count = 0
        for entry in self.entries.values():
            if not entry["stale"] and predicate(entry):
                entry["stale"] = True
                entry["stale_reason"] = reason
                count += 1
        if count:
            self.async_schedule_save()
        return count

    def counts(self) -> dict[str, int]:
        """Counts for the status sensor."""
        entries = list(self.entries.values())
        return {
            "candidates": sum(e["status"] == STATUS_CANDIDATE for e in entries),
            "proposed": sum(e["status"] == STATUS_PROPOSED for e in entries),
            "deferred": sum(e["status"] == STATUS_DEFERRED for e in entries),
            "approved": sum(e["status"] == STATUS_APPROVED for e in entries),
            "rejected": sum(e["status"] == STATUS_REJECTED for e in entries),
            "stale": sum(bool(e["stale"]) for e in entries),
        }
