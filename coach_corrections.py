"""Source-linked workout corrections, separate from unmodified Garmin records.

Only affirmative, completed rep corrections are parsed. Loads are deliberately not
supported: neither missing units nor per-hand conventions can be inferred here.
No model, network, environment or private-file access occurs at import.

Integration::

    store = CorrectionStore(private_db_path)
    if correction_intent(user_text):
        result = store.process_report(user_text, last_known_raw_activities,
                                      request_id=telegram_update_id, observed_on=today)
        # Refresh only result["candidate_activity_ids"]; save raw returned sets.
    effective_activities = store.apply(raw_activities)

Activities can be ordinary activity dicts or TrainingStore's {activity_id,payload}
rows. A complete sequence in the first logged_sets group is required. An optional
reference={activity_id,exercise,sequence_index} identifies a zero-based position
in the complete chronological sequence, including REST entries.
``source_quote`` is the verbatim correction sentence plus required antecedents,
not unrelated chat. The full message remains in the request audit and is exposed
as ``request_source_text`` by process_report/history.
"""

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _words(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).isoformat()


_COMPLETED_CORRECTION = re.compile(r"\b(?:corrected|edited|changed|fixed)\b", re.I)
_RECORDED_REPS = re.compile(
    r"\b(?:recorded|logged|counted|detected)\s+(?:as\s+)?(\d{1,3})\s+"
    r"(?:reps?|repetitions?)\b", re.I)


def _parse_clause(text):
    """Validate one complete correction context, including any attached guard."""
    lowered = text.casefold().replace("’", "'")
    if ("?" in text or re.search(
            r"\b(?:if|unless|would|could|should|might|maybe|perhaps|will|shall|"
            r"plan|planning|intend|intending|hypothetical|hypothetically|suppose|assuming|"
            r"imagine|tomorrow)\b|\bfor\s+example\b"
            r"|\bgoing\s+to\b|\b(?:do|did|have|can)\s+(?:i|you)\b"
            r"|\b(?:what|why|how|whether)\b|\b(?:i'll|we'll)\b"
            r"|\b(?:not|never)\s+(?:\w+\s+){0,3}(?:correct|edit|change)"
            r"|\b(?:not|never)\s+(?:do|done|happen|happened|true|accurate)\b"
            r"|\bnot\s+(?:sure|certain)\b"
            r"|\b(?:this|that|it)\s+(?:is|was)\s+not\b"
            r"|\b(?:didn't|haven't|hadn't|wasn't|isn't|don't|hasn't)\b",
            lowered)):
        return None
    if not _COMPLETED_CORRECTION.search(lowered):
        return None
    if not re.search(r"\b(?:reps?|repetitions?|rep\s+count)\b", lowered):
        return None
    if re.search(r"\b(?:kg|kilograms?|lbs?|pounds?|weight|load)\b", lowered):
        return None
    pairs = set()
    direct_reports = 0
    for match in re.finditer(
            r"\b(?:corrected|edited|changed|fixed)\b[^.!?;\n]{0,90}?"
            r"(?:from\s+)?(\d{1,3})\s*(?:reps?|repetitions?)?\s+to\s+"
            r"(\d{1,3})(?!\d|\.\d)", lowered):
        direct_reports += 1
        pairs.add((int(match[1]), int(match[2])))
    recorded = list(_RECORDED_REPS.finditer(lowered))
    changed = list(re.finditer(
        r"\b(?:corrected|edited|changed|fixed)\s+(?:it\s+|them\s+|that\s+|"
        r"(?:the\s+)?(?:reps?|rep\s+count)\s+)?to\s+(\d{1,3})(?!\d|\.\d)", lowered))
    if len(recorded) == len(changed) == 1 and recorded[0].start() < changed[0].start():
        pairs.add((int(recorded[0][1]), int(changed[0][1])))
    if not pairs:
        return None
    if len(pairs) != 1 or direct_reports > 1 or re.search(r"\bor\s+\d", lowered):
        return {"ambiguous": True}
    old, new = pairs.pop()
    if old == new or not (0 <= old <= 200 and 0 <= new <= 200):
        return None
    return {"field": "reps", "old_value": old, "corrected_value": new}


