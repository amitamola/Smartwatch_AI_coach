"""Versioned training intent, recorded outcomes, and conservative output checks.

This is bookkeeping and constraint enforcement, not a medical readiness engine.
"""

import json
import hashlib
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

import coach_clock


PLAN_MARKER = re.compile(r"\[\[SESSION_PLAN:\s*(.*?)\]\]", re.DOTALL | re.IGNORECASE)
PATCH_MARKER = re.compile(r"\[\[SESSION_PATCH:\s*(.*?)\]\]", re.DOTALL | re.IGNORECASE)


def canonical_exercise(name):
    name = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
    name = re.sub(r"\brdl\b", "romanian deadlift", name)
    name = re.sub(r"\b(?:standard|normal|conventional)\b", "", name)
    if re.search(r"\bdeadlifts?\b", name):
        name = re.sub(r"\bdeadlifts\b", "deadlift", name)
    name = re.sub(r"\b(?:dumbbells|db)\b", "dumbbell", name)
    name = re.sub(r"\bbarbells\b", "barbell", name)
    name = re.sub(r"^(.+?)\s+with\s+(?:a\s+)?(dumbbell|barbell)$", r"\2 \1", name)
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
    # A generic deadlift exclusion covers apparatus variants, not single-leg variants.
    generic = re.sub(r"\b(?:with\s+)?(?:dumbbell|barbell)\b", "", canonical)
    if "deadlift" in canonical and " ".join(generic.split()) in avoided_exercises(preferences):
        return True
    for record in preferences:
        key = record.get("key", "")
        if record.get("status", "active") == "active" and key.startswith("avoid_family:"):
            family = canonical_exercise(key.split(":", 1)[1])
            if family and (" " + family + " ") in (" " + canonical + " "):
                return True
    return False


def prescription_identity(plan):
    """Versioned identity of intent, excluding cosmetic prose and forecast bookkeeping."""
    cosmetic = {"objective", "reason", "program_adjustment", "program_revision",
                "rotation_slot", "rotation_defer", "projection_status", "projection_reason"}
    content = {key: value for key, value in plan.items() if key not in cosmetic}
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return "v1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def completion_matches(plan, report):
    if not report or report.get("status", "user_completed") != "user_completed":
        return False
    identity = report.get("prescription_id_at_report")
    if identity:
        current = plan.get("prescription_id")
        if current is None:
            try:
                current = prescription_identity(plan["payload"])
            except (TypeError, ValueError):
                return False
        return identity == current
    return report.get("plan_revision_at_report") == plan["revision"]


_PHASES = ("warmup", "main", "cooldown")
_PHASE_ALIASES = {"work": "main", "working": "main", "working_set": "main",
                  "ramp_up": "warmup", "ramp-up": "warmup"}
_DOSE_NUMBERS = {
    "weight_kg": (0, None), "rest_seconds": (0, None),
    "duration_seconds": (0, None), "duration_minutes": (0, None),
    "distance_meters": (0, None), "distance_km": (0, None),
    "rpe": (0, 10), "rir": (0, None), "target_hr_bpm": (1, None),
    "hr_zone": (1, 5), "speed_kph": (0, None), "cadence_rpm": (0, None),
    "incline_percent": (-100, 100),
}
_DOSE_COUNTS = {"repeats", "repetitions", "rounds", "hr_zone"}
_DOSE_TEXT = {"effort", "tempo", "target", "notes"}
_DOSE_ROLES = set(_PHASES) | set(_PHASE_ALIASES) | {"rest", "recovery"}


def _number(value, field, minimum=0, maximum=None, integer=False):
    if value is None:
        return
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum
            or (maximum is not None and value > maximum)
            or (integer and (not isinstance(value, int)))):
        raise ValueError(f"{field} must be a finite {'integer' if integer else 'number'} "
                         f"in its supported range, or null when unknown")


def _phase(exercise, item=None):
    item = item or {}
    value = item.get("phase") or item.get("role") or item.get("type")
    if value not in _DOSE_ROLES:
        value = exercise.get("phase") or exercise.get("role")
    return _PHASE_ALIASES.get(value, value) if value in _DOSE_ROLES else "main"


