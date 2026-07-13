"""Execution efficacy evaluators for rolling-wave replanning decisions."""

from __future__ import annotations

from statistics import mean
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.models.task_tree import TERMINAL_STATUSES, TaskStatus


class TaskExecutionSample(BaseModel):
    """Compact execution sample used for recent-window efficacy scoring."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str = Field(min_length=1)
    status: TaskStatus
    estimated_time: float = Field(gt=0)
    actual_time: float | None = Field(default=None, ge=0)


class ProgressMetrics(BaseModel):
    """Calculated progress indicators for adaptive replanning policies."""

    model_config = ConfigDict(extra="forbid", strict=True)

    completion_rate: float = Field(ge=0.0, le=1.0)
    procrastination_index: float = Field(ge=0.0)
    sample_size: int = Field(ge=0)


def evaluate_progress(samples: Sequence[TaskExecutionSample]) -> ProgressMetrics:
    """Compute completion rate and procrastination index.

    Completion rate reflects short-horizon execution efficacy.
    Procrastination index is defined as mean positive delay ratio:
    max(actual-estimated, 0) / estimated over completed tasks.
    """

    if not samples:
        return ProgressMetrics(completion_rate=0.0, procrastination_index=0.0, sample_size=0)

    completed = [sample for sample in samples if sample.status in TERMINAL_STATUSES]
    completion_rate = len(completed) / len(samples)

    delay_ratios: list[float] = []
    for sample in completed:
        if sample.actual_time is None:
            continue
        delay = max(sample.actual_time - sample.estimated_time, 0.0)
        delay_ratios.append(delay / sample.estimated_time)

    procrastination_index = mean(delay_ratios) if delay_ratios else 0.0

    return ProgressMetrics(
        completion_rate=completion_rate,
        procrastination_index=procrastination_index,
        sample_size=len(samples),
    )


def decide_replan_strategy(
    *,
    metrics: ProgressMetrics,
    low_threshold: float,
    high_threshold: float,
) -> Literal["fine", "coarse", "none"]:
    """Select replanning granularity according to threshold-based policy."""

    if metrics.completion_rate < low_threshold:
        return "fine"
    if metrics.completion_rate > high_threshold:
        return "coarse"
    return "none"
