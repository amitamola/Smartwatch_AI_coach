"""Private, durable transport state and bounded snapshot caching."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


class RuntimeStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS received_updates (
                    id INTEGER PRIMARY KEY, received_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL, created_at REAL NOT NULL,
                    last_error TEXT);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL, created_at REAL NOT NULL,
                    telegram_message_id TEXT, last_error TEXT);
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY, response TEXT NOT NULL,
                    created_at REAL NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE inbox SET status='pending' WHERE status='processing'")

    def offset(self, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM runtime_meta WHERE key='offset'").fetchone()
        return int(row[0]) if row else default

    def ingest(self, updates, now=None):
        """Persist complete updates and their acknowledgement cursor in one transaction."""
        now = time.time() if now is None else now
        with self.connection() as db:
            for update in updates:
                update_id = int(update["update_id"])
                inserted = db.execute("INSERT OR IGNORE INTO received_updates VALUES (?,?)",
                                      (update_id, now)).rowcount
                if not inserted:
                    continue
                msg = update.get("message") or update.get("edited_message")
                if msg:
                    document = msg.get("document") or {}
                    image_document = str(document.get("mime_type", "")).startswith("image/")
                    album = msg.get("media_group_id") if (msg.get("photo") or image_document) else None
                    key = (f"album:{msg['chat']['id']}:{album}" if album
                           else f"update:{update_id}")
                    prior = db.execute("SELECT * FROM inbox WHERE id=?", (key,)).fetchone()
                    if album and prior and prior["status"] == "pending":
                        payload = json.loads(prior["payload"])
                        if update_id not in payload["update_ids"]:
                            payload["messages"].append(msg)
                            payload["update_ids"].append(update_id)
                        db.execute("UPDATE inbox SET payload=?, available_at=? WHERE id=?",
                                   (json.dumps(payload), now + 3, key))
                    else:
                        # A very late album part is a separate request, never silently lost.
                        if album and prior:
                            key += f":late:{update_id}"
                        payload = {"messages": [msg], "update_ids": [update_id],
                                   "album": bool(album)}
                        db.execute(
                            "INSERT OR IGNORE INTO inbox(id,payload,available_at,created_at)"
                            " VALUES (?,?,?,?)",
                            (key, json.dumps(payload), now + (3 if album else 0), now))
                db.execute(
                    "INSERT INTO runtime_meta(key,value) VALUES ('offset',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=max(CAST(value AS INTEGER),"
                    "CAST(excluded.value AS INTEGER))", (str(update_id + 1),))

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM inbox WHERE status='pending' "
                "ORDER BY created_at,rowid LIMIT 1").fetchone()
            if not row or row["available_at"] > now:
                return None
            db.execute("UPDATE inbox SET status='processing', attempts=attempts+1 WHERE id=?",
                       (row["id"],))
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            result["attempts"] += 1
            return result

    def finish(self, request_id):
        with self.connection() as db:
            db.execute("UPDATE inbox SET status='done',last_error=NULL WHERE id=?",
                       (request_id,))

    def fail(self, request_id, error, max_attempts=4, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            row = db.execute("SELECT attempts FROM inbox WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(request_id)
            status = "failed" if row["attempts"] >= max_attempts else "pending"
            db.execute("UPDATE inbox SET status=?,available_at=?,last_error=? WHERE id=?",
                       (status, now + min(300, 15 * 2 ** row["attempts"]),
                        str(error)[:1000], request_id))
            return status

    def enqueue(self, key, chat_id, payload, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO outbox(id,chat_id,payload,available_at,created_at)"
                " VALUES (?,?,?,?,?)", (key, str(chat_id), json.dumps(payload), now, now))
        return key

    def due_messages(self, now=None, limit=20):
        now = time.time() if now is None else now
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE status='pending' "
                "ORDER BY created_at,rowid LIMIT ?", (limit,)).fetchall()
        due = []
        for row in rows:
            if row["available_at"] > now:
                break
            due.append(dict(row, payload=json.loads(row["payload"])))
        return due

    def delivered(self, key, message_id):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM outbox WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            db.execute("UPDATE outbox SET status='sent',telegram_message_id=?,last_error=NULL "
                       "WHERE id=?", (str(message_id), key))
            payload = json.loads(row["payload"])
            if payload.get("_summary_complete"):
                db.execute(
                    "INSERT OR REPLACE INTO runtime_meta(key,value) VALUES (?,?)",
                    ("summary_delivered:" + payload["_summary_date"], str(message_id)))

    def delivery_failed(self, key, error, retry_after=None, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            row = db.execute("SELECT attempts FROM outbox WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            delay = (float(retry_after) if retry_after is not None
                     else min(900, 5 * 2 ** min(row["attempts"], 8)))
            db.execute("UPDATE outbox SET attempts=attempts+1,available_at=?,last_error=? "
                       "WHERE id=?", (now + max(1, delay), str(error)[:1000], key))

    def saved_generation(self, key):
        if not key:
            return None
        with self.connection() as db:
            row = db.execute("SELECT response FROM generations WHERE id=?", (key,)).fetchone()
        return row["response"] if row else None

    def save_generation(self, key, response):
        if key:
            with self.connection() as db:
                db.execute("INSERT OR IGNORE INTO generations VALUES (?,?,?)",
                           (key, response, time.time()))

    def counts(self):
        with self.connection() as db:
            return {
                table: {row["status"]: row["n"] for row in db.execute(
                    f"SELECT status,count(*) AS n FROM {table} GROUP BY status")}
                for table in ("inbox", "outbox")
            }

    def summary_pending(self, day):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM outbox WHERE status='pending'").fetchall()
        return any(json.loads(row["payload"]).get("_summary_date") == day for row in rows)

    def summary_delivered(self, day):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM runtime_meta WHERE key=?",
                              ("summary_delivered:" + day,)).fetchone() is not None


class SnapshotCache:
    def __init__(self, loader, ttl_seconds=120):
        self.loader = loader
        self.ttl = ttl_seconds
        self.value = None
        self.loaded_at = 0
        self.loaded_day = None

    def invalidate(self):
        self.value = None

    def get(self, force=False):
        now = time.time()
        if (force or self.value is None or now - self.loaded_at >= self.ttl
                or self.loaded_day != time.strftime("%Y-%m-%d")):
            value = self.loader()
            if not isinstance(value, dict) or "__error__" in value:
                self.value = None
                return value
            self.value = value
            self.loaded_at = time.time()
            self.loaded_day = time.strftime("%Y-%m-%d")
        # Never mutate the cached data as downstream code enriches its own view.
        result = json.loads(json.dumps(self.value, default=str))
        result["snapshot_cache"] = {
            "age_seconds": round(time.time() - self.loaded_at, 1),
            "max_age_seconds": self.ttl,
        }
        return result


def message_key(context, sequence, chat_id, text):
    identity = (f"{context}:{sequence}:{chat_id}" if context
                else f"scheduled:{time.strftime('%Y-%m-%d')}:{chat_id}:{text}")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
