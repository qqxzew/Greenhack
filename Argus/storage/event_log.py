# storage/event_log.py

import sqlite3
import time
import threading
from pathlib import Path

from core.toon import encode_event, decode_event, encode_stream, decode_stream


DB_PATH = Path("argus_events.db")


class EventLogger:
    """
    Persists every LLM call event to SQLite as a single TOON record line.
    Events are encoded to TOON on write and decoded on read.

    The in-memory buffer keeps the original (decoded) event dicts for fast
    access by recent() and aggregate(); the DB stores the compact TOON line.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()
        self._buffer: list[dict] = []

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    agent_id  TEXT,
                    toon_line TEXT
                )
            """)
            conn.commit()

    def log(self, event: dict):
        event.setdefault("ts", time.time())
        toon_line = encode_event(event)

        with self._lock:
            # In-memory buffer stores decoded dicts for fast access
            self._buffer.append(event)
            if len(self._buffer) > 1000:
                self._buffer = self._buffer[-1000:]

            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO events (timestamp, agent_id, toon_line) VALUES (?,?,?)",
                    (event["ts"], event.get("agent_id", ""), toon_line),
                )
                conn.commit()

    def recent(self, n: int = 100) -> list[dict]:
        """Returns decoded event dicts from the in-memory buffer."""
        with self._lock:
            return list(self._buffer[-n:])

    def recent_from_db(self, n: int = 100) -> list[dict]:
        """Reads and decodes TOON lines directly from SQLite."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT toon_line FROM events ORDER BY id DESC LIMIT ?", (n,)
                ).fetchall()
        return [decode_event(r[0]) for r in reversed(rows) if r[0]]

    def export_toon(self, path: str):
        """Write all events as a .toon file."""
        events = self.recent_from_db(n=10_000)
        with open(path, "w") as f:
            f.write(encode_stream(events))

    def import_toon(self, path: str):
        """Load events from a .toon file into the in-memory buffer."""
        with open(path, "r") as f:
            content = f.read()
        with self._lock:
            self._buffer = decode_stream(content)

    def aggregate(self) -> dict:
        with self._lock:
            buf = list(self._buffer)
        if not buf:
            return {}
        costs    = [e["cost"]         for e in buf if "cost"         in e]
        tokens   = [e["tokens_total"] for e in buf if "tokens_total" in e]
        quality  = [e["quality"]      for e in buf if "quality"      in e]
        from collections import Counter
        models = Counter(e.get("model") for e in buf)
        return {
            "total_events":  len(buf),
            "total_cost":    round(sum(costs), 4),
            "total_tokens":  sum(tokens),
            "avg_quality":   round(sum(quality) / len(quality), 4) if quality else 0,
            "avg_cost":      round(sum(costs) / len(costs), 6) if costs else 0,
            "model_dist":    dict(models),
            "anomaly_count": sum(1 for e in buf if e.get("is_anomaly")),
            "stuck_count":   sum(1 for e in buf if e.get("sprt") == "STOP_STUCK"),
        }
