"""Task tree domain model with self-referencing Pydantic v2 definitions."""

from __future__ import annotations

from enum import Enum
from typing import Iterator
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(str, Enum):
    """Finite-state set for task execution lifecycle."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.SKIPPED}


class TaskNode(BaseModel):
    """Recursive task node for hierarchical planning and incremental mutation.

    A node may contain arbitrarily deep children, enabling rolling-wave
    decomposition where pending subtrees are replaced while preserving completed
    execution history.
    """

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    task_id: str = Field(min_length=1)
    parent_id: str | None = None
    title: str = Field(min_length=1)
    description: str = ""

    status: TaskStatus = TaskStatus.PENDING
    is_leaf: bool = True
    estimated_time: float = Field(default=1.0, gt=0)
    actual_time: float | None = Field(default=None, ge=0)

    children: list["TaskNode"] = Field(default_factory=list)

    @model_validator(mode="after")
    def sync_structure_metadata(self) -> "TaskNode":
        """Keep parent references and leaf flag consistent after validation."""
        for child in self.children:
            object.__setattr__(child, "parent_id", self.task_id)
        object.__setattr__(self, "is_leaf", len(self.children) == 0)
        return self

    @classmethod
    def from_llm_json(
        cls,
        payload: dict,
        parent_id: str | None = None,
    ) -> "TaskNode":
        """Instantiate a validated tree from model-produced JSON.

        The parser normalizes uncertain LLM outputs and enforces pending status
        on leaf nodes as required by the initial decomposition phase.
        """

        raw_status = payload.get("status", TaskStatus.PENDING)
        status = raw_status if isinstance(raw_status, TaskStatus) else TaskStatus(str(raw_status))

        normalized: dict = {
            "task_id": str(payload.get("task_id") or uuid4().hex),
            "parent_id": str(payload.get("parent_id")) if payload.get("parent_id") else parent_id,
            "title": str(payload.get("title") or "Untitled Task"),
            "description": str(payload.get("description") or ""),
            "status": status,
            "is_leaf": bool(payload.get("is_leaf", True)),
            "estimated_time": float(payload.get("estimated_time", 1.0)),
            "actual_time": (
                float(payload["actual_time"])
                if payload.get("actual_time") is not None
                else None
            ),
            "children": [],
        }

        node = cls.model_validate(normalized)
        for child_payload in payload.get("children", []):
            child = cls.from_llm_json(child_payload, parent_id=node.task_id)
            node.children.append(child)

        object.__setattr__(node, "is_leaf", len(node.children) == 0)
        if node.is_leaf:
            object.__setattr__(node, "status", TaskStatus.PENDING)
        return node

    def iter_nodes(self) -> Iterator["TaskNode"]:
        """Yield all nodes in depth-first order."""
        yield self
        for child in self.children:
            yield from child.iter_nodes()

    def iter_leaves(self) -> Iterator["TaskNode"]:
        """Yield leaf nodes for status aggregation and evaluation windows."""
        if self.is_leaf:
            yield self
            return

        for child in self.children:
            yield from child.iter_leaves()

    def find_node(self, task_id: str) -> "TaskNode" | None:
        """Locate a node by id using depth-first traversal."""
        if self.task_id == task_id:
            return self

        for child in self.children:
            hit = child.find_node(task_id)
            if hit is not None:
                return hit
        return None

    def has_pending_descendant(self) -> bool:
        """Return true when the current subtree still has executable work."""
        if self.status == TaskStatus.PENDING:
            return True
        return any(child.has_pending_descendant() for child in self.children)

    def enforce_pending_leaf_nodes(self) -> None:
        """Set all leaf nodes to pending to initialize executable frontier."""
        if self.is_leaf:
            self.status = TaskStatus.PENDING
            return

        for child in self.children:
            child.enforce_pending_leaf_nodes()

    def replace_pending_children(self, new_children: list["TaskNode"]) -> dict[str, int]:
        """Replace only pending children while preserving completed history.

        This method implements the subtree merge rule for rolling-wave planning:
        non-pending children are kept, pending children are replaced by the new
        decomposition from the cognitive replanning stage.
        """

        preserved_children = [child for child in self.children if child.status != TaskStatus.PENDING]
        replaced_count = len(self.children) - len(preserved_children)

        for child in new_children:
            object.__setattr__(child, "parent_id", self.task_id)
            child.enforce_pending_leaf_nodes()

        self.children = preserved_children + new_children
        object.__setattr__(self, "is_leaf", len(self.children) == 0)

        return {
            "preserved_children": len(preserved_children),
            "replaced_pending_children": replaced_count,
            "inserted_children": len(new_children),
        }


TaskNode.model_rebuild()
