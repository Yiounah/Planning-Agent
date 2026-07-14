# Scheduler Function

Standalone SYA scheduler function module with cognitive task decomposition and
rolling-wave planning. In the parent app this repo is intended to be linked as:

```text
function/scheduler
```

## What It Owns

- Natural-language decomposition through `decompose`.
- Scheduler-compatible task, timeline, stats, and config actions.
- Hierarchical task trees backed by `sya_task_scheduler`.
- Rolling-wave replanning that replaces pending work while preserving completed
  task history.
- Local debug page and legacy `/api/v1/tasks/*` endpoints for backend testing.

## Required SYA Files

```text
module.json
manifest.json
api.openapi.json
bin/scheduler-server
assets/
sya_task_scheduler/
```

`manifest.json` starts the local HTTP runtime. `api.openapi.json` defines the
Function API action names used by the Electron bridge.

## Function API

| Operation ID | Method | Path |
| --- | --- | --- |
| `health` | `GET` | `/health` |
| `manifest` | `GET` | `/manifest` |
| `decompose` | `POST` | `/api/scheduler/decompose` |
| `tasks.list` | `GET` | `/api/scheduler/tasks` |
| `tasks.get` | `GET` | `/api/scheduler/tasks/{taskId}` |
| `tasks.update` | `PUT` | `/api/scheduler/tasks/{taskId}` |
| `tasks.delete` | `DELETE` | `/api/scheduler/tasks/{taskId}` |
| `timeline.get` | `GET` | `/api/scheduler/timeline` |
| `tasks.reorder` | `POST` | `/api/scheduler/tasks/reorder` |
| `stats.get` | `GET` | `/api/scheduler/stats` |
| `config.get` | `GET` | `/api/scheduler/config` |
| `config.update` | `PUT` | `/api/scheduler/config` |

All endpoints except `/manifest` return:

```json
{ "ok": true, "data": {} }
```

The legacy debug routes remain under `/api/v1/tasks/*`, and the debug page is
served from `/`.

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r sya_task_scheduler/requirements.txt
npm test
PORT=8000 bin/scheduler-server
```

Open `http://127.0.0.1:8000` for the debug dashboard.

No LLM key is required for local tests. Without `SYA_OPENAI_API_KEY`, the
runtime uses deterministic mock plans.

## Build Artifact

```bash
npm run build
```

This writes:

```text
dist/sya-function-scheduler-0.1.0-<target>.tar.gz
```

The archive contains the files required by the SYA Electron function runtime.
It is a Python launcher artifact for integration testing; a production release
can later replace `bin/scheduler-server` with a bundled executable.

## Parent Repo Integration

From the SYA-UI parent repository, pin this repo at `function/scheduler`.

If `function/scheduler` is already a submodule:

```bash
git checkout dev
git pull --ff-only
git submodule set-url function/scheduler https://github.com/Yiounah/SYA-function-scheduler.git
git submodule update --init --recursive --remote function/scheduler
git add .gitmodules function/scheduler
git commit -m "feat: update scheduler function module"
```

If `function/scheduler` does not exist yet:

```bash
git checkout dev
git pull --ff-only
git submodule add -b main https://github.com/Yiounah/SYA-function-scheduler.git function/scheduler
git add .gitmodules function/scheduler
git commit -m "feat: add scheduler function module"
```

If UI changes are needed for new actions, add a feedback note in the parent
repo docs so the UI owner can map the new API to renderer features.
