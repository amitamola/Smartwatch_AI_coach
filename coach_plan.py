"""Versioned training intent, recorded outcomes, and conservative output checks.

This is bookkeeping and constraint enforcement, not a medical readiness engine.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta


PLAN_MARKER = re.compile(r"\[\[SESSION_PLAN:\s*(.*?)\]\]", re.DOTALL | re.IGNORECASE)


def canonical_exercise(name):
    name = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
    name = re.sub(r"\brdl\b", "romanian deadlift", name)
    name = re.sub(r"\b(?:standard|normal|conventional)\b", "", name)
    if re.search(r"\bdeadlifts?\b", name):
        name = re.sub(r"\bdeadlifts\b", "deadlift", name)
        name = re.sub(r"\b(?:with\s+)?(?:dumbbells?|barbells?|db)\b", "", name)
    return " ".join(name.split())


def avoided_exercises(preferences):
    result = set()
    for record in preferences:
        key = record.get("key", "")
        match = re.match(r"avoid[_ -]exercise[:_ -]+(.+)", key, re.IGNORECASE)
        if match and record.get("status", "active") == "active":
            result.add(canonical_exercise(match.group(1)))
    return result


def parse_plans(text, preferences=(), today=None):
    today = today or date.today()
    plans, errors = [], []
    forbidden = avoided_exercises(preferences)
    for raw in PLAN_MARKER.findall(text or ""):
        try:
            plan = json.loads(raw)
            if not isinstance(plan, dict):
                raise ValueError("plan must be an object")
            day = date.fromisoformat(plan["date"])
            if not today <= day <= today + timedelta(days=28):
                raise ValueError("new plans must be dated today through 28 days ahead")
            if plan.get("kind") not in ("rest", "recovery", "strength", "cardio", "mixed"):
                raise ValueError("unknown session kind")
            if plan.get("detail_level", "prescription") not in ("outline", "prescription"):
                raise ValueError("unknown plan detail level")
            if not isinstance(plan.get("objective"), str) or not plan["objective"].strip():
                raise ValueError("plan needs an objective")
            if not isinstance(plan.get("reason"), str) or not plan["reason"].strip():
                raise ValueError("plan needs a reason based on the available evidence")
            exercises = plan.get("exercises", [])
            if not isinstance(exercises, list):
                raise ValueError("exercises must be a list")
            if plan["kind"] == "rest" and exercises:
                raise ValueError("a rest day cannot contain a prescribed workout")
            for exercise in exercises:
                if (not isinstance(exercise, dict) or not isinstance(exercise.get("name"), str)
                        or not exercise["name"].strip()):
                    raise ValueError("every exercise needs a name")
                for field in ("sets", "blocks"):
                    if not isinstance(exercise.get(field, []), list):
                        raise ValueError(field + " must be a list")
                if not all(isinstance(item, dict) for item in exercise.get("sets", [])):
                    raise ValueError("every prescribed set must be an object")
                if plan.get("detail_level") == "outline" and (
                        exercise.get("sets") or exercise.get("blocks")):
                    raise ValueError("an outline must not contain placeholder working sets or blocks")
                if canonical_exercise(exercise["name"]) in forbidden:
                    raise ValueError("plan contains a movement the user explicitly excluded")
            plans.append(plan)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(str(exc))
    if len({p["date"] for p in plans}) != len(plans):
        errors.append("multiple competing plans for the same date")
    return PLAN_MARKER.sub("", text or "").rstrip(), plans, errors


def render_plans(plans):
    """Render the validated prescription; hidden JSON must not hide working sets."""
    sections = []
    for plan in plans:
        kind = plan["kind"] + (", provisional outline" if plan.get("detail_level") == "outline" else "")
        lines = [f"Session plan - {plan['date']} ({kind})",
                 "Goal: " + plan["objective"], "Reason: " + plan["reason"]]
        for number, exercise in enumerate(plan.get("exercises", []), 1):
            lines.append(f"{number}. {exercise['name']}")
            basis = exercise.get("weight_basis", "unspecified")
            for index, prescription in enumerate(exercise.get("sets", []), 1):
                fields = []
                for key, value in prescription.items():
                    if key == "weight_kg":
                        fields.append("load unspecified" if value is None else
                                      f"{value} kg ({str(basis).replace('_', ' ')})")
                    elif key == "rest_seconds":
                        fields.append(f"rest {value} s" if value is not None else "rest unspecified")
                    elif key == "reps":
                        fields.append(f"{value} reps" if value is not None else "reps unspecified")
                    else:
                        fields.append(key.replace("_", " ") + ": " +
                                      (str(value) if value is not None else "unspecified"))
                lines.append(f"   Set {index}: " + ("; ".join(fields) or "details unspecified"))
            for index, block in enumerate(exercise.get("blocks", []), 1):
                detail = ("; ".join(str(key).replace("_", " ") + ": " + str(value)
                                    for key, value in block.items())
                          if isinstance(block, dict) else str(block))
                lines.append(f"   Block {index}: " + detail)
            if exercise.get("effort"):
                lines.append("   Effort: " + str(exercise["effort"]))
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def unsupported_claims(text):
    pattern = (r"\b(?:zero[- ]spinal[- ]load|zero (?:spinal|back) stress|"
               r"guaranteed back[- ]safe|proves? (?:no|zero) muscle loss)\b")
    problems = []
    for match in re.finditer(pattern, text or "", re.IGNORECASE):
        prefix = text[max(0, match.start() - 70):match.start()].lower()
        if not re.search(r"\b(?:not|never|no|cannot|can't|isn't|avoid claiming|don't claim)\b",
                         prefix):
            problems.append("unsupported physiological certainty: " + match.group(0))
    return problems


class TrainingStore:
    def __init__(self, db_path):
        self.path = str(db_path)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS training_plans (
                    date TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS training_plan_events (
                    id INTEGER PRIMARY KEY, date TEXT NOT NULL, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL, source TEXT NOT NULL, recorded_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS training_outcomes (
                    activity_id TEXT PRIMARY KEY, date TEXT NOT NULL, payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS training_day_status (
                    date TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL);
                INSERT OR IGNORE INTO training_day_status
                    SELECT date,status,updated_at FROM training_plans
                    WHERE status IN ('user_completed','user_skipped','user_planned');
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, plans, source="model_proposal"):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connection() as db:
            for plan in plans:
                payload = json.dumps(plan, sort_keys=True, ensure_ascii=False)
                old = db.execute("SELECT payload,revision,status FROM training_plans WHERE date=?",
                                 (plan["date"],)).fetchone()
                if old and old[0] == payload:
                    continue
                revision = old[1] + 1 if old else 1
                report = db.execute("SELECT status FROM training_day_status WHERE date=?",
                                    (plan["date"],)).fetchone()
                day_status = report[0] if report else (old[2] if old else "proposed")
                db.execute(
                    "INSERT INTO training_plans VALUES (?,?,?,?,?) "
                    "ON CONFLICT(date) DO UPDATE SET payload=excluded.payload,"
                    "revision=excluded.revision,status=excluded.status,"
                    "updated_at=excluded.updated_at",
                    (plan["date"], payload, revision, day_status, now))
                db.execute("INSERT INTO training_plan_events(date,payload,revision,source,"
                           "recorded_at) VALUES (?,?,?,?,?)",
                           (plan["date"], payload, revision, source, now))

    def set_status(self, day, status):
        if status not in ("user_completed", "user_skipped", "user_planned"):
            raise ValueError("unknown training status")
        day = date.fromisoformat(str(day)).isoformat()
        now = datetime.now().isoformat(timespec="seconds")
        with self.connection() as db:
            db.execute("INSERT INTO training_day_status VALUES (?,?,?) "
                       "ON CONFLICT(date) DO UPDATE SET status=excluded.status,"
                       "updated_at=excluded.updated_at", (day, status, now))
            db.execute("UPDATE training_plans SET status=?,updated_at=? WHERE date=?",
                       (status, now, day))

    def record_activities(self, activities):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connection() as db:
            for activity in activities or []:
                aid = activity.get("activity_id")
                start = str(activity.get("start", ""))[:10]
                if aid is None or not start:
                    continue
                old = db.execute("SELECT payload FROM training_outcomes WHERE activity_id=?",
                                 (str(aid),)).fetchone()
                payload = json.loads(old[0]) if old else {}
                payload.update(activity)
                # A lightweight activity poll must not erase previously observed set detail.
                if old and not activity.get("logged_sets"):
                    prior_sets = json.loads(old[0]).get("logged_sets")
                    if prior_sets:
                        payload["logged_sets"] = prior_sets
                db.execute(
                    "INSERT INTO training_outcomes VALUES (?,?,?,?) "
                    "ON CONFLICT(activity_id) DO UPDATE SET payload=excluded.payload,"
                    "observed_at=excluded.observed_at",
                    (str(aid), start, json.dumps(payload, ensure_ascii=False, default=str), now))

    def context(self, today=None):
        today = today or date.today()
        start = (today - timedelta(days=28)).isoformat()
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            plans = [dict(r) for r in db.execute(
                "SELECT date,payload,revision,status FROM training_plans WHERE date>=? "
                "ORDER BY date", ((today - timedelta(days=7)).isoformat(),))]
            outcomes = [dict(r) for r in db.execute(
                "SELECT activity_id,date,payload FROM training_outcomes WHERE date>=? "
                "ORDER BY date,activity_id", (start,))]
            day_status = [dict(r) for r in db.execute(
                "SELECT date,status,updated_at FROM training_day_status WHERE date>=? "
                "ORDER BY date", (start,))]
        for row in plans + outcomes:
            row["payload"] = json.loads(row["payload"])
        outcomes.sort(key=lambda row: (
            str(row["payload"].get("start") or row["date"]).replace(" ", "T"),
            row["activity_id"]))
        for row in plans:
            row["proposal_status"] = "proposed"
            row["status_meaning"] = "last explicit user report for the day, not proof this revision was completed"
        # Keep exact latest observed sessions per movement across the four-week window.
        movements, weekly_sets = {}, {}
        week_start = (today - timedelta(days=6)).isoformat()
        missing_strength_detail = 0
        for row in outcomes:
            sets = row["payload"].get("logged_sets") or []
            if (row["date"] >= week_start and "strength" in str(row["payload"].get("type", ""))
                    and not sets):
                missing_strength_detail += 1
            for exercise in sets:
                key = canonical_exercise(exercise.get("exercise", "unknown"))
                movements[key] = {"date": row["date"], "activity_id": row["activity_id"],
                                  "recorded_sets": exercise,
                                  "effort_or_pain": "unknown unless explicitly reported"}
                count = exercise.get("sets")
                if row["date"] >= week_start and isinstance(count, int) and not isinstance(count, bool):
                    weekly_sets[key] = weekly_sets.get(key, 0) + count
        return {
            "plans": plans,
            "user_reported_day_status": day_status,
            "recent_outcomes": outcomes[-12:],
            "recent_outcomes_omitted": max(0, len(outcomes) - 12),
            "outcome_count_28d": len(outcomes),
            "latest_observed_per_movement": movements,
            "weekly_recorded_sets": {
                "window_start": week_start, "window_end": today.isoformat(),
                "by_exercise": weekly_sets,
                "strength_activities_missing_set_detail": missing_strength_detail,
                "coverage": "observed records only; not proof of complete history or hard-set volume",
            },
            "interpretation": (
                "Plans are proposals, not evidence of completion. Logged sets do not establish "
                "RPE, good form, pain-free execution, or readiness to increase load. Missing "
                "history is unknown, not rest or zero volume. Use full set chronology, not "
                "category first-appearance order. Revise dated plans explicitly when needed."),
        }
