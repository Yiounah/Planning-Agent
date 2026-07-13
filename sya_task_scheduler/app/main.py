"""FastAPI entrypoint wiring event bus, services, REST APIs, and WebSocket sync."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.core.cognitive_engine import CognitiveEngine, OpenAICompatiblePlannerGateway
from app.core.event_bus import AsyncEventBus
from app.core.memory_store import MemoryStore
from app.core.rolling_planner import RollingWavePlanner
from app.models.domain_events import (
    DomainEvent,
    EventType,
    NewTaskRequestPayload,
    StatusUpdatePayload,
)
from app.models.task_tree import TaskStatus
from app.services.task_manager import TaskManager
from app.services.ws_sync_service import WebSocketSyncService

FRONTEND_INDEX = Path(__file__).resolve().parent / "static" / "index.html"


class InitTaskRequest(BaseModel):
    """REST request body for phase-1 goal and persona input."""

    model_config = ConfigDict(extra="forbid", strict=True)

    goal: str = Field(min_length=1)
    persona: str = Field(default="balanced", min_length=1)


class UpdateTaskStatusRequest(BaseModel):
    """REST request body for phase-3 leaf status updates."""

    model_config = ConfigDict(extra="forbid")

    status: TaskStatus
    actual_time: float | None = Field(default=None, ge=0)


class EventAckResponse(BaseModel):
    """Generic async event acceptance response."""

    model_config = ConfigDict(extra="forbid", strict=True)

    accepted: bool
    event_id: str


class FunctionResponse(BaseModel):
    """Standard SYA function response envelope."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: Any | None = None
    error: dict[str, Any] | None = None


class FunctionStatusUpdateRequest(BaseModel):
    """Function API request body for status updates without path params."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_id: str = Field(alias="taskId", min_length=1)
    status: TaskStatus
    actual_time: float | None = Field(default=None, alias="actualTime", ge=0)


FUNCTION_MANIFEST: dict[str, Any] = {
    "id": "planning",
    "version": "0.1.0",
    "runtime": "local-http",
    "entry": {"command": "bin/planning-server", "args": []},
    "api": {
        "basePath": "/api/planning",
        "health": {"method": "GET", "path": "/health"},
        "events": {"transport": "websocket", "path": "/ws/tree"},
    },
    "capabilities": [
        "cognitive-task-decomposition",
        "rolling-wave-planning",
        "hierarchical-task-tree",
        "adaptive-replanning",
        "execution-feedback-evaluation",
        "websocket-tree-sync",
    ],
    "events": ["NEW_TASK", "TASK_STATUS_UPDATED", "TREE_MUTATED"],
    "permissions": ["llm-api"],
}


@dataclass(slots=True)
class AppContainer:
    """Dependency container for runtime service graph."""

    settings: Settings
    event_bus: AsyncEventBus
    memory_store: MemoryStore
    cognitive_engine: CognitiveEngine
    rolling_planner: RollingWavePlanner
    task_manager: TaskManager
    ws_sync_service: WebSocketSyncService


def build_container(settings: Settings) -> AppContainer:
    """Construct and wire all application services."""

    event_bus = AsyncEventBus()
    memory_store = MemoryStore(event_history_maxlen=settings.event_history_maxlen)

    llm_gateway = OpenAICompatiblePlannerGateway(settings)
    cognitive_engine = CognitiveEngine(
        event_bus=event_bus,
        memory_store=memory_store,
        llm_gateway=llm_gateway,
    )
    rolling_planner = RollingWavePlanner(event_bus=event_bus, memory_store=memory_store)
    task_manager = TaskManager(
        event_bus=event_bus,
        memory_store=memory_store,
        settings=settings,
    )
    ws_sync_service = WebSocketSyncService(event_bus=event_bus)

    return AppContainer(
        settings=settings,
        event_bus=event_bus,
        memory_store=memory_store,
        cognitive_engine=cognitive_engine,
        rolling_planner=rolling_planner,
        task_manager=task_manager,
        ws_sync_service=ws_sync_service,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop asynchronous infrastructure for the FastAPI app."""

    settings = get_settings()
    container = build_container(settings)

    container.cognitive_engine.register()
    container.rolling_planner.register()
    container.task_manager.register()
    container.ws_sync_service.register()

    container.event_bus.subscribe(None, container.memory_store.append_event)

    await container.event_bus.start()
    app.state.container = container

    try:
        yield
    finally:
        await container.event_bus.stop()


app = FastAPI(
    title="SYA Cognitive-aware Task Scheduler",
    version="0.1.0",
    lifespan=lifespan,
)


def get_container(request: Request) -> AppContainer:
    """Resolve container from application state for dependency injection."""

    return request.app.state.container


def function_ok(data: Any) -> FunctionResponse:
    """Wrap data in the standard SYA FunctionResponse envelope."""

    return FunctionResponse(ok=True, data=data)


async def publish_init_task(
    body: InitTaskRequest,
    container: AppContainer,
    *,
    source: str,
) -> EventAckResponse:
    """Publish a new planning request into the event bus."""

    payload = NewTaskRequestPayload(goal=body.goal, persona=body.persona)
    event = DomainEvent.build(
        event_type=EventType.NEW_TASK_REQUEST,
        source=source,
        payload_model=payload,
    )
    await container.event_bus.publish(event)

    return EventAckResponse(accepted=True, event_id=event.event_id)


