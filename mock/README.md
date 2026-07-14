# Scheduler Mock Server

Standalone mock HTTP server for the SYA Scheduler function. It mirrors the
format used by `SYA-UI/function/scheduler/mock` and serves deterministic data
from `data.json` so the UI can be tested before the real function runtime is
ready.

## Quick Start

```bash
cd mock
python3 server.py
```

The server listens on port `8766` by default. Override it with `PORT` or a
command-line argument:

```bash
PORT=9000 python3 server.py
python3 server.py 9000
```

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Health check |
| `GET` | `/manifest` | Function manifest |
| `GET` | `/api/scheduler/actions` | Callable action list |
| `GET` | `/api/scheduler/events` | SSE ready event |
| `GET` | `/api/scheduler/tasks` | List tasks, optional `?date=` and `?status=` |
| `GET` | `/api/scheduler/tasks/{id}` | Get a task with subtasks |
| `PUT` | `/api/scheduler/tasks/{id}` | Update status, priority, deadline, or title |
| `DELETE` | `/api/scheduler/tasks/{id}` | Delete a task |
| `GET` | `/api/scheduler/timeline` | Timeline view, optional `?date=` |
| `POST` | `/api/scheduler/decompose` | Return mock decomposition results |
| `POST` | `/api/scheduler/tasks/reorder` | Validate and echo task order |
| `GET` | `/api/scheduler/stats` | Daily task statistics |
| `GET` | `/api/scheduler/config` | Scheduler config |
| `PUT` | `/api/scheduler/config` | Merge scheduler config updates |

## Examples

```bash
curl http://127.0.0.1:8766/health
curl http://127.0.0.1:8766/api/scheduler/tasks
curl "http://127.0.0.1:8766/api/scheduler/tasks?date=2026-07-14"
curl http://127.0.0.1:8766/api/scheduler/timeline
curl http://127.0.0.1:8766/api/scheduler/stats
```

```bash
curl -X POST http://127.0.0.1:8766/api/scheduler/decompose \
  -H "Content-Type: application/json" \
  -d '{"input": "Prepare tomorrow's team update and review the PR"}'
```

```bash
curl -X PUT http://127.0.0.1:8766/api/scheduler/tasks/11111111-1111-4111-8111-111111111111 \
  -H "Content-Type: application/json" \
  -d '{"status": "completed"}'
```

## Mock Data

`data.json` contains common work and personal planning tasks:

1. Finish weekly research report
2. Review scheduler integration PR
3. Prepare interview study block
4. Plan grocery and meal prep
5. Clean inbox and reply to important messages
6. Evening workout

The dataset covers `pending`, `in_progress`, and `completed` states, multiple
priorities, subtasks, daily stats, and a timeline for `2026-07-14`.

All endpoints return the standard FunctionResponse envelope:

```json
{"ok": true, "data": {}}
```

Errors use:

```json
{"ok": false, "error": {"code": "ERROR_CODE", "message": "Human readable message"}}
```

PUT and DELETE mutate the in-memory copy only. Restarting the server reloads
the original `data.json`.
