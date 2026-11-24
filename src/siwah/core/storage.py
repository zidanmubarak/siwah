"""SQLite session store and JSONL trajectories."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.types import SUPERSEDED_TOOL_MESSAGE, Message, MessageAttachment, ToolCall, Usage
from ..harness.memory import redact_secrets
from ..paths import ensure_home, state_path, trajectories_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    cwd TEXT NOT NULL,
    model TEXT,
    mode TEXT NOT NULL DEFAULT 'build',
    status TEXT NOT NULL DEFAULT 'idle',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS run_timings (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    durations TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_timings_session ON run_timings(session_id, created_at);
CREATE TABLE IF NOT EXISTS run_observations (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    cwd TEXT NOT NULL,
    model TEXT NOT NULL,
    tag TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    tool_calls INTEGER NOT NULL,
    tool_errors INTEGER NOT NULL,
    correction INTEGER NOT NULL,
    aborted INTEGER NOT NULL,
    allowance REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS run_observations_by_tag ON run_observations(tag, cwd, model);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    reasoning TEXT,
    attachments TEXT NOT NULL DEFAULT '[]',
    tool_calls TEXT NOT NULL DEFAULT '[]',
    tool_call_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_session_seq ON messages(session_id, seq);
CREATE TABLE IF NOT EXISTS unapplied_corrections (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS unapplied_corrections_session ON unapplied_corrections(session_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    session_id UNINDEXED,
    message_id UNINDEXED,
    content
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT,
    name TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    status TEXT NOT NULL,
    duration_ms REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    allowance REAL NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0,
    list_cost REAL NOT NULL DEFAULT 0,
    billing_source TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_session ON usage(session_id);
CREATE TABLE IF NOT EXISTS trajectories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    commit_hash TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checkpoints_session ON checkpoints(session_id, created_at);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    latency_ms REAL NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    allowance REAL NOT NULL DEFAULT 0,
    tool_names TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_session ON traces(session_id, id);
"""