def _sentence_spans(text):
    """Keep verbatim offsets and terminal punctuation; never split decimal values."""
    spans, start = [], 0
    for boundary in re.finditer(r"[.!?]+(?=\s|$)|\n+", text):
        end = boundary.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _attached_guard(text):
    """Do not strip an adjacent negation/hypothetical referring to this correction."""
    lowered = text.casefold().replace("’", "'")
    if re.search(r"(?<![a-z])(?:weight|load|kg|kilograms?|lbs?|pounds?)\b", lowered):
        return False
    explicit_topic = re.search(
        r"\b(?:correct(?:ion|ed|ing)?|edit(?:ed|ing)?|change[ds]?|fix(?:ed)?|"
        r"reps?|repetitions?)\b", lowered)
    deictic = re.search(r"\b(?:this|that|it|following)\b", lowered)
    veracity = re.search(
        r"\b(?:hypothetical|hypothetically|true|accurate|right|happen(?:ed)?)\b", lowered)
    action_denial = re.fullmatch(
        r"\s*(?:i|we)\s+(?:(?:have|had|did)\s+not|haven't|hadn't|didn't)\s+"
        r"(?:actually\s+)?(?:do|done)\s+(?:this|that|it)(?:\s+yet)?[.!]\s*", lowered)
    topic = explicit_topic or (deictic and veracity) or action_denial
    guarded = re.search(
        r"\b(?:not|never|hypothetical|hypothetically|suppose|assuming|imagine|"
        r"if|unless|would|could|should|might|maybe|perhaps|plan|planning)\b"
        r"|\b(?:didn't|haven't|hadn't|wasn't|isn't|don't|hasn't)\b"
        r"|\bfor\s+example\b", lowered)
    question = ("?" in text and topic and re.search(
        r"\b(?:correct(?:ion|ed)?|edit(?:ed)?|change[ds]?|fix(?:ed)?|"
        r"reps?|repetitions?|right|true|accurate|happen(?:ed)?)\b", lowered))
    frame = re.fullmatch(r"\s*(?:for example|hypothetically|suppose|imagine)[.:]\s*", lowered)
    return bool(topic and guarded or question or frame)


def _parse_report(text):
    """Isolate correction evidence instead of interpreting unrelated chat as its scope."""
    if not isinstance(text, str) or not text.strip():
        return None
    spans = _sentence_spans(text)
    reports, seen = [], set()
    for index, (start, end) in enumerate(spans):
        sentence = text[start:end]
        if not _COMPLETED_CORRECTION.search(sentence):
            continue
        first, last = index, index
        needs_source = _parse_clause(sentence) is None
        while first > 0:
            previous = text[slice(*spans[first - 1])]
            source = (needs_source and _RECORDED_REPS.search(previous)
                      and not _COMPLETED_CORRECTION.search(previous))
            topic_intro = re.fullmatch(
                r"\s*(?:for|on|about|regarding)\s+(?:the\s+|my\s+)?"
                r"[a-z _-]+[.:]\s*", previous, re.I)
            if not (source or topic_intro or _attached_guard(previous)):
                break
            first -= 1
            if source:
                needs_source = False
        while last + 1 < len(spans) and _attached_guard(text[slice(*spans[last + 1])]):
            last += 1
        scoped = (spans[first][0], spans[last][1])
        if scoped in seen:
            continue
        seen.add(scoped)
        quote = text[slice(*scoped)].strip()
        report = _parse_clause(quote)
        if report:
            reports.append({**report, "source_quote": quote})
    if not reports:
        return None
    if len(reports) > 1:
        return {"ambiguous": True, "source_quote": text,
                "correction_quotes": [report["source_quote"] for report in reports]}
    return reports[0]


def correction_intent(source_text):
    """Recognize an explicit report even if Garmin no longer contains the old value."""
    return _parse_report(source_text) is not None


def _body(activity):
    payload = activity.get("payload")
    return payload if isinstance(payload, dict) else activity


