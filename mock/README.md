# Planning Mock Notes

This module does not need a separate mock server for local development.

When `SYA_OPENAI_API_KEY` is empty, `sya_task_scheduler` automatically uses a
deterministic mock planner from `app/core/cognitive_engine.py`. The same
FastAPI runtime and browser dashboard remain available.

```bash
cd planning_agent
bin/planning-server
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/api/planning/tasks/init \
  -H "Content-Type: application/json" \
  -d '{"goal":"Plan a release","persona":"balanced"}'
curl http://127.0.0.1:8000/api/planning/tasks/tree
```
