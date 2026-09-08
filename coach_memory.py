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
    """Keep contiguous testimony, including dependent sentences and corrections."""
    start = source.index(quote)
    if source.find(quote, start + 1) >= 0:
        raise ValueError("MEMORY source_quote is ambiguous; quote a longer, unique passage.")
    end = start + len(quote)
    boundaries = list(re.finditer(r"(?<=[.!?])\s+|\n+", source))
    spans = list(zip([0] + [boundary.end() for boundary in boundaries],
                     [boundary.start() for boundary in boundaries] + [len(source)]))
    spans = [(left, right) for left, right in spans if source[left:right].strip()]
    first = next(index for index, (_, right) in enumerate(spans) if right > start)
    last = next((index for index, (_, right) in enumerate(spans) if right >= end),
                len(spans) - 1)

    def dependent(index):
        sentence = source[slice(*spans[index])]
        return bool(re.search(
            r"^\W*(?:but|yet|however|although|though|except|instead|nevertheless|"
            r"still|now|currently|actually|today|since then|from now on|in fact|"
            r"these days|at the moment|and|so|because|therefore|otherwise|"
            r"only|unless|during|at rest|afterwards?|it|this|that|both|the same)\b"
            r"|\b(?:they|them|those|these|former|latter)\b"
            r"|\bI\s+(?:find it|found it)\b",
            sentence, re.IGNORECASE,
        ))

    while first > 0:
        # "Also" alone is not an antecedent dependency. Follow an enumeration
        # only when a later selected sentence refers back to its members.
        enumerated = first < last and re.search(
            r"\b(?:both|they|them|those|these|former|latter)\b",
            source[spans[first + 1][0]:spans[last][1]], re.IGNORECASE,
        ) and re.match(
            r"\W*(?:also\b|I\s+also\b)", source[slice(*spans[first])], re.IGNORECASE,
        )
        if not dependent(first) and not enumerated:
            break
        first -= 1
    while last + 1 < len(spans):
        current_regions = _regions(source[spans[first][0]:spans[last][1]])
        following = source[slice(*spans[last + 1])]
        next_regions = _regions(following)
        symptom_followup = current_regions and not next_regions and re.search(
            r"\b" + _SYMPTOM + r"\b", following, re.IGNORECASE,
        )
        if (not dependent(last + 1) and not current_regions.intersection(next_regions)
                and not symptom_followup):
            break
        last += 1
    return source[spans[first][0]:max(end, spans[last][1])]


_SYMPTOM = (
    r"(?:pain(?:ful)?|hurts?|hurting|aches?|aching|symptoms?|discomfort|"
    r"soreness|sore|injur\w*|issues?|problems?|tight(?:ness)?|stiff(?:ness)?|"
    r"numb(?:ness)?|tingling|tingly|tender(?:ness)?|uncomfortable|"
    r"twinges?|flare\w*|strains?|spasms?|irritation)"
)
_POSITIVE_STATE = (
    r"(?:fine|all good|good|great|well|normal|smoothly|alright|all right|okay|ok|"
    r"comfortable|better|pain free)"
)


def _negated_health_positive(evidence):
    words = " ".join(_words(evidence))
    return bool(re.search(
        r"\b(?:not sure|anything but|far from)\b"
        r"|\b(?:cannot|can t|not|don t|do not|wouldn t|couldn t)\s+"
        r"(?:say|claim|confirm|know|assume)\b"
        r"|\bnot\s+without\s+" + _SYMPTOM + r"\b",
        words,
    ) or re.search(
        r"\b(?:not|never|isn t|wasn t|aren t|weren t|doesn t|didn t|don t|"
        r"haven t|hasn t|can t|cannot)"
        r"\s+(?:(?:always|completely|quite|really|feel|feeling|felt|entirely|"
        r"yet|fully|exactly)\s+)*(?:" + _POSITIVE_STATE
        + r"|recovered|healed|resolved)\b", words,
    ))


