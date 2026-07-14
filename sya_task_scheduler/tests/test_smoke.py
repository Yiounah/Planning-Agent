from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app


def walk_leaves(node: dict) -> list[dict]:
    """Collect leaf nodes from a JSON task tree snapshot."""

    if not node.get("children"):
        return [node]

    leaves: list[dict] = []
    for child in node["children"]:
        leaves.extend(walk_leaves(child))
    return leaves


class SchedulerSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client_ctx = TestClient(app)
        self.client = self.client_ctx.__enter__()

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)

    def _drain_events(self) -> None:
        self.client.portal.call(self.client.app.state.container.event_bus.join)

    def test_init_creates_task_tree(self) -> None:
        response = self.client.post(
            "/api/v1/tasks/init",
            json={"goal": "Build an AI scheduling backend", "persona": "balanced"},
        )
        self.assertEqual(response.status_code, 200)

        self._drain_events()
        tree_response = self.client.get("/api/v1/tasks/tree")
        self.assertEqual(tree_response.status_code, 200)

        tree = tree_response.json()
        leaves = walk_leaves(tree)
        self.assertGreaterEqual(len(leaves), 1)
        self.assertTrue(all(leaf["status"] == "PENDING" for leaf in leaves))

    def test_frontend_shell_is_served(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("FocusFlow Planner", response.text)

    def test_function_manifest_and_health(self) -> None:
        health_response = self.client.get("/health")
        self.assertEqual(health_response.status_code, 200)
        health = health_response.json()
        self.assertTrue(health["ok"])
        self.assertEqual(health["data"]["functionId"], "scheduler")

        manifest_response = self.client.get("/manifest")
        self.assertEqual(manifest_response.status_code, 200)
        manifest = manifest_response.json()
        self.assertEqual(manifest["id"], "scheduler")
        self.assertEqual(manifest["runtime"], "local-http")

    def test_scheduler_endpoints_are_empty_before_init(self) -> None:
        response = self.client.get("/api/scheduler/tasks")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"], [])

        timeline_response = self.client.get("/api/scheduler/timeline")
        self.assertEqual(timeline_response.status_code, 200)
        timeline = timeline_response.json()
        self.assertTrue(timeline["ok"])
        self.assertEqual(timeline["data"]["events"], [])

    def test_scheduler_decompose_creates_tasks_and_timeline(self) -> None:
        response = self.client.post(
            "/api/scheduler/decompose",
            json={"input": "Build an AI scheduling backend", "attachments": []},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(len(payload["data"]["tasks"]), 1)
        self.assertGreaterEqual(len(payload["data"]["timeline"]), 1)

        tasks_response = self.client.get("/api/scheduler/tasks")
        self.assertEqual(tasks_response.status_code, 200)
        tasks_payload = tasks_response.json()
        self.assertTrue(tasks_payload["ok"])
        self.assertGreaterEqual(len(tasks_payload["data"]), 1)

    def test_scheduler_task_update_and_stats(self) -> None:
        self.client.post(
            "/api/scheduler/decompose",
            json={"input": "Build an AI scheduling backend", "attachments": []},
        )
        tasks = self.client.get("/api/scheduler/tasks").json()["data"]
        first_leaf = next(task for task in tasks if not task["subtasks"])

        response = self.client.put(
            f"/api/scheduler/tasks/{first_leaf['id']}",
            json={"status": "completed", "actualMinutes": 30},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        stats_response = self.client.get("/api/scheduler/stats")
        self.assertEqual(stats_response.status_code, 200)
        stats = stats_response.json()
        self.assertTrue(stats["ok"])
        self.assertGreaterEqual(stats["data"]["completedCount"], 1)

    def test_status_updates_and_replan_flow(self) -> None:
        self.client.post(
            "/api/v1/tasks/init",
            json={"goal": "Build an AI scheduling backend", "persona": "balanced"},
        )
        self._drain_events()

        initial_tree = self.client.get("/api/v1/tasks/tree").json()
        initial_leaves = walk_leaves(initial_tree)

        for index, leaf in enumerate(initial_leaves[:3], start=1):
            response = self.client.patch(
                f"/api/v1/tasks/{leaf['task_id']}/status",
                json={"status": "DONE", "actual_time": 1.0 + index},
            )
            self.assertEqual(response.status_code, 200)

        self._drain_events()
        final_tree = self.client.get("/api/v1/tasks/tree").json()
        final_leaves = walk_leaves(final_tree)
        final_statuses = {leaf["task_id"]: leaf["status"] for leaf in final_leaves}

        self.assertEqual(final_statuses[initial_leaves[0]["task_id"]], "DONE")
        self.assertNotIn(initial_leaves[1]["task_id"], final_statuses)
        self.assertNotIn(initial_leaves[2]["task_id"], final_statuses)
        self.assertTrue(any(task_id.endswith("-c1") for task_id in final_statuses))
        self.assertTrue(any(task_id.endswith("-c2") for task_id in final_statuses))

    def test_websocket_receives_initial_events(self) -> None:
        with self.client.websocket_connect("/ws/tree?scope=all") as websocket:
            response = self.client.post(
                "/api/v1/tasks/init",
                json={"goal": "Plan a release", "persona": "balanced"},
            )
            self.assertEqual(response.status_code, 200)

            self._drain_events()
            first_message = websocket.receive_json()
            second_message = websocket.receive_json()

        event_types = {first_message["event_type"], second_message["event_type"]}
        self.assertEqual(event_types, {"NEW_TASK", "TREE_MUTATED"})


if __name__ == "__main__":
    unittest.main()
