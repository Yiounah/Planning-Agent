"""Rolling-wave planner for tree instantiation, mutation, and subtree merging."""

from __future__ import annotations

import logging
from typing import Any

from app.core.event_bus import AsyncEventBus
from app.core.memory_store import MemoryStore
from app.models.domain_events import (
    DomainEvent,
    EventType,
    ReplanTriggerPayload,
    TaskTreeDraftPayload,
    TreeMutatedPayload,
)
from app.models.task_tree import TaskNode

logger = logging.getLogger(__name__)


class RollingWavePlanner:
    """Planner module that operationalizes rolling-wave decomposition updates."""

    def __init__(self, *, event_bus: AsyncEventBus, memory_store: MemoryStore) -> None:
        self._event_bus = event_bus
        self._memory_store = memory_store
        self._registered = False

    def register(self) -> None:
        """Register planner listeners for initial and adaptive decomposition."""
        if self._registered:
            return

        self._event_bus.subscribe(EventType.TASK_TREE_DRAFTED, self._on_task_tree_drafted)
        self._event_bus.subscribe(EventType.REPLAN_TRIGGER, self._on_replan_trigger)
        self._registered = True

    async def _on_task_tree_drafted(self, event: DomainEvent) -> None:
        """Instantiate initial task tree and emit first mutation signal."""

        payload = TaskTreeDraftPayload.model_validate(event.payload)
        root = TaskNode.from_llm_json(payload.tree_json)
        root.enforce_pending_leaf_nodes()

        await self._memory_store.set_task_tree(root)

        await self._event_bus.publish(
            DomainEvent.build(
                event_type=EventType.NEW_TASK,
                source="rolling_planner",
                payload_model={
                    "goal": payload.goal,
                    "persona": payload.persona,
                    "root_task_id": root.task_id,
                    "tree": root.model_dump(mode="json"),
                },
                correlation_id=event.event_id,
            )
        )

        mutation = TreeMutatedPayload(
            target_node_id=root.task_id,
            mutation_kind="INITIAL_TREE_CREATED",
            subtree=root.model_dump(mode="json"),
            metadata={"phase": "initial_decomposition"},
        )
        await self._event_bus.publish(
            DomainEvent.build(
                event_type=EventType.TREE_MUTATED,
                source="rolling_planner",
                payload_model=mutation,
                correlation_id=event.event_id,
            )
        )

    async def _on_replan_trigger(self, event: DomainEvent) -> None:
        """Merge LLM-produced pending subtree into active plan graph."""

        payload = ReplanTriggerPayload.model_validate(event.payload)
        raw_children = self._extract_children(payload.subtree_json)
        if not raw_children:
            logger.warning("Replan payload has no replacement children")
            return

        replacement_children = [
            TaskNode.from_llm_json(child_json, parent_id=payload.target_node_id)
            for child_json in raw_children
        ]

        def mutator(root: TaskNode) -> TreeMutatedPayload:
            target = root.find_node(payload.target_node_id)
            if target is None:
                raise KeyError(f"Replan target not found: {payload.target_node_id}")

            merge_stats = target.replace_pending_children(replacement_children)
            return TreeMutatedPayload(
                target_node_id=target.task_id,
                mutation_kind="SUBTREE_REPLACED",
                subtree=target.model_dump(mode="json"),
                metadata={
                    "strategy": payload.strategy.value,
                    **merge_stats,
                },
            )

        mutation_payload = await self._memory_store.mutate_tree(mutator)
        await self._event_bus.publish(
            DomainEvent.build(
                event_type=EventType.TREE_MUTATED,
                source="rolling_planner",
                payload_model=mutation_payload,
                correlation_id=event.event_id,
            )
        )

    def _extract_children(self, subtree_json: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize replan JSON so planner always receives child-node list."""

        children = subtree_json.get("children")
        if isinstance(children, list):
            return [item for item in children if isinstance(item, dict)]

        if {
            "task_id",
            "title",
            "estimated_time",
            "is_leaf",
            "children",
        }.issubset(subtree_json.keys()):
            return [subtree_json]

        return []
