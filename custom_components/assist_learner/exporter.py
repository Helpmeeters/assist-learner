"""Write approved entries to custom_sentences, safely."""

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, floor_registry as fr
from homeassistant.util import language as language_util

from .const import METADATA_KEY, SENTENCES_FILENAME, STATUS_APPROVED
from .store import LearnerStore

_LOGGER = logging.getLogger(__name__)

# The default agent tags custom sentences with this metadata key and prefers them.
HASS_CUSTOM_SENTENCE = "hass_custom_sentence"
_HEADER = (
    "# Written by the Assist Learner integration. Entries you edit by hand are\n"
    "# kept as-is. Deleting this file removes every learned sentence.\n"
)


@dataclass(slots=True)
class ExportResult:
    """What an export did."""

    written: list[str] = field(default_factory=list)
    exported: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    file_error: str | None = None


class ExportValidationError(Exception):
    """The generated file would break local intents."""


def resolve_language_variant(language: str) -> str | None:
    """Return the intents language variant the default agent will load, e.g. en for en-US."""
    from home_assistant_intents import get_languages  # noqa: PLC0415

    matches = language_util.matches(language, set(get_languages()))
    return matches[0] if matches else None


def sentences_path(hass: HomeAssistant, variant: str) -> Path:
    """Path of the owned sentences file for a language variant."""
    return Path(hass.config.path("custom_sentences", variant, SENTENCES_FILENAME))


def is_exportable(entry: dict[str, Any]) -> bool:
    """Approved, sentence-expressible, and not stale."""
    return (
        entry["status"] == STATUS_APPROVED
        and not entry.get("replay_only")
        and not entry.get("stale")
    )


