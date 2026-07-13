"""Persona schemas for adaptive planning style and cognitive bias injection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PersonaProfile(BaseModel):
    """Behavioral profile that conditions decomposition granularity."""

    model_config = ConfigDict(extra="forbid", strict=True)

    persona_id: str = Field(default="default", min_length=1)
    label: str = Field(default="Balanced Executor", min_length=1)

    planning_style: Literal["balanced", "micro", "macro"] = "balanced"
    risk_tolerance: float = Field(default=0.5, ge=0.0, le=1.0)
    focus_span_minutes: int = Field(default=45, ge=10, le=240)


DEFAULT_PERSONAS: dict[str, PersonaProfile] = {
    "balanced": PersonaProfile(
        persona_id="balanced",
        label="Balanced Executor",
        planning_style="balanced",
        risk_tolerance=0.5,
        focus_span_minutes=45,
    ),
    "micro": PersonaProfile(
        persona_id="micro",
        label="Detail-oriented Optimizer",
        planning_style="micro",
        risk_tolerance=0.3,
        focus_span_minutes=30,
    ),
    "macro": PersonaProfile(
        persona_id="macro",
        label="High-level Navigator",
        planning_style="macro",
        risk_tolerance=0.7,
        focus_span_minutes=60,
    ),
}


def resolve_persona(persona: str) -> PersonaProfile:
    """Map input persona string to a configured profile with fallback."""
    normalized = persona.strip().lower()
    return DEFAULT_PERSONAS.get(normalized, DEFAULT_PERSONAS["balanced"])
