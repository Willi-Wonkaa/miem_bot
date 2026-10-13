"""Shared SQLite telemetry; independent of Redis and available before other services start."""

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)
EVENT_TYPES = {
    "startup": "Запуск компонентов",
    "traffic": "Статистика сообщений",
    "indexer": "Ход индексации",
    "files": "Готовые файлы",
    "errors": "Ошибки",
}


def duration(seconds):
    if seconds is None:
        return "пока неизвестно"
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}ч {(seconds % 3600) // 60}м {seconds % 60}с"


class Telemetry:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "control.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS components (
                  name TEXT PRIMARY KEY, run_id TEXT, state TEXT, started REAL, updated REAL, detail TEXT);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, component TEXT, text TEXT,
                  payload TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS messages (
                  chat INTEGER, message INTEGER, run_id TEXT, received REAL, processed REAL,
                  success INTEGER, PRIMARY KEY(chat,message));
                CREATE INDEX IF NOT EXISTS messages_received ON messages(received);
                CREATE TABLE IF NOT EXISTS requests (
                  id TEXT PRIMARY KEY, received REAL, completed REAL, success INTEGER);
                CREATE INDEX IF NOT EXISTS requests_received ON requests(received);
                CREATE TABLE IF NOT EXISTS subscribers (
                  chat INTEGER PRIMARY KEY, prefs TEXT NOT NULL, cursor INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS watchers (chat INTEGER PRIMARY KEY);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def component(self, name, state="ready", run_id=None, detail=""):
        now = time.time()
        with self.connection() as db:
            old = db.execute("SELECT * FROM components WHERE name=?", (name,)).fetchone()
            run_id = run_id or (old["run_id"] if old else uuid.uuid4().hex)
            started = old["started"] if old and old["run_id"] == run_id else now
            changed = not old or old["run_id"] != run_id or old["state"] != state
            db.execute(
                "INSERT OR REPLACE INTO components VALUES (?,?,?,?,?,?)",
                (name, run_id, state, started, now, detail),
            )
        if changed:
            self.event(
                "errors"
                if state in {"failed", "interrupted", "completed_with_errors"}
                else "startup",
                name,
                f"{name}: {state}" + (f" — {detail}" if detail else ""),
            )
        return run_id

    def components(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM components ORDER BY name")]

    def event(self, kind, component, text, payload=None):
        with self.connection() as db:
            row = db.execute(
                "INSERT INTO events(kind,component,text,payload,created) VALUES (?,?,?,?,?)",
                (kind, component, text, json.dumps(payload or {}), time.time()),
            )
            return row.lastrowid

    def last_event(self):
        with self.connection() as db:
            return db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]

    def events(self, after, limit=100):
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit)
                )
            ]

    def received(self, chat, message, run_id):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,NULL,NULL)",
                (chat, message, run_id, time.time()),
            )

    def processed(self, chat, message, success=True):
        with self.connection() as db:
            db.execute(
                "UPDATE messages SET processed=?,success=? WHERE chat=? AND message=?",
                (time.time(), int(success), chat, message),
            )

    def message_stats(self, now=None):
        now = now or time.time()
        run_id = next((row["run_id"] for row in self.components() if row["name"] == "bot"), "")
        with self.connection() as db:
            windows = {
                str(minutes): db.execute(
                    "SELECT COUNT(*) FROM messages WHERE received>=?", (now - minutes * 60,)
                ).fetchone()[0]
                for minutes in [1, 5, 30]
            }
            total = db.execute(
                "SELECT COUNT(*), COUNT(processed), COALESCE(SUM(success=0),0) "
                "FROM messages WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return {"windows": windows, "received": total[0], "processed": total[1], "errors": total[2]}

    def subscribe(self, chat):
        prefs = {"all": True, **dict.fromkeys(EVENT_TYPES, True)}
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO subscribers VALUES (?,?,?)",
                (chat, json.dumps(prefs), self.last_event()),
            )

    def request_received(self):
        request_id = uuid.uuid4().hex
        with self.connection() as db:
            db.execute("INSERT INTO requests VALUES (?,?,NULL,NULL)", (request_id, time.time()))
        return request_id

    def request_processed(self, request_id, success=True):
        with self.connection() as db:
            db.execute(
                "UPDATE requests SET completed=?,success=? WHERE id=?",
                (time.time(), int(success), request_id),
            )

    def request_stats(self, now=None):
        now = now or time.time()
        with self.connection() as db:
            windows = {
                str(minutes): db.execute(
                    "SELECT COUNT(*) FROM requests WHERE received>=?", (now - minutes * 60,)
                ).fetchone()[0]
                for minutes in [1, 5, 30]
            }
            total = db.execute(
                "SELECT COUNT(*), COUNT(completed), COALESCE(SUM(success=0),0) FROM requests"
            ).fetchone()
        return {"windows": windows, "received": total[0], "processed": total[1], "errors": total[2]}

    def subscribers(self):
        with self.connection() as db:
            return [
                {**dict(row), "prefs": json.loads(row["prefs"])}
                for row in db.execute("SELECT * FROM subscribers")
            ]

    def toggle(self, chat, kind):
        if kind not in {"all", *EVENT_TYPES}:
            raise ValueError("Unknown notification type")
        self.subscribe(chat)
        with self.connection() as db:
            row = db.execute("SELECT prefs FROM subscribers WHERE chat=?", (chat,)).fetchone()
            prefs = json.loads(row[0])
            prefs[kind] = not prefs[kind]
            db.execute(
                "UPDATE subscribers SET prefs=?,cursor=? WHERE chat=?",
                (json.dumps(prefs), self.last_event(), chat),
            )
        return prefs

    def advance(self, chat, event_id):
        with self.connection() as db:
            db.execute("UPDATE subscribers SET cursor=? WHERE chat=?", (event_id, chat))

    def watch(self, chat, enabled=True):
        with self.connection() as db:
            if enabled:
                db.execute("INSERT OR IGNORE INTO watchers VALUES (?)", (chat,))
            else:
                db.execute("DELETE FROM watchers WHERE chat=?", (chat,))

    def watchers(self):
        with self.connection() as db:
            return [row[0] for row in db.execute("SELECT chat FROM watchers")]

    def host(self):
        try:
            path = Path(os.environ.get("HOST_STATUS_PATH", str(self.directory / "host.json")))
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