def _source_sequence(activity):
    groups = _body(activity).get("logged_sets") or []
    if not isinstance(groups, list) or not groups or not isinstance(groups[0], dict):
        return []
    sequence = groups[0].get("set_sequence")
    return sequence if isinstance(sequence, list) else []


def _source_set(point):
    # Reapplying an overlay must start with source values, not last render's correction.
    return copy.deepcopy(point.get("garmin_original_set") or point)


def _points(activities):
    found = []
    seen = set()
    for activity in activities or []:
        if not isinstance(activity, dict):
            continue
        body = _body(activity)
        aid = body.get("activity_id", activity.get("activity_id"))
        if aid is None:
            continue
        for position, point in enumerate(_source_sequence(activity)):
            if not isinstance(point, dict):
                continue
            raw = _source_set(point)
            if raw.get("set_type") != "ACTIVE":
                continue
            target = {"activity_id": str(aid), "exercise": _words(raw.get("exercise", "")),
                      "sequence_index": raw.get("sequence_index", position),
                      "source_index": raw.get("source_index"),
                      "start_time": raw.get("start_time"),
                      "message_index": raw.get("message_index")}
            key = (_json(target), _json(raw))
            if key not in seen:
                found.append({"target": target, "raw": raw, "activity": activity,
                              "date": str(body.get("start") or activity.get("date") or "")[:10]})
                seen.add(key)
    return found


def _same_target(stored, current):
    if any(stored.get(k) != current.get(k) for k in ("activity_id", "exercise")):
        return False
    if stored.get("start_time"):
        return stored["start_time"] == current.get("start_time")
    if stored.get("message_index") is not None:
        return stored["message_index"] == current.get("message_index")
    return (stored.get("source_index") == current.get("source_index")
            and stored.get("sequence_index") == current.get("sequence_index"))


def _constraints(text, points, reference, observed_on):
    selected = points
    normalized = _words(text)
    if reference:
        allowed = {"activity_id", "exercise", "sequence_index"}
        if set(reference) - allowed:
            raise ValueError("Unknown correction reference field.")
        for key, value in reference.items():
            expected = _words(value) if key == "exercise" else str(value) if key == "activity_id" else value
            selected = [p for p in selected if p["target"].get(key) == expected]
    activity_ids = re.findall(r"\bactivity(?:\s+id)?\s*[:#]?\s*(\d+)\b", text, re.I)
    if activity_ids:
        selected = [p for p in selected if p["target"]["activity_id"] in activity_ids]
    names = {p["target"]["exercise"] for p in points}
    mentioned = {name for name in names if name and re.search(r"\b" + re.escape(name) + r"\b", normalized)}
    # Prefer the most-specific full name: "bench press" must not also select "press".
    mentioned = {name for name in mentioned if not any(
        name != other and name in other for other in mentioned)}
    if mentioned:
        selected = [p for p in selected if p["target"]["exercise"] in mentioned]
    explicit_names = []
    prefix = re.match(r"\s*([a-z][a-z _-]+)\s*:", text, re.I)
    if prefix and _words(prefix[1]) not in ("correction", "rep correction", "watch", "garmin"):
        explicit_names.append(_words(prefix[1]))
    for match in re.finditer(
            r"\b(?:my|the)\s+([a-z][a-z _-]*?)\s+(?:reps?|repetitions?)\s+"
            r"(?:were|was|are|got)\b", text, re.I):
        explicit_names.append(_words(match[1]))
    for match in re.finditer(
            r"\b(?:corrected|edited|changed|fixed)\s+(?:my\s+|the\s+)?"
            r"([a-z][a-z _-]*?)\s+from\s+\d", text, re.I):
        name = re.sub(r"\s+(?:reps?|repetitions?|rep count)$", "", _words(match[1]))
        if name not in ("it", "them", "that", "reps", "rep", "rep count", "repetitions"):
            explicit_names.append(name)
    for match in re.finditer(
            r"\b(?:for|on)\s+(?:my\s+|the\s+)?([a-z][a-z _-]*?)"
            r"(?=\s+(?:from|set|reps|was|were|I)\b|[.,;!?]|$)", text, re.I):
        name = _words(match[1])
        if name not in ("watch", "garmin", "garmin connect"):
            explicit_names.append(name)
    if explicit_names:
        selected = [p for p in selected if all(
            p["target"]["exercise"] == name for name in explicit_names)]
    days = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if not days and re.search(r"\byesterday\b", text, re.I):
        days = [(date.fromisoformat(observed_on) - timedelta(days=1)).isoformat()]
    if not days and re.search(r"\btoday\b", text, re.I):
        days = [observed_on]
    if days:
        selected = [p for p in selected if p["date"] in days]
    ordinal = re.search(r"\bset\s*#?\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+set\b", text, re.I)
    if ordinal and not (reference and "sequence_index" in reference):
        number = int(ordinal[1] or ordinal[2])
        counters, narrowed = {}, []
        for point in selected:
            aid = point["target"]["activity_id"]
            counters[aid] = counters.get(aid, 0) + 1
            if counters[aid] == number:
                narrowed.append(point)
        selected = narrowed
    return selected


