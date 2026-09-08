"""Versioned training intent, recorded outcomes, and conservative output checks.

This is bookkeeping and constraint enforcement, not a medical readiness engine.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta


PLAN_MARKER = re.compile(r"\[\[SESSION_PLAN:\s*(.*?)\]\]", re.DOTALL | re.IGNORECASE)
PATCH_MARKER = re.compile(r"\[\[SESSION_PATCH:\s*(.*?)\]\]", re.DOTALL | re.IGNORECASE)


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


def exercise_excluded(name, preferences):
    canonical = canonical_exercise(name)
    if canonical in avoided_exercises(preferences):
        return True
    for record in preferences:
        key = record.get("key", "")
        if record.get("status", "active") == "active" and key.startswith("avoid_family:"):
            family = canonical_exercise(key.split(":", 1)[1])
            if family and (" " + family + " ") in (" " + canonical + " "):
                return True
    return False


def parse_plans(text, preferences=(), today=None, existing_plans=()):
    today = today or date.today()
    plans, errors = [], []
    raw_plans = PLAN_MARKER.findall(text or "")
    existing = {row["date"]: row.get("payload", row) for row in existing_plans}
    patches = {}
    for raw in PATCH_MARKER.findall(text or ""):
        try:
            patch = json.loads(raw)
            day = patch["date"]
            if day not in existing:
                raise ValueError("SESSION_PATCH needs an existing dated plan")
            plan = patches.setdefault(day, json.loads(json.dumps(existing[day])))
            matches = [i for i, move in enumerate(plan.get("exercises", []))
                       if canonical_exercise(move["name"]) == canonical_exercise(patch["exercise"])]
            if len(matches) != 1 or not isinstance(patch.get("replacement"), dict):
                raise ValueError("SESSION_PATCH must identify one existing exercise and its replacement")
            if not isinstance(patch.get("reason"), str) or not patch["reason"].strip():
                raise ValueError("SESSION_PATCH needs a reason")
            plan["exercises"][matches[0]] = patch["replacement"]
            plan["reason"] = patch["reason"]
            plan["program_adjustment"] = patch["reason"]
            if "program_revision" in patch:
                plan["program_revision"] = patch["program_revision"]
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(str(exc))
    raw_plans += [json.dumps(plan) for plan in patches.values()]
    for raw in raw_plans:
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
            if plan.get("delivery", "self_directed") not in ("self_directed", "instructor_led"):
                raise ValueError("unknown session delivery")
            if plan.get("delivery") == "instructor_led":
                guidance = plan.get("participation_guidance")
                if (not isinstance(guidance, list) or not 1 <= len(guidance) <= 6
                        or any(not isinstance(g, str) or not g.strip() for g in guidance)):
                    raise ValueError("An instructor-led class needs explicit participation guidance")
                if plan.get("kind") not in ("strength", "cardio", "mixed", "recovery"):
                    raise ValueError("An instructor-led class must name its actual activity kind")
            if "start_time" in plan and (
                    not isinstance(plan["start_time"], str) or not re.fullmatch(
                        r"(?:[01]\d|2[0-3]):[0-5]\d", plan["start_time"])):
                raise ValueError("start_time must use local HH:MM")
            if not isinstance(plan.get("objective"), str) or not plan["objective"].strip():
                raise ValueError("plan needs an objective")
            if not isinstance(plan.get("reason"), str) or not plan["reason"].strip():
                raise ValueError("plan needs a reason based on the available evidence")
            exercises = plan.get("exercises", [])
            if not isinstance(exercises, list):
                raise ValueError("exercises must be a list")
            if plan["kind"] == "rest" and exercises:
                raise ValueError("a rest day cannot contain a prescribed workout")
            if day > today and not exercises:
                plan["detail_level"] = "outline"
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
                if exercise_excluded(exercise["name"], preferences):
                    raise ValueError("plan contains a movement the user explicitly excluded")
            plans.append(plan)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(str(exc))
    if len({p["date"] for p in plans}) != len(plans):
        errors.append("multiple competing plans for the same date")
    return PATCH_MARKER.sub("", PLAN_MARKER.sub("", text or "")).rstrip(), plans, errors


def render_plans(plans):
    """Render the validated prescription; hidden JSON must not hide working sets."""
    sections, upcoming = [], []
    for plan in plans:
        day = date.fromisoformat(plan["date"])
        label = "Today" if day == date.today() else day.strftime("%a %d %b")
        if plan.get("delivery") == "instructor_led" and day == date.today():
            lines = ["**Today · Instructor-led session**",
                     plan["objective"] + (f" · {plan['start_time']}" if plan.get("start_time") else ""),
                     "**Why:** " + plan["reason"],
                     "Class content/intensity must be confirmed; this is not a prescribed set list."]
            lines.extend("- " + g for g in plan["participation_guidance"])
            sections.append("\n".join(lines))
            continue
        if plan.get("detail_level") == "outline" or (day > date.today() and not plan.get("exercises")):
            objective = re.sub(r"\s*\(provisional outline\)", "", plan["objective"],
                               flags=re.IGNORECASE)
            start = f" · {plan['start_time']}" if plan.get("start_time") else ""
            upcoming.append(f"- **{label}{start}**: {objective} (provisional outline)")
            continue
        icon = "\U0001F33F" if plan["kind"] == "rest" else "\U0001F4CB"
        lines = [f"**{icon} {label} \u00b7 {plan['kind'].title()} plan**",
                 plan["objective"], "**Why:** " + plan["reason"]]
        for number, exercise in enumerate(plan.get("exercises", []), 1):
            lines.extend(["", f"**{number}. {exercise['name']}**"])
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
    if upcoming:
        sections.append("**\U0001F5D3\uFE0F Coming up**\n" + "\n".join(upcoming))
    return "\n\n".join(sections)


def unsupported_claims(text):
    pattern = (r"\b(?:zero[- ]spinal[- ]load|zero (?:spinal|back) stress|"
               r"guaranteed back[- ]safe|proves? (?:no|zero) muscle loss|"
               r"(?:without|no) (?:axial |added |any )?(?:spinal|spine|lumbar) "
               r"(?:loading|load|compression|stress|strain|overload)|"
               r"muscular reserves (?:are |remain )?untouched|"
               r"(?:deep )?neural recovery (?:is )?(?:already )?(?:running )?(?:behind|complete))\b")
    semantic_patterns = [
        r"\bproductive\b[^.!?\n]{0,150}\b(?:only|requires?)\b[^.!?\n]{0,100}"
        r"\b(?:continual\w*|constant\w*)\b[^.!?\n]{0,50}\b(?:increas\w*|climb\w*)",
        r"\bproductive\b[^.!?\n]{0,100}\b(?:simply requires?|only requires?|requires? "
        r"(?:ratchet\w*|increas\w*))\b[^.!?\n]{0,100}\bload\b",
        r"\bmaintaining\b[^.!?\n]{0,160}\b(?:proves?|confirms?|guarantees?)\b"
        r"[^.!?\n]{0,100}\b(?:muscl\w*|muscular|lean mass)\b",
        r"\bmaintaining\b[^.!?\n]{0,160}\b(?:protects?|preserv\w*|retains?)\b"
        r"[^.!?\n]{0,100}\b(?:muscl\w*|capacity|lean mass)\b",
        r"\b(?:moderate|readiness)\b[^.!?\n]{0,130}\b"
        r"(?:capacity for light activity|light (?:activity|exercise) only|only light (?:activity|exercise))",
        r"\b\d+\s*hours?\b[^.!?\n]{0,90}\b(?:is|are)\s+(?:categorically |always )?"
        r"insufficient\b[^.!?\n]{0,80}\brecovery\b",
        r"\b(?:reps|repetitions|set)\b[^.!?\n]{0,90}\b(?:keeps?|confirms?|proves?|ensures?|means?)\b"
        r"[^.!?\n]{0,80}\b(?:reserve|RIR|RPE)\b",
        r"\b(?:holding|taking|scheduling)\b[^.!?\n]{0,100}\brest\b"
        r"[^.!?\n]{0,60}\bensures?\b[^.!?\n]{0,100}\brecover\w*",
        r"\b(?:you are|you're|you have been)\s+(?:fully )?cleared\s+to\s+"
        r"(?:join|attend|train|exercise|work out)",
        r"\b(?:systemic|neural|muscular|full)\s+recovery\s+(?:will|should)\s+be\s+"
        r"(?:solid|complete|good|fine)",
        r"\brecovery (?:time|timer)\b[^.!?\n]{0,50}\b(?:will|should)\s+"
        r"(?:clear|(?:be|reach|hit) (?:at )?zero)",
        r"\b(?:[5-7](?:th)?|fifth|sixth|seventh|extra)\s+(?:rolling )?"
        r"(?:session|training day)\s+is\s+(?:fine|safe)\b",
    ]
    problems = []
    for match in re.finditer(pattern + "|" + "|".join(semantic_patterns), text or "", re.IGNORECASE):
        prefix = re.split(r"[.!?;\n]", text[:match.start()])[-1][-100:].lower()
        claim = match.group(0).lower()
        denial = r"\b(?:not|never|cannot|can't|isn't|avoid claiming|don't claim|no guarantee)\b"
        if not re.search(denial, prefix) and not re.search(denial, claim):
            problems.append("unsupported physiological certainty: " + match.group(0))
    return problems


class TrainingStore:
    def __init__(self, db_path):
        from coach_corrections import CorrectionStore
        self.path = str(db_path)
        self.corrections = CorrectionStore(db_path)
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
                CREATE TABLE IF NOT EXISTS training_completion_context (
                    date TEXT PRIMARY KEY, plan_kind_at_report TEXT,
                    plan_revision_at_report INTEGER, reported_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS training_plan_sources (
                    date TEXT NOT NULL, revision INTEGER NOT NULL, request_id TEXT,
                    source_text TEXT NOT NULL, PRIMARY KEY(date,revision));
                INSERT OR IGNORE INTO training_day_status
                    SELECT date,status,updated_at FROM training_plans
                    WHERE status IN ('user_completed','user_skipped','user_planned');
            """)
            legacy_reports = db.execute(
                "SELECT date,updated_at FROM training_day_status WHERE status='user_completed' "
                "AND date NOT IN (SELECT date FROM training_completion_context)").fetchall()
            for day, reported_at in legacy_reports:
                # Same-second or later proposals cannot establish the plan the report referred to.
                earlier = db.execute(
                    "SELECT payload,revision FROM training_plan_events "
                    "WHERE date=? AND recorded_at<? ORDER BY recorded_at DESC,revision DESC LIMIT 1",
                    (day, reported_at)).fetchone()
                if db.execute("SELECT 1 FROM training_plan_events WHERE date=? AND recorded_at=?",
                              (day, reported_at)).fetchone():
                    earlier = None
                db.execute("INSERT INTO training_completion_context VALUES (?,?,?,?)",
                           (day, json.loads(earlier[0]).get("kind") if earlier else None,
                            earlier[1] if earlier else None, reported_at))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, plans, source="model_proposal", request_id=None, source_text=""):
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
                db.execute("INSERT INTO training_plan_sources VALUES (?,?,?,?)",
                           (plan["date"], revision, request_id, source_text))

    def set_status(self, day, status):
        if status not in ("user_completed", "user_skipped", "user_planned"):
            raise ValueError("unknown training status")
        day = date.fromisoformat(str(day)).isoformat()
        now = datetime.now().isoformat(timespec="seconds")
        with self.connection() as db:
            if status == "user_completed":
                proposed = db.execute("SELECT payload,revision FROM training_plans WHERE date=?",
                                      (day,)).fetchone()
                kind = json.loads(proposed[0]).get("kind") if proposed else None
                db.execute(
                    "INSERT OR REPLACE INTO training_completion_context VALUES (?,?,?,?)",
                    (day, kind, proposed[1] if proposed else None, now))
            db.execute("INSERT INTO training_day_status VALUES (?,?,?) "
                       "ON CONFLICT(date) DO UPDATE SET status=excluded.status,"
                       "updated_at=excluded.updated_at", (day, status, now))
            db.execute("UPDATE training_plans SET status=?,updated_at=? WHERE date=?",
                       (status, now, day))

    def record_activities(self, activities):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connection() as db:
            for activity in activities or []:
                if any(g.get("user_corrections") or g.get("garmin_original_summary")
                       for g in activity.get("logged_sets", []) or []):
                    raise ValueError("Correction overlays must not overwrite raw Garmin outcomes.")
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

    def raw_activities(self, today=None, days=28):
        today = today or date.today()
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM training_outcomes WHERE date>=? AND date<=? "
                "ORDER BY date,activity_id",
                ((today - timedelta(days=days)).isoformat(), today.isoformat())).fetchall()
        return [json.loads(row[0]) for row in rows]

    def context(self, today=None, outcome_limit=12):
        today = today or date.today()
        if outcome_limit is not None and (
                isinstance(outcome_limit, bool) or not isinstance(outcome_limit, int)
                or outcome_limit < 1):
            raise ValueError("outcome_limit must be a positive integer or None.")
        start = (today - timedelta(days=28)).isoformat()
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            plans = [dict(r) for r in db.execute(
                "SELECT date,payload,revision,status FROM training_plans WHERE date>=? "
                "ORDER BY date", ((today - timedelta(days=7)).isoformat(),))]
            outcomes = [dict(r) for r in db.execute(
                "SELECT activity_id,date,payload FROM training_outcomes WHERE date>=? AND date<=? "
                "ORDER BY date,activity_id", (start, today.isoformat()))]
            day_status = [dict(r) for r in db.execute(
                "SELECT s.date,s.status,s.updated_at,c.plan_kind_at_report,c.plan_revision_at_report "
                "FROM training_day_status s LEFT JOIN training_completion_context c "
                "ON c.date=s.date AND c.reported_at=s.updated_at AND s.status='user_completed' "
                "WHERE s.date>=? ORDER BY s.date", (start,))]
        for row in plans + outcomes:
            row["payload"] = json.loads(row["payload"])
        outcomes = self.corrections.apply(outcomes)
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
            "recent_outcomes": outcomes if outcome_limit is None else outcomes[-outcome_limit:],
            "recent_outcomes_omitted": (0 if outcome_limit is None else
                                      max(0, len(outcomes) - outcome_limit)),
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
                "RPE, good form, pain-free execution, or readiness to increase load. "
                "Completion-report context records the proposal kind at report time, not proof "
                "that its prescribed sets were completed. Older unclassified reports stay unknown. "
                "Missing history is unknown, not rest or zero volume. Use full set chronology, not "
                "category first-appearance order. Revise dated plans explicitly when needed."),
        }
