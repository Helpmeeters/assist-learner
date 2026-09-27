"""Decide whether an exchange can be learned, and abstract it into a sentence."""

from collections.abc import Iterable
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, intent

from .denylist import blocked_entities
from .models import CapturedCall, Exchange
from .normalize import NormalizationError, check_utterance, contains_phrase, is_number

# Tool arg -> hassil range list. Only these numbers can become sentence slots.
RANGE_SLOTS: dict[str, str] = {
    "brightness": "brightness",
    "temperature": "temperature",
    "position": "position",
    "volume_level": "volume",
}
_UNIT_SUFFIX = {"percent": "[%| percent]", "degrees": "[\u00b0| degrees]"}
_DROPPED_ARGS = frozenset({"preferred_area_id", "preferred_floor_id"})
_QUESTION_ENDINGS = ("?", ";", "\uff1f")


@dataclass(slots=True)
class Evaluation:
    """Outcome of evaluating one exchange."""

    eligible: bool
    reason: str | None = None
    text: str = ""
    sentence: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    room_relative: bool = False
    replay_only: bool = False
    replay_only_reason: str | None = None
    language: str | None = None
    area_id: str | None = None
    agent_id: str | None = None
    key: str = ""


def _reject(reason: str) -> Evaluation:
    return Evaluation(eligible=False, reason=reason)


def intent_types(hass: HomeAssistant) -> set[str]:
    """Return the registered intent types."""
    return {handler.intent_type for handler in intent.async_get(hass)}


def _check_result(
    call: CapturedCall, known_intents: set[str]
) -> tuple[str | None, list[str], str | None]:
    """Return (rejection, resolved entities, kind) for one tool call."""
    domain, sep, local = call.name.partition("__")
    if not sep:
        return f"tool {call.name} has no domain prefix", [], None
    result = call.result
    if result is None:
        return f"tool {call.name} has no result", [], None
    if "error" in result:
        return f"tool {call.name} failed: {result.get('error')}", [], None

    if local in known_intents:
        if result.get("response_type") != "action_done":
            return f"tool {call.name} did not perform an action", [], None
        data = result.get("data") or {}
        if data.get("failed"):
            return f"tool {call.name} partially failed", [], None
        entities = [
            target["id"]
            for target in data.get("success") or []
            if target.get("type") == "entity" and target.get("id")
        ]
        if not entities:
            return f"tool {call.name} targeted no entities", [], None
        return None, entities, "intent"

    if domain == "script":
        if result.get("success") is not True:
            return f"tool {call.name} did not succeed", [], None
        return None, [f"script.{local}"], "script"

    return f"tool {call.name} is a read or unsupported tool", [], None


def _abstract_intent_call(
    hass: HomeAssistant,
    call: CapturedCall,
    tokens: list[str],
    text: str,
    satellite_area_id: str | None,
) -> tuple[dict[str, Any], bool, str | None]:
    """Return (abstract call, room_relative, replay_only_reason); mutates tokens."""
    slots: dict[str, Any] = {}
    room_relative = False
    replay_only_reason: str | None = None

    for key, value in call.args.items():
        if key in _DROPPED_ARGS:
            continue
        if isinstance(value, list):
            if len(value) == 1:
                value = value[0]
            else:
                replay_only_reason = f"slot {key} has several values"
        slots[key] = value

    if isinstance(area_value := slots.get("area"), str):
        area_reg = ar.async_get(hass)
        areas = list(intent.find_areas(area_value, area_reg))
        area = areas[0] if len(areas) == 1 else None
        if area is not None:
            named = [area.name, *(area.aliases or ())]
            mentioned = any(contains_phrase(text, name) for name in named)
            if satellite_area_id == area.id and not mentioned:
                del slots["area"]
                room_relative = True
            else:
                slots["area"] = area.name

    for key, list_name in RANGE_SLOTS.items():
        value = slots.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        for index, token in enumerate(tokens):
            if is_number(token) and float(token) == float(value):
                ref = f"{{{list_name}}}" if list_name == key else f"{{{list_name}:{key}}}"
                if index + 1 < len(tokens) and tokens[index + 1] in _UNIT_SUFFIX:
                    ref += _UNIT_SUFFIX[tokens.pop(index + 1)]
                tokens[index] = ref
                del slots[key]
                break

    return {"name": call.name, "slots": slots}, room_relative, replay_only_reason


def compute_key(sentence: str, calls: list[dict[str, Any]], room_relative: bool) -> str:
    """Agreement key: the same wording that produced the same abstract actions."""
    payload = json.dumps(
        {"sentence": sentence, "calls": calls, "room_relative": room_relative},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def evaluate(
    hass: HomeAssistant,
    exchange: Exchange,
    extra_denylist: Iterable[str] = (),
) -> Evaluation:
    """Evaluate a finalized exchange."""
    if exchange.adapter_error:
        return _reject(f"context unavailable: {exchange.adapter_error}")
    if exchange.superseded:
        return _reject("another turn followed before this one finished")
    if exchange.turn_index != 0:
        return _reject("follow-up turn in an ongoing conversation")
    if not exchange.calls:
        return _reject("no actions were taken")
    if any(call.external for call in exchange.calls):
        return _reject("handled by the local matcher")
    if len(exchange.agent_ids) != 1:
        return _reject("more than one agent took part")
    if exchange.final_speech and exchange.final_speech.strip().endswith(
        _QUESTION_ENDINGS
    ):
        return _reject("agent asked a clarifying question")
    if exchange.context is None:
        return _reject("context unavailable")

    known_intents = intent_types(hass)
    entities: list[str] = []
    kinds: list[str] = []
    for call in exchange.calls:
        rejection, call_entities, kind = _check_result(call, known_intents)
        if rejection:
            return _reject(rejection)
        entities.extend(call_entities)
        kinds.append(kind or "")

    if blocked := blocked_entities(hass, entities, extra_denylist):
        return _reject(f"targets denylisted entities: {', '.join(sorted(blocked))}")

    try:
        text = check_utterance(exchange.text)
    except NormalizationError as err:
        return _reject(str(err))

    tokens = text.split()
    calls: list[dict[str, Any]] = []
    room_relative = False
    replay_only_reason: str | None = None
    for call, kind in zip(exchange.calls, kinds, strict=True):
        if kind == "intent":
            abstract, relative, reason = _abstract_intent_call(
                hass, call, tokens, text, exchange.context.area_id
            )
            room_relative = room_relative or relative
            replay_only_reason = replay_only_reason or reason
            calls.append(abstract)
        else:
            calls.append({"name": call.name, "args": dict(call.args)})
            replay_only_reason = replay_only_reason or "scripts can't be sentences"

    if any(is_number(token) for token in tokens):
        return _reject("utterance has a number that isn't tied to a slot")

    if len(calls) > 1:
        replay_only_reason = replay_only_reason or "several actions in one command"

    sentence = " ".join(tokens)
    return Evaluation(
        eligible=True,
        text=text,
        sentence=sentence,
        calls=calls,
        entities=sorted(set(entities)),
        room_relative=room_relative,
        replay_only=replay_only_reason is not None,
        replay_only_reason=replay_only_reason,
        language=exchange.context.language,
        area_id=exchange.context.area_id,
        agent_id=next(iter(exchange.agent_ids)),
        key=compute_key(sentence, calls, room_relative),
    )