def _summarize(groups):
    """Rebuild only ACTIVE grouped measurements; retain order, units and all REST rows."""
    sequence = groups[0]["set_sequence"]
    for group in groups:
        name = _words(group.get("exercise", ""))
        points = [p for p in sequence if p.get("set_type") == "ACTIVE"
                  and _words(p.get("exercise", "")) == name]
        if not points:
            continue
        corrections = [copy.deepcopy(p["user_correction"]) for p in points if p.get("user_correction")]
        if corrections:
            group["user_corrections"] = corrections
            group.setdefault("garmin_original_summary", {
                key: copy.deepcopy(group.get(key)) for key in
                ("sets", "reps", "top_weight_kg", "set_indices")})
        reps = [p["reps"] for p in points if isinstance(p.get("reps"), (int, float))
                and not isinstance(p["reps"], bool) and p["reps"] >= 0]
        weights = [p["weight_kg"] for p in points if isinstance(p.get("weight_kg"), (int, float))
                   and not isinstance(p["weight_kg"], bool) and p["weight_kg"] >= 0]
        group["sets"] = len(points)
        group["reps"] = (str(int(reps[0])) if reps and min(reps) == max(reps) else
                         f"{int(min(reps))}-{int(max(reps))}" if reps else None)
        group["top_weight_kg"] = max(weights) if weights else None
        group["set_indices"] = [p["sequence_index"] for p in points]


