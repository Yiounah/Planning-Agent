# SYA Planning Function

Standalone SYA function module for cognitive task decomposition and rolling-wave
planning. In the parent app this repo should be linked as:

```text
function/planning
```

## What It Owns

- Goal-to-task-tree planning through `sya_task_scheduler`.
- Persona-aware initial decomposition: `balanced`, `micro`, `macro`.
- Task status updates for hierarchical task nodes.
- Progress evaluation with completion rate and procrastination index.
- Fine/coarse replanning that replaces pending work while preserving completed
  nodes.
- WebSocket tree events for the local debug page.

## Required SYA Files

```text
module.json
manifest.json
api.openapi.json
bin/planning-server
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
| `actions.list` | `GET` | `/api/planning/actions` |
| `config.get` | `GET` | `/api/planning/config` |
| `tasks.init` | `POST` | `/api/planning/tasks/init` |
| `tasks.tree` | `GET` | `/api/planning/tasks/tree` |
| `tasks.status.update` | `POST` | `/api/planning/tasks/status` |

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
PORT=8000 bin/planning-server
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
dist/sya-function-planning-0.1.0-<target>.tar.gz
```

The archive contains the files required by the SYA Electron function runtime.
It is a Python launcher artifact for integration testing; a production release
can later replace `bin/planning-server` with a bundled executable.

## Parent Repo Integration

From the SYA-UI parent repository:

```bash
git checkout dev
git pull --ff-only
git submodule add -b main git@github.com:<owner>/SYA-function-planning.git function/planning
git add .gitmodules function/planning
git commit -m "feat: add planning function module"
```

If UI changes are needed for new actions, add a feedback note in the parent
repo docs so the UI owner can map the new API to renderer features.
