#!/usr/bin/env python3
"""Mock HTTP server for the SYA scheduler function."""

from __future__ import annotations

import copy
import http.server
import json
import os
import re
import sys
import urllib.parse

MOCK_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_ROOT = os.path.dirname(MOCK_DIR)
DATA_PATH = os.path.join(MOCK_DIR, "data.json")
MANIFEST_PATH = os.path.join(MODULE_ROOT, "manifest.json")
DEFAULT_PORT = 8766

_tasks_by_id: dict[str, dict] = {}
_timeline: list[dict] = []
_config: dict = {}


def _load_data() -> None:
    global _tasks_by_id, _timeline, _config
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    _tasks_by_id = {task["id"]: task for task in data.get("tasks", [])}
    _timeline = data.get("timeline", [])
    _config = data.get("config", {})


_load_data()


def response(data):
    return {"ok": True, "data": data, "error": None}


def error_response(code, message, details=None):
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "data": None, "error": error}


def resolve_port() -> int:
    if len(sys.argv) > 1:
        try:
            return int(sys.argv[1])
        except ValueError:
            pass
    if os.environ.get("PORT"):
        try:
            return int(os.environ["PORT"])
        except ValueError:
            pass
    return DEFAULT_PORT


def load_manifest() -> dict:
    if not os.path.exists(MANIFEST_PATH):
        return {
            "id": "scheduler",
            "version": "0.1.0",
            "runtime": "local-http",
            "capabilities": [],
            "events": [],
            "permissions": [],
        }
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_status(value):
    if value == "active":
        return "in_progress"
    return value


def timeline_status(value):
    if value == "in_progress":
        return "active"
    if value == "pending":
        return "upcoming"
    return value


def make_timeline_event(event):
    start = event.get("startTime", "")
    end = event.get("endTime", "")
    enriched = dict(event)
    enriched["time"] = f"{start} - {end}".strip(" -")
    enriched["status"] = timeline_status(event.get("status"))
    return enriched


def handle_health():
    return {
        "status": "ok",
        "functionId": "scheduler",
        "version": load_manifest().get("version", "0.1.0"),
    }


def handle_manifest():
    return load_manifest()


def handle_actions():
    return {
        "actions": [
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
        ]
    }


def handle_decompose(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("input"), str):
        return False, error_response("INVALID_INPUT", "Request body must contain a non-empty 'input' string.")

    user_input = payload["input"].strip()
    if not user_input:
        return False, error_response("INVALID_INPUT", "The 'input' field must not be empty.")

    tasks = list(_tasks_by_id.values())[:3]
    result_tasks = [
        {
            "title": task["title"],
            "estimatedMinutes": task["estimatedMinutes"],
            "priority": task["priority"],
            "deadline": task.get("deadline"),
            "subtasks": task.get("subtasks", []),
        }
        for task in tasks
    ]
    return True, {
        "input": user_input,
        "tasks": result_tasks,
        "timeline": [make_timeline_event(event) for event in _timeline[:5]],
    }


def handle_list_tasks(query):
    date_filter = query.get("date")
    status_filter = normalize_status(query.get("status"))
    tasks = list(_tasks_by_id.values())
    if date_filter:
        tasks = [task for task in tasks if task.get("scheduledDate") == date_filter]
    if status_filter:
        tasks = [task for task in tasks if task.get("status") == status_filter]
    return tasks


def handle_get_task(task_id):
    task = _tasks_by_id.get(task_id)
    if task is None:
        return False, error_response("NOT_FOUND", f"Task '{task_id}' not found.")
    return True, task


def handle_update_task(task_id, payload):
    task = _tasks_by_id.get(task_id)
    if task is None:
        return False, error_response("NOT_FOUND", f"Task '{task_id}' not found.")
    if not isinstance(payload, dict) or not payload:
        return False, error_response("INVALID_PAYLOAD", "Update body must be a non-empty JSON object.")

    allowed = {"status", "priority", "deadline", "title", "description", "actualMinutes"}
    for key, value in payload.items():
        if key not in allowed:
            continue
        if key == "status":
            value = normalize_status(value)
            if value not in ("pending", "in_progress", "completed", "cancelled"):
                continue
        if key == "priority" and value not in ("low", "medium", "high", "urgent"):
            continue
        task[key] = value

    task["updatedAt"] = "2026-07-14T12:00:00Z"
    return True, task


def handle_delete_task(task_id):
    task = _tasks_by_id.pop(task_id, None)
    if task is None:
        return False, error_response("NOT_FOUND", f"Task '{task_id}' not found.")
    return True, {"deleted": task_id}


def handle_get_timeline(query):
    date_filter = query.get("date")
    events = _timeline
    if date_filter:
        task_ids = {
            task["id"]
            for task in _tasks_by_id.values()
            if task.get("scheduledDate") == date_filter
        }
        filtered = [
            event
            for event in _timeline
            if event.get("taskId") in task_ids or event.get("taskId", "").startswith("break-")
        ]
        if filtered:
            events = filtered
    return {
        "date": date_filter or "2026-07-14",
        "events": [make_timeline_event(event) for event in events],
    }


