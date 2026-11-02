"""Private durable transport state; schema upgrades are additive/backward compatible.

Response chunks must share an explicit group_id; ungrouped messages are independent.
Expiry is opt-in for scheduled messages, never inferred for user-requested results.
Artifacts are immutable JSON, not a substitute for idempotent domain-effect writes.
"""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


class ArtifactConflictError(ValueError):
    """A stable artifact key was reused for different content."""


class RuntimeStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
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
                CREATE TABLE IF NOT EXISTS runtime_artifacts (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(outbox)")}
            for name, kind in (("group_id", "TEXT"), ("expires_at", "REAL")):
                if name not in columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {kind}")
            db.execute("CREATE INDEX IF NOT EXISTS outbox_group ON outbox(group_id,status)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
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
            db.execute("BEGIN IMMEDIATE")
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
                            payload.setdefault("updates", []).append(update)
                        db.execute("UPDATE inbox SET payload=?, available_at=? WHERE id=?",
                                   (json.dumps(payload), now + 3, key))
                    else:
                        # A very late album part is a separate request, never silently lost.
                        if album and prior:
                            key += f":late:{update_id}"
                        payload = {"messages": [msg], "update_ids": [update_id],
                                   "album": bool(album), "updates": [update]}
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
                "SELECT * FROM inbox WHERE status IN ('pending','processing') "
                "ORDER BY created_at,rowid LIMIT 1").fetchone()
            if not row or row["status"] == "processing" or row["available_at"] > now:
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

    def enqueue(self, key, chat_id, payload, now=None, *, group_id=None, expires_at=None):
        """Append one chunk. Replay is idempotent; original payload always wins."""
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            terminal = None
            if group_id is not None:
                terminal = db.execute(
                    "SELECT status FROM outbox WHERE group_id=? "
                    "AND status IN ('failed','cancelled','expired') LIMIT 1",
                    (group_id,)).fetchone()
            status = ("expired" if terminal["status"] == "expired" else "cancelled"
                      ) if terminal else "pending"
            db.execute(
                "INSERT OR IGNORE INTO outbox"
                "(id,chat_id,payload,available_at,created_at,group_id,expires_at,status)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (key, str(chat_id), json.dumps(payload), now, now, group_id, expires_at, status))
        return key

    def due_messages(self, now=None, limit=20):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE outbox SET status='expired',last_error='delivery deadline expired' "
                "WHERE status='pending' AND (expires_at<=? OR group_id IN "
                "(SELECT group_id FROM outbox WHERE expires_at<=? "
                "AND group_id IS NOT NULL))", (now, now))
            rows = db.execute(
                "SELECT o.* FROM outbox o WHERE o.status='pending' AND o.available_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM outbox earlier "
                "WHERE o.group_id IS NOT NULL AND earlier.group_id=o.group_id "
                "AND earlier.rowid<o.rowid AND earlier.status!='sent' "
                "AND (earlier.status!='pending' OR earlier.available_at>?)) "
                "ORDER BY o.rowid LIMIT ?", (now, now, limit)).fetchall()
        # A caller must skip remaining chunks in a group after any delivery failure.
        return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def delivered(self, key, message_id):
        with self.connection() as db:
            row = db.execute("SELECT payload,status FROM outbox WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["status"] != "pending":
                return
            db.execute("UPDATE outbox SET status='sent',telegram_message_id=?,last_error=NULL "
                       "WHERE id=?", (str(message_id), key))
            payload = json.loads(row["payload"])
            if payload.get("_summary_complete"):
                db.execute(
                    "INSERT OR REPLACE INTO runtime_meta(key,value) VALUES (?,?)",
                    ("summary_delivered:" + payload["_summary_date"], str(message_id)))

    def delivery_failed(self, key, error, retry_after=None, now=None, *,
                        permanent=False, max_attempts=8):
        """Return pending/failed; terminal failures cancel unsent sibling chunks."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["status"] != "pending":
                return row["status"]
            status = "failed" if permanent or row["attempts"] + 1 >= max_attempts else "pending"
            delay = (float(retry_after) if retry_after is not None
                     else min(900, 5 * 2 ** min(row["attempts"], 8)))
            db.execute("UPDATE outbox SET attempts=attempts+1,status=?,available_at=?,last_error=? "
                       "WHERE id=?", (status, now + max(1, delay), str(error)[:1000], key))
            if status == "failed" and row["group_id"] is not None:
                db.execute(
                    "UPDATE outbox SET status='cancelled',last_error=? "
                    "WHERE group_id=? AND status='pending'",
                    (f"response bundle failed at {key}", row["group_id"]))
            return status

    def get_artifact(self, key):
        """Return a detached JSON value, or None when no artifact exists."""
        with self.connection() as db:
            row = db.execute("SELECT value FROM runtime_artifacts WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def cancel_delivery(self, key, reason):
        """Cancel unsent chunks together; retain the original payload and audit reason."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT group_id FROM outbox WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            db.execute(
                "UPDATE outbox SET status='cancelled',last_error=? WHERE status='pending' "
                "AND (id=? OR (group_id IS NOT NULL AND group_id=?))",
                (reason, key, row["group_id"]))

    def note_summary_request(self, day):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO runtime_meta VALUES (?,?)",
                       ("summary_requested:" + day, str(time.time())))

    def summary_requested(self, day):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM runtime_meta WHERE key=?",
                              ("summary_requested:" + day,)).fetchone() is not None

    def daily_summary(self, day):
        with self.connection() as db:
            row = db.execute("SELECT value FROM runtime_meta WHERE key=?",
                             ("daily_summary:" + day,)).fetchone()
        return json.loads(row["value"]) if row else None

    def save_daily_summary(self, day, record):
        """Cache a validated rendering; training_plans remains the plan authority."""
        encoded = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self.connection() as db:
            db.execute("INSERT INTO runtime_meta VALUES (?,?) ON CONFLICT(key) "
                       "DO UPDATE SET value=excluded.value", ("daily_summary:" + day, encoded))

    def save_artifact(self, key, value):
        """Write once, returning stored JSON; conflicting replays raise ArtifactConflictError."""
        if not isinstance(key, str) or not key:
            raise ValueError("artifact key must be a nonempty string")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM runtime_artifacts WHERE key=?", (key,)).fetchone()
            if row and row["value"] != encoded:
                raise ArtifactConflictError(f"artifact conflict: {key}")
            if not row:
                db.execute("INSERT INTO runtime_artifacts VALUES (?,?,?)",
                           (key, encoded, time.time()))
        return json.loads(encoded)

    def saved_generation(self, key):
        if not key:
            return None
        with self.connection() as db:
            row = db.execute("SELECT response FROM generations WHERE id=?", (key,)).fetchone()
        return row["response"] if row else None

    def save_generation(self, key, response):
        if key:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT response FROM generations WHERE id=?", (key,)).fetchone()
                if prior and prior["response"] != response:
                    raise ArtifactConflictError(f"generation conflict: {key}")
                db.execute("INSERT OR IGNORE INTO generations VALUES (?,?,?)",
                           (key, response, time.time()))

    def counts(self):
        with self.connection() as db:
            return {
                table: {row["status"]: row["n"] for row in db.execute(
                    f"SELECT status,count(*) AS n FROM {table} GROUP BY status")}
                for table in ("inbox", "outbox")
            }

    def queue_health(self, now=None):
        """Read-only queue ages/counters; never expires or deletes any records."""
        now = time.time() if now is None else now
        with self.connection() as db:
            return {
                table: {
                    row["status"]: {"count": row["n"], "attempts": row["attempts"],
                                    "oldest_age_seconds": max(0, now - row["oldest"])}
                    for row in db.execute(
                        f"SELECT status,count(*) AS n,sum(attempts) AS attempts,"
                        f"min(created_at) AS oldest FROM {table} GROUP BY status")
                } for table in ("inbox", "outbox")
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
        self.invalidated = False

    def invalidate(self):
        self.invalidated = True

    def get(self, force=False, *, allow_stale=False):
        now = time.time()
        error = None
        if (force or self.invalidated or self.value is None or now - self.loaded_at >= self.ttl
                or self.loaded_day != time.strftime("%Y-%m-%d")):
            self.invalidated = True
            try:
                value = self.loader()
            except OSError as exc:
                if not allow_stale or self.value is None:
                    raise
                value = {"__error__": str(exc)}
            if not isinstance(value, dict) or "__error__" in value:
                self.invalidated = True
                if not allow_stale or self.value is None:
                    return value
                error = value.get("__error__") if isinstance(value, dict) else "invalid snapshot"
            else:
                self.value = json.loads(json.dumps(value, default=str))
                self.loaded_at = time.time()
                self.loaded_day = time.strftime("%Y-%m-%d")
                self.invalidated = False
        # Never mutate the cached data as downstream code enriches its own view.
        result = json.loads(json.dumps(self.value, default=str))
        result["snapshot_cache"] = {
            "age_seconds": round(time.time() - self.loaded_at, 1),
            "max_age_seconds": self.ttl,
            "loaded_at": self.loaded_at,
            "loaded_day": self.loaded_day,
            "stale": self.invalidated,
            "load_error": error,
        }
        return result


def message_key(context, sequence, chat_id, text):
    identity = (f"{context}:{sequence}:{chat_id}" if context
                else f"scheduled:{time.strftime('%Y-%m-%d')}:{chat_id}:{text}")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