def build_block(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (intent type, hassil data block) for an exportable entry."""
    call = entry["calls"][0]
    intent_type = call["name"].partition("__")[2]
    block: dict[str, Any] = {"sentences": [entry["sentence"]]}
    if call["slots"]:
        block["slots"] = dict(call["slots"])
    if entry.get("room_relative"):
        block["requires_context"] = {"area": {"slot": True}}
    block["metadata"] = {METADATA_KEY: entry["id"]}
    return intent_type, block


def block_hash(intent_type: str, block: dict[str, Any]) -> str:
    """Hash of a block's meaning, ignoring metadata."""
    body = {k: v for k, v in block.items() if k != "metadata"}
    payload = json.dumps([intent_type, body], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _iter_blocks(doc: dict[str, Any] | None):
    for intent_type, intent_data in ((doc or {}).get("intents") or {}).items():
        for block in (intent_data or {}).get("data") or []:
            yield intent_type, block


def _read_yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as file:
        loaded = yaml.safe_load(file)
    return loaded if isinstance(loaded, dict) else None


def _other_custom_docs(directory: Path, own: Path) -> list[dict[str, Any]]:
    docs = []
    if directory.is_dir():
        for path in sorted(directory.rglob("*.yaml")):
            if path.resolve() == own.resolve():
                continue
            if (doc := _read_yaml(path)) is not None:
                docs.append(doc)
    return docs


def _merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _tag_custom(doc: dict[str, Any]) -> dict[str, Any]:
    doc = json.loads(json.dumps(doc, default=str))
    for _, block in _iter_blocks(doc):
        block.setdefault("metadata", {})[HASS_CUSTOM_SENTENCE] = True
    return doc


def _recognize(text: str, intents: Any, slot_lists: dict, context: dict | None, variant: str):
    from hassil.errors import MissingListError  # noqa: PLC0415
    from hassil.intents import TextSlotList  # noqa: PLC0415
    from hassil.recognize import recognize_best  # noqa: PLC0415

    lists = dict(slot_lists)
    for _ in range(20):
        try:
            return recognize_best(
                text,
                intents,
                slot_lists=lists,
                intent_context=context,
                language=variant,
                best_metadata_key=HASS_CUSTOM_SENTENCE,
                best_slot_name="name",
            )
        except MissingListError as err:
            missing = str(err).split("{")[-1].rstrip("}")
            lists[missing] = TextSlotList.from_strings([])
        except Exception as err:
            raise ExportValidationError(f"sentences don't parse: {err}") from err
    raise ExportValidationError("too many missing slot lists")


class _Validator:
    """Runs in the executor. Mirrors how the default agent merges sentence files."""

    def __init__(
        self,
        variant: str,
        other_docs: list[dict[str, Any]],
        names: dict[str, list[str]],
        area_names: dict[str, str],
    ) -> None:
        from hassil.intents import TextSlotList  # noqa: PLC0415
        from home_assistant_intents import get_intents  # noqa: PLC0415

        self.variant = variant
        self.builtin = get_intents(variant) or {}
        self.other_docs = [_tag_custom(doc) for doc in other_docs]
        self.slot_lists = {
            key: TextSlotList.from_strings(values) for key, values in names.items()
        }
        self.area_names = area_names

    def _base(self) -> dict[str, Any]:
        base = json.loads(json.dumps(self.builtin))
        for doc in self.other_docs:
            _merge(base, doc)
        return base

    def _intents(self, own: dict[str, Any] | None):
        from hassil import Intents  # noqa: PLC0415

        merged = self._base()
        if own:
            _merge(merged, _tag_custom(own))
        try:
            intents = Intents.from_dict(merged)
            if own:
                # hassil parses templates lazily; a bad one would only fail at recognition time.
                for intent_type in (own.get("intents") or {}):
                    for data in intents.intents[intent_type].data:
                        _ = data.sentences
        except Exception as err:
            raise ExportValidationError(f"sentences don't parse: {err}") from err
        return intents

    def _context(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        area_name = self.area_names.get(entry.get("area_id") or "")
        if not area_name:
            return None
        return {"area": {"value": area_name, "text": area_name}}

    def check_entry(self, entry: dict[str, Any], intent_type: str, block: dict) -> None:
        """Validate one new block in isolation and against existing sentences."""
        own = {"language": self.variant, "intents": {intent_type: {"data": [block]}}}
        context = self._context(entry)
        if entry.get("room_relative") and context is None:
            raise ExportValidationError("room-relative entry has no source area")

        # Shadowing: if existing sentences already match, ours would override them.
        if not entry.get("user_sentence"):
            existing = _recognize(
                entry["raw_text"], self._intents(None), self.slot_lists, context, self.variant
            )
            if existing is not None and not existing.unmatched_entities:
                raise ExportValidationError(
                    f"an existing sentence already handles it ({existing.intent.name})"
                )

        intents = self._intents(own)
        if entry.get("user_sentence"):
            return
        result = _recognize(entry["raw_text"], intents, self.slot_lists, context, self.variant)
        if result is None or (result.intent_metadata or {}).get(METADATA_KEY) != entry["id"]:
            raise ExportValidationError("the sentence does not recognize its own utterance")

    def check_file(self, doc: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        """Validate the whole file merged with everything else."""
        intents = self._intents(doc)
        for entry in entries:
            if entry.get("user_sentence"):
                continue
            result = _recognize(
                entry["raw_text"], intents, self.slot_lists, self._context(entry), self.variant
            )
            if result is None or (result.intent_metadata or {}).get(METADATA_KEY) != entry["id"]:
                raise ExportValidationError(
                    f"entry {entry['id']} no longer recognizes in the merged file"
                )


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".assist_learner.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _slot_names(hass: HomeAssistant) -> tuple[dict[str, list[str]], dict[str, str]]:
    area_reg = ar.async_get(hass)
    areas: list[str] = []
    area_names: dict[str, str] = {}
    for area in area_reg.async_list_areas():
        area_names[area.id] = area.name
        areas.extend([area.name, *(area.aliases or ())])
    floors: list[str] = []
    for floor in fr.async_get(hass).async_list_floors():
        floors.extend([floor.name, *(floor.aliases or ())])
    names = [state.name for state in hass.states.async_all() if state.name]
    return {"name": names, "area": areas, "floor": floors}, area_names


def _plan_variant(
    path: Path,
    variant: str,
    entries: list[dict[str, Any]],
    known: dict[str, dict[str, Any]],
    validator: _Validator,
    result: ExportResult,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """Build the new file contents. Runs in the executor."""
    existing = _read_yaml(path)
    kept: list[tuple[str, dict[str, Any]]] = []
    handled: set[str] = set()
    for intent_type, block in _iter_blocks(existing):
        entry_id = (block.get("metadata") or {}).get(METADATA_KEY)
        if not entry_id:
            kept.append((intent_type, block))
            continue
        entry = known.get(entry_id)
        if entry is None:
            continue
        if entry.get("export_hash") and block_hash(intent_type, block) != entry["export_hash"]:
            # Hand-edited: never rewrite it.
            kept.append((intent_type, block))
            handled.add(entry_id)
        elif not is_exportable(entry):
            handled.add(entry_id)

    hashes: dict[str, str] = {}
    exported: list[dict[str, Any]] = []
    seen_sentences: dict[str, str] = {
        s.strip().lower(): intent_type for intent_type, b in kept for s in b.get("sentences", [])
    }
    for entry in entries:
        if entry["id"] in handled:
            continue
        intent_type, block = build_block(entry)
        sentence = entry["sentence"].strip().lower()
        if sentence in seen_sentences:
            result.errors[entry["id"]] = "another learned sentence has the same wording"
            continue
        try:
            validator.check_entry(entry, intent_type, block)
        except ExportValidationError as err:
            result.errors[entry["id"]] = str(err)
            continue
        seen_sentences[sentence] = intent_type
        kept.append((intent_type, block))
        hashes[entry["id"]] = block_hash(intent_type, block)
        exported.append(entry)

    doc: dict[str, Any] = {"language": variant, "intents": {}}
    for intent_type, block in kept:
        doc["intents"].setdefault(intent_type, {"data": []})["data"].append(block)
    validator.check_file(doc, exported)
    return doc, exported, hashes


def _export_variant_sync(
    path: Path,
    variant: str,
    entries: list[dict[str, Any]],
    known: dict[str, dict[str, Any]],
    other_docs_dir: Path,
    names: dict[str, list[str]],
    area_names: dict[str, str],
    result: ExportResult,
) -> tuple[list[dict[str, Any]], dict[str, str], bool]:
    validator = _Validator(variant, _other_custom_docs(other_docs_dir, path), names, area_names)
    doc, exported, hashes = _plan_variant(path, variant, entries, known, validator, result)
    text = _HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    if current == text or (current is None and not doc["intents"]):
        return exported, hashes, False
    _write_atomic(path, text)
    return exported, hashes, True


async def async_export(
    hass: HomeAssistant, store: LearnerStore, default_language: str
) -> ExportResult:
    """Export approved entries for every language, then reload conversation."""
    result = ExportResult()
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for entry in store.entries.values():
        variant = resolve_language_variant(entry.get("language") or default_language)
        if variant is None:
            continue
        by_variant.setdefault(variant, [])
        if is_exportable(entry):
            by_variant[variant].append(entry)
    if (default_variant := resolve_language_variant(default_language)) is not None:
        by_variant.setdefault(default_variant, [])

    names, area_names = _slot_names(hass)
    wrote_any = False
    for variant, entries in by_variant.items():
        path = sentences_path(hass, variant)
        try:
            exported, hashes, wrote = await hass.async_add_executor_job(
                _export_variant_sync,
                path,
                variant,
                entries,
                dict(store.entries),
                path.parent,
                names,
                area_names,
                result,
            )
        except (ExportValidationError, OSError, yaml.YAMLError) as err:
            _LOGGER.error("Assist Learner export for %s failed: %s", variant, err)
            result.file_error = f"{variant}: {err}"
            continue
        for entry in exported:
            entry["export_hash"] = hashes[entry["id"]]
            entry["export_error"] = None
            result.exported.append(entry["id"])
        if wrote:
            result.written.append(str(path))
            wrote_any = True

    for entry_id, error in result.errors.items():
        if entry := store.get(entry_id):
            entry["export_error"] = error
    store.async_schedule_save()

    if wrote_any:
        await hass.services.async_call("conversation", "reload", {}, blocking=True)
    return result