def handle_reorder(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("taskIds"), list):
        return False, error_response("INVALID_PAYLOAD", "Request body must contain a 'taskIds' array.")

    task_ids = payload["taskIds"]
    unknown = [task_id for task_id in task_ids if task_id not in _tasks_by_id]
    if unknown:
        return False, error_response("UNKNOWN_TASK_IDS", f"Unknown task IDs: {', '.join(unknown)}")
    return True, {"order": task_ids, "reordered": True}


def handle_get_stats(query):
    tasks = handle_list_tasks({"date": query.get("date")} if query.get("date") else {})
    completed = [task for task in tasks if task["status"] == "completed"]
    pending = [task for task in tasks if task["status"] == "pending"]
    in_progress = [task for task in tasks if task["status"] == "in_progress"]
    return {
        "date": query.get("date") or "2026-07-14",
        "completedCount": len(completed),
        "totalEstimateMinutes": sum(task["estimatedMinutes"] for task in tasks),
        "actualMinutes": sum(task.get("actualMinutes", 0) for task in tasks),
        "pendingCount": len(pending),
        "inProgressCount": len(in_progress),
    }


def handle_get_config():
    config = copy.deepcopy(_config)
    if "llm" in config and "apiKey" in config["llm"]:
        config["llm"]["apiKey"] = "sk-redacted"
    return config


def handle_update_config(payload):
    if not isinstance(payload, dict):
        return False, error_response("INVALID_PAYLOAD", "Config update must be a JSON object.")
    for section in ("llm", "storage", "decompose"):
        if section in payload and isinstance(payload[section], dict):
            _config.setdefault(section, {}).update(payload[section])
    return True, handle_get_config()


class SchedulerMockHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=MOCK_DIR, **kwargs)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if path == "/health":
            self._send_json(200, response(handle_health()))
            return
        if path == "/manifest":
            self._send_json(200, response(handle_manifest()))
            return
        if path == "/api/scheduler/actions":
            self._send_json(200, response(handle_actions()))
            return
        if path == "/api/scheduler/events":
            self._send_event_stream()
            return
        if path == "/api/scheduler/tasks":
            self._send_json(200, response(handle_list_tasks(query)))
            return
        if path == "/api/scheduler/timeline":
            self._send_json(200, response(handle_get_timeline(query)))
            return
        if path == "/api/scheduler/stats":
            self._send_json(200, response(handle_get_stats(query)))
            return
        if path == "/api/scheduler/config":
            self._send_json(200, response(handle_get_config()))
            return

        task_match = re.match(r"^/api/scheduler/tasks/([^/]+)$", path)
        if task_match:
            ok, data = handle_get_task(task_match.group(1))
            self._send_json(200 if ok else 404, response(data) if ok else data)
            return

        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self._read_json_body()

        if path == "/api/scheduler/decompose":
            ok, data = handle_decompose(payload)
            self._send_json(200 if ok else 400, response(data) if ok else data)
            return
        if path == "/api/scheduler/tasks/reorder":
            ok, data = handle_reorder(payload)
            self._send_json(200 if ok else 400, response(data) if ok else data)
            return

        self.send_error(404)

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self._read_json_body()

        task_match = re.match(r"^/api/scheduler/tasks/([^/]+)$", path)
        if task_match:
            ok, data = handle_update_task(task_match.group(1), payload)
            self._send_json(200 if ok else 404, response(data) if ok else data)
            return
        if path == "/api/scheduler/config":
            ok, data = handle_update_config(payload)
            self._send_json(200 if ok else 400, response(data) if ok else data)
            return

        self.send_error(404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        task_match = re.match(r"^/api/scheduler/tasks/([^/]+)$", parsed.path)
        if task_match:
            ok, data = handle_delete_task(task_match.group(1))
            self._send_json(200 if ok else 404, response(data) if ok else data)
            return
        self.send_error(404)

    def _read_json_body(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError:
            return {}

    def _send_json(self, status_code, payload):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _send_event_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        event = json.dumps(
            {
                "functionId": "scheduler",
                "event": "ready",
                "payload": {"message": "Scheduler mock stream connected", "tasks": len(_tasks_by_id)},
            }
        )
        self.wfile.write(f"event: ready\ndata: {event}\n\n".encode("utf-8"))
        self.wfile.flush()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = resolve_port()
    with http.server.HTTPServer(("127.0.0.1", port), SchedulerMockHandler) as httpd:
        print(f"Scheduler mock server running on http://127.0.0.1:{port}")
        print(f"Data file: {DATA_PATH}")
        print("Endpoints: /health, /manifest, /api/scheduler/*")
        httpd.serve_forever()