def _positive_health_report(evidence):
    """Recognize unqualified positive reports, not diagnoses or implied recovery."""
    words = " ".join(_words(evidence))
    # Ambiguous or mixed testimony stays intact rather than masking a restriction.
    if re.search(r"\b(?:except|unless|if|only)\b", words) or _negated_health_positive(words):
        return False
    area = "(?:" + "|".join(re.escape(alias) for aliases in _REGIONS.values()
                          for alias in aliases) + ")"
    without_symptoms = re.sub(
        r"\b(?:no|without|not experiencing|not having|"
        r"(?:don t|didn t|did not|do not)\s+(?:have|feel|experience))\s+"
        r"(?:(?:any|more|further)\s+)?(?:" + area + r"\s+)?"
        + _SYMPTOM + r"(?:\s+(?:or|and)\s+" + _SYMPTOM + r")*\b"
        r"|\b(?:doesn t|didn t|does not|did not|do not|don t)\s+hurt\b"
        r"|\bnot\s+(?:painful|sore)\b"
        r"|\bno longer\s+(?:hurts?|painful|sore)\b"
        r"|\bpain free\b"
        r"|\b" + _SYMPTOM + r"\s+(?:(?:is|are|has|have|now|all|completely|fully|been)\s+)*"
        r"(?:gone|resolved|healed|recovered)\b",
        "", words,
    )
    positive = re.search(
        r"\b" + _POSITIVE_STATE + r"\b|\b(?:resolved|healed|recovered|tolerated)\b",
        words,
    ) or without_symptoms != words
    return bool(positive and not re.search(r"\b" + _SYMPTOM + r"\b", without_symptoms))


def _family_pattern(key):
    return r"\s+".join(
        re.escape(word) + r"(?:es)?" if word.endswith("ss") else
        re.escape(word[:-1] if word.endswith("s") else word) + r"s?"
        for word in _words(key.split(":", 1)[1])
    )


def _family_scope_supported(key, words):
    family = _family_pattern(key)
    return bool(re.search(
        r"\b(?:all|any|every|the entire|the whole)\s+(?:the\s+)?"
        + family + r"\b"
        r"|\b" + family + r"\s+(?:(?:exercise|movement)\s+)?family\b"
        r"|\b" + family + r"\s+(?:and\s+)?all\s+(?:its|their|the)\s+"
        r"(?:variants|variations)\b"
        r"|\bno\s+" + family + r"\s+(?:variants|variations)\b",
        words,
    ))


def _family_avoidance_supported(key, evidence):
    """Require explicit whole-family intent; two named variants are not a family."""
    words = " ".join(_words(evidence))
    if "?" in evidence or re.search(
        r"\b(?:if|might|maybe|could|would|should|hypothetical|suppose|"
        r"except|unless|only|apart from|other than|"
        r"suggest\w*|recommend(?:ed|s)|said|told)\b", words,
    ):
        return False
    if re.search(
        r"\b(?:not|never|don t|do not|didn t|did not)\s+"
        r"(?:(?:want|wish|need|intend|plan)\s+to\s+)?(?:avoid|exclude|skip|ban)\b",
        words,
    ):
        return False
    intent_pattern = (
        r"^(?:please\s+)?(?:avoid|exclude|skip|ban|do not recommend|don t recommend|no)\b"
        r"|\bi\s+(?:(?:want|need|prefer|choose)\s+(?:you\s+)?to\s+|"
        r"(?:am|m)\s+)?(?:avoid|avoiding|exclude|excluding|skip|skipping)\b"
    )
    return any(
        re.search(intent_pattern, sentence) and _family_scope_supported(key, sentence)
        for sentence in (" ".join(_words(part)) for part in re.split(
            r"[.!?;\n]+|\b(?:because|but|although|though|whereas|while)\b",
            evidence, flags=re.IGNORECASE,
        ))
    )


