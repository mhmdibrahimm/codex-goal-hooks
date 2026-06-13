import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import goal_status_report as report


SCHEMA = """
CREATE TABLE thread_goals (
    thread_id TEXT PRIMARY KEY NOT NULL,
    goal_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    time_used_seconds INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
)
"""


def create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def set_goal(
    path: Path,
    *,
    thread_id: str = "thread-1",
    goal_id: str = "goal-1",
    status: str = "active",
    objective: str = "Keep the build moving",
    updated_at_ms: int = 100,
    token_budget: int | None = 1000,
    tokens_used: int = 0,
    time_used_seconds: int = 0,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO thread_goals (
                thread_id, goal_id, objective, status, token_budget,
                tokens_used, time_used_seconds, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                goal_id,
                objective,
                status,
                token_budget,
                tokens_used,
                time_used_seconds,
                1,
                updated_at_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_goal(path: Path, thread_id: str = "thread-1") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM thread_goals WHERE thread_id = ?", (thread_id,))
        conn.commit()
    finally:
        conn.close()


def update_goal_payload(
    status: str,
    *,
    thread_id: str = "thread-1",
    goal_id: str = "goal-1",
    tool_use_id: str = "tool-1",
    updated_at_ms: int = 200,
    objective: str = "Do sensitive work",
) -> dict:
    return {
        "event_name": "PostToolUse",
        "tool_name": "update_goal",
        "tool_use_id": tool_use_id,
        "tool_response": {
            "goal": {
                "thread_id": thread_id,
                "goal_id": goal_id,
                "objective": objective,
                "status": status,
                "token_budget": 1000,
                "tokens_used": 10,
                "time_used_seconds": 2,
                "updated_at_ms": updated_at_ms,
            }
        },
    }


class FakeUrlopenResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b"ok"


class GoalStatusReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_dir = self.root / "state"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_report(self, payload: dict | None = None, extra_args: list[str] | None = None, config: dict | None = None) -> None:
        args = ["--state-dir", str(self.state_dir)]
        if config is not None:
            config_path = self.root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            args.extend(["--config", str(config_path)])
        if extra_args:
            args.extend(extra_args)
        report.main(args, stdin_text=json.dumps(payload or {}), raise_errors=True)

    def read_events(self) -> list[dict]:
        path = self.state_dir / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_direct_post_tool_use_complete_payload_produces_complete_event(self):
        self.run_report(update_goal_payload("complete"), ["--no-db"])

        events = self.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["new_status"], "complete")
        self.assertEqual(events[0]["source"], "post_tool_use_update_goal")

    def test_direct_post_tool_use_blocked_payload_produces_blocked_event(self):
        self.run_report(update_goal_payload("blocked"), ["--no-db"])

        events = self.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["new_status"], "blocked")
        self.assertEqual(events[0]["source"], "post_tool_use_update_goal")

    def test_db_diff_active_to_paused_produces_paused_event(self):
        db_path = self.root / "goals.sqlite"
        create_db(db_path)
        set_goal(db_path, status="active", updated_at_ms=100)
        self.run_report({"event_name": "UserPromptSubmit"}, ["--snapshot-only", "--db-path", str(db_path)])

        set_goal(db_path, status="paused", updated_at_ms=200)
        self.run_report({"event_name": "SessionStart"}, ["--db-path", str(db_path)])

        events = self.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["old_status"], "active")
        self.assertEqual(events[0]["new_status"], "paused")
        self.assertEqual(events[0]["source"], "sqlite_goal_state")

    def test_db_diff_paused_to_active_produces_active_event(self):
        db_path = self.root / "goals.sqlite"
        create_db(db_path)
        set_goal(db_path, status="paused", updated_at_ms=100)
        self.run_report({"event_name": "UserPromptSubmit"}, ["--snapshot-only", "--db-path", str(db_path)])

        set_goal(db_path, status="active", updated_at_ms=200)
        self.run_report({"event_name": "Stop"}, ["--db-path", str(db_path)])

        events = self.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["old_status"], "paused")
        self.assertEqual(events[0]["new_status"], "active")

    def test_reading_both_sqlite_paths_prefers_freshest_updated_at_ms(self):
        older = self.root / "goals-old.sqlite"
        newer = self.root / "goals-new.sqlite"
        create_db(older)
        create_db(newer)
        set_goal(older, thread_id="thread-1", status="active", updated_at_ms=100)
        set_goal(newer, thread_id="thread-1", status="paused", updated_at_ms=200)

        self.run_report(
            {"event_name": "SessionStart"},
            ["--db-path", str(older), "--db-path", str(newer)],
        )

        events = self.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["new_status"], "paused")
        snapshot = json.loads((self.state_dir / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["rows"]["thread-1"]["status"], "paused")

    def test_missing_row_does_not_become_complete(self):
        db_path = self.root / "goals.sqlite"
        create_db(db_path)
        set_goal(db_path, status="active", updated_at_ms=100)
        self.run_report({"event_name": "UserPromptSubmit"}, ["--snapshot-only", "--db-path", str(db_path)])

        delete_goal(db_path)
        self.run_report({"event_name": "Stop"}, ["--db-path", str(db_path)])

        self.assertEqual(self.read_events(), [])

    def test_ntfy_notification_uses_status_only_text(self):
        db_path = self.root / "goals.sqlite"
        create_db(db_path)
        set_goal(db_path, thread_id="thread-secret", status="active", updated_at_ms=100)
        config = {
            "ntfy": {
                "enabled": True,
                "server": "https://ntfy.example",
                "topic": "private-topic",
                "mode": "all",
                "detail": "status_only",
            }
        }
        self.run_report(
            {"event_name": "UserPromptSubmit"},
            ["--snapshot-only", "--db-path", str(db_path)],
            config,
        )

        requests = []

        def fake_urlopen(request, timeout=0):
            requests.append(request)
            return FakeUrlopenResponse()

        payload = update_goal_payload(
            "blocked",
            thread_id="thread-secret",
            tool_use_id="tool-secret",
            updated_at_ms=200,
            objective="SECRET PROMPT CONTENT",
        )
        with mock.patch.object(report.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.run_report(payload, ["--no-db"], config)

        self.assertEqual(len(requests), 1)
        body = requests[0].data.decode("utf-8")
        self.assertEqual(body, "Goal status: active -> blocked")
        self.assertNotIn("SECRET", body)
        self.assertNotIn("thread-secret", body)

    def test_macos_notification_respects_mode(self):
        config = {"macos": {"enabled": True, "mode": "complete", "detail": "status_only"}}
        with mock.patch.object(report.platform, "system", return_value="Darwin"), mock.patch.object(
            report.subprocess, "run"
        ) as run:
            self.run_report(update_goal_payload("blocked", tool_use_id="tool-blocked"), ["--no-db"], config)
            self.run_report(update_goal_payload("complete", tool_use_id="tool-complete", updated_at_ms=300), ["--no-db"], config)

        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("osascript", command[0])
        self.assertIn("Goal status: blocked -> complete", command[-1])

    def test_duplicate_direct_update_goal_payload_does_not_resend(self):
        config = {
            "ntfy": {
                "enabled": True,
                "server": "https://ntfy.example",
                "topic": "private-topic",
                "mode": "all",
            }
        }
        payload = update_goal_payload("complete", tool_use_id="same-tool", updated_at_ms=555)
        calls = []

        def fake_urlopen(request, timeout=0):
            calls.append(request)
            return FakeUrlopenResponse()

        with mock.patch.object(report.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.run_report(payload, ["--no-db"], config)
            self.run_report(payload, ["--no-db"], config)

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.read_events()), 1)

    def test_config_can_disable_sqlite_signal(self):
        db_path = self.root / "goals.sqlite"
        create_db(db_path)
        set_goal(db_path, status="active", updated_at_ms=100)
        self.run_report(
            {"event_name": "UserPromptSubmit"},
            ["--snapshot-only", "--db-path", str(db_path)],
            {"signals": {"sqlite_goal_state": True}},
        )

        set_goal(db_path, status="paused", updated_at_ms=200)
        self.run_report(
            {"event_name": "Stop"},
            ["--db-path", str(db_path)],
            {"signals": {"sqlite_goal_state": False}},
        )

        self.assertEqual(self.read_events(), [])

    def test_config_can_disable_direct_update_goal_signal(self):
        self.run_report(
            update_goal_payload("blocked"),
            ["--no-db"],
            {"signals": {"post_tool_use_update_goal": False}},
        )

        self.assertEqual(self.read_events(), [])


if __name__ == "__main__":
    unittest.main()
