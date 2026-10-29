"""Best-effort, local observations of pipeline work.

The pipeline state remains authoritative. This database must never be used to
decide whether a remote request was accepted or a PDF can be published.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_METRICS = {"bytes", "pages", "images", "requests", "completed", "failed", "pdfs"}
_HASH_METRICS = {"renderer_signature"}


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Telemetry:
    def __init__(self, exam_root: Path, clock_ns=None):
        self.root = Path(exam_root)
        self.path = self.root / "state" / "telemetry.sqlite3"
        self.clock_ns = clock_ns or time.perf_counter_ns

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        db.execute("PRAGMA busy_timeout=2000")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            recorded_at_utc TEXT NOT NULL,
            project_id TEXT NOT NULL,
            span_id TEXT,
            stage TEXT NOT NULL,
            event_name TEXT NOT NULL,
            locality TEXT NOT NULL,
            status TEXT,
            request_hash TEXT,
            duration_ms REAL,
            metrics_json TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS events_stage_time ON events(stage, recorded_at_utc)")
        db.execute("CREATE INDEX IF NOT EXISTS events_span ON events(span_id)")
        return db

    def record(self, event_name: str, stage: str, *, span_id=None, locality="local",
               status=None, request_hash=None, duration_ms=None, metrics=None):
        """Store only bounded technical fields; silently degrade on disk errors."""
        if not _NAME.fullmatch(event_name) or not _NAME.fullmatch(stage):
            return False
        if locality not in {"local", "remote_observed", "user"}:
            return False
        if status is not None and (not isinstance(status, str) or not _NAME.fullmatch(status)):
            return False
        if request_hash is not None and not _HASH.fullmatch(request_hash):
            request_hash = None
        if duration_ms is not None and (not isinstance(duration_ms, (int, float)) or not math.isfinite(duration_ms) or duration_ms < 0):
            return False
        safe_metrics = {key: value for key, value in (metrics or {}).items()
                        if key in _METRICS and type(value) in (int, float) and math.isfinite(value) and value >= 0}
        safe_metrics.update({key: value for key, value in (metrics or {}).items()
                             if key in _HASH_METRICS and isinstance(value, str) and _HASH.fullmatch(value)})
        try:
            db = self._connect()
            try:
                db.execute("""INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (uuid.uuid4().hex, SCHEMA_VERSION, _utc_now(), self.root.name,
                            span_id, stage, event_name, locality, status, request_hash,
                            duration_ms, json.dumps(safe_metrics, separators=(",", ":"))))
                db.commit()
            finally:
                db.close()
            return True
        except (OSError, sqlite3.Error):
            return False

    @contextmanager
    def span(self, stage: str, *, request_hash=None, metrics=None):
        span_id = uuid.uuid4().hex
        started = self.clock_ns()
        self.record(stage + "_started", stage, span_id=span_id,
                    request_hash=request_hash, metrics=metrics)
        try:
            yield span_id
        except BaseException:
            elapsed = max(0, (self.clock_ns() - started) / 1_000_000)
            self.record(stage + "_completed", stage, span_id=span_id,
                        status="failure", request_hash=request_hash,
                        duration_ms=elapsed, metrics=metrics)
            raise
        else:
            elapsed = max(0, (self.clock_ns() - started) / 1_000_000)
            self.record(stage + "_completed", stage, span_id=span_id,
                        status="success", request_hash=request_hash,
                        duration_ms=elapsed, metrics=metrics)

    def recent(self, limit=200):
        """Read observations only; missing or damaged telemetry does not hide jobs."""
        if not self.path.is_file():
            return []
        try:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2)
            try:
                db.row_factory = sqlite3.Row
                rows = db.execute("SELECT * FROM events ORDER BY recorded_at_utc DESC, rowid DESC LIMIT ?",
                                  (max(1, min(int(limit), 1000)),)).fetchall()
            finally:
                db.close()
            return [dict(row) for row in rows]
        except (OSError, sqlite3.Error):
            return []
