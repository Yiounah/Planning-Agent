"""In-memory state store for goal context, task tree, and execution traces."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.models.domain_events import DomainEvent, ReplanStrategy
from app.models.task_tree import TERMINAL_STATUSES, TaskNode, TaskStatus
from app.utils.metrics_evaluator import TaskExecutionSample

T = TypeVar("T")


class StatusTransition(BaseModel):
    """Result object describing one status transition in the task tree."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str = Field(min_length=1)
    old_status: TaskStatus
    new_status: TaskStatus
    is_leaf: bool
    estimated_time: float = Field(gt=0)
    actual_time: float | None = Field(default=None, ge=0)
    transitioned_to_terminal_leaf: bool


class MemoryStore:
    """Concurrency-safe in-memory store for runtime scheduling state."""

    def __init__(self, event_history_maxlen: int = 2000) -> None:
        self._lock = asyncio.Lock()

        self._goal: str | None = None
        self._persona: str | None = None
        self._task_tree: TaskNode | None = None

        self._event_history: deque[DomainEvent] = deque(maxlen=event_history_maxlen)
        self._execution_log: deque[TaskExecutionSample] = deque(maxlen=event_history_maxlen)

    async def set_goal_context(self, *, goal: str, persona: str) -> None:
        """Persist latest top-level user objective and persona context."""
        async with self._lock:
            self._goal = goal
            self._persona = persona

    async def get_goal_context(self) -> tuple[str | None, str | None]:
        """Read current goal and persona context."""
        async with self._lock:
            return self._goal, self._persona

    async def append_event(self, event: DomainEvent) -> None:
        """Append domain event to bounded in-memory history."""
        async with self._lock:
            self._event_history.append(event)

    async def set_task_tree(self, root: TaskNode) -> None:
        """Replace the full active task tree with a new validated root."""
        async with self._lock:
            self._task_tree = root

    async def get_task_tree_snapshot(self) -> TaskNode | None:
        """Return a deep copy of the current task tree for API responses."""
        async with self._lock:
            return self._task_tree.model_copy(deep=True) if self._task_tree else None

    async def get_subtree_snapshot(self, task_id: str) -> dict | None:
        """Return a JSON-ready subtree snapshot for targeted replanning prompts."""
        async with self._lock:
            if self._task_tree is None:
                return None
            node = self._task_tree.find_node(task_id)
            if node is None:
                return None
            return node.model_dump(mode="json")

    async def mutate_tree(self, mutator: Callable[[TaskNode], T]) -> T:
        """Apply an atomic tree mutation under lock and return mutation output."""
        async with self._lock:
            if self._task_tree is None:
                raise ValueError("Task tree is not initialized")
            return mutator(self._task_tree)

    async def update_task_status(
        self,
        *,
        task_id: str,
        new_status: TaskStatus,
        actual_time: float | None,
    ) -> StatusTransition:
        """Update node status and append execution sample for metrics evaluation."""
        async with self._lock:
            if self._task_tree is None:
                raise ValueError("Task tree is not initialized")

            node = self._task_tree.find_node(task_id)
            if node is None:
                raise KeyError(f"Task node not found: {task_id}")

            old_status = node.status
            node.status = new_status
            if actual_time is not None:
                node.actual_time = actual_time

            sample = TaskExecutionSample(
                task_id=node.task_id,
                status=node.status,
                estimated_time=node.estimated_time,
                actual_time=node.actual_time,
            )
            self._execution_log.append(sample)

            transitioned_to_terminal_leaf = (
                node.is_leaf
                and old_status not in TERMINAL_STATUSES
                and node.status in TERMINAL_STATUSES
            )

            return StatusTransition(
                task_id=node.task_id,
                old_status=old_status,
                new_status=node.status,
                is_leaf=node.is_leaf,
                estimated_time=node.estimated_time,
                actual_time=node.actual_time,
                transitioned_to_terminal_leaf=transitioned_to_terminal_leaf,
            )

    async def get_recent_execution_samples(self, window_size: int) -> list[TaskExecutionSample]:
        """Return a recency window of execution samples for progress evaluation."""
        async with self._lock:
            if window_size <= 0:
                return []
            return list(self._execution_log)[-window_size:]

    async def select_replan_target(self, strategy: ReplanStrategy) -> str | None:
        """Select pending branch target according to requested granularity strategy."""
        async with self._lock:
            if self._task_tree is None:
                return None

            candidates: list[tuple[int, TaskNode]] = []
            self._collect_pending_branches(self._task_tree, depth=0, bucket=candidates)
            if not candidates:
                return None

            if strategy == ReplanStrategy.FINE:
                depth, node = max(candidates, key=lambda item: item[0])
            else:
                depth, node = min(candidates, key=lambda item: item[0])

            _ = depth
            return node.task_id

    def _collect_pending_branches(
        self,
        node: TaskNode,
        *,
        depth: int,
        bucket: list[tuple[int, TaskNode]],
    ) -> None:
        """Collect internal nodes that still have pending descendants."""
        has_pending_child = any(child.has_pending_descendant() for child in node.children)
        if node.children and has_pending_child:
            bucket.append((depth, node))

        for child in node.children:
            self._collect_pending_branches(child, depth=depth + 1, bucket=bucket)
