"""Durable archive of observed states, not the panel's internal event memory."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path


class SatelHistory:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS events ("
                             "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                             "fingerprint TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS metadata ("
                             "key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def append(self, event):
        event = {k: v for k, v in event.items() if k != "id"}
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO events(fingerprint,payload) VALUES (?,?)",
                             (fingerprint, payload))
            row = self._db.execute("SELECT id FROM events WHERE fingerprint=?",
                                   (fingerprint,)).fetchone()
        return dict(event, id=row[0])

    def page(self, limit=100, before=None):
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._db.execute(
                "SELECT id,payload FROM events WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before if before is not None else 9223372036854775807, limit + 1),
            ).fetchall()
        events = [dict(json.loads(payload), id=event_id) for event_id, payload in rows[:limit]]
        return {"events": events,
                "next_before": events[-1]["id"] if len(rows) > limit else None}

    def get(self, key):
        with self._lock:
            row = self._db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", (key, value))

    def close(self):
        with self._lock:
            self._db.close()
