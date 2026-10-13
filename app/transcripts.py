"""Permanent conversation archive, separate from the model's expiring Redis context."""

import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def timestamp(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value) if value is not None else time.time()


class Transcripts:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "conversations.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                  id INTEGER PRIMARY KEY, name TEXT, username TEXT, search TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                  role TEXT NOT NULL, text TEXT NOT NULL, occurred REAL NOT NULL,
                  recorded REAL NOT NULL, transport_key TEXT UNIQUE, question_id INTEGER,
                  answer_id INTEGER, attachment TEXT, deleted REAL);
                CREATE INDEX IF NOT EXISTS messages_user_time ON messages(user_id, occurred, id);
                CREATE TABLE IF NOT EXISTS answers (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                  question_id INTEGER NOT NULL, text TEXT NOT NULL, answered REAL NOT NULL,
                  model TEXT, route TEXT, tokens INTEGER, duration REAL);
                CREATE INDEX IF NOT EXISTS answers_user_time ON answers(user_id, answered);
                CREATE TABLE IF NOT EXISTS feedback (
                  answer_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, helpful INTEGER NOT NULL DEFAULT 0,
                  comment TEXT, comment_message_id INTEGER, created REAL, updated REAL,
                  rated REAL, commented REAL, expires REAL, buttons_key TEXT, buttons_active INTEGER DEFAULT 0,
                  needs_prompt INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS feedback_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                  answer_id INTEGER NOT NULL, helpful INTEGER, comment TEXT,
                  comment_message_id INTEGER, request_key TEXT UNIQUE, created REAL);
                CREATE TABLE IF NOT EXISTS feedback_prompts (
                  transport_key TEXT PRIMARY KEY, user_id INTEGER, answer_id INTEGER, completed INTEGER DEFAULT 0);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def user(self, user_id, name="", username=None):
        with self.connection() as db:
            db.execute(
                "INSERT INTO users VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "name=CASE WHEN excluded.name!='' THEN excluded.name ELSE users.name END, "
                "username=COALESCE(excluded.username,users.username), "
                "search=CASE WHEN excluded.name!='' THEN excluded.search ELSE users.search END, "
                "updated=excluded.updated",
                (user_id, name, username, f"{name} {username or ''}".casefold(), time.time()),
            )

    def message(self, user_id, role, text, occurred, transport_key, question_id=None,
                answer_id=None, attachment=None):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO messages "
                "(user_id,role,text,occurred,recorded,transport_key,question_id,answer_id,attachment) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, role, text, timestamp(occurred), time.time(), transport_key,
                 question_id, answer_id, json.dumps(attachment, ensure_ascii=False) if attachment else None),
            )
            return db.execute("SELECT id FROM messages WHERE transport_key=?", (transport_key,)).fetchone()[0]

    def answer(self, user_id, question_id, result):
        with self.connection() as db:
            cursor = db.execute(
                "INSERT INTO answers (user_id,question_id,text,answered,model,route,tokens,duration) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user_id, question_id, result["answer"], timestamp(result.get("answered_at")),
                 result.get("model"), result.get("route"),
                 result.get("tokens", 0), result.get("time_sec", 0)),
            )
            answer_id = cursor.lastrowid
            db.execute("INSERT INTO feedback (answer_id,user_id,helpful,created) VALUES (?,?,0,?)",
                       (answer_id,user_id,time.time()))
            return answer_id

    def deleted(self, transport_key):
        with self.connection() as db:
            db.execute("UPDATE messages SET deleted=? WHERE transport_key=?", (time.time(), transport_key))

    def users(self, search="", offset=0, limit=10):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT users.*, (SELECT COUNT(*) FROM messages WHERE user_id=users.id) AS messages "
                "FROM users WHERE search LIKE ? OR CAST(id AS TEXT)=? "
                "ORDER BY updated DESC,id LIMIT ? OFFSET ?",
                (f"%{search.lstrip('@').casefold()}%", search, limit, offset),
            )]

    def profile(self, user_id):
        with self.connection() as db:
            row = db.execute("SELECT id,name,username FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else {"id": user_id, "name": str(user_id), "username": None}

    def full_answer(self, answer_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM answers WHERE id=?", (answer_id,)).fetchone()
        if row is None:
            raise ValueError("Ответ не найден")
        return dict(row)

    def messages(self, user_id, offset=0, limit=3, after=0, before=253402300799):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT m.*, a.answered AS model_answered, a.model, a.route, "
                "f.helpful,f.comment AS feedback_comment,f.rated AS rated_at "
                "FROM messages m LEFT JOIN answers a ON a.id=m.answer_id "
                "LEFT JOIN feedback f ON f.answer_id=m.answer_id "
                "WHERE m.user_id=? AND m.occurred>=? AND m.occurred<? "
                "ORDER BY m.occurred DESC,m.id DESC LIMIT ? OFFSET ?",
                (user_id, after, before, limit, offset),
            )]

    def rated_answer(self, answer_id, user_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM answers WHERE id=? AND user_id=?", (answer_id,user_id)).fetchone()
        if row is None:
            raise ValueError("Ответ принадлежит другому пользователю или не найден")
        return dict(row)

    def rate(self, answer_id, user_id, score, request_key, buttons_key):
        if score not in (-1,1):
            raise ValueError("Invalid feedback score")
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM feedback WHERE answer_id=? AND user_id=?",
                             (answer_id,user_id)).fetchone()
            if (row is None or row["helpful"] != 0 or not row["buttons_active"]
                    or row["buttons_key"] != buttons_key or row["expires"] is None or row["expires"] <= now):
                raise ValueError("Оценка недоступна или уже сохранена")
            db.execute("INSERT INTO feedback_events (user_id,answer_id,helpful,request_key,created) "
                       "VALUES (?,?,?,?,?)", (user_id,answer_id,score,request_key,now))
            db.execute("UPDATE feedback SET helpful=?,rated=?,updated=?,needs_prompt=? WHERE answer_id=?",
                       (score,now,now,int(score == -1),answer_id))

    def sent_feedback(self, answer_id, buttons_key, occurred):
        with self.connection() as db:
            db.execute("UPDATE feedback SET buttons_key=?,expires=?,buttons_active=1 WHERE answer_id=? "
                       "AND buttons_key IS NULL", (buttons_key,timestamp(occurred)+600,answer_id))

    def cleanup_buttons(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM feedback WHERE buttons_active=1 AND (helpful!=0 OR expires<=?)",
                (time.time(),))]

    def close_buttons(self, answer_id):
        with self.connection() as db:
            db.execute("UPDATE feedback SET buttons_active=0 WHERE answer_id=?", (answer_id,))

    def prompt_queue(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT f.*,a.question_id,q.transport_key AS question_key FROM feedback f "
                "JOIN answers a ON a.id=f.answer_id JOIN messages q ON q.id=a.question_id "
                "WHERE f.helpful=-1 AND f.needs_prompt=1")]

    def prompt(self, transport_key, user_id, answer_id):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO feedback_prompts (transport_key,user_id,answer_id) VALUES (?,?,?)",
                       (transport_key,user_id,answer_id))
            db.execute("UPDATE feedback SET needs_prompt=0 WHERE answer_id=?", (answer_id,))

    def pending(self, transport_key, user_id):
        with self.connection() as db:
            row = db.execute("SELECT answer_id FROM feedback_prompts WHERE transport_key=? "
                             "AND user_id=? AND completed=0", (transport_key,user_id)).fetchone()
        return row["answer_id"] if row else None

    def comment(self, answer_id, user_id, message_id, text, prompt_key):
        self.rated_answer(answer_id,user_id)
        now = time.time()
        with self.connection() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO feedback_events "
                "(user_id,answer_id,comment,comment_message_id,request_key,created) VALUES (?,?,?,?,?,?)",
                (user_id,answer_id,text,message_id,f"comment:{message_id}",now))
            if inserted.rowcount:
                db.execute("UPDATE feedback SET comment=?,comment_message_id=?,updated=?,commented=? WHERE answer_id=?",
                           (text,message_id,now,now,answer_id))
                db.execute("UPDATE feedback_prompts SET completed=1 WHERE answer_id=? AND user_id=?",
                           (answer_id,user_id))

    def export(self, user_id):
        # Stream rows to disk: the size of a user's archive must not dictate service-bot RAM.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                         prefix="export-", suffix=".json", delete=False) as stream:
            path = Path(stream.name)
            try:
                with self.connection() as db:
                    row = db.execute("SELECT id,name,username FROM users WHERE id=?", (user_id,)).fetchone()
                    stream.write('{"user":')
                    json.dump(dict(row) if row else {"id": user_id}, stream, ensure_ascii=False)
                    for table in ("messages", "answers", "feedback", "feedback_events"):
                        stream.write(f', "{table}": [')
                        separator = ""
                        order = "occurred" if table == "messages" else "answered" if table == "answers" else "created"
                        identity = "answer_id" if table == "feedback" else "id"
                        for row in db.execute(f"SELECT * FROM {table} WHERE user_id=? ORDER BY {order},{identity}", (user_id,)):
                            item = dict(row)
                            item.pop("transport_key", None)
                            item.pop("user_id", None)
                            for field in ("occurred", "recorded", "answered", "deleted", "created", "updated",
                                          "rated", "commented", "expires"):
                                if item.get(field) is not None:
                                    item[field] = datetime.fromtimestamp(item[field], timezone.utc).isoformat()
                            for field in ("attachment",):
                                if item.get(field):
                                    item[field] = json.loads(item[field])
                            stream.write(separator)
                            json.dump(item, stream, ensure_ascii=False)
                            separator = ","
                        stream.write("]")
                    stream.write("}")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return path
