"""Revisionable private coaching memory; no filesystem access occurs at import.

Callers supply the private SQLite path. ``upsert`` is a trusted application API;
model output must go through ``process_markers`` instead. Verification means
traceable user testimony, never independent medical or performance verification.
"""

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path


log = logging.getLogger(__name__)
KINDS = frozenset(("preference", "anchor", "health"))
STATUSES = frozenset(("active", "resolved"))


class MemoryStoreError(RuntimeError):
    """Memory could not be read or durably saved; callers must surface this."""


class MemoryBudgetError(MemoryStoreError):
    """A complete, honestly labelled memory rendering cannot fit its budget."""

    def __init__(self, kind, required_chars, max_chars):
        self.kind = kind
        self.required_chars = required_chars
        self.max_chars = max_chars
        super().__init__(
            f"{kind} memory requires {required_chars} characters; budget is "
            f"{max_chars}. No standing constraints were silently dropped."
        )


class LegacyMigrationError(MemoryStoreError):
    """A legacy source is invalid; its migration transaction was rolled back."""


def normalize_key(key):
    """Normalize spacing/case while preserving meaningful namespace colons."""
    if not isinstance(key, str):
        raise ValueError("Memory key must be a nonempty string.")
    key = unicodedata.normalize("NFKC", key).casefold().strip()
    parts = [re.sub(r"[\W_]+", "_", part, flags=re.UNICODE).strip("_")
             for part in key.split(":")]
    if not parts or any(not part for part in parts):
        raise ValueError("Memory key must contain a name in each namespace.")
    return ":".join(parts)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError("Memory kind must be preference, anchor or health.")


def _check_status(status):
    if status not in STATUSES:
        raise ValueError("Memory status must be active or resolved.")


def _observed_date(value, default_today=True):
    if value is None:
        return date.today().isoformat() if default_today else ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError("observed_on must be an ISO date.")
    return date.fromisoformat(value).isoformat()


# Versioning is namespaced: transport and training stores can share this database.
_MIGRATIONS = (
    (1, (
        """CREATE TABLE memory_records (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('preference','anchor','health')),
            key TEXT NOT NULL,
            text TEXT NOT NULL,
            observed_on TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','resolved')),
            source_type TEXT NOT NULL,
            source_text TEXT NOT NULL,
            verified INTEGER NOT NULL CHECK(verified IN (0,1)),
            source_metadata TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_source_text TEXT NOT NULL DEFAULT '',
            UNIQUE(kind,key))""",
        """CREATE TABLE memory_revisions (
            record_id INTEGER NOT NULL REFERENCES memory_records(id),
            revision INTEGER NOT NULL,
            action TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY(record_id,revision))""",
        "CREATE INDEX memory_active ON memory_records(kind,status)",
    )),
    (2, (
        """CREATE TABLE memory_legacy_imports (
            source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            record_id INTEGER NOT NULL REFERENCES memory_records(id),
            imported_at TEXT NOT NULL,
            PRIMARY KEY(source_file,source_line,source_hash))""",
    )),
)


_REGIONS = {
    "low_back": ("low back", "lower back", "lumbar"),
    "thoracic": ("thoracic", "upper back", "mid back", "middle back", "t4", "t 4"),
    "neck": ("neck", "cervical"),
    "shoulder": ("shoulder", "shoulders"),
    "knee": ("knee", "knees"),
    "ankle": ("ankle", "ankles"),
    "wrist": ("wrist", "wrists"),
    "elbow": ("elbow", "elbows"),
    "hip": ("hip", "hips"),
    "foot": ("foot", "feet"),
}


def _words(value):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())


def _regions(value):
    value = " ".join(_words(value))
    found = set()
    for region, aliases in _REGIONS.items():
        for alias in aliases:
            for match in re.finditer(r"\b" + re.escape(alias) + r"\b", value):
                before = value[:match.start()].split()
                side = before[-1] if before and before[-1] in ("left", "right") else ""
                found.add((side + "_" if side else "") + region)
    return found


def _source_context(source, quote):
    """Keep enclosing sentences so quote selection cannot strip a user's negation."""
    start = source.index(quote)
    end = start + len(quote)
    boundaries = list(re.finditer(r"(?<=[.!?])\s+|\n+", source))
    left = max([0] + [boundary.end() for boundary in boundaries if boundary.end() <= start])
    right = next((boundary.start() for boundary in boundaries if boundary.start() >= end),
                 len(source))
    return source[left:right]


