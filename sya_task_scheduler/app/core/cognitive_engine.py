"""Cognitive engine handling intent capture, efficacy contexting, and LLM prompting."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any
from uuid import uuid4

import httpx

from app.config import Settings
from app.core.event_bus import AsyncEventBus
from app.core.memory_store import MemoryStore
from app.models.domain_events import (
    DomainEvent,
    EventType,
    NewTaskRequestPayload,
    ReplanDecisionPayload,
    ReplanStrategy,
    ReplanTriggerPayload,
    TaskTreeDraftPayload,
)
from app.models.persona import PersonaProfile, resolve_persona

logger = logging.getLogger(__name__)


class LLMPlannerGateway(ABC):
    """Abstract LLM adapter for OpenAI-compatible task decomposition."""

    @abstractmethod
    async def generate_task_tree(
        self,
        *,
        goal: str,
        persona: PersonaProfile,
        strategy: ReplanStrategy | None,
        context: dict[str, Any],
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Return JSON matching the provided schema for planning operations."""


class OpenAICompatiblePlannerGateway(LLMPlannerGateway):
    """OpenAI-format client with deterministic fallback for offline development."""

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.openai_base_url.rstrip("/")
        self._api_key = settings.openai_api_key
        self._model = settings.openai_model
        self._timeout_seconds = settings.llm_timeout_seconds

    async def generate_task_tree(
        self,
        *,
        goal: str,
        persona: PersonaProfile,
        strategy: ReplanStrategy | None,
        context: dict[str, Any],
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate decomposition tree via OpenAI API format or local fallback."""

        if not self._api_key:
            return self._mock_plan(goal=goal, persona=persona, strategy=strategy, context=context)

        system_prompt = (
            "You are a cognitive-aware planning model. Output strict JSON only. "
            "No markdown, no explanations, and obey the schema exactly."
        )
        user_prompt = self._compose_prompt(goal, persona, strategy, context)

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "task_tree",
                    "schema": response_schema,
                    "strict": True,
                },
            },
            "temperature": 0.2,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                response_payload = response.json()

            content = response_payload["choices"][0]["message"]["content"]
            return self._load_json_content(content)
        except Exception:
            logger.exception("LLM request failed, fallback plan will be used")
            return self._mock_plan(goal=goal, persona=persona, strategy=strategy, context=context)

    def _compose_prompt(
        self,
        goal: str,
        persona: PersonaProfile,
        strategy: ReplanStrategy | None,
        context: dict[str, Any],
    ) -> str:
        """Build prompt text with intent and efficacy-aware planning context."""

        strategy_text = strategy.value if strategy else "INITIAL"
        return (
            f"Goal: {goal}\n"
            f"Persona: {persona.model_dump(mode='json')}\n"
            f"Strategy: {strategy_text}\n"
            f"Context: {json.dumps(context, ensure_ascii=True)}\n"
            "Produce actionable tasks with realistic estimated_time and explicit hierarchy."
        )

    def _load_json_content(self, content: str) -> dict[str, Any]:
        """Parse JSON body and recover from fenced blocks when necessary."""

        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json", "", 1).strip()
        payload = json.loads(cleaned)
        if not isinstance(payload, dict):
            raise ValueError("Expected object JSON payload")
        return payload

    def _mock_plan(
        self,
        *,
        goal: str,
        persona: PersonaProfile,
        strategy: ReplanStrategy | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate deterministic local plans when remote LLM is unavailable."""

        if strategy is None:
            root_id = f"root-{uuid4().hex[:8]}"
            return {
                "task_id": root_id,
                "title": goal,
                "description": f"Top-level plan for persona {persona.persona_id}",
                "estimated_time": 8.0,
                "is_leaf": False,
                "children": [
                    {
                        "task_id": f"{root_id}-a",
                        "title": "Clarify success criteria",
                        "description": "Define measurable completion outcomes.",
                        "estimated_time": 1.0,
                        "is_leaf": True,
                        "children": [],
                    },
                    {
                        "task_id": f"{root_id}-b",
                        "title": "Build execution roadmap",
                        "description": "Segment milestones and dependencies.",
                        "estimated_time": 2.0,
                        "is_leaf": False,
                        "children": [
                            {
                                "task_id": f"{root_id}-b1",
                                "title": "Schedule milestone sprint",
                                "description": "Create near-term wave with concrete outputs.",
                                "estimated_time": 1.5,
                                "is_leaf": True,
                                "children": [],
                            },
                            {
                                "task_id": f"{root_id}-b2",
                                "title": "Define risk mitigation checks",
                                "description": "Prepare contingencies for blockers.",
                                "estimated_time": 1.0,
                                "is_leaf": True,
                                "children": [],
                            },
                        ],
                    },
                    {
                        "task_id": f"{root_id}-c",
                        "title": "Execute and monitor",
                        "description": "Track outcomes and capture metrics.",
                        "estimated_time": 3.0,
                        "is_leaf": True,
                        "children": [],
                    },
                ],
            }

        target_id = str(context.get("target_node_id", f"target-{uuid4().hex[:6]}"))
        if strategy == ReplanStrategy.FINE:
            return {
                "target_node_id": target_id,
                "children": [
                    {
                        "task_id": f"{target_id}-m1",
                        "title": "Prepare micro-task checklist",
                        "description": "Break pending work into 15-30 minute chunks.",
                        "estimated_time": 0.5,
                        "is_leaf": True,
                        "children": [],
                    },
                    {
                        "task_id": f"{target_id}-m2",
                        "title": "Add explicit time boxing",
                        "description": "Assign strict short deadlines for each chunk.",
                        "estimated_time": 0.5,
                        "is_leaf": True,
                        "children": [],
                    },
                    {
                        "task_id": f"{target_id}-m3",
                        "title": "Define immediate validation points",
                        "description": "Create quick completion checks after each chunk.",
                        "estimated_time": 0.5,
                        "is_leaf": True,
                        "children": [],
                    },
                ],
            }

        return {
            "target_node_id": target_id,
            "children": [
                {
                    "task_id": f"{target_id}-c1",
                    "title": "Merge routine activities",
                    "description": "Group repetitive pending steps into one macro action.",
                    "estimated_time": 2.0,
                    "is_leaf": True,
                    "children": [],
                },
                {
                    "task_id": f"{target_id}-c2",
                    "title": "Focus on milestone output",
                    "description": "Prioritize high-impact deliverables over micro tracking.",
                    "estimated_time": 2.5,
                    "is_leaf": True,
                    "children": [],
                },
            ],
        }


class CognitiveEngine:
    """Brain module for intent capture and adaptive replanning prompt assembly."""

    def __init__(
        self,
        *,
        event_bus: AsyncEventBus,
        memory_store: MemoryStore,
        llm_gateway: LLMPlannerGateway,
    ) -> None:
        self._event_bus = event_bus
        self._memory_store = memory_store
        self._llm_gateway = llm_gateway
        self._registered = False

    def register(self) -> None:
        """Attach cognitive handlers to the asynchronous event bus."""
        if self._registered:
            return

        self._event_bus.subscribe(EventType.NEW_TASK_REQUEST, self._on_new_task_request)
        self._event_bus.subscribe(EventType.REPLAN_FINE_GRAINED, self._on_replan_request)
        self._event_bus.subscribe(EventType.REPLAN_COARSE_GRAINED, self._on_replan_request)
        self._registered = True

    async def _on_new_task_request(self, event: DomainEvent) -> None:
        """Handle phase-1 intent capture and phase-2 initial decomposition."""

        payload = NewTaskRequestPayload.model_validate(event.payload)
        persona = resolve_persona(payload.persona)

        await self._memory_store.set_goal_context(goal=payload.goal, persona=persona.persona_id)

        context = self._assemble_intent_context(goal=payload.goal, persona=persona)
        tree_json = await self._llm_gateway.generate_task_tree(
            goal=payload.goal,
            persona=persona,
            strategy=None,
            context=context,
            response_schema=self._initial_tree_schema(),
        )

        draft_payload = TaskTreeDraftPayload(
            goal=payload.goal,
            persona=persona.persona_id,
            tree_json=tree_json,
        )
        await self._event_bus.publish(
            DomainEvent.build(
                event_type=EventType.TASK_TREE_DRAFTED,
                source="cognitive_engine",
                payload_model=draft_payload,
                correlation_id=event.event_id,
            )
        )

    async def _on_replan_request(self, event: DomainEvent) -> None:
        """Handle phase-4 dynamic replanning guidance generation."""

        decision = ReplanDecisionPayload.model_validate(event.payload)
        strategy = (
            ReplanStrategy.FINE
            if event.event_type == EventType.REPLAN_FINE_GRAINED
            else ReplanStrategy.COARSE
        )

        goal, persona_name = await self._memory_store.get_goal_context()
        if not goal:
            return

        persona = resolve_persona(persona_name or "balanced")
        target_subtree = await self._memory_store.get_subtree_snapshot(decision.target_node_id)
        if target_subtree is None:
            return

        replan_context = self._assemble_replan_context(
            goal=goal,
            persona=persona,
            strategy=strategy,
            target_node_id=decision.target_node_id,
            target_subtree=target_subtree,
            completion_rate=decision.completion_rate,
            procrastination_index=decision.procrastination_index,
        )

        subtree_json = await self._llm_gateway.generate_task_tree(
            goal=goal,
            persona=persona,
            strategy=strategy,
            context=replan_context,
            response_schema=self._replan_schema(),
        )

        trigger_payload = ReplanTriggerPayload(
            target_node_id=decision.target_node_id,
            strategy=strategy,
            subtree_json=subtree_json,
        )
        await self._event_bus.publish(
            DomainEvent.build(
                event_type=EventType.REPLAN_TRIGGER,
                source="cognitive_engine",
                payload_model=trigger_payload,
                correlation_id=event.event_id,
            )
        )

    def _assemble_intent_context(
        self,
        *,
        goal: str,
        persona: PersonaProfile,
    ) -> dict[str, Any]:
        """Create cognitive context for initial intent-to-plan translation."""

        return {
            "cognitive_phase": "intent_capture",
            "goal": goal,
            "persona": persona.model_dump(mode="json"),
            "constraints": {
                "must_return_json": True,
                "leaf_nodes_default_pending": True,
                "hierarchy_depth_min": 2,
            },
        }

    def _assemble_replan_context(
        self,
        *,
        goal: str,
        persona: PersonaProfile,
        strategy: ReplanStrategy,
        target_node_id: str,
        target_subtree: dict[str, Any],
        completion_rate: float,
        procrastination_index: float,
    ) -> dict[str, Any]:
        """Create efficacy-aware context for dynamic granularity adaptation."""

        return {
            "cognitive_phase": "dynamic_replanning",
            "goal": goal,
            "persona": persona.model_dump(mode="json"),
            "target_node_id": target_node_id,
            "strategy": strategy.value,
            "metrics": {
                "completion_rate": completion_rate,
                "procrastination_index": procrastination_index,
            },
            "target_subtree": target_subtree,
            "instruction": (
                "Return replacement children for pending work only."
                "Keep task titles executable and estimated_time realistic."
            ),
        }

    def _initial_tree_schema(self) -> dict[str, Any]:
        """Schema for full initial decomposition tree output."""

        return {
            "type": "object",
            "required": ["task_id", "title", "estimated_time", "is_leaf", "children"],
            "additionalProperties": False,
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "estimated_time": {"type": "number", "exclusiveMinimum": 0},
                "is_leaf": {"type": "boolean"},
                "children": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["task_id", "title", "estimated_time", "is_leaf", "children"],
                        "additionalProperties": True,
                        "properties": {
                            "task_id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "estimated_time": {"type": "number", "exclusiveMinimum": 0},
                            "is_leaf": {"type": "boolean"},
                            "children": {"type": "array"},
                        },
                    },
                },
            },
        }

    def _replan_schema(self) -> dict[str, Any]:
        """Schema for pending-subtree replacement outputs."""

        return {
            "type": "object",
            "required": ["target_node_id", "children"],
            "additionalProperties": False,
            "properties": {
                "target_node_id": {"type": "string"},
                "children": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["task_id", "title", "estimated_time", "is_leaf", "children"],
                        "additionalProperties": True,
                        "properties": {
                            "task_id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "estimated_time": {"type": "number", "exclusiveMinimum": 0},
                            "is_leaf": {"type": "boolean"},
                            "children": {"type": "array"},
                        },
                    },
                },
            },
        }