def _validate_dose(item):
    if not isinstance(item, dict) or not item:
        raise ValueError("every prescribed set or block must be a nonempty object")
    for key, value in item.items():
        if key == "reps":
            if isinstance(value, str):
                match = re.fullmatch(r"([1-9]\d*)-([1-9]\d*)", value)
                if not match or int(match[1]) > int(match[2]):
                    raise ValueError("reps must be a positive integer, ordered integer range, or null")
            else:
                _number(value, key, 1, integer=True)
        elif key in _DOSE_NUMBERS:
            _number(value, key, *_DOSE_NUMBERS[key], integer=key in _DOSE_COUNTS)
            if value == 0 and key in ("duration_seconds", "duration_minutes",
                                     "distance_meters", "distance_km"):
                raise ValueError(key + " must be positive, or null when unknown")
        elif key in _DOSE_COUNTS:
            _number(value, key, 1, integer=True)
        elif key in ("phase", "role", "type"):
            if value not in (_PHASES if key == "phase" else _DOSE_ROLES):
                raise ValueError("unsupported set/block " + key)
        elif key in _DOSE_TEXT:
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(key + " must be meaningful text or null")
        else:
            raise ValueError("unsupported dose/unit field: " + key)
    if not set(item) & {"reps", "duration_seconds", "duration_minutes",
                        "distance_meters", "distance_km"}:
        raise ValueError("a set or block needs an explicit dose field (null if unknown)")
    for first, second in (("duration_seconds", "duration_minutes"),
                          ("distance_meters", "distance_km")):
        if item.get(first) is not None and item.get(second) is not None:
            raise ValueError("use only one unit for " + first.split("_")[0])


def _validate_prescription(plan, require_phases, constraints):
    for field in ("duration_minutes", "max_duration_minutes"):
        if field in plan:
            _number(plan[field], field, 0)
    constraints = constraints or {}
    limit = constraints.get("max_duration_minutes")
    if limit is not None:
        _number(limit, "max_duration_minutes", 0)
    limits = [n for n in (limit, plan.get("duration_minutes"),
                          plan.get("max_duration_minutes")) if n is not None]
    if limit is not None and plan.get("duration_minutes") is not None and plan["duration_minutes"] > limit:
        raise ValueError("plan exceeds the available duration")
    available = constraints.get("available_equipment")
    if available is not None:
        if not isinstance(available, (list, tuple, set)) or any(not isinstance(x, str) for x in available):
            raise ValueError("available_equipment must be a collection of equipment names")
        available = {x.strip().casefold() for x in available} | {"none", "bodyweight"}
    phases, timed_seconds = [], 0
    detailed = (plan.get("detail_level", "prescription") == "prescription"
                and plan.get("delivery", "self_directed") == "self_directed")
    strict = require_phases and detailed and plan["kind"] in ("strength", "cardio", "mixed")
    for exercise in plan.get("exercises", []):
        if "phase" in exercise and exercise["phase"] not in _PHASES:
            raise ValueError("exercise phase must be warmup, main or cooldown")
        if exercise.get("weight_basis") not in (None, "unspecified", "per_hand", "total",
                                               "machine_stack", "bodyweight", "per_side",
                                               "added_load", "assistance"):
            raise ValueError("unsupported weight_basis")
        for field in ("effort",):
            if exercise.get(field) is not None and (
                    not isinstance(exercise[field], str) or not exercise[field].strip()):
                raise ValueError(field + " must be meaningful text or null")
        equipment = exercise.get("equipment", [])
        if isinstance(equipment, str):
            equipment = [equipment]
        if equipment is None:
            equipment = []
        if not isinstance(equipment, list) or any(not isinstance(x, str) or not x.strip() for x in equipment):
            raise ValueError("equipment must be a name, list of names, or null")
        if available is not None and any(x.strip().casefold() not in available for x in equipment):
            raise ValueError("exercise requires unavailable equipment")
        items = exercise.get("sets", []) + exercise.get("blocks", [])
        if strict and not items:
            raise ValueError("a detailed exercise needs sets or timed/distance blocks")
        for item in items:
            _validate_dose(item)
            phase = _phase(exercise, item)
            if phase in _PHASES:
                phases.append(phase)
            multiplier = 1
            for key in ("repeats", "repetitions", "rounds"):
                multiplier *= item.get(key) or 1
            timed_seconds += multiplier * ((item.get("duration_seconds") or 0)
                                          + 60 * (item.get("duration_minutes") or 0)
                                          + (item.get("rest_seconds") or 0))
        if not items and exercise.get("phase") in _PHASES:
            phases.append(exercise["phase"])
    if strict and (set(phases) != set(_PHASES)
                   or phases != sorted(phases, key=_PHASES.index)):
        raise ValueError("detailed self-directed training needs ordered warmup, main and cooldown phases")
    if limits and timed_seconds > min(limits) * 60:
        raise ValueError("prescribed timed work/rest exceeds the available duration")