def _resolution_supported(record, quote):
    """Require a named, affirmative resolution clause, not just any matching quote."""
    clauses = re.split(r"[.!?;\n]+|,\s*|\b(?:but|yet|and)\b", quote.casefold())
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
        if re.search(r"\b(?:except|unless)\b", quote, re.IGNORECASE):
            return False
        for clause in clauses:
            mentioned = _regions(clause)
            if mentioned and actual and not actual.intersection(mentioned):
                continue
            if _negated_health_positive(clause):
                return False
            if not mentioned and re.search(
                r"\b(?:only|during|after|while|when|at rest|usually|sometimes)\b", clause,
            ):
                return False
            if re.search(r"\b" + _SYMPTOM + r"\b", clause) and not _positive_health_report(clause):
                # A plainly historical symptom clause may precede a current clear.
                if not re.search(r"\b(?:used to|previously|last year)\b", clause):
                    return False
        cleared = set()
        for clause in clauses:
            if re.search(negation, " ".join(_words(clause))):
                continue
            if re.search(
                r"\b(?:during|after|while|when|with|only|at rest|on|for|in|"
                r"through|throughout|until|whenever|most|sometimes|usually|often|"
                r"occasionally|temporarily|almost|mostly|"
                r"was|were|had|used to|previously|before|yesterday|ago|last)\b",
                clause,
            ):
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
    if re.search(
        r"\b(?:suggest\w*|recommend(?:ed|s)|said|told|consider\w*|"
        r"hypothetical|suppose)\b", quote, re.IGNORECASE,
    ):
        return False
    for clause in re.split(
            r"\n+|\b(?:because|since|although|though|while)\b", "\n".join(clauses)):
        words = set(_words(clause))
        if record["key"].startswith("avoid_family:"):
            if not _family_scope_supported(record["key"], " ".join(_words(clause))):
                continue
        elif not target <= words:
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

    def reclassify_tolerance(self, record_id, anchor_key):
        """Atomically correct one misclassified health report; not a clinical clear.

        Trusted maintenance API only; model markers cannot invoke it. Requires
        an active, verified user report of positive tolerance naming the target
        movement. Existing anchors and earlier unresolved symptom revisions are
        never overwritten. Returns ``{"health": receipt, "anchor": receipt}``.

        The health revision action is ``reclassified``. Its truthy
        ``source_metadata["reclassification"]`` marks administrative retirement;
        consumers must not present it as user-reported recovery.
        """
        if type(record_id) is not int or record_id < 1:
            raise ValueError("record_id must be a positive integer.")
        anchor_key = normalize_key(anchor_key)
        movement_key = anchor_key.split(":")[-1]
        generic_keys = {"exercise", "workout", "session", "tolerance"}
        generic_keys.update(normalize_key(alias) for aliases in _REGIONS.values()
                            for alias in aliases)
        if movement_key in generic_keys:
            raise ValueError("anchor_key must name a specific movement in the original report.")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM memory_records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise ValueError("The health record does not exist.")
            record = self._record(row)
            if record["kind"] != "health" or record["status"] != "active":
                raise ValueError("Reclassification requires an active health record.")
            if (not record["verified"] or record["source_type"] != "user"
                    or not record["text"].strip() or record["text"] not in record["source_text"]):
                raise ValueError("Reclassification requires verbatim, verified user provenance.")
            evidence = _source_context(record["source_text"], record["text"])
            if (not _positive_health_report(record["text"]) or not _positive_health_report(evidence)
                    or "?" in evidence or re.search(
                        r"\b(?:if|might|maybe|could|would|should|hope|wish|assume|"
                        r"hypothetical|probably|possibly)\b", evidence, re.IGNORECASE)):
                raise ValueError("Only unambiguous positive-only exercise tolerance can be reclassified.")
            movement = r"\b" + _family_pattern("exercise:" + movement_key) + r"\b"
            report = " ".join(_words(record["text"]))
            if (not re.search(movement, report) or not (
                    re.search(r"\b(?:during|after|while|with|on|for)\b", report)
                    or re.search(movement + r".*\b(?:felt|feels|was|were|went|is|are)\b", report))):
                raise ValueError("anchor_key must name an exercise reported as tolerated in the quote.")
            for previous_row in db.execute(
                    "SELECT snapshot_json FROM memory_revisions WHERE record_id=? AND revision<? "
                    "ORDER BY revision DESC", (record_id, record["revision"])):
                try:
                    previous = json.loads(previous_row["snapshot_json"])
                    if previous["status"] == "resolved":
                        break
                    if not _positive_health_report(previous["text"]):
                        raise ValueError(
                            "Earlier active symptom history requires explicit review; "
                            "reclassification cannot silently retire it."
                        )
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise MemoryStoreError("Invalid memory revision history; database needs review.") from exc
            if db.execute(
                    "SELECT 1 FROM memory_records WHERE kind='anchor' AND key=?", (anchor_key,)
            ).fetchone():
                raise ValueError("The target anchor already exists; reclassification cannot overwrite it.")
            anchor_metadata = dict(record["source_metadata"])
            anchor_metadata["reclassified_from"] = {
                "record_id": record_id, "kind": "health", "key": record["key"],
                "revision": record["revision"],
            }
            anchor = self._upsert(
                db, "anchor", anchor_key, record["text"], record["source_text"],
                "user", record["observed_on"], True, "active", anchor_metadata,
            )
            metadata = dict(record["source_metadata"])
            metadata["reclassification"] = {
                "reason": "positive_exercise_tolerance", "clinical_resolution": False,
                "kind": "anchor", "record_id": anchor["id"], "key": anchor_key,
                "source_revision": record["revision"],
            }
            stamp = _now()
            db.execute(
                "UPDATE memory_records SET status='resolved', revision=revision+1, "
                "updated_at=?, resolved_at=?, resolution_source_text='', source_metadata=? WHERE id=?",
                (stamp, stamp, json.dumps(metadata, ensure_ascii=False, sort_keys=True), record_id),
            )
            retired = self._record(db.execute(
                "SELECT * FROM memory_records WHERE id=?", (record_id,),
            ).fetchone())
            self._snapshot(db, retired, "reclassified")
            return {"health": self._receipt(retired, "reclassified"), "anchor": anchor}

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

    def render(self, kind, query="", max_chars=None, verified_only=False):
        """Render whole current facts, never arbitrary character/recency slices.

        Preferences and health constraints cannot be budget-evicted. Anchors rank
        *all* entity keys by query relevance, then recency, and disclose omissions.
        Superseded revisions remain auditable but are never current capabilities.
        Anchor-only ``verified_only`` excludes non-user or unverified records,
        disclosing their count without reproducing their claims. It does not
        change stored records or imply independent verification of user reports.
        """
        _check_kind(kind)
        if not isinstance(query, str):
            raise ValueError("Memory query must be a string.")
        if max_chars is not None and (type(max_chars) is not int or max_chars < 0):
            raise ValueError("max_chars must be a nonnegative integer or None.")
        if not isinstance(verified_only, bool):
            raise ValueError("verified_only must be a boolean.")
        if verified_only and kind != "anchor":
            raise ValueError("verified_only is anchor-only; standing constraints cannot be hidden.")
        records = self.records(kind)
        if not records:
            return ""
        excluded = 0
        if verified_only:
            trusted = [record for record in records
                       if record["verified"] and record["source_type"] == "user"]
            excluded = len(records) - len(trusted)
            records = trusted
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
        if excluded:
            header += (
                f"\n[{excluded} active anchor records excluded by verified-only filter "
                "(unverified or non-user provenance).]"
            )
        lines = []
        for record in records:
            provenance = ("user-reported" if record["verified"] else
                          "UNVERIFIED " + record["source_type"])
            lines.append(
                f"- [{record['key']} | {record['date'] or 'date unknown'} | "
                f"{provenance} | revision {record['revision']}] {record['text']}"
            )
        full = "\n".join([header] + lines)
        if max_chars is None or len(full) <= max_chars:
            return full
        if kind != "anchor" or not lines:
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
        An upsert saves complete contiguous source context, including dependent
        sentences, not a model paraphrase or a fragment stripped of negation.
        Positive tolerance belongs in anchors, not active health flags. Family
        avoidance requires explicit whole-family intent. No matching current
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
                    if kind == "health" and _positive_health_report(evidence):
                        raise ValueError(
                            "Positive exercise-tolerance or symptom-free testimony is not an "
                            "active health constraint. Re-emit kind='anchor' with the named "
                            "movement key and the verbatim source_quote, preserving its scope. "
                            "Do not resolve health unless the source explicitly clears that "
                            "named condition; exercise tolerance alone does not."
                        )
                    if kind == "preference" and key.startswith("avoid_family:") and not (
                            _family_avoidance_supported(key, evidence)):
                        raise ValueError(
                            "avoid_family requires explicit current user intent to avoid the "
                            "named whole family/all variants. Quote that request, or use "
                            "separate avoid_exercise keys for the exact excluded movements; "
                            "a suggestion or hypothetical option is not permission."
                        )
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