def _resolution_supported(record, quote):
    """Require a named, affirmative resolution clause, not just any matching quote."""
    clauses = re.split(r"[.!?;\n]+|,\s*|\b(?:but|yet|while|and)\b", quote.casefold())
    if "?" in quote:
        return False
    if re.search(
        r"\b(?:if|might|maybe|could|would|should|hope|wish|hopefully|probably|possibly|assume)\b",
        quote.casefold(),
    ):
        return False
    negation = (
        r"\b(?:not|never|cannot|do not)\b"
        r"|\b(?:don|doesn|didn|isn|wasn|weren|hasn|haven|hadn|can|couldn|wouldn|shouldn|mustn) t\b"
    )
    if record["kind"] == "health":
        actual = _regions(record["key"]) or _regions(record["text"])
        cleared = set()
        for clause in clauses:
            if re.search(negation, " ".join(_words(clause))):
                continue
            recovery = re.search(
                r"\b(?:resolved|recovered|healed|gone|pain[- ]free)\b"
                r"|\b(?:no|without)\s+(?:more\s+)?(?:pain|symptoms?|discomfort|soreness)\b"
                r"|\bno longer\s+(?:hurts?|painful|sore)\b",
                clause,
            )
            if not recovery:
                fine_now = re.fullmatch(
                    r"(?:my |the )?(.+?)(?: area| symptoms?)? "
                    r"(?:is|are|feels?|feel) (?:fine|alright|all right) now",
                    " ".join(_words(clause)),
                )
                recovery = fine_now and any(
                    fine_now[1] in (alias, "left " + alias, "right " + alias)
                    for aliases in _REGIONS.values() for alias in aliases
                )
            if not recovery:
                continue
            mentioned = _regions(clause)
            if actual:
                cleared.update(mentioned)
            elif set(_words(record["key"])) <= set(_words(clause)):
                return True
        return bool(actual and actual <= cleared)
    target = set(_words(record["key"].split(":")[-1]))
    for clause in clauses:
        words = set(_words(clause))
        if not target <= words:
            continue
        if re.search(negation + r"|\b(?:keep|continue)\b", " ".join(_words(clause))):
            continue
        if re.search(
            r"\b(?:remove|revoke|rescind|clear|delete|retire|reintroduce)\b"
            r"|\b(?:stop|no longer)\s+(?:avoid|avoiding|exclude|excluding)\b",
            clause,
        ):
            return True
    return False