def parse_plans(text, preferences=(), today=None, existing_plans=(), require_phases=False,
                constraints=None):
    """Validate new intent; strict phases are opt-in, historical reads stay compatible.

    constraints accepts source-backed max_duration_minutes and available_equipment.
    It never derives constraints from absent food logs or other missing evidence.
    """
    today = today or coach_clock.today()
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
                if day > today and plan.get("kind") == "rest":
                    plan["reason"] = plan["objective"]
                else:
                    raise ValueError("plan needs a reason based on the available evidence")
            if "rotation_slot" in plan and (
                    not isinstance(plan["rotation_slot"], str)
                    or not re.fullmatch(r"\d+:\d+", plan["rotation_slot"])):
                raise ValueError("rotation_slot must be the application-supplied occurrence identifier")
            if "rotation_defer" in plan:
                deferred = plan["rotation_defer"]
                if (not isinstance(deferred, dict)
                        or any(not isinstance(deferred.get(k), str) or not deferred[k].strip()
                               for k in ("slot", "reason", "reassess_on", "resume_when"))):
                    raise ValueError("rotation_defer needs slot, reason, reassess_on and resume_when")
                date.fromisoformat(deferred["reassess_on"])
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
            _validate_prescription(plan, require_phases, constraints)
            claims = unsupported_claims(render_plans([plan]))
            # Outlines omit their reason and guidance in the compact rendered card.
            claims += unsupported_claims(json.dumps(plan, ensure_ascii=False))
            if claims:
                raise ValueError("; ".join(dict.fromkeys(claims)))
            plans.append(plan)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(str(exc))
    if len({p["date"] for p in plans}) != len(plans):
        errors.append("multiple competing plans for the same date")
    return PATCH_MARKER.sub("", PLAN_MARKER.sub("", text or "")).rstrip(), plans, errors


