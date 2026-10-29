"""SQLite aggregate storage. Every journey transition and its outbox are atomic."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS journeys (id TEXT PRIMARY KEY, event_id TEXT UNIQUE, payload TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def put(db, journey):
        db.execute("INSERT INTO journeys VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                   (journey["id"], journey["event_id"], json.dumps(journey)))

    @staticmethod
    def find_event(db, event_id):
        row = db.execute("SELECT id FROM journeys WHERE event_id=?", (event_id,)).fetchone()
        return row["id"] if row else None

    @staticmethod
    def get(db, journey_id):
        row = db.execute("SELECT payload FROM journeys WHERE id=?", (journey_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def all(self):
        with self.connection() as db:
            return [json.loads(row["payload"]) for row in db.execute("SELECT payload FROM journeys ORDER BY rowid DESC")]
