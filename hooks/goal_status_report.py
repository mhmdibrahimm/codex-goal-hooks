#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


SNAPSHOT_VERSION = 1
STDIN_PREVIEW_LIMIT = 4000
OBJECTIVE_PREVIEW_LIMIT = 160
MAX_DEDUPE_KEYS = 2000

DEFAULT_STATE_DIR = "~/.codex/goal-status-hook"
DEFAULT_GOAL_DB_PATHS = [
    "~/.codex/goals_1.sqlite",
    "~/.codex/sqlite/goals_1.sqlite",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "goal_db_paths": DEFAULT_GOAL_DB_PATHS,
    "signals": {
        "post_tool_use_update_goal": True,
        "sqlite_goal_state": True,
    },
    "macos": {
        "enabled": False,
        "mode": "all",
        "detail": "status_only",
        "timeout_seconds": 3,
    },
    "ntfy": {
        "enabled": False,
        "server": "https://ntfy.sh",
        "topic": "",
        "mode": "all",
        "detail": "status_only",
        "priority": "",
        "tags": "",
        "token": "",
        "click": "",
        "timeout_seconds": 3,
    },
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def now_ms() -> int:
    return int(time.time() * 1000)


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def preview_text(value: Any, limit: int = OBJECTIVE_PREVIEW_LIMIT) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def append_error(state_dir: Path, message: str, exc: BaseException | None = None) -> None:
    ensure_state_dir(state_dir)
    parts = [f"[{utc_now()}] {message}"]
    if exc is not None:
        parts.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    with (state_dir / "errors.log").open("a", encoding="utf-8") as handle:
        handle.write("\n".join(parts).rstrip() + "\n")


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        return default
    except OSError:
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.stem}.{os.getpid()}{path.suffix}.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


@contextlib.contextmanager
def exclusive_lock(state_dir: Path) -> Iterable[None]:
    ensure_state_dir(state_dir)
    lock_path = state_dir / "state.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def normalize_snapshot(raw: Any) -> dict[str, Any]:
    snapshot = raw if isinstance(raw, dict) else {}
    rows = snapshot.get("rows", {})
    normalized_rows: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("thread_id"):
                normalized_rows[str(row["thread_id"])] = dict(row)
    elif isinstance(rows, dict):
        for thread_id, row in rows.items():
            if isinstance(row, dict):
                item = dict(row)
                item.setdefault("thread_id", thread_id)
                normalized_rows[str(thread_id)] = item
    return {
        "version": SNAPSHOT_VERSION,
        "updated_at": snapshot.get("updated_at") or utc_now(),
        "rows": normalized_rows,
    }


def load_snapshot(state_dir: Path) -> dict[str, Any]:
    return normalize_snapshot(load_json(state_dir / "snapshot.json", {}))


def save_snapshot(state_dir: Path, snapshot: dict[str, Any]) -> None:
    snapshot["version"] = SNAPSHOT_VERSION
    snapshot["updated_at"] = utc_now()
    atomic_write_json(state_dir / "snapshot.json", snapshot)


def load_dedupe(state_dir: Path) -> dict[str, Any]:
    raw = load_json(state_dir / "dedupe.json", {})
    if not isinstance(raw, dict):
        raw = {}
    keys = raw.get("keys", {})
    if not isinstance(keys, dict):
        keys = {}
    return {"version": 1, "keys": keys}


def save_dedupe(state_dir: Path, dedupe: dict[str, Any]) -> None:
    keys = dedupe.get("keys", {})
    if isinstance(keys, dict) and len(keys) > MAX_DEDUPE_KEYS:
        ordered = sorted(keys.items(), key=lambda item: str(item[1]))
        keys = dict(ordered[-MAX_DEDUPE_KEYS:])
    atomic_write_json(state_dir / "dedupe.json", {"version": 1, "keys": keys})