def render_plans(plans):
    """Render the validated prescription; hidden JSON must not hide working sets."""
    sections, upcoming = [], []
    today = coach_clock.today()
    for plan in plans:
        day = date.fromisoformat(plan["date"])
        label = "Today" if day == today else day.strftime("%a %d %b")
        if plan.get("projection_status") == "needs_replanning":
            sections.append(
                f"**{label} · Needs replanning**\n"
                "Previously proposed: " + plan["objective"] + "\n"
                "**Retained history only — not an active prescription.**\n"
                + plan.get("projection_reason", "Confirm an updated plan before training."))
            continue
        if plan.get("delivery") == "instructor_led" and day == today:
            lines = ["**Today · Instructor-led session**",
                     plan["objective"] + (f" · {plan['start_time']}" if plan.get("start_time") else ""),
                     "**Why:** " + plan["reason"],
                     "Class content/intensity must be confirmed; this is not a prescribed set list."]
            lines.extend("- " + g for g in plan["participation_guidance"])
            sections.append("\n".join(lines))
            continue
        if plan.get("detail_level") == "outline" or (day > today and not plan.get("exercises")):
            objective = re.sub(r"\s*\(provisional outline\)", "", plan["objective"],
                               flags=re.IGNORECASE)
            start = f" · {plan['start_time']}" if plan.get("start_time") else ""
            upcoming.append(f"- **{label}{start}**: {objective} (provisional outline)")
            continue
        icon = "\U0001F33F" if plan["kind"] == "rest" else "\U0001F4CB"
        lines = [f"**{icon} {label} \u00b7 {plan['kind'].title()} plan**",
                 plan["objective"], "**Why:** " + plan["reason"]]
        previous_phase = None
        for number, exercise in enumerate(plan.get("exercises", []), 1):
            items = exercise.get("sets", []) + exercise.get("blocks", [])
            phase = _phase(exercise, items[0] if items else None)
            if phase != previous_phase:
                lines.append("\n**" + {"warmup": "Warm-up / ramp-up", "main": "Main session",
                                       "cooldown": "Cool-down"}.get(phase, phase.title()) + "**")
                previous_phase = phase
            lines.extend(["", f"**{number}. {exercise['name']}**"])
            basis = exercise.get("weight_basis", "unspecified")
            previous_set_phase = None
            for index, prescription in enumerate(exercise.get("sets", []), 1):
                phase = _phase(exercise, prescription)
                if phase != previous_phase:
                    lines.append("**" + {"warmup": "Warm-up / ramp-up", "main": "Main session",
                                        "cooldown": "Cool-down"}.get(phase, phase.title()) + "**")
                    previous_phase = phase
                if phase != previous_set_phase:
                    lines.append("   " + {"warmup": "Ramp-up sets", "main": "Working sets",
                                          "cooldown": "Cool-down sets"}.get(phase, phase.title()))
                    previous_set_phase = phase
                fields = []
                for key, value in prescription.items():
                    if key in ("phase", "role", "type"):
                        continue
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
                phase = _phase(exercise, block) if isinstance(block, dict) else previous_phase
                if phase != previous_phase:
                    lines.append("**" + {"warmup": "Warm-up / ramp-up", "main": "Main session",
                                        "cooldown": "Cool-down"}.get(phase, phase.title()) + "**")
                    previous_phase = phase
                detail = ("; ".join(str(key).replace("_", " ") + ": " +
                                    (str(value) if value is not None else "unspecified")
                                    for key, value in block.items()
                                    if key not in ("phase", "role", "type"))
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
        r"\b(?:confirms?|confirming|proves?|proving)\s+(?:your )?systemic freshness\b",
        r"\brest\s+(?:has )?cleared\s+(?:your )?(?:acute )?fatigue\b",
    ]
    problems = []
    for match in re.finditer(pattern + "|" + "|".join(semantic_patterns), text or "", re.IGNORECASE):
        prefix = re.split(r"[.!?;\n]", text[:match.start()])[-1][-100:].lower()
        claim = match.group(0).lower()
        denial = r"\b(?:not|never|cannot|can't|isn't|avoid claiming|don't claim|no guarantee)\b"
        if not re.search(denial, prefix) and not re.search(denial, claim):
            problems.append("unsupported physiological certainty: " + match.group(0))
    missing_food = (r"(?:\b(?:no|missing|absent|incomplete)\s+(?:food|meal|nutrition|intake|calorie)"
                    r"(?:\s+(?:logs?|entries|records?|data))?\b|"
                    r"\b(?:food|meals?|intake|nutrition)\s+(?:was |is |were )?"
                    r"(?:not logged|unlogged|not recorded)\b)")
    consequence = (r"\b(?:under[- ]?fuel\w*|under[- ]?eat\w*|poor intake|"
                   r"reduce\w*|lighter|rest|skip\w*|cut\w*|avoid\w*)\b")
    food_patterns = [
        missing_food + r"[^.!?\n]{0,80}\b(?:so|therefore|means?|indicates?|suggests?)\b"
        r"[^.!?\n]{0,80}" + consequence,
        consequence + r"[^.!?\n]{0,80}\b(?:because|due to)\b[^.!?\n]{0,40}" + missing_food,
    ]
    for match in re.finditer("|".join(food_patterns), text or "", re.IGNORECASE):
        prefix = re.split(r"[.!?;\n]", text[:match.start()])[-1][-100:].lower()
        if (not re.search(r"\b(?:not|never|cannot|can't|don't|doesn't)\b", prefix)
                and not re.search(r"\b(?:does not|doesn't|cannot|can't|never|not evidence)\b",
                                  match.group(0), re.IGNORECASE)):
            problems.append("missing optional food logs are not evidence of poor intake "
                            "or a reason to change training: " + match.group(0))
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
                CREATE TABLE IF NOT EXISTS training_completion_identity (
                    date TEXT PRIMARY KEY, prescription_id TEXT, rotation_slot TEXT,
                    source TEXT NOT NULL, session_id TEXT, reported_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS training_status_events (
                    id INTEGER PRIMARY KEY, date TEXT NOT NULL, status TEXT NOT NULL,
                    prescription_id TEXT, rotation_slot TEXT, source TEXT NOT NULL,
                    session_id TEXT, reported_at TEXT NOT NULL);
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
            for day, revision, reported_at in db.execute(
                    "SELECT date,plan_revision_at_report,reported_at FROM training_completion_context "
                    "WHERE date NOT IN (SELECT date FROM training_completion_identity)").fetchall():
                earlier = db.execute(
                    "SELECT payload FROM training_plan_events WHERE date=? AND revision=?",
                    (day, revision)).fetchone()
                payload = json.loads(earlier[0]) if earlier else None
                try:
                    identity = prescription_identity(payload) if payload else None
                except (ValueError, TypeError):
                    identity = None
                db.execute("INSERT INTO training_completion_identity VALUES (?,?,?,?,?,?)",
                           (day, identity, payload.get("rotation_slot") if payload else None,
                            "legacy_report", None, reported_at))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, plans, source="model_proposal", request_id=None, source_text=""):
        now = coach_clock.now().isoformat(timespec="seconds")
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

    def set_status(self, day, status, source="user_report", session_id=None):
        if status not in ("user_completed", "user_skipped", "user_planned"):
            raise ValueError("unknown training status")
        day = date.fromisoformat(str(day)).isoformat()
        now = coach_clock.now().isoformat(timespec="seconds")
        with self.connection() as db:
            proposed = db.execute("SELECT payload,revision FROM training_plans WHERE date=?",
                                  (day,)).fetchone()
            payload = json.loads(proposed[0]) if proposed else None
            identity = prescription_identity(payload) if payload else None
            slot = payload.get("rotation_slot") if payload else None
            if status == "user_completed":
                kind = payload.get("kind") if payload else None
                db.execute(
                    "INSERT OR REPLACE INTO training_completion_context VALUES (?,?,?,?)",
                    (day, kind, proposed[1] if proposed else None, now))
                db.execute("INSERT OR REPLACE INTO training_completion_identity VALUES (?,?,?,?,?,?)",
                           (day, identity, slot, source, session_id, now))
            db.execute("INSERT INTO training_status_events(date,status,prescription_id,rotation_slot,"
                       "source,session_id,reported_at) VALUES (?,?,?,?,?,?,?)",
                       (day, status, identity, slot, source, session_id, now))
            db.execute("INSERT INTO training_day_status VALUES (?,?,?) "
                       "ON CONFLICT(date) DO UPDATE SET status=excluded.status,"
                       "updated_at=excluded.updated_at", (day, status, now))
            db.execute("UPDATE training_plans SET status=?,updated_at=? WHERE date=?",
                       (status, now, day))
        return {"date": day, "status": status, "prescription_id_at_report": identity,
                "rotation_slot_at_report": slot, "plan_revision_at_report": proposed[1] if proposed else None,
                "source": source, "session_id": session_id}

    def record_activities(self, activities):
        now = coach_clock.now().isoformat(timespec="seconds")
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
        today = today or coach_clock.today()
        with self.connection() as db:
            rows = db.execute(
                "SELECT payload FROM training_outcomes WHERE date>=? AND date<=? "
                "ORDER BY date,activity_id",
                ((today - timedelta(days=days)).isoformat(), today.isoformat())).fetchall()
        return [json.loads(row[0]) for row in rows]

    def context(self, today=None, outcome_limit=12):
        today = today or coach_clock.today()
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
                "SELECT s.date,s.status,s.updated_at,c.plan_kind_at_report,c.plan_revision_at_report,"
                "i.prescription_id AS prescription_id_at_report,i.rotation_slot AS rotation_slot_at_report,"
                "i.source AS completion_source,i.session_id AS completion_session_id "
                "FROM training_day_status s LEFT JOIN training_completion_context c "
                "ON c.date=s.date AND c.reported_at=s.updated_at AND s.status='user_completed' "
                "LEFT JOIN training_completion_identity i ON i.date=c.date "
                "AND i.reported_at=c.reported_at "
                "WHERE s.date>=? ORDER BY s.date", (start,))]
        for row in plans + outcomes:
            row["payload"] = json.loads(row["payload"])
        outcomes = self.corrections.apply(outcomes)
        outcomes.sort(key=lambda row: (
            str(row["payload"].get("start") or row["date"]).replace(" ", "T"),
            row["activity_id"]))
        for row in plans:
            try:
                row["prescription_id"] = prescription_identity(row["payload"])
            except (ValueError, TypeError):
                row["prescription_id"] = None
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
