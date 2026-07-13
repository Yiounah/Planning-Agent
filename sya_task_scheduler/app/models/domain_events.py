"""Typed domain events and payload contracts for event-driven orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.models.task_tree import TaskStatus


class EventType(str, Enum):
    """Canonical event taxonomy shared by all scheduler components."""

    NEW_TASK_REQUEST = "NEW_TASK_REQUEST"
    TASK_TREE_DRAFTED = "TASK_TREE_DRAFTED"

    NEW_TASK = "NEW_TASK"
    STATUS_UPDATE = "STATUS_UPDATE"
    TASK_STATUS_UPDATED = "TASK_STATUS_UPDATED"

    EVALUATE_PROGRESS = "EVALUATE_PROGRESS"
    REPLAN_FINE_GRAINED = "REPLAN_FINE_GRAINED"
    REPLAN_COARSE_GRAINED = "REPLAN_COARSE_GRAINED"
    REPLAN_TRIGGER = "REPLAN_TRIGGER"

    TREE_MUTATED = "TREE_MUTATED"


class ReplanStrategy(str, Enum):
    """Replanning granularity options used by cognitive prompts."""

    FINE = "FINE"
    COARSE = "COARSE"


class NewTaskRequestPayload(BaseModel):
    """Payload for creating a new planning session from user intent."""

    model_config = ConfigDict(extra="forbid", strict=True)

    goal: str = Field(min_length=1)
    persona: str = Field(min_length=1)


class TaskTreeDraftPayload(BaseModel):
    """Payload carrying a full initial decomposition tree from the LLM."""

    model_config = ConfigDict(extra="forbid", strict=True)

    goal: str = Field(min_length=1)
    persona: str = Field(min_length=1)
    tree_json: dict[str, Any]


class StatusUpdatePayload(BaseModel):
    """Client-submitted update for a specific task node."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    status: TaskStatus
    actual_time: float | None = Field(default=None, ge=0)


class TaskStatusUpdatedPayload(BaseModel):
    """Derived event emitted after in-memory task tree mutation."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    old_status: TaskStatus
    new_status: TaskStatus
    is_leaf: bool
    estimated_time: float = Field(gt=0)
    actual_time: float | None = Field(default=None, ge=0)
    transitioned_to_terminal_leaf: bool


class EvaluateProgressPayload(BaseModel):
    """Trigger payload for efficacy evaluation window."""

    model_config = ConfigDict(extra="forbid", strict=True)

    window_size: int = Field(ge=1)


class ReplanDecisionPayload(BaseModel):
    """Decision payload produced by the evaluator before LLM replanning."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target_node_id: str = Field(min_length=1)
    completion_rate: float = Field(ge=0.0, le=1.0)
    procrastination_index: float = Field(ge=0.0)


class ReplanTriggerPayload(BaseModel):
    """Payload sent to the planner with newly generated subtree JSON."""

    model_config = ConfigDict(extra="forbid")

    target_node_id: str = Field(min_length=1)
    strategy: ReplanStrategy
    subtree_json: dict[str, Any]


class TreeMutatedPayload(BaseModel):
    """Incremental tree mutation payload for front-end partial redraw."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target_node_id: str = Field(min_length=1)
    mutation_kind: str = Field(min_length=1)
    subtree: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class DomainEvent(BaseModel):
    """Event envelope used by the asynchronous event bus."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    source: str = Field(min_length=1)
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        event_type: EventType,
        source: str,
        payload_model: BaseModel | dict[str, Any],
        correlation_id: str | None = None,
    ) -> "DomainEvent":
        """Factory method converting payload models to JSON-ready dictionaries."""

        payload = (
            payload_model.model_dump(mode="json")
            if isinstance(payload_model, BaseModel)
            else payload_model
        )
        return cls(
            event_type=event_type,
            source=source,
            correlation_id=correlation_id,
            payload=payload,
        )

    def to_transport_dict(self) -> dict[str, Any]:
        """Serialize event for websocket transport and logging."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }
