from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "state.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS problems(
          id TEXT PRIMARY KEY, metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
          started REAL, deadline REAL, session TEXT, errors INTEGER NOT NULL DEFAULT 0,
          phase TEXT NOT NULL DEFAULT 'research', last_candidate TEXT, detail TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS attempts(
          id INTEGER PRIMARY KEY, problem TEXT NOT NULL REFERENCES problems(id),
          started REAL NOT NULL, finished REAL, status TEXT NOT NULL, directory TEXT NOT NULL,
          session TEXT, usage TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS nodes(
          problem TEXT NOT NULL REFERENCES problems(id), id TEXT NOT NULL,
          fingerprint TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '', PRIMARY KEY(problem,id));
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, problem TEXT NOT NULL REFERENCES problems(id), node TEXT NOT NULL,
          fingerprint TEXT NOT NULL, status TEXT NOT NULL, remote_id TEXT,
          created REAL NOT NULL, polled REAL NOT NULL DEFAULT 0, result TEXT,
          UNIQUE(problem,node,fingerprint,id));
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, time REAL NOT NULL, problem TEXT, kind TEXT NOT NULL, data TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def execute(self, sql, params=()):
        with self.db:
            return self.db.execute(sql, params)

    def rows(self, sql, params=()):
        return [dict(r) for r in self.db.execute(sql, params)]

    def problem(self, problem: str):
        rows = self.rows("SELECT * FROM problems WHERE id=?", (problem,))
        if not rows:
            raise ValueError(f"Unknown problem: {problem}")
        return rows[0]

    def update(self, problem: str, **fields):
        allowed = {"status", "started", "deadline", "session", "errors", "phase", "last_candidate", "detail"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid problem update")
        self.execute("UPDATE problems SET " + ",".join(k + "=?" for k in fields) + " WHERE id=?",
                     (*fields.values(), problem))

    def start(self, problem: str, hours: float, now: float | None = None):
        now = time.time() if now is None else now
        self.execute("UPDATE problems SET started=COALESCE(started,?), deadline=COALESCE(deadline,?), "
                     "status='running' WHERE id=?", (now, now + hours * 3600, problem))
        return self.problem(problem)

    def event(self, problem, kind: str, data):
        self.execute("INSERT INTO events(time,problem,kind,data) VALUES(?,?,?,?)",
                     (time.time(), problem, kind, json.dumps(data, ensure_ascii=False)))

    def recover(self):
        self.execute("UPDATE attempts SET status='interrupted', finished=? WHERE status='running'", (time.time(),))
        # The remote server may have received a request whose response was lost.
        # Never retry these implicitly: the user can attach the real remote ID.
        self.execute("UPDATE jobs SET status='unknown' WHERE status='submitting'")
