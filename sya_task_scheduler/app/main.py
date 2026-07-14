"""FastAPI entrypoint wiring event bus, services, REST APIs, and WebSocket sync."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
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


class DecomposeRequest(BaseModel):
    """Scheduler API request body for natural-language task decomposition."""

    model_config = ConfigDict(extra="forbid", strict=True)

    input: str = Field(min_length=1)
    attachments: list[str] = Field(default_factory=list)


class ReorderTasksRequest(BaseModel):
    """Scheduler API request body for task reordering."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_ids: list[str] = Field(alias="taskIds", min_length=1)


FUNCTION_MANIFEST: dict[str, Any] = {
    "id": "scheduler",
    "version": "0.1.0",
    "runtime": "local-http",
    "entry": {"command": "bin/scheduler-server", "args": []},
    "api": {
        "basePath": "/api/scheduler",
        "health": {"method": "GET", "path": "/health"},
        "events": {"transport": "sse", "path": "/api/scheduler/events"},
    },
    "capabilities": [
        "task-decomposition",
        "timeline-scheduling",
        "task-storage",
        "rolling-wave-planning",
        "hierarchical-task-tree",
        "adaptive-replanning",
    ],
    "events": ["task.created", "task.updated", "task.deleted", "timeline.changed"],
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


def _scheduler_status(status: TaskStatus) -> str:
    if status == TaskStatus.IN_PROGRESS:
        return "in_progress"
    if status == TaskStatus.DONE:
        return "completed"
    if status in (TaskStatus.BLOCKED, TaskStatus.SKIPPED):
        return "cancelled"
    return "pending"


def _task_status(value: Any) -> TaskStatus | None:
    if value is None:
        return None
    normalized = str(value).strip().upper().replace("-", "_")
    aliases = {
        "PENDING": TaskStatus.PENDING,
        "IN_PROGRESS": TaskStatus.IN_PROGRESS,
        "COMPLETED": TaskStatus.DONE,
        "COMPLETE": TaskStatus.DONE,
        "DONE": TaskStatus.DONE,
        "CANCELLED": TaskStatus.SKIPPED,
        "CANCELED": TaskStatus.SKIPPED,
        "BLOCKED": TaskStatus.BLOCKED,
        "SKIPPED": TaskStatus.SKIPPED,
    }
    return aliases.get(normalized)


def _minutes(hours: float | None) -> int:
    return max(1, int(round(float(hours or 0.5) * 60)))


def _iter_nodes(node: Any) -> list[Any]:
    return list(node.iter_nodes()) if node else []


def _iter_leaves(node: Any) -> list[Any]:
    return list(node.iter_leaves()) if node else []


def _node_to_subtask(node: Any, order: int) -> dict[str, Any]:
    return {
        "id": node.task_id,
        "taskId": node.parent_id or node.task_id,
        "title": node.title,
        "estimatedMinutes": _minutes(node.estimated_time),
        "status": "completed" if node.status == TaskStatus.DONE else "pending",
        "order": order,
    }


def _node_to_task(node: Any) -> dict[str, Any]:
    return {
        "id": node.task_id,
        "title": node.title,
        "description": node.description,
        "status": _scheduler_status(node.status),
        "priority": "medium",
        "estimatedMinutes": _minutes(node.estimated_time),
        "actualMinutes": _minutes(node.actual_time) if node.actual_time is not None else None,
        "deadline": None,
        "scheduledDate": None,
        "scheduledStart": None,
        "subtasks": [_node_to_subtask(child, index) for index, child in enumerate(node.children)],
        "sourceText": None,
        "attachments": [],
        "createdAt": None,
        "updatedAt": None,
    }


def _timeline_for_tree(root: Any) -> dict[str, Any]:
    cursor = 9 * 60
    events: list[dict[str, Any]] = []
    for leaf in _iter_leaves(root):
        duration = _minutes(leaf.estimated_time)
        start = cursor
        end = cursor + duration
        events.append(
            {
                "taskId": leaf.task_id,
                "title": leaf.title,
                "startTime": f"{start // 60:02d}:{start % 60:02d}",
                "endTime": f"{end // 60:02d}:{end % 60:02d}",
                "priority": "medium",
                "status": _scheduler_status(leaf.status),
            }
        )
        cursor = end
    return {"date": None, "events": events}


def _scheduler_config(settings: Settings) -> dict[str, Any]:
    return {
        "llm": {
            "provider": "openai-compatible",
            "apiKey": "",
            "model": settings.openai_model,
            "baseUrl": settings.openai_base_url,
        },
        "storage": {"dbPath": "memory://sya-task-tree"},
        "decompose": {
            "maxSubtasks": 8,
            "defaultEstimateMinutes": 30,
            "includeBreaks": False,
            "breakDurationMinutes": 5,
        },
        "planning": {
            "completionLowThreshold": settings.completion_low_threshold,
            "completionHighThreshold": settings.completion_high_threshold,
            "evaluationLeafBatchSize": settings.evaluation_leaf_batch_size,
            "metricsWindowSize": settings.metrics_window_size,
        },
    }


def _resolve_task_id(path_value: str, payload: dict[str, Any] | None = None) -> str:
    if not path_value.startswith("{"):
        return path_value
    payload = payload or {}
    return str(payload.get("taskId") or payload.get("task_id") or path_value)


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


@app.post("/api/scheduler/decompose", response_model=FunctionResponse)
async def scheduler_decompose(
    body: DecomposeRequest,
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """Decompose free-form input into scheduler tasks and timeline events."""

    ack = await publish_init_task(
        InitTaskRequest(goal=body.input, persona="balanced"),
        container,
        source="scheduler_api",
    )
    await container.event_bus.join()
    tree = await container.memory_store.get_task_tree_snapshot()
    tasks = [_node_to_task(node) for node in _iter_leaves(tree)]
    timeline = _timeline_for_tree(tree)["events"] if tree else []
    return function_ok({"tasks": tasks, "timeline": timeline, "eventId": ack.event_id})


@app.get("/api/scheduler/tasks", response_model=FunctionResponse)
async def scheduler_list_tasks(
    status: str | None = None,
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """List scheduler tasks from the active task tree."""

    tree = await container.memory_store.get_task_tree_snapshot()
    tasks = [_node_to_task(node) for node in _iter_nodes(tree)]
    if tree is not None:
        tasks = [task for task in tasks if task["id"] != tree.task_id]
    if status:
        tasks = [task for task in tasks if task["status"] == status]
    return function_ok(tasks)


@app.get("/api/scheduler/tasks/{taskId}", response_model=FunctionResponse)
async def scheduler_get_task(
    taskId: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """Get one scheduler task by id."""

    task_id = _resolve_task_id(taskId, dict(request.query_params))
    tree = await container.memory_store.get_task_tree_snapshot()
    node = tree.find_node(task_id) if tree is not None else None
    if node is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return function_ok(_node_to_task(node))


@app.put("/api/scheduler/tasks/{taskId}", response_model=FunctionResponse)
async def scheduler_update_task(
    taskId: str,
    body: dict[str, Any],
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """Update task fields supported by the rolling planner tree."""

    task_id = _resolve_task_id(taskId, body)
    status = _task_status(body.get("status"))
    if status is not None:
        await publish_status_update(
            task_id=task_id,
            body=UpdateTaskStatusRequest(
                status=status,
                actual_time=body.get("actualMinutes") / 60 if body.get("actualMinutes") else None,
            ),
            container=container,
            source="scheduler_api",
        )

    def mutator(root: Any) -> dict[str, Any]:
        node = root.find_node(task_id)
        if node is None:
            raise KeyError(task_id)
        if "title" in body and body["title"]:
            node.title = str(body["title"])
        if "description" in body:
            node.description = str(body["description"] or "")
        return _node_to_task(node)

    try:
        task = await container.memory_store.mutate_tree(mutator)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    await container.event_bus.join()
    tree = await container.memory_store.get_task_tree_snapshot()
    node = tree.find_node(task_id) if tree is not None else None
    return function_ok(_node_to_task(node) if node is not None else task)


@app.delete("/api/scheduler/tasks/{taskId}", response_model=FunctionResponse)
async def scheduler_delete_task(
    taskId: str,
    body: dict[str, Any] | None = None,
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    """Delete a non-root task from the active task tree."""

    task_id = _resolve_task_id(taskId, body or {})

    def remove_from(parent: Any) -> bool:
        for index, child in enumerate(parent.children):
            if child.task_id == task_id:
                del parent.children[index]
                parent.is_leaf = len(parent.children) == 0
                return True
            if remove_from(child):
                return True
        return False

    def mutator(root: Any) -> dict[str, str]:
        if root.task_id == task_id:
            raise ValueError("Cannot delete root task")
        if not remove_from(root):
            raise KeyError(task_id)
        return {"taskId": task_id}

    try:
        return function_ok(await container.memory_store.mutate_tree(mutator))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc


@app.get("/api/scheduler/timeline", response_model=FunctionResponse)
async def scheduler_timeline(
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    tree = await container.memory_store.get_task_tree_snapshot()
    return function_ok(_timeline_for_tree(tree))


@app.post("/api/scheduler/tasks/reorder", response_model=FunctionResponse)
async def scheduler_reorder_tasks(
    body: ReorderTasksRequest,
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    order = {task_id: index for index, task_id in enumerate(body.task_ids)}

    def mutator(root: Any) -> None:
        for node in root.iter_nodes():
            node.children.sort(key=lambda child: order.get(child.task_id, len(order)))

    await container.memory_store.mutate_tree(mutator)
    return function_ok(None)


@app.get("/api/scheduler/stats", response_model=FunctionResponse)
async def scheduler_stats(
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    tree = await container.memory_store.get_task_tree_snapshot()
    leaves = _iter_leaves(tree)
    completed = [leaf for leaf in leaves if leaf.status == TaskStatus.DONE]
    return function_ok(
        {
            "date": None,
            "completedCount": len(completed),
            "totalEstimateMinutes": sum(_minutes(leaf.estimated_time) for leaf in leaves),
            "actualMinutes": sum(_minutes(leaf.actual_time) for leaf in leaves if leaf.actual_time),
            "pendingCount": sum(1 for leaf in leaves if leaf.status == TaskStatus.PENDING),
            "inProgressCount": sum(1 for leaf in leaves if leaf.status == TaskStatus.IN_PROGRESS),
        }
    )


@app.get("/api/scheduler/config", response_model=FunctionResponse)
async def scheduler_get_config(
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    return function_ok(_scheduler_config(container.settings))


@app.put("/api/scheduler/config", response_model=FunctionResponse)
async def scheduler_update_config(
    body: dict[str, Any],
    container: AppContainer = Depends(get_container),
) -> FunctionResponse:
    config = _scheduler_config(container.settings)
    for key, value in body.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return function_ok(config)


@app.get("/api/scheduler/events")
async def scheduler_events() -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        yield 'event: ready\ndata: {"functionId":"scheduler"}\n\n'

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/scheduler/actions", response_model=FunctionResponse)
async def function_actions() -> FunctionResponse:
    """List stable Function API action names for this runtime."""

    return function_ok(
        {
            "functionId": FUNCTION_MANIFEST["id"],
            "actions": [
                {"action": "health", "method": "GET", "path": "/health"},
                {"action": "manifest", "method": "GET", "path": "/manifest"},
                {"action": "decompose", "method": "POST", "path": "/api/scheduler/decompose"},
                {"action": "tasks.list", "method": "GET", "path": "/api/scheduler/tasks"},
                {"action": "tasks.get", "method": "GET", "path": "/api/scheduler/tasks/{taskId}"},
                {"action": "tasks.update", "method": "PUT", "path": "/api/scheduler/tasks/{taskId}"},
                {"action": "tasks.delete", "method": "DELETE", "path": "/api/scheduler/tasks/{taskId}"},
                {"action": "timeline.get", "method": "GET", "path": "/api/scheduler/timeline"},
                {"action": "tasks.reorder", "method": "POST", "path": "/api/scheduler/tasks/reorder"},
                {"action": "stats.get", "method": "GET", "path": "/api/scheduler/stats"},
                {"action": "config.get", "method": "GET", "path": "/api/scheduler/config"},
                {"action": "config.update", "method": "PUT", "path": "/api/scheduler/config"},
            ],
        }
    )


@app.post("/api/v1/tasks/init", response_model=EventAckResponse)
async def init_task(
    body: InitTaskRequest,
    container: AppContainer = Depends(get_container),
) -> EventAckResponse:
    """Phase-1 API: submit new goal and persona to event bus."""

    return await publish_init_task(body, container, source="rest_api")


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


@app.get("/api/v1/tasks/tree")
async def get_task_tree(
    container: AppContainer = Depends(get_container),
) -> dict:
    """Return full task tree snapshot for initial UI hydration."""

    tree = await container.memory_store.get_task_tree_snapshot()
    if tree is None:
        raise HTTPException(status_code=404, detail="Task tree is not initialized")
    return tree.model_dump(mode="json")


@app.websocket("/ws/tree")
async def tree_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint for incremental tree mutation and status events."""

    container: AppContainer = websocket.app.state.container
    await container.ws_sync_service.serve(websocket)