class CorrectionStore:
    """Namespaced SQLite audit store; safe to share TrainingStore's database file."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        if not self.db_path or self.db_path == ":memory:":
            raise ValueError("Provide a persistent SQLite path.")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS workout_correction_requests (
                request_id TEXT PRIMARY KEY, source_text TEXT NOT NULL,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS workout_corrections (
                id INTEGER PRIMARY KEY, target_json TEXT NOT NULL, field TEXT NOT NULL,
                original_value INTEGER NOT NULL, corrected_value INTEGER NOT NULL,
                original_set_json TEXT NOT NULL, source_quote TEXT NOT NULL,
                observed_on TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                supersedes INTEGER REFERENCES workout_corrections(id),
                status TEXT NOT NULL CHECK(status IN ('active','superseded')),
                created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS workout_correction_observations (
                correction_id INTEGER NOT NULL REFERENCES workout_corrections(id),
                evidence_hash TEXT NOT NULL, evidence_json TEXT NOT NULL,
                observed_at TEXT NOT NULL, PRIMARY KEY(correction_id,evidence_hash))""")

    @contextmanager
    def _connection(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _record(row):
        record = dict(row)
        record["target"] = json.loads(record.pop("target_json"))
        record["original_set"] = json.loads(record.pop("original_set_json"))
        record["source_type"] = "user_reported_correction"
        record["measurement_verified"] = False
        return record

    def process_report(self, source_text, activities, request_id, observed_on=None, reference=None):
        """Save only one uniquely source-linked correction; ambiguity changes no outcome.

        Resolve against last-known raw detail before refreshing Garmin. Requests are
        idempotent, including clarification outcomes; a clarified user turn needs its
        own request ID. Exact references may resolve a now-matching source record.
        Parse and target constraints use the same scoped quote; the request audit
        retains source_text in full, including unrelated sentences.
        """
        if request_id is None or not str(request_id).strip():
            raise ValueError("A stable request_id is required.")
        if not isinstance(source_text, str):
            raise ValueError("source_text must be the verbatim user report.")
        if reference and "sequence_index" in reference and (
                isinstance(reference["sequence_index"], bool)
                or not isinstance(reference["sequence_index"], int)
                or reference["sequence_index"] < 0):
            raise ValueError("sequence_index must be a nonnegative integer.")
        request_id = str(request_id)
        day = (observed_on.date() if isinstance(observed_on, datetime) else observed_on)
        day = date.fromisoformat(str(day or date.today())).isoformat()
        report = _parse_report(source_text)
        with self._connection(write=True) as db:
            prior = db.execute("SELECT source_text,result_json FROM workout_correction_requests "
                               "WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                if prior["source_text"] != source_text:
                    raise ValueError("request_id was already used for a different source report.")
                result = json.loads(prior["result_json"])
                result["replayed"] = True
                return result
            result = {"saved": False, "needs_clarification": False, "candidate_activity_ids": [],
                      "candidates": [], "correction": None, "replayed": False,
                      "reason": "not_an_explicit_completed_rep_correction"}
            if report:
                points = _points(activities)
                scoped = _constraints(report["source_quote"], points, reference, day)
                active = [self._record(row) for row in db.execute(
                    "SELECT * FROM workout_corrections WHERE status='active'")]
                matches = []
                for point in scoped:
                    previous = [r for r in active if _same_target(r["target"], point["target"])]
                    effective = previous[0]["corrected_value"] if len(previous) == 1 else point["raw"].get("reps")
                    if (not report.get("ambiguous") and
                            (effective == report["old_value"] or
                             (reference and all(k in reference for k in
                              ("activity_id", "exercise", "sequence_index"))
                              and point["raw"].get("reps") == report["corrected_value"]))):
                        matches.append(point)
                candidates = matches or scoped
                result["candidate_activity_ids"] = list(dict.fromkeys(
                    p["target"]["activity_id"] for p in candidates))
                result["candidates"] = [
                    {**p["target"], "recorded_reps": p["raw"].get("reps"), "date": p["date"]}
                    for p in candidates]
                result["needs_clarification"] = True
                result["reason"] = ("multiple_reports" if report.get("ambiguous") else
                                    "multiple_matching_sets" if len(matches) > 1 else "no_matching_source_set")
                unique_identity = (len(matches) == 1 and sum(
                    _same_target(matches[0]["target"], p["target"]) for p in points) == 1)
                if len(matches) == 1 and not unique_identity:
                    result["reason"] = "ambiguous_source_identity"
                if unique_identity and not report.get("ambiguous"):
                    point = matches[0]
                    previous = [r for r in active if _same_target(r["target"], point["target"])]
                    if len(previous) > 1:
                        raise ValueError("Multiple active corrections address the same source set.")
                    supersedes = previous[0]["id"] if previous else None
                    if supersedes:
                        db.execute("UPDATE workout_corrections SET status='superseded' WHERE id=?",
                                   (supersedes,))
                    cursor = db.execute(
                        "INSERT INTO workout_corrections(target_json,field,original_value,"
                        "corrected_value,original_set_json,source_quote,observed_on,request_id,"
                        "supersedes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,'active',?)",
                        (_json(point["target"]), report["field"], report["old_value"],
                         report["corrected_value"], _json(point["raw"]), report["source_quote"], day,
                         request_id, supersedes, _now()))
                    record = self._record(db.execute(
                        "SELECT * FROM workout_corrections WHERE id=?", (cursor.lastrowid,)).fetchone())
                    record["request_source_text"] = source_text
                    result.update(saved=True, needs_clarification=False, reason="saved_user_report",
                                  correction=record)
            db.execute("INSERT INTO workout_correction_requests VALUES (?,?,?,?)",
                       (request_id, source_text, _json(result), _now()))
            return result

    def apply(self, activities):
        """Return a repeatable deep-copy overlay, never write back Garmin measurements."""
        result = copy.deepcopy(activities or [])
        for activity in result:
            body = _body(activity)
            body.pop("correction_warnings", None)
            for group in body.get("logged_sets") or []:
                original = group.pop("garmin_original_summary", None)
                if original:
                    group.update(original)
                group.pop("user_corrections", None)
            sequence = _source_sequence(activity)
            for index, point in enumerate(sequence):
                if isinstance(point, dict) and point.get("garmin_original_set"):
                    sequence[index] = _source_set(point)
        points = _points(result)
        with self._connection(write=True) as db:
            records = [self._record(row) for row in db.execute(
                "SELECT * FROM workout_corrections WHERE status='active' ORDER BY id")]
            for record in records:
                matches = [p for p in points if _same_target(record["target"], p["target"])]
                if len(matches) != 1:
                    for activity in result:
                        body = _body(activity)
                        aid = body.get("activity_id", activity.get("activity_id"))
                        if str(aid) == record["target"]["activity_id"]:
                            body["correction_warnings"] = [{
                                "correction_id": record["id"], "status": "source_set_unresolved",
                                "source_quote": record["source_quote"],
                                "reason": "Source identity changed or became ambiguous; no overlay applied."}]
                    continue
                point = matches[0]
                groups = _body(point["activity"])["logged_sets"]
                sequence = groups[0]["set_sequence"]
                index = next(i for i, row in enumerate(sequence)
                             if row.get("sequence_index", i) == point["target"]["sequence_index"])
                raw = point["raw"]
                fresh = groups[0].get("data_freshness") or {}
                unavailable = fresh.get("status") in ("stale", "error", "deferred", "unavailable")
                same = raw.get(record["field"]) == record["corrected_value"]
                evidence = {
                    "status": ("cached_source_matches_refresh_unavailable" if same and unavailable else
                               "reconciled" if same else
                               "source_conflict_refresh_unavailable" if unavailable else "source_conflict"),
                    "source_record_matches": same,
                    "current_garmin_value": raw.get(record["field"]),
                    "user_reported_value": record["corrected_value"],
                    "source_freshness": copy.deepcopy(fresh),
                    "measurement_verified": False,
                }
                annotated = copy.deepcopy(raw)
                annotated["garmin_original_set"] = copy.deepcopy(raw)
                annotated[record["field"]] = record["corrected_value"]
                annotated["user_correction"] = {
                    "correction_id": record["id"], "field": record["field"],
                    "original_reported_value": record["original_value"],
                    "corrected_value": record["corrected_value"],
                    "source_type": "user_reported_correction", "source_quote": record["source_quote"],
                    "observed_on": record["observed_on"], "supersedes": record["supersedes"],
                    **evidence,
                }
                sequence[index] = annotated
                _summarize(groups)
                audit_evidence = copy.deepcopy(evidence)
                audit_evidence["source_freshness"].pop("cache_age_hours", None)
                digest = hashlib.sha256(_json(audit_evidence).encode("utf-8")).hexdigest()
                db.execute("INSERT OR IGNORE INTO workout_correction_observations VALUES (?,?,?,?)",
                           (record["id"], digest, _json(evidence), _now()))
        return result

    def history(self, activity_id=None):
        """Original source snapshots, superseded reports and observed reconciliation evidence."""
        with self._connection() as db:
            records = [self._record(row) for row in db.execute("SELECT * FROM workout_corrections ORDER BY id")]
            if activity_id is not None:
                records = [r for r in records if r["target"]["activity_id"] == str(activity_id)]
            for record in records:
                request = db.execute(
                    "SELECT source_text FROM workout_correction_requests WHERE request_id=?",
                    (record["request_id"],)).fetchone()
                record["request_source_text"] = request["source_text"] if request else None
                record["observations"] = [
                    {**json.loads(row["evidence_json"]), "observed_at": row["observed_at"]}
                    for row in db.execute(
                        "SELECT evidence_json,observed_at FROM workout_correction_observations "
                        "WHERE correction_id=? ORDER BY observed_at,evidence_hash", (record["id"],))]
            return records
