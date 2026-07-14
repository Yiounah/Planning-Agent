# SYA Task Scheduler

A backend reference implementation for **Cognitive-aware Dynamic Task Decomposition** and **Rolling-wave Planning**.
The project targets high-freedom tasks with event-driven orchestration, adaptive replanning, and incremental task-tree synchronization.

## 1. Architecture Highlights

- **Event-Driven Architecture (EDA):** all state transitions are asynchronous domain events over an internal event bus.
- **Cognitive Loop:** intent capture, execution efficacy evaluation, and dynamic granularity adaptation.
- **Hierarchical Task Tree:** arbitrary-depth self-referencing tree using Pydantic v2 with strict validation.
- **Incremental UI Sync:** websocket push of `TREE_MUTATED` payloads for partial front-end redraw.

## 2. Project Structure

```text
sya_task_scheduler/
├── app/
│   ├── core/
│   │   ├── event_bus.py
│   │   ├── cognitive_engine.py
│   │   ├── rolling_planner.py
│   │   └── memory_store.py
│   ├── models/
│   │   ├── domain_events.py
│   │   ├── task_tree.py
│   │   └── persona.py
│   ├── services/
│   │   ├── task_manager.py
│   │   └── ws_sync_service.py
│   ├── utils/
│   │   └── metrics_evaluator.py
│   ├── main.py
│   └── config.py
├── requirements.txt
└── README.md
```

## 3. Phase-Aligned Execution Flow

### Phase 1: Initialization & Intent Capture

1. Client submits `(goal, persona)` to `POST /api/v1/tasks/init`.
2. API publishes `NEW_TASK_REQUEST` into EventBus.
3. `CognitiveEngine` consumes the event and assembles cognitive context.

### Phase 2: Initial Task Decomposition

1. `CognitiveEngine` calls LLM adapter using OpenAI-compatible format.
2. LLM returns strict JSON tree schema.
3. `RollingWavePlanner` validates JSON into recursive `TaskNode`.
4. Leaf nodes are forced to `PENDING`.

### Phase 3: Execution & Status Tracking

1. Client updates leaf status via `PATCH /api/v1/tasks/{task_id}/status`.
2. API emits `STATUS_UPDATE`.
3. `TaskManager` updates the in-memory tree and records `actual_time`.
4. `TASK_STATUS_UPDATED` event is emitted.

### Phase 4: Efficacy Evaluation & Replanning Decision

1. After `evaluation_leaf_batch_size` terminal leaf completions, `EVALUATE_PROGRESS` is emitted.
2. `metrics_evaluator` computes:
   - completion rate
   - procrastination index
3. Decision policy:
   - `< completion_low_threshold` -> `REPLAN_FINE_GRAINED`
   - `> completion_high_threshold` -> `REPLAN_COARSE_GRAINED`

### Phase 5: Sub-tree Merging & UI Synchronization

1. `CognitiveEngine` receives replan decision and asks LLM for replacement subtree.
2. `RollingWavePlanner` replaces only **pending** children of target node.
3. Planner emits `TREE_MUTATED` with local subtree payload.
4. `WebSocketSyncService` pushes the event for partial UI rerender.

## 4. Run Instructions

```bash
cd sya_task_scheduler
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## 5. From 0 to API Smoke Test

This section is the shortest path from a fresh checkout to end-to-end API verification.

### Step 1: Prepare Python Environment

Recommended: Python 3.11+.

```bash
cd sya_task_scheduler
python3 --version
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If `python3` on your machine is too old, use a newer interpreter explicitly. For example:

```bash
/opt/miniconda3/bin/python3 -m venv .venv313
source .venv313/bin/activate
pip install -r requirements.txt
```

### Step 2: Start the Service

```bash
uvicorn app.main:app --reload
```

After startup, the service listens on `http://127.0.0.1:8000`.

Note:

- You do not need `SYA_OPENAI_API_KEY` for local smoke testing.
- If no API key is configured, the system falls back to deterministic mock plans.
- You can also open `http://127.0.0.1:8000` in a browser to use the built-in minimal frontend.

### Step 3: Health Check

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"ok":true,"data":{"status":"ok","functionId":"scheduler","version":"0.1.0"},"error":null}
```

### Step 4: Initialize a Task Planning Session

```bash
curl -X POST http://127.0.0.1:8000/api/v1/tasks/init \
  -H "Content-Type: application/json" \
  -d '{"goal":"Build an AI scheduling backend","persona":"balanced"}'
