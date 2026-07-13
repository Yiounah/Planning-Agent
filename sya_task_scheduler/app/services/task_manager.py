"""Task manager for status mutation, progress evaluation, and replan triggers."""

from __future__ import annotations

from app.config import Settings
from app.core.event_bus import AsyncEventBus
from app.core.memory_store import MemoryStore
from app.models.domain_events import (
    DomainEvent,
    EvaluateProgressPayload,
    EventType,
    ReplanDecisionPayload,
    ReplanStrategy,
    StatusUpdatePayload,
    TaskStatusUpdatedPayload,
)
from app.utils.metrics_evaluator import decide_replan_strategy, evaluate_progress


class TaskManager:
    """Service orchestrating phase-3 status tracking and phase-4 evaluation."""

    def __init__(
        self,
        *,
        event_bus: AsyncEventBus,
        memory_store: MemoryStore,
        settings: Settings,
    ) -> None:
        self._event_bus = event_bus
        self._memory_store = memory_store
        self._settings = settings

        self._completed_leaf_since_eval = 0
        self._registered = False

    def register(self) -> None:
        """Attach task lifecycle handlers to the event bus."""
        if self._registered:
            return

        self._event_bus.subscribe(EventType.STATUS_UPDATE, self._on_status_update)
        self._event_bus.subscribe(EventType.TASK_STATUS_UPDATED, self._on_task_status_updated)
        self._event_bus.subscribe(EventType.EVALUATE_PROGRESS, self._on_evaluate_progress)
        self._registered = True

    async def _on_status_update(self, event: DomainEvent) -> None:
        """Apply client status updates to task tree and emit derived event."""

        payload = StatusUpdatePayload.model_validate(event.payload)
        transition = await self._memory_store.update_task_status(
            task_id=payload.task_id,
            new_status=payload.status,
            actual_time=payload.actual_time,
        )

        updated_payload = TaskStatusUpdatedPayload(
            task_id=transition.task_id,
            old_status=transition.old_status,
            new_status=transition.new_status,
            is_leaf=transition.is_leaf,
            estimated_time=transition.estimated_time,
            actual_time=transition.actual_time,
            transitioned_to_terminal_leaf=transition.transitioned_to_terminal_leaf,
        )
        await self._event_bus.publish(
            DomainEvent.build(
                event_type=EventType.TASK_STATUS_UPDATED,
                source="task_manager",
                payload_model=updated_payload,
                correlation_id=event.event_id,
            )
        )

    async def _on_task_status_updated(self, event: DomainEvent) -> None:
        """Count terminal leaf completions and trigger periodic evaluations."""

        payload = TaskStatusUpdatedPayload.model_validate(event.payload)
        if not payload.transitioned_to_terminal_leaf:
            return

        self._completed_leaf_since_eval += 1
        if self._completed_leaf_since_eval < self._settings.evaluation_leaf_batch_size:
            return

        self._completed_leaf_since_eval = 0
        eval_payload = EvaluateProgressPayload(window_size=self._settings.metrics_window_size)
        await self._event_bus.publish(
            DomainEvent.build(
                event_type=EventType.EVALUATE_PROGRESS,
                source="task_manager",
                payload_model=eval_payload,
                correlation_id=event.event_id,
            )
        )

    async def _on_evaluate_progress(self, event: DomainEvent) -> None:
        """Evaluate efficacy metrics and choose fine/coarse replanning."""

        payload = EvaluateProgressPayload.model_validate(event.payload)
        samples = await self._memory_store.get_recent_execution_samples(payload.window_size)
        metrics = evaluate_progress(samples)

        strategy = decide_replan_strategy(
            metrics=metrics,
            low_threshold=self._settings.completion_low_threshold,
            high_threshold=self._settings.completion_high_threshold,
        )
        if strategy == "none":
            return

        replan_strategy = ReplanStrategy.FINE if strategy == "fine" else ReplanStrategy.COARSE
        target_node_id = await self._memory_store.select_replan_target(replan_strategy)
        if not target_node_id:
            return

        decision_payload = ReplanDecisionPayload(
            target_node_id=target_node_id,
            completion_rate=metrics.completion_rate,
            procrastination_index=metrics.procrastination_index,
        )

        event_type = (
            EventType.REPLAN_FINE_GRAINED
            if replan_strategy == ReplanStrategy.FINE
            else EventType.REPLAN_COARSE_GRAINED
        )
        await self._event_bus.publish(
            DomainEvent.build(
                event_type=event_type,
                source="task_manager",
                payload_model=decision_payload,
                correlation_id=event.event_id,
            )
        )