async def publish_status_update(
    *,
    task_id: str,
    body: UpdateTaskStatusRequest,
    container: AppContainer,
    source: str,
) -> EventAckResponse:
    """Publish a task status update into the event bus."""

    payload = StatusUpdatePayload(
        task_id=task_id,
        status=body.status,
        actual_time=body.actual_time,
    )
    event = DomainEvent.build(
        event_type=EventType.STATUS_UPDATE,
        source=source,
        payload_model=payload,
    )
    await container.event_bus.publish(event)

    return EventAckResponse(accepted=True, event_id=event.event_id)


@app.get("/", include_in_schema=False)
async def frontend() -> FileResponse:
    """Serve the minimal browser UI for local task-tree debugging."""

    return FileResponse(FRONTEND_INDEX)


@app.get("/health")
async def health() -> FunctionResponse:
    """Health probe endpoint."""

    return function_ok(
        {
            "status": "ok",
            "functionId": FUNCTION_MANIFEST["id"],
            "version": FUNCTION_MANIFEST["version"],
        }
    )


@app.get("/manifest")
async def manifest() -> dict[str, Any]:
    """Return the SYA function manifest consumed by Electron."""

    return FUNCTION_MANIFEST


@app.get("/api/planning/actions", response_model=FunctionResponse)
async def function_actions() -> FunctionResponse:
    """List stable Function API action names for this runtime."""

    return function_ok(
        {
            "functionId": FUNCTION_MANIFEST["id"],
            "actions": [
                {"action": "health", "method": "GET", "path": "/health"},
                {"action": "manifest", "method": "GET", "path": "/manifest"},
                {"action": "actions.list", "method": "GET", "path": "/api/planning/actions"},
                {"action": "config.get", "method": "GET", "path": "/api/planning/config"},
                {"action": "tasks.init", "method": "POST", "path": "/api/planning/tasks/init"},
                {"action": "tasks.tree", "method": "GET", "path": "/api/planning/tasks/tree"},
                {
                    "action": "tasks.status.update",
                    "method": "POST",
                    "path": "/api/planning/tasks/status",
                },
            ],
        }
    )


@app.get("/api/planning/config", response_model=FunctionResponse)
async def function_config(
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """Expose runtime planning thresholds and LLM settings without secrets."""

    settings = container.settings
    return function_ok(
        {
            "appName": settings.app_name,
            "debug": settings.debug,
            "planning": {
                "completionLowThreshold": settings.completion_low_threshold,
                "completionHighThreshold": settings.completion_high_threshold,
                "evaluationLeafBatchSize": settings.evaluation_leaf_batch_size,
                "metricsWindowSize": settings.metrics_window_size,
            },
            "sync": {
                "eventHistoryMaxlen": settings.event_history_maxlen,
                "wsPingIntervalSeconds": settings.ws_ping_interval_seconds,
            },
            "llm": {
                "provider": "openai-compatible",
                "baseUrl": settings.openai_base_url,
                "model": settings.openai_model,
                "timeoutSeconds": settings.llm_timeout_seconds,
                "configured": bool(settings.openai_api_key),
            },
            "personas": ["balanced", "micro", "macro"],
        }
    )


@app.post("/api/v1/tasks/init", response_model=EventAckResponse)
async def init_task(
    body: InitTaskRequest,
    container: AppContainer = Depends(get_container),
) -> EventAckResponse:
    """Phase-1 API: submit new goal and persona to event bus."""

    return await publish_init_task(body, container, source="rest_api")


@app.post("/api/planning/tasks/init", response_model=FunctionResponse)
async def function_init_task(
    body: InitTaskRequest,
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """Function API wrapper for starting a planning run."""

    ack = await publish_init_task(body, container, source="function_api")
    return function_ok(ack.model_dump(mode="json"))


@app.patch("/api/v1/tasks/{task_id}/status", response_model=EventAckResponse)
async def update_task_status(
    task_id: str,
    body: UpdateTaskStatusRequest,
    container: AppContainer = Depends(get_container),
) -> EventAckResponse:
    """Phase-3 API: submit task status mutation for execution tracking."""

    return await publish_status_update(
        task_id=task_id,
        body=body,
        container=container,
        source="rest_api",
    )


@app.post("/api/planning/tasks/status", response_model=FunctionResponse)
async def function_update_task_status(
    body: FunctionStatusUpdateRequest,
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """Function API wrapper for task status updates without URL path params."""

    ack = await publish_status_update(
        task_id=body.task_id,
        body=UpdateTaskStatusRequest(status=body.status, actual_time=body.actual_time),
        container=container,
        source="function_api",
    )
    return function_ok(ack.model_dump(mode="json"))


@app.get("/api/v1/tasks/tree")
async def get_task_tree(
    container: AppContainer = Depends(get_container),
) -> dict:
    """Return full task tree snapshot for initial UI hydration."""

    tree = await container.memory_store.get_task_tree_snapshot()
    if tree is None:
        raise HTTPException(status_code=404, detail="Task tree is not initialized")
    return tree.model_dump(mode="json")


@app.get("/api/planning/tasks/tree", response_model=FunctionResponse)
async def function_get_task_tree(
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """Function API tree snapshot; returns null before initialization."""

    tree = await container.memory_store.get_task_tree_snapshot()
    return function_ok({"tree": tree.model_dump(mode="json") if tree is not None else None})


@app.websocket("/ws/tree")
async def tree_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint for incremental tree mutation and status events."""

    container: AppContainer = websocket.app.state.container
    await container.ws_sync_service.serve(websocket)