class Session(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str | None = None
    cwd: str
    model: str | None = None
    mode: str = "build"
    status: str = "idle"
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class UsageTotals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    allowance: float = 0.0
    cost: float = 0.0
    list_cost: float = 0.0
    steps: int = 0


class Store:
    def __init__(self, path: Path | None = None) -> None:
        ensure_home()
        self.path = path or state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(SCHEMA)
            columns = {
                str(row[1]) for row in self._connection.execute("PRAGMA table_info(messages)")
            }
            if "attachments" not in columns:
                self._connection.execute(
                    "ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
                )
            tool_columns = {
                str(row[1]) for row in self._connection.execute("PRAGMA table_info(tool_calls)")
            }
            if "duration_ms" not in tool_columns:
                self._connection.execute("ALTER TABLE tool_calls ADD COLUMN duration_ms REAL")
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def create_session(
        self,
        *,
        cwd: str,
        model: str | None,
        mode: str = "build",
        title: str | None = None,
    ) -> Session:
        now = _now()
        session = Session(
            id=f"ses_{uuid.uuid4().hex}",
            title=title,
            cwd=cwd,
            model=model,
            mode=mode,
            status="idle",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._connection.execute(
                "INSERT INTO sessions (id, title, cwd, model, mode, status, created_at, updated_at, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    redact_secrets(session.title) if session.title else session.title,
                    session.cwd,
                    session.model,
                    session.mode,
                    session.status,
                    session.created_at,
                    session.updated_at,
                    "{}",
                ),
            )
            self._connection.commit()
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[Session]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    def session_ids(self) -> set[str]:
        """Return every persisted session id, without a display limit."""

        with self._lock:
            rows = self._connection.execute("SELECT id FROM sessions").fetchall()
        return {str(row[0]) for row in rows}

    def update_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        with self._lock:
            if status is not None:
                self._connection.execute(
                    "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, session_id),
                )
            if model is not None:
                self._connection.execute(
                    "UPDATE sessions SET model = ?, updated_at = ? WHERE id = ?",
                    (model, now, session_id),
                )
            if mode is not None:
                self._connection.execute(
                    "UPDATE sessions SET mode = ?, updated_at = ? WHERE id = ?",
                    (mode, now, session_id),
                )
            if title is not None:
                self._connection.execute(
                    "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                    (redact_secrets(title), now, session_id),
                )
            if metadata is not None:
                self._connection.execute(
                    "UPDATE sessions SET metadata = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(_redacted_copy(metadata), ensure_ascii=False), now, session_id),
                )
            self._connection.commit()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def append_message(
        self, session_id: str, message: Message, *, message_id: str | None = None
    ) -> str:
        message_id = message_id or f"msg_{uuid.uuid4().hex}"
        # Never mutate provider/tool objects: live arguments must still execute
        # verbatim, while every durable representation is a redacted copy.
        message = Message.model_validate(_redacted_copy(message.model_dump(mode="json")))
        now = _now()
        with self._lock:
            seq = self._next_message_seq(session_id)
            self._connection.execute(
                "INSERT INTO messages (id, session_id, seq, role, content, reasoning, attachments,"
                " tool_calls, tool_call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    session_id,
                    seq,
                    message.role,
                    message.content,
                    message.reasoning,
                    json.dumps(
                        [item.model_dump() for item in message.attachments], ensure_ascii=False
                    ),
                    json.dumps(
                        [call.model_dump() for call in message.tool_calls], ensure_ascii=False
                    ),
                    message.tool_call_id,
                    now,
                ),
            )
            if message.content:
                self._connection.execute(
                    "INSERT INTO messages_fts (session_id, message_id, content) VALUES (?, ?, ?)",
                    (session_id, message_id, message.content),
                )
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
            self._connection.commit()
        return message_id

    def _next_message_seq(self, session_id: str) -> int:
        return self._connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM ("
            "SELECT MAX(seq) AS seq FROM messages WHERE session_id = ? UNION ALL "
            "SELECT MAX(seq) AS seq FROM unapplied_corrections WHERE session_id = ?)",
            (session_id, session_id),
        ).fetchone()[0]

    def record_unapplied_correction(self, session_id: str, message_id: str, content: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO unapplied_corrections (id, session_id, seq, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    message_id,
                    session_id,
                    self._next_message_seq(session_id),
                    redact_secrets(content),
                    _now(),
                ),
            )

    def delete_unapplied_correction(self, session_id: str, message_id: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM unapplied_corrections WHERE session_id = ? AND id = ?",
                (session_id, message_id),
            )
            return cursor.rowcount > 0

    def get_messages(self, session_id: str) -> list[Message]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
        messages: list[Message] = []
        for row in rows:
            tool_calls = json.loads(row["tool_calls"] or "[]")
            attachments = json.loads(row["attachments"] or "[]")
            messages.append(
                Message(
                    role=row["role"],
                    content=row["content"],
                    reasoning=row["reasoning"],
                    attachments=[MessageAttachment.model_validate(item) for item in attachments],
                    tool_calls=[ToolCall.model_validate(call) for call in tool_calls],
                    tool_call_id=row["tool_call_id"],
                )
            )
        return messages

    def max_message_seq(self, session_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def get_context_messages(self, session_id: str) -> list[Message]:
        session = self.get_session(session_id)
        summary = (session.metadata or {}).get("context_summary") if session else None
        through = (session.metadata or {}).get("context_compacted_through") if session else None
        if not summary:
            return self.get_messages(session_id)
        cutoff = int(through) if isinstance(through, (int, float)) else 0
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE session_id = ? AND seq > ? ORDER BY seq",
                (session_id, cutoff),
            ).fetchall()
        recent: list[Message] = []
        for row in rows:
            tool_calls = json.loads(row["tool_calls"] or "[]")
            attachments = json.loads(row["attachments"] or "[]")
            recent.append(
                Message(
                    role=row["role"],
                    content=row["content"],
                    reasoning=row["reasoning"],
                    attachments=[MessageAttachment.model_validate(item) for item in attachments],
                    tool_calls=[ToolCall.model_validate(call) for call in tool_calls],
                    tool_call_id=row["tool_call_id"],
                )
            )
        return [
            Message(role="system", content="[compacted context]\n" + str(summary)),
            *recent,
        ]

    def set_context_summary(self, session_id: str, summary: str, through_seq: int) -> None:
        session = self.get_session(session_id)
        if session is None:
            return
        metadata = dict(session.metadata)
        metadata["context_summary"] = summary
        metadata["context_compacted_through"] = through_seq
        self.update_session(session_id, metadata=metadata)

    def clear_context_summary(self, session_id: str) -> None:
        session = self.get_session(session_id)
        if session is None:
            return
        metadata = dict(session.metadata)
        metadata.pop("context_summary", None)
        metadata.pop("context_compacted_through", None)
        self.update_session(session_id, metadata=metadata)

    def get_message_views(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
            tool_rows = self._connection.execute(
                "SELECT id, message_id, name, arguments, result, status, duration_ms FROM tool_calls"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            legacy_durations = {}
            if any(row["duration_ms"] is None for row in tool_rows):
                legacy_durations = {
                    row["tool_call_id"]: row["duration_ms"]
                    for row in self._connection.execute(
                        "SELECT json_extract(payload, '$.tool_call_id') AS tool_call_id,"
                        " json_extract(payload, '$.duration_ms') AS duration_ms"
                        " FROM trajectories WHERE session_id = ?"
                        " AND json_extract(payload, '$.type') = 'tool'"
                        " AND json_type(payload, '$.duration_ms') IN ('integer', 'real')"
                        " ORDER BY seq",
                        (session_id,),
                    )
                }
            corrections = self._connection.execute(
                "SELECT id, seq, content, created_at FROM unapplied_corrections WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        views: list[dict[str, Any]] = []
        call_to_message: dict[str, str] = {}
        for row in rows:
            item = dict(row)
            item["tool_calls"] = json.loads(item.get("tool_calls") or "[]")
            item["attachments"] = json.loads(item.get("attachments") or "[]")
            for call in item["tool_calls"]:
                if isinstance(call, dict) and isinstance(call.get("id"), str):
                    call_to_message[call["id"]] = item["id"]
            views.append(item)
        diffs: dict[str, list[dict[str, str]]] = {}
        outputs: dict[str, dict[str, str]] = {}
        edited: dict[str, dict[str, bool]] = {}
        outcomes: dict[str, dict[str, Any]] = {}
        for row in tool_rows:
            message_id = row["message_id"] or call_to_message.get(row["id"])
            try:
                arguments = json.loads(row["arguments"] or "{}")
                result = json.loads(row["result"] or "{}")
            except json.JSONDecodeError:
                continue
            data = result.get("data") if isinstance(result, dict) else None
            if not message_id:
                continue
            error = result.get("error") if isinstance(result, dict) else None
            status = row["status"]
            if (
                status == "error"
                and isinstance(error, str)
                and error.startswith(SUPERSEDED_TOOL_MESSAGE)
            ):
                status = "skipped"
            if status in {"ok", "error", "cancelled", "skipped"}:
                outcomes.setdefault(message_id, {})[row["id"]] = {
                    "status": status,
                    "error": error,
                    "duration_ms": (
                        row["duration_ms"]
                        if row["duration_ms"] is not None
                        else legacy_durations.get(row["id"])
                    ),
                }
            full_output = data.get("full_output") if isinstance(data, dict) else None
            if isinstance(full_output, str):
                # Older versions appended the verifier summary to full_output.
                # Strip that suffix when replaying write_file output.
                if row["name"] == "write_file":
                    marker = full_output.find("\n\nVerifier:")
                    if marker >= 0:
                        full_output = full_output[:marker]
                outputs.setdefault(message_id, {})[row["id"]] = full_output
            if isinstance(data, dict) and isinstance(data.get("edited"), bool):
                edited.setdefault(message_id, {})[row["id"]] = data["edited"]
            patch = data.get("diff") if isinstance(data, dict) else None
            if not isinstance(patch, str) or not patch:
                continue
            path = arguments.get("path") if isinstance(arguments, dict) else None
            diffs.setdefault(message_id, []).append({"path": str(path or ""), "patch": patch})
        for item in views:
            item["diffs"] = diffs.get(item["id"], [])
            item["tool_outputs"] = outputs.get(item["id"], {})
            item["tool_edited"] = edited.get(item["id"], {})
            item["tool_outcomes"] = outcomes.get(item["id"], {})
        if corrections:
            views.extend(
                {**dict(row), "role": "user", "delivery": "unapplied"} for row in corrections
            )
            views.sort(key=lambda item: item["seq"])
        return views

    def last_usage_tokens(self, session_id: str) -> tuple[int, int]:
        with self._lock:
            row = self._connection.execute(
                "SELECT input_tokens, cache_read_tokens FROM usage WHERE session_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["input_tokens"]), int(row["cache_read_tokens"]))

    def record_run_timing(self, session_id: str, run_id: str, durations: dict[str, float]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO run_timings VALUES (?, ?, ?, ?)",
                (run_id, session_id, json.dumps(durations), _now()),
            )
            self._connection.commit()

    def latest_run_timing(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT run_id, durations FROM run_timings WHERE session_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return {"run_id": row["run_id"], "durations": json.loads(row["durations"])} if row else None

    def record_run_observation(
        self,
        *,
        run_id: str,
        session: Session,
        model: str,
        tag: str,
        tools: int,
        errors: int,
        corrected: bool,
        aborted: bool,
        allowance: float,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO run_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    session.id,
                    session.cwd,
                    model,
                    tag,
                    _now(),
                    tools,
                    errors,
                    int(corrected),
                    int(aborted),
                    allowance,
                ),
            )
            self._connection.commit()

    def compare_run_observations(
        self, *, before: str, after: str, min_sessions: int
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT cwd, model, tag, COUNT(*) AS turns, COUNT(DISTINCT session_id) AS sessions,"
                " COALESCE(1.0 * SUM(tool_errors) / NULLIF(SUM(tool_calls), 0), 0) AS tool_error_rate,"
                " AVG(correction) AS correction_hint_rate, AVG(aborted) AS abort_rate,"
                " AVG(allowance) AS allowance_per_turn"
                " FROM run_observations WHERE tag IN (?, ?) GROUP BY cwd, model, tag",
                (before, after),
            ).fetchall()
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            metrics = dict(row)
            cwd, model, tag = (str(metrics.pop(key)) for key in ("cwd", "model", "tag"))
            groups.setdefault((cwd, model), {})[tag] = metrics
        return [
            {
                "cwd": cwd,
                "model": model,
                "before": sides.get(before),
                "after": sides[after],
                "ready": all(
                    sides.get(tag, {}).get("sessions", 0) >= min_sessions for tag in (before, after)
                ),
            }
            for (cwd, model), sides in groups.items()
            if after in sides
        ]

    def recall_messages(
        self,
        query: str,
        limit: int = 10,
        *,
        exclude_session_id: str | None = None,
        compacted_through: int = 0,
        cwd: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT f.session_id, f.message_id, f.content, s.title, s.cwd, s.updated_at"
            " FROM messages_fts f JOIN sessions s ON s.id = f.session_id"
            " JOIN messages m ON m.id = f.message_id AND m.session_id = f.session_id"
            " WHERE messages_fts MATCH ?"
        )
        params: list[Any] = [query]
        if exclude_session_id:
            sql += " AND (f.session_id != ? OR m.seq <= ?)"
            params.extend([exclude_session_id, compacted_through])
        if cwd is not None:
            sql += " AND s.cwd = ?"
            params.append(cwd)
        sql += " ORDER BY bm25(messages_fts), s.updated_at DESC, m.seq DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def recall_window(
        self, message_id: str, *, cwd: str, radius: int, current_id: str, through: int | None
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT m.id AS message_id, m.session_id, m.role, m.content"
                " FROM messages anchor JOIN sessions s ON s.id = anchor.session_id"
                " JOIN messages m ON m.session_id = anchor.session_id"
                " WHERE anchor.id = ? AND s.cwd = ?"
                " AND (? IS NULL OR s.id != ? OR (anchor.seq <= ? AND m.seq <= ?))"
                " AND m.seq BETWEEN anchor.seq - ? AND anchor.seq + ?"
                " AND m.role IN ('user', 'assistant') ORDER BY m.seq",
                (message_id, cwd, through, current_id, through, through, radius, radius),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_messages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT session_id, message_id, content FROM messages_fts WHERE messages_fts MATCH ?"
                " LIMIT ?",
                (query, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def fork_session(self, session_id: str, *, title: str | None = None) -> Session:
        source = self.get_session(session_id)
        if source is None:
            raise ValueError(f"Session not found: {session_id}")
        forked = self.create_session(
            cwd=source.cwd,
            model=source.model,
            mode=source.mode,
            title=title or (f"{source.title} (fork)" if source.title else "Fork"),
        )
        with self._lock:
            source_metadata = dict(source.metadata)
            if source_metadata:
                self.update_session(forked.id, metadata=source_metadata)
        for message in self.get_messages(session_id):
            self.append_message(forked.id, message)
        return self.get_session(forked.id) or forked

    def delete_session(self, session_id: str) -> bool:
        """Remove a session and its rows (used to undo a failed fork)."""

        with self._lock:
            cursor = self._connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            for table in (
                "messages",
                "tool_calls",
                "usage",
                "trajectories",
                "checkpoints",
                "traces",
            ):
                self._connection.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
            self._connection.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
            self._connection.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Tool calls and usage
    # ------------------------------------------------------------------
    def record_tool_call(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        message_id: str | None,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        status: str,
        duration_ms: float | None = None,
    ) -> None:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO tool_calls"
                " (id, session_id, message_id, name, arguments, result, status, duration_ms, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " name = excluded.name, arguments = excluded.arguments,"
                " result = excluded.result, status = excluded.status, duration_ms = excluded.duration_ms"
                " WHERE tool_calls.session_id = excluded.session_id"
                " AND tool_calls.message_id IS excluded.message_id",
                (
                    tool_call_id,
                    session_id,
                    message_id,
                    name,
                    json.dumps(_redacted_copy(arguments), ensure_ascii=False),
                    json.dumps(_redacted_copy(result), ensure_ascii=False),
                    status,
                    duration_ms,
                    _now(),
                ),
            )
            if cursor.rowcount == 0:
                self._connection.rollback()
                raise ValueError("Tool call id already belongs to another message or session")
            self._connection.commit()

    def get_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["arguments"] = json.loads(item["arguments"] or "{}")
            item["result"] = json.loads(item["result"] or "{}")
            result.append(item)
        return result

    def get_tool_call(self, tool_call_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tool_calls WHERE id = ?", (tool_call_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["arguments"] = json.loads(item["arguments"] or "{}")
        item["result"] = json.loads(item["result"] or "{}")
        return item

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------
    def record_checkpoint(
        self, *, session_id: str, commit_hash: str, label: str | None = None
    ) -> str:
        checkpoint_id = f"cp_{uuid.uuid4().hex}"
        with self._lock:
            self._connection.execute(
                "INSERT INTO checkpoints (id, session_id, commit_hash, label, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (checkpoint_id, session_id, commit_hash, label, _now()),
            )
            self._connection.commit()
        return checkpoint_id

    def list_checkpoints(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY created_at, id LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_usage(
        self,
        *,
        session_id: str,
        step_id: str,
        provider: str | None,
        model: str | None,
        usage: Usage,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO usage (session_id, step_id, provider, model, input_tokens, output_tokens,"
                " reasoning_tokens, cache_read_tokens, cache_write_tokens, allowance, cost, list_cost,"
                " billing_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    step_id,
                    provider,
                    model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.reasoning_tokens,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    usage.allowance,
                    usage.cost,
                    usage.list_cost,
                    usage.billing_source,
                    _now(),
                ),
            )
            self._connection.commit()

    def usage_totals(self, session_id: str) -> UsageTotals:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                " COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,"
                " COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,"
                " COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,"
                " COALESCE(SUM(allowance), 0) AS allowance,"
                " COALESCE(SUM(cost), 0) AS cost,"
                " COALESCE(SUM(list_cost), 0) AS list_cost,"
                " COUNT(*) AS steps FROM usage WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return UsageTotals.model_validate(dict(row))

    # ------------------------------------------------------------------
    # Traces
    # ------------------------------------------------------------------
    def record_trace(
        self,
        *,
        session_id: str,
        step_id: str,
        provider: str | None,
        model: str | None,
        latency_ms: float,
        usage: Usage,
        tool_names: list[str],
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO traces (session_id, step_id, provider, model, latency_ms,"
                " input_tokens, output_tokens, cache_read_tokens, allowance, tool_names,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    step_id,
                    provider,
                    model,
                    latency_ms,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read_tokens,
                    usage.allowance,
                    json.dumps(tool_names, ensure_ascii=False),
                    _now(),
                ),
            )
            self._connection.commit()

    def list_traces(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM traces WHERE session_id = ? ORDER BY id LIMIT ?",
                (session_id, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["tool_names"] = json.loads(item.get("tool_names") or "[]")
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Trajectories
    # ------------------------------------------------------------------
    def append_trajectory(self, session_id: str, payload: dict[str, Any]) -> None:
        payload = _redacted_copy(payload)
        now = _now()
        with self._lock:
            seq = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM trajectories WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            self._connection.execute(
                "INSERT INTO trajectories (session_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
                (session_id, seq, json.dumps(payload, ensure_ascii=False), now),
            )
            self._connection.commit()
        path = trajectories_dir() / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"seq": seq, "ts": now, **payload}, ensure_ascii=False) + "\n")


def _session_from_row(row: sqlite3.Row) -> Session:
    raw = dict(row)
    raw["metadata"] = json.loads(raw.get("metadata") or "{}")
    return Session.model_validate(raw)


def _redacted_copy(value: Any) -> Any:
    """Recursively sanitize text and keyed secret values without aliasing inputs."""

    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str):
                # The shared detector recognizes assignments as well as token
                # shapes. Preserve valid JSON structure by redacting the value,
                # not by replacing fragments in a serialized JSON document.
                assignment = f"{key}={item}"
                if redact_secrets(assignment) != assignment:
                    safe[key] = redact_secrets(item)
                    if safe[key] == item:
                        safe[key] = "[REDACTED]"
                    continue
            safe[key] = _redacted_copy(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_redacted_copy(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()