class MemoryStore:
    """A file-backed store using short, atomic transactions and revision snapshots."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.path = self.db_path
        if not self.db_path or self.db_path == ":memory:":
            raise ValueError("Provide a persistent, private SQLite file path.")
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Memory directory creation failed (%s).", type(exc).__name__)
            raise MemoryStoreError("Cannot create the private memory directory.") from exc
        with self._connection(write=True) as db:
            result = db.execute("PRAGMA quick_check").fetchall()
            if [row[0] for row in result] != ["ok"]:
                log.error("Coaching memory failed SQLite integrity validation.")
                raise MemoryStoreError("Coaching memory database failed integrity validation.")
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            versions = {row[0] for row in db.execute(
                "SELECT version FROM memory_schema_migrations"
            )}
            if versions != set(range(1, len(versions) + 1)) or (
                    versions and max(versions) > _MIGRATIONS[-1][0]):
                log.error("Coaching memory has an unsupported schema version.")
                raise MemoryStoreError("Unsupported coaching memory schema version.")
            for version, statements in _MIGRATIONS:
                if version not in versions:
                    for statement in statements:
                        db.execute(statement)
                    db.execute(
                        "INSERT INTO memory_schema_migrations(version,applied_at) VALUES (?,?)",
                        (version, _now()),
                    )
            # Missing tables on an allegedly migrated database are corruption, not empty memory.
            for table in ("memory_records", "memory_revisions", "memory_legacy_imports"):
                db.execute("SELECT * FROM " + table + " LIMIT 0")

    @contextmanager
    def _connection(self, write=False):
        db = None
        try:
            db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA foreign_keys=ON")
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                log.warning("Memory database could not enable WAL; using %s.", mode)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except (sqlite3.Error, OSError) as exc:
            if db is not None and db.in_transaction:
                db.rollback()
            log.error("Coaching memory database operation failed (%s).", type(exc).__name__)
            raise MemoryStoreError(
                "Coaching memory database operation failed; memory was not discarded."
            ) from exc
        except BaseException:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _record(row):
        result = dict(row)
        result["verified"] = bool(result["verified"])
        result["date"] = result["observed_on"]
        try:
            result["source_metadata"] = json.loads(result["source_metadata"])
            if not isinstance(result["source_metadata"], dict):
                raise ValueError("Expected a provenance object.")
        except (ValueError, TypeError) as exc:
            log.error("Coaching memory contains invalid provenance JSON.")
            raise MemoryStoreError("Invalid memory provenance; database needs review.") from exc
        return result

    @staticmethod
    def _receipt(record, action, saved=True):
        return dict(record, saved=saved, action=action)

    def _snapshot(self, db, record, action):
        db.execute(
            "INSERT INTO memory_revisions"
            "(record_id,revision,action,snapshot_json,recorded_at) VALUES (?,?,?,?,?)",
            (record["id"], record["revision"], action,
             json.dumps(record, ensure_ascii=False, sort_keys=True), record["updated_at"]),
        )

    def _upsert(self, db, kind, key, text, source_text, source_type, observed_on,
                verified, status, source_metadata=None):
        row = db.execute(
            "SELECT * FROM memory_records WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        old = self._record(row) if row else None
        metadata = source_metadata if source_metadata is not None else {}
        values = {
            "text": text, "source_text": source_text, "source_type": source_type,
            "observed_on": observed_on, "verified": verified, "status": status,
            "source_metadata": metadata,
        }
        if old and all(old[field] == value for field, value in values.items()):
            return self._receipt(old, "unchanged", saved=False)
        stamp = _now()
        serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        resolved_at = stamp if status == "resolved" else None
        if old:
            db.execute(
                """UPDATE memory_records SET text=?, source_text=?, source_type=?,
                observed_on=?, verified=?, status=?, source_metadata=?, revision=?,
                updated_at=?, resolved_at=?, resolution_source_text='' WHERE id=?""",
                (text, source_text, source_type, observed_on, int(verified), status,
                 serialized, old["revision"] + 1, stamp, resolved_at, old["id"]),
            )
            record_id = old["id"]
            action = "resolved" if status == "resolved" and old["status"] == "active" else "updated"
        else:
            cursor = db.execute(
                """INSERT INTO memory_records
                (kind,key,text,source_text,source_type,observed_on,verified,status,
                 source_metadata,revision,created_at,updated_at,resolved_at)
                VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (kind, key, text, source_text, source_type, observed_on, int(verified),
                 status, serialized, stamp, stamp, resolved_at),
            )
            record_id, action = cursor.lastrowid, "created"
        record = self._record(db.execute(
            "SELECT * FROM memory_records WHERE id=?", (record_id,)
        ).fetchone())
        self._snapshot(db, record, action)
        return self._receipt(record, action)

    def upsert(self, kind, key, text, source_text="", source_type="user",
               observed_on=None, verified=False, status="active"):
        """Save a complete fact, superseding its normalized key without losing history.

        ``verified=True`` requires verbatim text within actual user provenance.
        An unchanged operation returns ``saved=False, action='unchanged'``.
        """
        _check_kind(kind)
        _check_status(status)
        key = normalize_key(key)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Memory text must be a nonempty string.")
        if not isinstance(source_text, str) or not isinstance(source_type, str) or not source_type.strip():
            raise ValueError("Memory provenance must contain string fields.")
        if not isinstance(verified, bool):
            raise ValueError("verified must be a boolean.")
        source_type = source_type.strip().casefold()
        if verified and (source_type != "user" or not source_text or text not in source_text):
            raise ValueError("Verified memory requires verbatim, actual user evidence.")
        observed_on = _observed_date(observed_on)
        with self._connection(write=True) as db:
            return self._upsert(db, kind, key, text, source_text, source_type,
                                observed_on, verified, status)

    def records(self, kind, status="active"):
        """Return current complete records; ``status=None`` includes resolved facts."""
        _check_kind(kind)
        if status is not None:
            _check_status(status)
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM memory_records WHERE kind=? "
                "AND (? IS NULL OR status=?) ORDER BY updated_at DESC,id DESC",
                (kind, status, status),
            ).fetchall()
            return [self._record(row) for row in rows]

    def revisions(self, kind, key):
        """Return oldest-first complete snapshots, with ``action`` and revision number."""
        _check_kind(kind)
        key = normalize_key(key)
        with self._connection() as db:
            rows = db.execute(
                """SELECT r.snapshot_json,r.action FROM memory_revisions r
                JOIN memory_records m ON m.id=r.record_id
                WHERE m.kind=? AND m.key=? ORDER BY r.revision""",
                (kind, key),
            ).fetchall()
            try:
                return [dict(json.loads(row["snapshot_json"]), action=row["action"])
                        for row in rows]
            except (ValueError, TypeError) as exc:
                log.error("Coaching memory contains invalid revision JSON.")
                raise MemoryStoreError("Invalid memory revision history; database needs review.") from exc

    def render(self, kind, query="", max_chars=None):
        """Render whole current facts, never arbitrary character/recency slices.

        Preferences and health constraints cannot be budget-evicted. Anchors rank
        *all* entity keys by query relevance, then recency, and disclose omissions.
        Superseded revisions remain auditable but are never current capabilities.
        """
        _check_kind(kind)
        if not isinstance(query, str):
            raise ValueError("Memory query must be a string.")
        if max_chars is not None and (type(max_chars) is not int or max_chars < 0):
            raise ValueError("max_chars must be a nonnegative integer or None.")
        records = self.records(kind)
        if not records:
            return ""
        if kind == "anchor" and query.strip():
            tokens = set(_words(query))

            def relevance(record):
                key_words = set(_words(record["key"]))
                text_words = set(_words(record["text"]))
                return (len(tokens & key_words) * 4 + len(tokens & text_words),
                        record["updated_at"], record["id"])

            records.sort(key=relevance, reverse=True)
        header = (
            "Coaching memory: user-reported is not independently verified. "
            "UNVERIFIED claims do not establish effort, pain-free status, safety or causes."
        )
        if kind in ("preference", "health"):
            header += " Retain active restrictions pending explicit correction or resolution."
        lines = []
        for record in records:
            provenance = ("user-reported" if record["verified"] else
                          "UNVERIFIED " + record["source_type"])
            lines.append(
                f"- [{record['key']} | {record['date'] or 'date unknown'} | "
                f"{provenance} | revision {record['revision']}] {record['text']}"
            )
        full = header + "\n" + "\n".join(lines)
        if max_chars is None or len(full) <= max_chars:
            return full
        if kind != "anchor":
            raise MemoryBudgetError(kind, len(full), max_chars)

        def omission(count):
            return f"[{count} active anchor records omitted by character budget.]"

        minimum = len(header) + 1 + len(omission(len(lines)))
        if minimum > max_chars:
            raise MemoryBudgetError(kind, minimum, max_chars)
        selected = []
        size = len(header)
        for line in lines:
            # Reserve an honest omission count before choosing complete records.
            needed = size + 1 + len(line) + 1 + len(omission(len(lines) - len(selected) - 1))
            if needed <= max_chars:
                selected.append(line)
                size += len(line) + 1
        omitted = len(lines) - len(selected)
        return "\n".join([header] + selected + ([omission(omitted)] if omitted else []))

    def _resolve_record(self, db, record, source_text):
        if record["status"] == "resolved":
            return self._receipt(record, "unchanged", saved=False)
        db.execute(
            """UPDATE memory_records SET status='resolved', revision=revision+1,
            updated_at=?,resolved_at=?,resolution_source_text=? WHERE id=?""",
            (_now(), _now(), source_text, record["id"]),
        )
        resolved = self._record(db.execute(
            "SELECT * FROM memory_records WHERE id=?", (record["id"],)
        ).fetchone())
        self._snapshot(db, resolved, "resolved")
        return self._receipt(resolved, "resolved")

    def resolve_health(self, areas, source_text="", *, allow_all=False):
        """Resolve exact keys or unambiguous body areas; never generic substring matches.

        The caller must explicitly authorize a global clear with ``allow_all=True``.
        Multi-area records stay active unless *every* area is cleared. Prefer one
        body area per key. Recovery never clears a separate movement preference.
        """
        if isinstance(areas, str):
            areas = [areas]
        if not isinstance(areas, (list, tuple, set)) or not all(
                isinstance(area, str) and area.strip() for area in areas):
            raise ValueError("areas must be a string or collection of named areas.")
        if not isinstance(source_text, str):
            raise ValueError("source_text must be a string.")
        keys = {normalize_key(area) for area in areas}
        clear_all = "all" in keys
        if clear_all and (not allow_all or keys != {"all"}):
            raise ValueError("Global health resolution requires only 'all' and allow_all=True.")
        regions = set().union(*(_regions(area) for area in areas)) if areas else set()
        changed = []
        with self._connection(write=True) as db:
            for row in db.execute(
                    "SELECT * FROM memory_records WHERE kind=? AND status=? ORDER BY id",
                    ("health", "active")).fetchall():
                record = self._record(row)
                actual = _regions(record["key"]) or _regions(record["text"])
                if clear_all or record["key"] in keys or (actual and actual <= regions):
                    changed.append(self._resolve_record(db, record, source_text))
        return changed

    def migrate_legacy(self, state_dir):
        """Import known JSONL files once per original line, without modifying them.

        Missing files are normal. Invalid nonblank lines fail the entire batch,
        visibly, instead of silently losing constraints. Legacy claims are always
        unverified. Unkeyed or colliding entries get distinct stable legacy keys:
        no model-based entity guessing or speculative supersession occurs.
        """
        sources = (("health.jsonl", "health"), ("preferences.jsonl", "preference"),
                   ("anchors.jsonl", "anchor"))
        imported = skipped = 0
        files = []
        try:
            with self._connection(write=True) as db:
                for filename, kind in sources:
                    path = Path(state_dir) / filename
                    try:
                        raw_file = path.read_bytes()
                    except FileNotFoundError:
                        continue
                    source_file = str(path.resolve())
                    files.append(source_file)
                    for line_number, raw_line in enumerate(
                            raw_file.decode("utf-8-sig").splitlines(), 1):
                        if not raw_line.strip():
                            continue
                        digest = hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
                        if db.execute(
                            "SELECT 1 FROM memory_legacy_imports "
                            "WHERE source_file=? AND source_line=? AND source_hash=?",
                            (source_file, line_number, digest),
                        ).fetchone():
                            skipped += 1
                            continue
                        try:
                            entry = json.loads(raw_line)
                            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                                raise ValueError("Expected an object with text.")
                            if not entry["text"].strip():
                                raise ValueError("Empty legacy text.")
                            status = entry.get("status", "active")
                            _check_status(status)
                            observed = _observed_date(
                                entry.get("date") or None, default_today=False)
                            identity = f"{source_file}\n{line_number}\n{digest}"
                            fallback = "legacy:" + kind + ":" + hashlib.sha256(
                                identity.encode("utf-8")).hexdigest()
                            key = normalize_key(entry["key"]) if entry.get("key") else fallback
                            if db.execute(
                                "SELECT 1 FROM memory_records WHERE kind=? AND key=?", (kind, key)
                            ).fetchone():
                                key = fallback
                            metadata = {
                                "source_file": source_file, "source_line": line_number,
                                "source_hash": digest, "legacy_entry": entry,
                                "legacy_raw_line": raw_line,
                            }
                            original_source = entry.get("source_text", entry["text"])
                            if not isinstance(original_source, str):
                                original_source = entry["text"]
                            receipt = self._upsert(
                                db, kind, key, entry["text"], original_source, "legacy",
                                observed, False, status, metadata)
                            db.execute(
                                "INSERT INTO memory_legacy_imports"
                                "(source_file,source_line,source_hash,record_id,imported_at) "
                                "VALUES (?,?,?,?,?)",
                                (source_file, line_number, digest, receipt["id"], _now()),
                            )
                            imported += 1
                        except (ValueError, TypeError) as exc:
                            raise LegacyMigrationError(
                                f"Invalid {filename} line {line_number}; no legacy imports saved."
                            ) from exc
        except (OSError, UnicodeError) as exc:
            log.error("Legacy memory source could not be read (%s).", type(exc).__name__)
            raise LegacyMigrationError("Cannot read legacy memory; no legacy imports saved.") from exc
        except LegacyMigrationError:
            log.error("Legacy memory migration rejected; batch rolled back.")
            raise
        return {"imported": imported, "skipped": skipped, "files": files}

    def process_markers(self, text, source_text, source_type="user"):
        """Strip MEMORY markers and return ``(cleaned_text, receipts, errors)``.

        Allowed JSON fields: kind, key, text, source_quote, action, observed_on.
        An upsert saves the source quote's complete enclosing sentence(s), not a
        model paraphrase or a fragment stripped of negation. No matching current
        user quote means no save. A resolve targets one existing exact key, never
        a model-emitted 'all', and requires affirmative named resolution evidence.
        Legacy marker formats are stripped/rejected rather than accepted as evidence.
        Database failures propagate: the caller must not claim that a save succeeded.
        """
        if not isinstance(text, str) or not isinstance(source_text, str):
            raise ValueError("Marker text and current user source must be strings.")
        receipts, errors = [], []
        allowed = {"kind", "key", "text", "source_quote", "action", "observed_on"}
        decoder = json.JSONDecoder()
        opener = re.compile(r"\[\[MEMORY\s*:", re.IGNORECASE)
        cursor, parts = 0, []
        while True:
            match = opener.search(text, cursor)
            if match is None:
                parts.append(text[cursor:])
                break
            parts.append(text[cursor:match.start()])
            payload_start = match.end()
            while payload_start < len(text) and text[payload_start].isspace():
                payload_start += 1
            try:
                payload, end = decoder.raw_decode(text, payload_start)
                close = end
                while close < len(text) and text[close].isspace():
                    close += 1
                if text[close:close + 2] != "]]":
                    raise ValueError("MEMORY marker lacks a closing delimiter.")
                cursor = close + 2
            except ValueError:
                close = text.find("]]", payload_start)
                cursor = len(text) if close < 0 else close + 2
                errors.append("Malformed MEMORY marker was not saved.")
                continue
            try:
                if not isinstance(payload, dict) or set(payload) - allowed:
                    raise ValueError("MEMORY contains unsupported fields.")
                kind = payload.get("kind")
                _check_kind(kind)
                key = normalize_key(payload.get("key"))
                action = payload.get("action", "upsert")
                if action not in ("upsert", "resolve"):
                    raise ValueError("MEMORY action must be upsert or resolve.")
                quote = payload.get("source_quote")
                if source_type != "user" or not isinstance(quote, str) or not quote.strip() or quote not in source_text:
                    raise ValueError("MEMORY requires an exact quote from the current user message.")
                evidence = _source_context(source_text, quote)
                if action == "upsert":
                    if not isinstance(payload.get("text"), str) or not payload["text"].strip():
                        raise ValueError("MEMORY upsert requires nonempty text.")
                    receipt = self.upsert(
                        kind, key, evidence, source_text=source_text, source_type="user",
                        observed_on=payload.get("observed_on"), verified=True,
                    )
                else:
                    if key == "all":
                        raise ValueError("A model marker cannot authorize a global memory clear.")
                    with self._connection(write=True) as db:
                        row = db.execute(
                            "SELECT * FROM memory_records WHERE kind=? AND key=?", (kind, key)
                        ).fetchone()
                        if row is None:
                            raise ValueError("MEMORY resolution key does not exist.")
                        record = self._record(row)
                        if not _resolution_supported(record, evidence):
                            raise ValueError(
                                "Resolution quote must explicitly clear this named fact or body area."
                            )
                        receipt = self._resolve_record(db, record, source_text)
                receipts.append(receipt)
            except (ValueError, TypeError) as exc:
                errors.append(str(exc))
        cleaned = "".join(parts)
        legacy = re.compile(
            r"\[\[(?:ANCHOR|PREF|HEALTH_FLAG|HEALTH_CLEAR)\s*:[\s\S]*?(?:\]\]|$)",
            re.IGNORECASE,
        )
        if legacy.search(cleaned):
            cleaned = legacy.sub("", cleaned)
            errors.append("Legacy memory markers were not saved; use source-quoted MEMORY updates.")
        if errors:
            log.warning("Rejected %d memory marker update(s).", len(errors))
        return cleaned.rstrip(), receipts, errors
