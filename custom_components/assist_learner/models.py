"""Data shapes shared across Assist Learner modules."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Request details that the chat-log events themselves don't carry."""

    language: str | None
    device_id: str | None
    area_id: str | None


@dataclass(slots=True)
class CapturedCall:
    """One tool call recorded from the chat log."""

    id: str
    name: str
    args: dict[str, Any]
    external: bool
    agent_id: str
    result: dict[str, Any] | None = None


@dataclass(slots=True)
class Exchange:
    """One finalized user turn and everything the agent did in response."""

    conversation_id: str
    text: str
    turn_index: int | None
    context: TurnContext | None = None
    calls: list[CapturedCall] = field(default_factory=list)
    agent_ids: set[str] = field(default_factory=set)
    final_speech: str | None = None
    superseded: bool = False
    adapter_error: str | None = None
    system_prompt: str | None = None
    tool_names: list[str] = field(default_factory=list)