```

Expected response shape:

```json
{"accepted":true,"event_id":"..."}
```

Important:

- This response only means the request was accepted by the event bus.
- The actual task tree is generated asynchronously right after this.

### Step 5: Fetch the Generated Task Tree

```bash
curl http://127.0.0.1:8000/api/v1/tasks/tree
```

Expected result:

- A JSON task tree with a root node
- Several leaf tasks with status `PENDING`
- Task IDs similar to `root-xxxx-a`, `root-xxxx-b1`, `root-xxxx-b2`

### Step 6: Mark Leaf Tasks as Done

Pick leaf task IDs from the previous response, then call:

```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/tasks/<task_id>/status \
  -H "Content-Type: application/json" \
  -d '{"status":"DONE","actual_time":1.2}'
```

For a minimal end-to-end test, update 3 leaf tasks to `DONE`.

Why 3?

- The default setting `evaluation_leaf_batch_size=3` means the system evaluates progress after 3 terminal leaf completions.
- That evaluation may trigger rolling-wave replanning.

### Step 7: Fetch the Task Tree Again

```bash
curl http://127.0.0.1:8000/api/v1/tasks/tree
```

Expected result after 3 completed leaf tasks:

- Completed leaves remain `DONE`
- Some pending subtree nodes may be replaced
- In the default mock flow, you will typically see new children like `...-c1` and `...-c2`

This means the following chain is working:

1. `PATCH /status`
2. `TASK_STATUS_UPDATED`
3. `EVALUATE_PROGRESS`
4. `REPLAN_*`
5. `REPLAN_TRIGGER`
6. `TREE_MUTATED`

### Step 8: Optional WebSocket Check

Open browser devtools console on any page and run:

```javascript
const ws = new WebSocket("ws://127.0.0.1:8000/ws/tree?scope=all");
ws.onmessage = (event) => console.log(JSON.parse(event.data));
ws.onopen = () => console.log("ws connected");
```

Then call `POST /api/v1/tasks/init` again or update task status with `PATCH`.

Expected WebSocket events include:

- `NEW_TASK`
- `TASK_STATUS_UPDATED`
- `TREE_MUTATED`

### Step 9: Fast Troubleshooting

If `GET /health` fails:

- Check whether `uvicorn app.main:app --reload` is still running
- Check whether port `8000` is already occupied

If dependency installation fails:

- Make sure you are using Python 3.11+
- Try a clean virtualenv with a newer interpreter

If `GET /api/v1/tasks/tree` returns `404` right after `POST /init`:

- Wait a moment and retry because tree generation is asynchronous

If you want a quick regression check without manual curl calls:

```bash
python -m unittest discover -s tests -p 'test*.py' -v
```

## 6. API Surface

- `GET /` browser UI for local task-tree inspection
- `GET /health`
- `GET /manifest`
- `POST /api/scheduler/decompose`
- `GET /api/scheduler/tasks`
- `GET /api/scheduler/tasks/{taskId}`
- `PUT /api/scheduler/tasks/{taskId}`
- `DELETE /api/scheduler/tasks/{taskId}`
- `GET /api/scheduler/timeline`
- `POST /api/scheduler/tasks/reorder`
- `GET /api/scheduler/stats`
- `GET /api/scheduler/config`
- `PUT /api/scheduler/config`
- `GET /api/scheduler/events`
- `POST /api/v1/tasks/init`
- `PATCH /api/v1/tasks/{task_id}/status`
- `GET /api/v1/tasks/tree`
- `WS /ws/tree`

The `/api/scheduler/*` routes are the SYA Function API wrapper. They return the
standard `{ "ok": true, "data": ... }` envelope expected by the Electron
function runtime. The `/api/v1/tasks/*` routes are kept for the bundled browser
debug UI and return the original raw task-tree shapes.

## 7. Example Requests

### Init Task

```bash
curl -X POST http://127.0.0.1:8000/api/v1/tasks/init \
  -H "Content-Type: application/json" \
  -d '{"goal":"Build an AI scheduling backend","persona":"balanced"}'
```

### Update Task Status

```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/tasks/<task_id>/status \
  -H "Content-Type: application/json" \
  -d '{"status":"DONE","actual_time":1.2}'
```

### WebSocket

- Connect `ws://127.0.0.1:8000/ws/tree`
- Optional scoped subscription:
  - `ws://127.0.0.1:8000/ws/tree?scope=all`
  - `ws://127.0.0.1:8000/ws/tree?scope=<target_node_id>`

## 8. LLM Adapter Notes

`OpenAICompatiblePlannerGateway` is a unified interface for OpenAI-style chat completion requests.
If `SYA_OPENAI_API_KEY` is missing, the system automatically falls back to deterministic mock plans for local development.