def load_config(args: argparse.Namespace, state_dir: Path) -> dict[str, Any]:
    config_path = args.config or os.environ.get("CODEX_GOAL_STATUS_CONFIG")
    if not config_path:
        candidate = state_dir / "config.json"
        config_path = str(candidate) if candidate.exists() else ""

    config = DEFAULT_CONFIG
    if config_path:
        try:
            with expand_path(config_path).open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                config = deep_merge(DEFAULT_CONFIG, loaded)
        except Exception as exc:
            append_error(state_dir, f"Failed to read config: {config_path}", exc)
            config = DEFAULT_CONFIG
    return config


def parse_json_maybe(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def get_any(mapping: Any, names: list[str], default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for name in names:
        if name in mapping:
            return mapping[name]
    lowered = {str(key).lower(): key for key in mapping.keys()}
    for name in names:
        key = lowered.get(name.lower())
        if key is not None:
            return mapping[key]
    return default


def normalize_status(value: Any) -> str:
    return str(value or "").strip().lower()


def safe_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def extract_event_name(payload: Any) -> str:
    return str(
        get_any(payload, ["hook_event_name", "hookEventName", "event_name", "eventName", "event"], "")
        or ""
    )


def extract_tool_name(payload: Any) -> str:
    direct = get_any(payload, ["tool_name", "toolName"], "")
    if direct:
        return str(direct)
    tool = get_any(payload, ["tool", "tool_call", "toolCall"], {})
    return str(get_any(tool, ["name", "tool_name", "toolName"], "") or "")


def extract_tool_use_id(payload: Any) -> str:
    direct = get_any(payload, ["tool_use_id", "toolUseId", "tool_call_id", "toolCallId"], "")
    if direct:
        return str(direct)
    tool = get_any(payload, ["tool", "tool_call", "toolCall"], {})
    return str(get_any(tool, ["id", "tool_use_id", "toolUseId"], "") or "")


def normalized_hook_event(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def append_run_log(state_dir: Path, argv: list[str], stdin_text: str, payload: Any, unknown_args: list[str]) -> None:
    append_jsonl(
        state_dir / "runs.jsonl",
        {
            "observed_at": utc_now(),
            "argv": argv,
            "unknown_args": unknown_args,
            "cwd": os.getcwd(),
            "event_name": extract_event_name(payload),
            "tool_name": extract_tool_name(payload),
            "stdin_preview": stdin_text[:STDIN_PREVIEW_LIMIT],
        },
    )


def append_watcher_run_log(state_dir: Path, argv: list[str], unknown_args: list[str]) -> None:
    append_jsonl(
        state_dir / "runs.jsonl",
        {
            "observed_at": utc_now(),
            "argv": argv,
            "unknown_args": unknown_args,
            "cwd": os.getcwd(),
            "event_name": "watch",
            "tool_name": "",
            "stdin_preview": "",
        },
    )


def find_goal_object(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    containers: list[Any] = []
    for key in ["tool_response", "toolResponse", "response", "result", "tool_result", "toolResult"]:
        value = parse_json_maybe(get_any(payload, [key]))
        if value is not None:
            containers.append(value)
    containers.append(payload)

    for container in containers:
        container = parse_json_maybe(container)
        if not isinstance(container, dict):
            continue
        goal = parse_json_maybe(get_any(container, ["goal"]))
        if isinstance(goal, dict):
            return goal, container
        output = parse_json_maybe(get_any(container, ["output", "content", "text"]))
        if isinstance(output, dict):
            goal = parse_json_maybe(get_any(output, ["goal"]))
            if isinstance(goal, dict):
                return goal, output
            if get_any(output, ["status"]):
                return output, container
        if get_any(container, ["status"]):
            return container, container
    return {}, {}


def build_row_from_direct_payload(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    event_name = normalized_hook_event(extract_event_name(payload))
    tool_name = extract_tool_name(payload)
    if tool_name != "update_goal":
        return None, ""
    if event_name and event_name != "posttooluse":
        return None, ""

    goal, container = find_goal_object(payload)
    status = normalize_status(get_any(goal, ["status", "new_status", "newStatus"]))
    if not status:
        return None, ""

    updated_raw = get_any(goal, ["updated_at_ms", "updatedAtMs", "updated_at", "updatedAt"])
    updated_at_ms = safe_int(updated_raw)
    event_updated_at_ms = updated_at_ms if updated_at_ms is not None else now_ms()

    thread_id = str(
        get_any(goal, ["thread_id", "threadId"], None)
        or get_any(container, ["thread_id", "threadId"], None)
        or get_any(payload, ["thread_id", "threadId"], "")
        or ""
    )
    goal_id = str(
        get_any(goal, ["goal_id", "goalId", "id"], None)
        or get_any(container, ["goal_id", "goalId"], None)
        or ""
    )

    row = {
        "thread_id": thread_id,
        "goal_id": goal_id,
        "status": status,
        "objective_preview": preview_text(get_any(goal, ["objective", "objective_preview", "objectivePreview"])),
        "token_budget": safe_int(get_any(goal, ["token_budget", "tokenBudget"])),
        "tokens_used": safe_int(get_any(goal, ["tokens_used", "tokensUsed"]), 0),
        "time_used_seconds": safe_int(get_any(goal, ["time_used_seconds", "timeUsedSeconds"]), 0),
        "updated_at_ms": event_updated_at_ms,
    }

    dedupe_key = "|".join(
        [
            extract_tool_use_id(payload),
            thread_id,
            status,
            "" if updated_raw is None else str(updated_raw),
        ]
    )
    return row, dedupe_key


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sqlite_uri(path: Path) -> str:
    return "file:" + urllib.parse.quote(str(path), safe="/:") + "?mode=ro"


def choose_goal_table(conn: sqlite3.Connection) -> str | None:
    tables = [
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if str(row[0]) != "_sqlx_migrations"
    ]
    if "thread_goals" in tables:
        return "thread_goals"

    for table in tables:
        columns = {
            str(row[1]).lower()
            for row in conn.execute(f"PRAGMA table_info({quote_identifier(table)})")
        }
        has_thread = bool({"thread_id", "threadid"} & columns)
        has_status = "status" in columns
        if has_thread and has_status:
            return table
    return None


def row_value(row: sqlite3.Row, aliases: list[str], default: Any = None) -> Any:
    keys = {key.lower(): key for key in row.keys()}
    for alias in aliases:
        key = keys.get(alias.lower())
        if key is not None:
            return row[key]
    return default


def normalize_db_row(row: sqlite3.Row) -> dict[str, Any] | None:
    thread_id = row_value(row, ["thread_id", "threadId"])
    status = normalize_status(row_value(row, ["status"]))
    if not thread_id or not status:
        return None
    return {
        "thread_id": str(thread_id),
        "goal_id": str(row_value(row, ["goal_id", "goalId", "id"], "") or ""),
        "status": status,
        "objective_preview": preview_text(row_value(row, ["objective", "objective_preview", "objectivePreview"])),
        "token_budget": safe_int(row_value(row, ["token_budget", "tokenBudget"])),
        "tokens_used": safe_int(row_value(row, ["tokens_used", "tokensUsed"], 0), 0),
        "time_used_seconds": safe_int(row_value(row, ["time_used_seconds", "timeUsedSeconds"], 0), 0),
        "updated_at_ms": safe_int(row_value(row, ["updated_at_ms", "updatedAtMs", "updated_at", "updatedAt"]), 0)
        or 0,
    }


def newer_row(candidate: dict[str, Any], existing: dict[str, Any] | None) -> bool:
    if existing is None:
        return True
    return int(candidate.get("updated_at_ms") or 0) >= int(existing.get("updated_at_ms") or 0)


def read_goal_rows_from_db(path: Path, state_dir: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(sqlite_uri(path), uri=True, timeout=0.25)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
            table = choose_goal_table(conn)
            if table is None:
                return {}
            for raw_row in conn.execute(f"SELECT * FROM {quote_identifier(table)}"):
                row = normalize_db_row(raw_row)
                if row and newer_row(row, rows.get(row["thread_id"])):
                    rows[row["thread_id"]] = row
        finally:
            conn.close()
    except sqlite3.Error as exc:
        append_error(state_dir, f"Failed to read goal DB: {path}", exc)
    return rows


def read_goal_rows(paths: list[Path], state_dir: Path) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        for thread_id, row in read_goal_rows_from_db(path, state_dir).items():
            if newer_row(row, merged.get(thread_id)):
                merged[thread_id] = row
    return merged


def row_for_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread_id": row.get("thread_id", ""),
        "goal_id": row.get("goal_id", ""),
        "status": row.get("status", ""),
        "objective_preview": row.get("objective_preview", ""),
        "token_budget": row.get("token_budget"),
        "tokens_used": row.get("tokens_used", 0),
        "time_used_seconds": row.get("time_used_seconds", 0),
        "updated_at_ms": row.get("updated_at_ms", 0),
    }


def make_event(old_status: str | None, row: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "event": "goal_status_changed",
        "observed_at": utc_now(),
        "thread_id": row.get("thread_id", ""),
        "goal_id": row.get("goal_id", ""),
        "old_status": old_status,
        "new_status": row.get("status", ""),
        "objective_preview": row.get("objective_preview", ""),
        "token_budget": row.get("token_budget"),
        "tokens_used": row.get("tokens_used", 0),
        "time_used_seconds": row.get("time_used_seconds", 0),
        "goal_updated_at_ms": row.get("updated_at_ms", 0),
        "source": source,
    }


def apply_rows_to_snapshot(
    snapshot: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    source: str,
    emit_initial: bool,
    emit_changes: bool,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    snapshot_rows = snapshot.setdefault("rows", {})

    for thread_id, row in rows.items():
        old_row = snapshot_rows.get(thread_id)
        old_updated = safe_int(get_any(old_row, ["updated_at_ms"]) if isinstance(old_row, dict) else None, 0) or 0
        new_updated = safe_int(row.get("updated_at_ms"), 0) or 0
        if old_row and new_updated and old_updated and new_updated < old_updated:
            continue

        old_status = normalize_status(get_any(old_row, ["status"]) if isinstance(old_row, dict) else None) or None
        new_status = normalize_status(row.get("status"))

        should_emit = False
        if emit_changes:
            if old_row is None and emit_initial:
                should_emit = True
            elif old_row is not None and old_status != new_status:
                should_emit = True

        if should_emit:
            events.append(make_event(old_status, row, source))
        snapshot_rows[thread_id] = row_for_snapshot(row)

    return events


def apply_direct_payload(
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    dedupe: dict[str, Any],
) -> list[dict[str, Any]]:
    row, dedupe_key = build_row_from_direct_payload(payload)
    if row is None:
        return []

    keys = dedupe.setdefault("keys", {})
    if dedupe_key and dedupe_key in keys:
        return []
    if dedupe_key:
        keys[dedupe_key] = utc_now()

    snapshot_rows = snapshot.setdefault("rows", {})
    thread_id = row.get("thread_id") or "unknown"
    row["thread_id"] = thread_id
    old_row = snapshot_rows.get(thread_id)
    old_status = normalize_status(get_any(old_row, ["status"]) if isinstance(old_row, dict) else None) or None
    snapshot_rows[thread_id] = row_for_snapshot(row)

    if old_row is not None and old_status == row["status"]:
        return []
    return [make_event(old_status, row, "post_tool_use_update_goal")]


def parse_mode(mode: Any) -> set[str]:
    if mode is None:
        return {"all"}
    text = str(mode).strip().lower()
    if not text:
        return {"all"}
    for sep in [";", "|"]:
        text = text.replace(sep, ",")
    parts = set()
    for chunk in text.replace(" ", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            parts.add(chunk)
    return parts or {"all"}


def mode_allows(mode: Any, status: str) -> bool:
    parts = parse_mode(mode)
    if parts & {"disabled", "off", "none", "false"}:
        return False
    if "all" in parts:
        return True
    if status in parts:
        return True
    if status == "active" and parts & {"resume", "resumed"}:
        return True
    return False


def config_bool(config: dict[str, Any], path: list[str], default: bool) -> bool:
    value: Any = config
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def signal_enabled(config: dict[str, Any], source: str) -> bool:
    return config_bool(config, ["signals", source], True)


def status_message(event: dict[str, Any]) -> str:
    old_status = event.get("old_status")
    new_status = event.get("new_status")
    if old_status:
        return f"Goal status: {old_status} -> {new_status}"
    return f"Goal status: {new_status}"


def local_message(event: dict[str, Any], detail: str) -> str:
    message = status_message(event)
    if detail not in {"summary", "local_detail", "debug"}:
        return message

    extras = []
    objective_preview = event.get("objective_preview")
    if objective_preview:
        extras.append(str(objective_preview))
    tokens_used = event.get("tokens_used")
    token_budget = event.get("token_budget")
    if token_budget is not None:
        extras.append(f"Tokens: {tokens_used}/{token_budget}")
    elif tokens_used:
        extras.append(f"Tokens: {tokens_used}")
    time_used = event.get("time_used_seconds")
    if time_used:
        extras.append(f"Time: {time_used}s")
    if extras:
        return message + "\n" + " | ".join(extras)
    return message


def ntfy_url(server: str, topic: str) -> str:
    return server.rstrip("/") + "/" + urllib.parse.quote(topic.strip(), safe="")


def send_ntfy(config: dict[str, Any], event: dict[str, Any], state_dir: Path) -> None:
    ntfy = config.get("ntfy", {})
    if not ntfy.get("enabled"):
        return
    status = str(event.get("new_status") or "")
    if not mode_allows(ntfy.get("mode", "all"), status):
        return

    topic = str(ntfy.get("topic") or "").strip()
    if not topic or topic == "YOUR_TOPIC_HERE":
        append_error(state_dir, "ntfy is enabled but no topic is configured")
        return

    body = status_message(event).encode("utf-8")
    headers = {"Title": "Codex goal status"}
    if ntfy.get("priority"):
        headers["Priority"] = str(ntfy["priority"])
    if ntfy.get("tags"):
        headers["Tags"] = str(ntfy["tags"])
    if ntfy.get("click"):
        headers["Click"] = str(ntfy["click"])
    token = str(ntfy.get("token") or "").strip()
    if token:
        if token.lower().startswith(("bearer ", "basic ")):
            headers["Authorization"] = token
        else:
            headers["Authorization"] = "Bearer " + token

    timeout = float(ntfy.get("timeout_seconds") or 3)
    request = urllib.request.Request(
        ntfy_url(str(ntfy.get("server") or "https://ntfy.sh"), topic),
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except Exception as exc:
        append_error(state_dir, "ntfy notification failed", exc)


def applescript_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + escaped + '"'


def send_macos(config: dict[str, Any], event: dict[str, Any], state_dir: Path) -> None:
    macos = config.get("macos", {})
    if not macos.get("enabled"):
        return
    status = str(event.get("new_status") or "")
    if not mode_allows(macos.get("mode", "all"), status):
        return
    if platform.system() != "Darwin":
        append_error(state_dir, "macOS notification requested on a non-macOS platform")
        return

    body = local_message(event, str(macos.get("detail") or "status_only"))
    script = (
        "display notification "
        + applescript_string(body)
        + " with title "
        + applescript_string("Codex goal status")
    )
    timeout = float(macos.get("timeout_seconds") or 3)
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=timeout)
    except Exception as exc:
        append_error(state_dir, "macOS notification failed", exc)


def notify(config: dict[str, Any], event: dict[str, Any], state_dir: Path) -> None:
    send_ntfy(config, event, state_dir)
    send_macos(config, event, state_dir)


def emit_events(events: list[dict[str, Any]], config: dict[str, Any], state_dir: Path) -> None:
    for event in events:
        append_jsonl(state_dir / "events.jsonl", event)
        notify(config, event, state_dir)


def configured_db_paths(args: argparse.Namespace, config: dict[str, Any]) -> list[Path]:
    raw_paths = args.db_path or config.get("goal_db_paths") or DEFAULT_GOAL_DB_PATHS
    return [expand_path(path) for path in raw_paths]


def check_db_once(
    args: argparse.Namespace,
    config: dict[str, Any],
    state_dir: Path,
    snapshot: dict[str, Any],
    emit_changes: bool,
) -> list[dict[str, Any]]:
    if args.no_db or not signal_enabled(config, "sqlite_goal_state"):
        return []
    rows = read_goal_rows(configured_db_paths(args, config), state_dir)
    return apply_rows_to_snapshot(
        snapshot,
        rows,
        source="sqlite_goal_state",
        emit_initial=emit_changes,
        emit_changes=emit_changes,
    )


def run_hook_once(
    args: argparse.Namespace,
    config: dict[str, Any],
    state_dir: Path,
    stdin_text: str,
    payload: Any,
) -> None:
    with exclusive_lock(state_dir):
        snapshot = load_snapshot(state_dir)
        dedupe = load_dedupe(state_dir)

        events: list[dict[str, Any]] = []
        if (
            not args.snapshot_only
            and isinstance(payload, dict)
            and signal_enabled(config, "post_tool_use_update_goal")
        ):
            events.extend(apply_direct_payload(payload, snapshot, dedupe))
        events.extend(
            check_db_once(
                args,
                config,
                state_dir,
                snapshot,
                emit_changes=not args.snapshot_only,
            )
        )

        emit_events(events, config, state_dir)
        save_snapshot(state_dir, snapshot)
        save_dedupe(state_dir, dedupe)


def run_watcher(args: argparse.Namespace, config: dict[str, Any], state_dir: Path, argv: list[str], unknown: list[str]) -> None:
    append_watcher_run_log(state_dir, argv, unknown)
    interval = max(0.05, float(args.interval))
    while True:
        try:
            with exclusive_lock(state_dir):
                snapshot = load_snapshot(state_dir)
                events = check_db_once(args, config, state_dir, snapshot, emit_changes=True)
                emit_events(events, config, state_dir)
                save_snapshot(state_dir, snapshot)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            append_error(state_dir, "watcher iteration failed", exc)
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Notify when Codex goal status changes.")
    parser.add_argument("--config", help="Path to config JSON. Defaults to ~/.codex/goal-status-hook/config.json.")
    parser.add_argument("--state-dir", help="State/log directory. Defaults to ~/.codex/goal-status-hook.")
    parser.add_argument("--db-path", action="append", default=[], help="Codex goals SQLite path. Can be repeated.")
    parser.add_argument("--no-db", action="store_true", help="Skip SQLite polling for this run.")
    parser.add_argument("--snapshot-only", action="store_true", help="Refresh the local snapshot without emitting notifications.")
    parser.add_argument("--watch", action="store_true", help="Poll SQLite goal state until interrupted.")
    parser.add_argument("--interval", type=float, default=0.25, help="Watcher poll interval in seconds.")
    return parser


def main(argv: list[str] | None = None, stdin_text: str | None = None, raise_errors: bool = False) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit as exc:
        state_dir = expand_path(os.environ.get("CODEX_GOAL_STATUS_STATE_DIR") or DEFAULT_STATE_DIR)
        append_error(state_dir, "Argument parsing failed")
        if raise_errors:
            raise exc
        return 0

    state_dir = expand_path(args.state_dir or os.environ.get("CODEX_GOAL_STATUS_STATE_DIR") or DEFAULT_STATE_DIR)
    ensure_state_dir(state_dir)
    config = load_config(args, state_dir)

    try:
        if unknown:
            append_error(state_dir, "Ignoring unknown arguments: " + " ".join(unknown))

        if args.watch:
            run_watcher(args, config, state_dir, [sys.argv[0], *argv], unknown)
            return 0

        if stdin_text is None:
            stdin_text = sys.stdin.read()
        try:
            payload = json.loads(stdin_text) if stdin_text.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        append_run_log(state_dir, [sys.argv[0], *argv], stdin_text, payload, unknown)
        run_hook_once(args, config, state_dir, stdin_text, payload)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        append_error(state_dir, "goal_status_report failed", exc)
        if raise_errors:
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
