"""Durable programme proposals and conservative, source-linked review checks.

No Garmin, model, scheduler or private-profile access occurs here. Callers supply
observations and active memory. Verification means user provenance, not medical
verification. Progression checks establish evidence presence, not tolerability.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from coach_plan import canonical_exercise, exercise_excluded, unsupported_claims


_MARKER = re.compile(r"\[\[\s*TRAINING_PROGRAM\b", re.IGNORECASE)
_KINDS = {"strength", "cardio", "mixed", "recovery"}
_ACTIONS = {"keep", "progress", "reduce", "replace", "introduce", "remove"}
_WORKING_LOAD = re.compile(r"\b\d+(?:\.\d+)?\s*(?:kg|kilograms?|lbs?|pounds?|watts?|w)\b",
                           re.IGNORECASE)
_CAPABILITY_ASSERTION = re.compile(
    r"\b(?:(?:good|solid|stable|comfortable|proven|pain[- ]free)\s+"
    r"(?:\w+\s+){0,2}(?:tolerability|tolerance|control|execution|reserve|mechanics|depth|form)"
    r"|without(?:\s+\w+){0,2}\s+(?:strain|overload|pain|discomfort))\b", re.IGNORECASE)
_NUMBER = r"(?:\d+|one|two|three|four|five|six|seven)"
_DAYS = re.compile(
    rf"\b({_NUMBER})\s*(?:training\s+)?days?\s*"
    r"(?:(?:per|a|each|every)\s+week|/\s*week|weekly)\b", re.IGNORECASE)
_EFFORT = re.compile(
    r"\b(?:effort|rpe|rir|reserve|tolerat\w*|comfort\w*|symptom\w*|pain|form)\b",
    re.IGNORECASE)
_CAPABILITY = re.compile(
    r"\b(?:effort|rpe|rir|reserve|tolerat\w*|comfort\w*|pain|form|"
    r"reps?|repetitions?|kg|able|capable|managed|completed|easy|hard|heavy|difficult|controlled)\b",
    re.IGNORECASE)
_ACTIVITY_UNITS = {
    "duration_s": "s", "distance_m": "m", "avg_hr": "bpm", "max_hr": "bpm",
    "avg_power": "W", "max_power": "W", "normalized_power": "W",
    "avg_speed_mps": "m/s", "max_speed_mps": "m/s",
    "avg_pace_s_per_km": "s/km", "pace_s_per_km": "s/km",
    "avg_pace_min_per_km": "min/km", "pace_min_per_km": "min/km",
    "training_load": "device training-load score, not external weight",
    "aerobic_te": "device training-effect score",
    "anaerobic_te": "device training-effect score",
    "elevation_gain_m": "m", "avg_cadence_spm": "steps/min",
    "moderate_intensity_min": "min", "vigorous_intensity_min": "min",
}


def _day(value=None):
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _compact(value):
    """Keep supplied grouped measurements; never synthesize missing numbers."""
    if isinstance(value, dict):
        return {str(k): _compact(v) for k, v in value.items() if k != "set_sequence"}
    if isinstance(value, (list, tuple)):
        return [_compact(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _verified(record):
    text, source = record.get("text"), record.get("source_text")
    return (record.get("verified") in (True, 1)
            and record.get("source_type") == "user"
            and isinstance(text, str) and bool(text.strip())
            and isinstance(source, str) and text in source)


def _availability(text):
    matches = list(_DAYS.finditer(text))
    if not matches:
        return None
    words = {word: index for index, word in
             enumerate(("one", "two", "three", "four", "five", "six", "seven"), 1)}
    values = {words.get(m[1].lower(), int(m[1]) if m[1].isdigit() else None)
              for m in matches}
    if len(values) != 1 or next(iter(values)) not in range(1, 8):
        return None
    # Scope uncertainty to schedule statements, not unrelated profile constraints.
    for match in matches:
        left = max([0] + [m.end() for m in re.finditer(r"[.!?;\n]", text[:match.start()])])
        boundary = re.search(r"[.!?;\n]", text[match.end():])
        right = match.end() + boundary.end() if boundary else len(text)
        clause = text[left:right]
        if ("?" in clause
                or re.search(r"\b(?:maybe|might|could|would|wish|hope|ideally|"
                             r"not|cannot|can't|don't|used to|previously)\b"
                             rf"|{_NUMBER}\s*(?:-|–|to|or)\s*{_NUMBER}", clause, re.IGNORECASE)):
            return None
    return next(iter(values))


def _hard_frequency_limit(text):
    """Only explicit limits are constraints; a starting frequency is a target."""
    limits = []
    numbers = {word: i for i, word in
               enumerate(("one", "two", "three", "four", "five", "six", "seven"), 1)}
    for match in _DAYS.finditer(text):
        prefix = re.split(r"[.!?;\n]", text[:match.start()])[-1]
        if re.search(r"\b(?:don't|do not|no longer)\b", prefix, re.I):
            continue
        if re.search(r"\b(?:at most|maximum(?: of)?|max(?:imum)?\s*:|no more than|"
                     r"not more than|only(?: train)?|limit(?:ed)? to|"
                     r"(?:cannot|can't) (?:train|do) more than)\s*$", prefix, re.IGNORECASE):
            limits.append(numbers.get(match[1].lower(), int(match[1]) if match[1].isdigit() else None))
    return limits[0] if limits and len(set(limits)) == 1 and limits[0] in range(1, 8) else None


def build_review_evidence(training_context, memory_records, profile, today=None):
    """Build compact evidence from the full observed 28-day context.

    Active older memory remains a standing constraint, but cannot establish
    recent progression. Plans and completion statuses are not exercise proof.
    The caller must classify outcomes and request context(outcome_limit=None).
    """
    today = _day(today)
    start = today - timedelta(days=28)
    refs, observations, comparable, days = {}, [], {}, set()
    excluded_rows, duplicates = 0, 0
    seen = set()
    for row in training_context.get("recent_outcomes", []):
        payload = row.get("payload") or {}
        aid = row.get("activity_id", payload.get("activity_id"))
        try:
            raw_date = row.get("date") or payload.get("start")
            if not raw_date:
                raise ValueError("missing observation date")
            observed = _day(raw_date)
        except (TypeError, ValueError):
            excluded_rows += 1
            continue
        if aid is None or not str(aid).strip() or not start <= observed <= today:
            excluded_rows += 1
            continue
        ref = "activity:" + str(aid)
        if ref in seen:
            duplicates += 1
            continue
        seen.add(ref)
        classification = row.get("classification", "unknown")
        if classification not in {"intentional_training", "active_recovery", "transport", "unknown"}:
            classification = "unknown"
        groups = [_compact(group) for group in payload.get("logged_sets", []) or []
                  if isinstance(group, dict)]
        exercises = sorted({
            canonical_exercise(group["exercise"]) for group in groups
            if isinstance(group.get("exercise"), str) and group["exercise"].strip()
            and canonical_exercise(group["exercise"]) not in {"unknown", "no active sets"}
            and group.get("sets") != 0
        })
        # A supplied modality is useful evidence; a title is not an exact variant.
        modality = payload.get("type")
        if (isinstance(modality, str) and modality.strip()
                and canonical_exercise(modality) not in
                {"strength", "strength training", "weight training", "mixed", "other", "unknown"}):
            exercises.append(canonical_exercise(modality))
        exercises = sorted(set(exercises))
        observation = {
            "ref": ref, "activity_id": str(aid), "date": observed.isoformat(),
            "classification": classification, "canonical_exercises": exercises,
            "logged_sets": groups,
            "effort_form_tolerability": "unknown unless explicitly user-reported",
        }
        for key in (*_ACTIVITY_UNITS, "start", "name", "type", "logged_sets_coverage",
                    "training_effect_label", "avg_pace", "pace", "pace_unit"):
            if key in payload:
                observation[key] = _compact(payload[key])
        observation["metric_units"] = {
            key: unit for key, unit in _ACTIVITY_UNITS.items() if key in payload}
        for key in ("avg_pace", "pace"):
            if key in payload:
                observation["metric_units"][key] = payload.get("pace_unit") or "unspecified"
        observations.append(observation)
        refs[ref] = {
            "type": "activity", "date": observed.isoformat(),
            "classification": classification, "canonical_exercises": exercises,
            "observed": True, "within_window": True,
        }
        if classification == "intentional_training":
            days.add(observed.isoformat())
        if classification in {"intentional_training", "active_recovery"}:
            for exercise in exercises:
                summary = comparable.setdefault(exercise, {"dates": set(), "refs": []})
                summary["dates"].add(observed.isoformat())
                summary["refs"].append(ref)
    for summary in comparable.values():
        summary["dates"] = sorted(summary["dates"])
        summary["count"] = len(summary["dates"])
        summary["refs"].sort()
        summary["comparison_basis"] = (
            "Same canonical exercise or recorded modality, distinct dates only; "
            "intensity, duration, conditions and tolerance comparability are unverified.")
    memories, scheduling = [], []
    for supplied in memory_records or ():
        if (supplied.get("status", "active") != "active"
                or supplied.get("kind") not in {"anchor", "health", "preference"}
                or supplied.get("id") is None):
            continue
        try:
            observed = _day(supplied.get("observed_on")) if supplied.get("observed_on") else None
        except (TypeError, ValueError):
            observed = None
        if observed and observed > today:
            continue
        record = _compact(supplied)
        record["ref"] = "memory:" + str(record["id"])
        record["verified"] = _verified(record)
        record["within_window"] = observed is not None and start <= observed <= today
        memories.append(record)
        refs[record["ref"]] = {
            "type": "memory", "kind": record["kind"], "key": record.get("key", ""),
            "text": record.get("text", ""), "source_type": record.get("source_type"),
            "verified": record["verified"], "within_window": record["within_window"],
            "date": observed.isoformat() if observed else None,
        }
        text = str(record.get("text", ""))
        schedule_key = re.search(r"schedule|availability|training_days|weekly_training",
                                 str(record.get("key", "")), re.IGNORECASE)
        if (record["verified"] and observed and
                (schedule_key or (_DAYS.search(text) and re.search(
                    r"\b(?:train\w*|workout\w*|gym|exercise|available)\b", text, re.IGNORECASE)))):
            scheduling.append((observed.isoformat(), record.get("updated_at") or "",
                               record["ref"], _availability(text), _hard_frequency_limit(text)))
    profile_text = profile if isinstance(profile, str) else ""
    if profile_text.strip():
        refs["profile"] = {"type": "profile", "verified": False, "text": profile_text}
    availability = _availability(profile_text)
    hard_limit = _hard_frequency_limit(profile_text)
    availability_refs = ["profile"] if availability is not None else []
    if scheduling:
        newest = max((item[0], item[1]) for item in scheduling)
        current = [item for item in scheduling if item[:2] == newest]
        values = {item[3] for item in current}
        availability = next(iter(values)) if len(values) == 1 else None
        availability_refs = [item[2] for item in current]
        limits = {item[4] for item in current}
        hard_limit = next(iter(limits)) if len(limits) == 1 else None
    observations.sort(key=lambda row: (row["date"], row["ref"]))
    memories.sort(key=lambda row: row["ref"])
    omitted = training_context.get("recent_outcomes_omitted", 0)
    return {
        "window_start": start.isoformat(), "window_end": today.isoformat(),
        "observations": observations, "memory": memories,
        "profile": {"ref": "profile" if profile_text.strip() else None, "text": profile_text},
        "refs": refs, "comparable_sessions": comparable,
        "intentional_training_days": {
            "dates": sorted(days), "count": len(days),
            "last_7_days": sum(day >= (today - timedelta(days=6)).isoformat() for day in days),
        },
        "weekly_training_days": availability,
        "weekly_training_days_refs": availability_refs,
        "hard_weekly_limit": hard_limit,
        "availability_caveat": (
            "Frequency is a planning target, not a physiological ceiling. Only "
            "hard_weekly_limit represents an explicitly stated maximum. One-off changes "
            "do not silently change the ongoing target or establish recovery."
            if availability is not None else
            "Weekly availability is unknown or ambiguous; ask, do not assume a number."),
        "coverage": {
            "observed_activity_count": len(observations),
            "supplied_outcomes_omitted": omitted,
            "excluded_invalid_future_or_out_of_window": excluded_rows,
            "duplicate_activity_rows_ignored": duplicates,
            "complete_history": False,
        },
        "caveats": [
            "Observed records only; missing history is unknown, not rest or zero volume.",
            "Plans, legacy completion reports and programme templates are proposals, not completed sets.",
            "Only intentional-training observations count as training days; active recovery can "
            "support its own capability comparisons, but transport and unknown classes cannot.",
            "Grouped recorded sets are not hard-set counts; RPE, form, pain and readiness cannot be inferred.",
            "Same-day activities count as one comparable session date for each canonical exercise.",
            "Canonical matching is bookkeeping, not proof that equipment variants are interchangeable or safe.",
            "Cardio modality matching does not equate intensities or conditions. Device HR, power and "
            "training-load scores do not establish perceived effort or readiness. Pace is not derived "
            "from speed; ambiguous supplied pace units remain unspecified.",
            "Older active memory remains a constraint; future memory is excluded and older anchors are not recent progression proof.",
            "Progression validation checks evidence presence only; coaching policy must still establish tolerability.",
        ],
    }


def _text(value, field, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a nonempty string of at most {limit} characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{field} contains control characters")
    claims = unsupported_claims(value)
    if claims:
        raise ValueError("; ".join(claims))
    return value.strip()


def _strategy_text(value, field, limit=1000):
    value = _text(value, field, limit)
    if _WORKING_LOAD.search(value):
        raise ValueError(field + ": numeric working resistance/power belongs in dated SESSION_PLAN, "
                         "not a persistent programme; refer to the latest tolerable reported load")
    return value


def _shape(value, required, optional, field):
    if not isinstance(value, dict):
        raise ValueError(field + " must be an object")
    missing, extra = set(required) - value.keys(), value.keys() - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f"{field}: missing fields {sorted(missing)}; unsupported fields "
                         f"{sorted(extra)} (dates and deadlines are application-owned)")


def _list(value, field, minimum=0, maximum=24):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} must be a list with {minimum}..{maximum} items")
    return value


def _identity(name):
    return " ".join(re.findall(r"[a-z0-9]+", name.lower()))


def _previous_exercises(previous):
    if not previous:
        return {}
    review = previous.get("review", previous)
    result = {}
    for template in review.get("session_templates", []):
        for exercise in template.get("exercises", []):
            key = _identity(exercise["name"])
            if key not in result or exercise.get("role") == "anchor":
                result[key] = exercise
    return result


def _anchor_for(source, exercise):
    if not (source.get("type") == "memory" and source.get("kind") == "anchor"
            and source.get("verified") is True and source.get("source_type") == "user"
            and source.get("within_window") is True
            and _CAPABILITY.search(str(source.get("text", "")))):
        return False
    key = canonical_exercise(str(source.get("key", "")).split(":")[-1])
    text = canonical_exercise(source.get("text", ""))
    return key == exercise or (" " + exercise + " ") in (" " + text + " ")


def _validate_review(review, evidence, preferences, previous):
    fields = {"goal", "weekly_training_days", "session_templates", "decisions",
              "recovery_rule", "success_signals", "variety_review", "questions"}
    _shape(review, fields, (), "programme")
    result = {"goal": _text(review["goal"], "goal", 600)}
    budget, requested = evidence.get("weekly_training_days"), review["weekly_training_days"]
    if requested is not None and (type(requested) is not int or not 1 <= requested <= 7):
        raise ValueError("weekly_training_days must be an integer 1..7 or null")
    if requested is not None and (budget is None or requested > budget):
        raise ValueError("weekly_training_days cannot be invented or exceed known availability; "
                         "use null when unknown, or reduce to the evidenced budget")
    result["weekly_training_days"] = requested
    constraints = list(preferences) + evidence.get("memory", [])
    templates, exercises, ids = [], {}, set()
    total = 0
    for item in _list(review["session_templates"], "session_templates", 1, 12):
        _shape(item, {"id", "kind", "purpose", "exercises"}, (), "session template")
        slug = _text(item["id"], "template id", 64)
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", slug) or slug in ids:
            raise ValueError("template ids must be unique stable lowercase slugs")
        ids.add(slug)
        if item["kind"] not in _KINDS:
            raise ValueError("template kind must be strength, cardio, mixed or recovery")
        template = {"id": slug, "kind": item["kind"],
                    "purpose": _strategy_text(item["purpose"], "template purpose", 600), "exercises": []}
        local = set()
        for move in _list(item["exercises"], "template exercises", 0, 24):
            _shape(move, {"name", "role", "progression_rule"}, {"stop_rule"}, "exercise")
            name = _strategy_text(move["name"], "exercise name", 160)
            canonical = canonical_exercise(name)
            if not canonical or canonical in local:
                raise ValueError("exercise names within a template must be distinct")
            local.add(canonical)
            if exercise_excluded(name, constraints):
                raise ValueError(f"excluded movement cannot be prescribed: {name}")
            if move["role"] not in {"anchor", "accessory", "skill"}:
                raise ValueError("exercise role must be anchor, accessory or skill")
            rule = _strategy_text(move["progression_rule"], "progression_rule for " + name, 1000)
            stop = _strategy_text(move["stop_rule"], "stop_rule", 600) if "stop_rule" in move else None
            if not _EFFORT.search(rule) and not stop:
                raise ValueError(f"{name} needs an explicit stop_rule or effort/tolerance "
                                 "condition; check EVERY exercise, including mobility/core")
            if re.search(r"\b(?:always|must|automatically)\s+increase\b"
                         r"|\bregardless of (?:pain|symptoms|effort|tolerance)\b", rule, re.IGNORECASE):
                raise ValueError("progression must not force increases regardless of tolerance")
            cleaned = {"name": name, "role": move["role"], "progression_rule": rule}
            if stop:
                cleaned["stop_rule"] = stop
            template["exercises"].append(cleaned)
            key = _identity(name)
            if key not in exercises or cleaned["role"] == "anchor":
                exercises[key] = cleaned
            total += 1
        templates.append(template)
    if total > 96:
        raise ValueError("programme must have at most 96 exercise entries")
    result["session_templates"] = templates
    refs = evidence.get("refs", {})
    prior = _previous_exercises(previous)
    decisions, by_name, replacements = [], {}, set()
    for item in _list(review["decisions"], "decisions", 1, 96):
        _shape(item, {"exercise", "action", "reason", "evidence_refs"}, {"replacement"}, "decision")
        name = _text(item["exercise"], "decision exercise", 160)
        identity, canonical = _identity(name), canonical_exercise(name)
        action = item["action"]
        if action not in _ACTIONS:
            raise ValueError("unknown decision action; choose keep/progress/reduce/replace/introduce/remove")
        if identity in by_name:
            raise ValueError(f"competing duplicate decisions for {name}")
        source_refs = _list(item["evidence_refs"], "evidence_refs", 1, 32)
        if any(not isinstance(ref, str) or ref not in refs for ref in source_refs):
            raise ValueError(f"decision for {name} cites an unknown evidence ref")
        if len(set(source_refs)) != len(source_refs):
            raise ValueError("evidence_refs must be distinct; duplicates are not extra evidence")
        decision = {"exercise": name, "action": action,
                    "reason": _strategy_text(item["reason"], "decision reason for " + name, 1500),
                    "evidence_refs": source_refs[:]}
        if (_CAPABILITY_ASSERTION.search(decision["reason"])
                and not any(_anchor_for(refs[ref], canonical) for ref in source_refs)):
            raise ValueError(f"{name}: recorded sessions do not establish form, reserve or "
                             "tolerability; describe observed participation, and attribute any "
                             "actual capability claim to relevant verified user feedback")
        if action in {"remove", "replace"}:
            if identity in exercises:
                raise ValueError(f"{name} cannot be removed/replaced and still prescribed")
        elif identity not in exercises:
            raise ValueError(f"{action} decision for {name} must name an exercise in templates")
        if exercise_excluded(name, constraints) and action not in {"remove", "replace"}:
            raise ValueError(f"excluded movement may only be removed or replaced: {name}")
        if action == "replace":
            replacement = _text(item.get("replacement"), "replacement for " + name, 160)
            key = _identity(replacement)
            if (key == identity or canonical_exercise(replacement) == canonical
                    or key not in exercises
                    or exercise_excluded(replacement, constraints)):
                raise ValueError("replace needs a different nonexcluded canonical replacement present "
                                 "in templates; use keep for a spelling/alias-only change")
            decision["replacement"] = replacement
            replacements.add(key)
        elif "replacement" in item:
            raise ValueError("replacement is only allowed for a replace decision")
        if action == "progress":
            dates = {refs[ref].get("date") for ref in source_refs
                     if refs[ref].get("type") == "activity"
                     and refs[ref].get("observed") is True
                     and refs[ref].get("within_window") is True
                     and refs[ref].get("classification") in {"intentional_training", "active_recovery"}
                     and canonical in refs[ref].get("canonical_exercises", [])}
            dates.discard(None)
            if len(dates) < 2:
                raise ValueError(f"progress for {name} needs two distinct observed non-transport "
                                 "session dates for the same canonical exercise; choose keep/clarify")
            if not any(_anchor_for(refs[ref], canonical) for ref in source_refs):
                raise ValueError(f"progress for {name} needs a recent verified user capability/effort "
                                 "anchor naming that exercise; profile and model plans are not proof")
        decisions.append(decision)
        by_name[identity] = decision
    for identity, move in exercises.items():
        if identity not in by_name and identity not in replacements:
            raise ValueError(f"missing decision for template exercise: {move['name']}")
        decision = by_name.get(identity)
        observed = any(canonical_exercise(move["name"]) in row.get("canonical_exercises", [])
                       for row in evidence.get("observations", []))
        if (identity not in prior and not observed
                and identity not in replacements
                and decision["action"] != "introduce"):
            raise ValueError(f"new unobserved exercise {move['name']} needs introduce/replace, "
                             "not invented successful history")
    for identity, move in prior.items():
        if move.get("role") != "anchor":
            continue
        if identity not in exercises:
            if identity not in by_name or by_name[identity]["action"] not in {"remove", "replace"}:
                raise ValueError(f"previous anchor {move['name']} needs an explicit remove/replace decision")
        elif exercises[identity]["role"] != "anchor" and by_name.get(identity, {}).get("action") != "reduce":
            raise ValueError(f"changed anchor role for {move['name']} needs a reduce decision and reason")
    result["decisions"] = decisions
    for field in ("recovery_rule", "variety_review"):
        result[field] = _text(review[field], field, 1500)
    result["success_signals"] = [
        _text(item, "success signal", 500)
        for item in _list(review["success_signals"], "success_signals", 1, 12)]
    result["questions"] = [
        _text(item, "question", 500) for item in _list(review["questions"], "questions", 0, 2)]
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: " + key)
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-JSON numeric constant: " + value)


def parse_program_review(text, evidence, preferences=(), previous=None):
    """Return (visible prose, complete validated proposal or None, errors).

    This is an all-or-nothing structural/evidence gate, not semantic proof of
    exercise safety, correct form, or permission to increase training load.
    """
    text = text or ""
    starts = list(_MARKER.finditer(text))
    clean = text
    raw = None
    malformed = False
    for match in reversed(starts):
        try:
            colon = re.match(r"\s*:\s*", text[match.end():])
            if not colon:
                raise ValueError("missing colon")
            offset = match.end() + colon.end()
            candidate, consumed = json.JSONDecoder(
                object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            ).raw_decode(text[offset:])
            end = offset + consumed
            closing = re.match(r"\s*\]\]", text[end:])
            if not closing:
                raise ValueError("missing closing marker")
            raw = candidate
            clean = clean[:match.start()] + clean[end + closing.end():]
        except (ValueError, TypeError, RecursionError):
            malformed = True
            closing = text.find("]]", match.end())
            end = closing + 2 if closing >= 0 else len(text)
            clean = clean[:match.start()] + clean[end:]
    clean = clean.strip()
    if len(starts) != 1:
        return clean, None, ["exactly one [[TRAINING_PROGRAM: JSON]] marker is required"]
    if malformed:
        return clean, None, ["malformed TRAINING_PROGRAM JSON or closing marker; no update applied"]
    if len(text) > 100000:
        return clean, None, ["programme response exceeds 100000 characters"]
    try:
        errors = unsupported_claims(clean)
        if errors:
            return clean, None, errors
        result = _validate_review(raw, evidence, preferences, previous)
        if len(_json(result)) > 60000:
            raise ValueError("programme JSON exceeds 60000 characters")
        return clean, result, []
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        return clean, None, [str(exc)]


class ProgramStore:
    """Private proposal ledger; callers own source fingerprints and scheduling.

    Fingerprints should reflect active training-relevant feedback/profile, NOT
    outcome counts or capture timestamps. Shared SQLite tables are namespaced.
    ``save_review`` revalidates and never promotes proposals into user memory.
    """

    def __init__(self, db_path, review_days=7, block_days=28):
        for name, value in (("review_days", review_days), ("block_days", block_days)):
            if type(value) is not int or not 1 <= value <= 366:
                raise ValueError(f"{name} must be an integer 1..366")
        self.path = str(db_path)
        if not self.path or self.path == ":memory:":
            raise ValueError("Provide a persistent private SQLite path")
        self.review_days, self.block_days = review_days, block_days
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS training_program_reviews (
                    revision INTEGER PRIMARY KEY, record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL, recorded_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS training_program_requests (
                    request_key TEXT PRIMARY KEY, revision INTEGER NOT NULL
                    REFERENCES training_program_reviews(revision));
                CREATE TABLE IF NOT EXISTS training_program_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    active_revision INTEGER, dirty_fingerprint TEXT,
                    last_success_at TEXT, last_error TEXT, retry_after TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT);
                INSERT OR IGNORE INTO training_program_state(singleton) VALUES (1);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(training_program_state)")}
            if "feedback_detected_on" not in columns:
                db.execute("ALTER TABLE training_program_state ADD COLUMN feedback_detected_on TEXT")
            if "pending_review_reason" not in columns:
                db.execute("ALTER TABLE training_program_state ADD COLUMN pending_review_reason TEXT")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _active(db):
        row = db.execute(
            "SELECT r.record_json FROM training_program_reviews r "
            "JOIN training_program_state s ON s.active_revision=r.revision "
            "WHERE s.singleton=1").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _request(db, request_key):
        row = db.execute(
            "SELECT r.record_json FROM training_program_requests q "
            "JOIN training_program_reviews r ON r.revision=q.revision WHERE q.request_key=?",
            (request_key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _block_baseline(db, active):
        """Return the first block event; only its evidence is observational proof."""
        if active is None:
            return None
        baseline = None
        for row in db.execute(
                "SELECT record_json FROM training_program_reviews "
                "WHERE revision<=? ORDER BY revision DESC", (active["revision"],)):
            record = json.loads(row[0])
            if record["block_start"] != active["block_start"]:
                break
            baseline = record
        return baseline

    def review_for_request(self, request_key):
        if request_key is None:
            return None
        with self.connection() as db:
            return self._request(db, request_key)

    def context(self, today=None, fingerprint=None, now=None):
        instant = _timestamp(now)
        today = _day(today) if today is not None else (_day(now) if now else _day())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state = dict(db.execute("SELECT * FROM training_program_state WHERE singleton=1").fetchone())
            active = self._active(db)
            baseline = self._block_baseline(db, active)
            if fingerprint is not None and not isinstance(fingerprint, str):
                raise ValueError("fingerprint must be a string or None")
            dirty = fingerprint if fingerprint is not None else state["dirty_fingerprint"]
            detected = state["feedback_detected_on"]
            if active and dirty is not None and dirty != active["source_fingerprint"]:
                detected = detected or today.isoformat()
            else:
                detected = None
            if dirty != state["dirty_fingerprint"] or detected != state["feedback_detected_on"]:
                db.execute("UPDATE training_program_state SET dirty_fingerprint=?,"
                           "feedback_detected_on=? WHERE singleton=1", (dirty, detected))
        reason = None
        next_date = active["next_review_date"] if active else today.isoformat()
        feedback_date = (_day(detected) + timedelta(days=1)).isoformat() if detected else None
        if feedback_date:
            next_date = min(next_date, feedback_date)
        if not active:
            reason = "initial"
        elif today >= _day(active["block_end"]):
            reason = "block"
        elif today >= _day(active["next_review_date"]):
            reason = "weekly"
        elif feedback_date and today >= _day(feedback_date):
            reason = "feedback_changed"
        if reason is None and state["pending_review_reason"]:
            reason = state["pending_review_reason"]
            next_date = min(next_date, today.isoformat())
        if reason is None and state["last_error"]:
            reason = "retry"
            if state["retry_after"]:
                retry_day = _timestamp(state["retry_after"]).astimezone().date().isoformat()
                next_date = min(next_date, retry_day)
        in_backoff = bool(state["retry_after"] and instant < _timestamp(state["retry_after"]))
        return {
            "active": active, "block_baseline": baseline,
            "review_due": reason is not None and not in_backoff,
            "due_reason": reason, "next_review_date": next_date,
            "last_error": state["last_error"], "retry_after": state["retry_after"],
            "last_success_at": state["last_success_at"],
            "dirty_fingerprint": dirty, "failure_count": state["failure_count"],
            "feedback_detected_on": detected,
            "last_attempt_at": state["last_attempt_at"],
            "pending_review_reason": state["pending_review_reason"],
        }

    def request_review(self, reason):
        reason = _text(reason, "review reason", 100)
        with self.connection() as db:
            db.execute("UPDATE training_program_state SET pending_review_reason="
                       "COALESCE(pending_review_reason,?) WHERE singleton=1", (reason,))

    def save_review(self, review, evidence, fingerprint, today=None, request_key=None):
        today = _day(today)
        instant = _timestamp().isoformat(timespec="microseconds")
        if fingerprint is not None and not isinstance(fingerprint, str):
            raise ValueError("fingerprint must be a string or None")
        if request_key is not None:
            _text(request_key, "request_key", 1000)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key is not None:
                replay = self._request(db, request_key)
                if replay:
                    return replay
            active = self._active(db)
            _, validated, errors = parse_program_review(
                "[[TRAINING_PROGRAM: " + _json(review) + "]]", evidence, previous=active)
            if errors:
                raise ValueError("Programme not saved: " + "; ".join(errors))
            if active and today < _day(active["reviewed_on"]):
                raise ValueError("Cannot backdate a programme review")
            content_hash = hashlib.sha256(_json({
                "review": validated, "evidence": evidence, "fingerprint": fingerprint,
                "reviewed_on": today.isoformat(),
            }).encode("utf-8")).hexdigest()
            old = db.execute("SELECT content_hash FROM training_program_reviews WHERE revision=?",
                             (active["revision"],)).fetchone() if active else None
            if old and old[0] == content_hash:
                if request_key is not None:
                    db.execute("INSERT INTO training_program_requests VALUES (?,?)",
                               (request_key, active["revision"]))
                db.execute("UPDATE training_program_state SET last_error=NULL,retry_after=NULL,"
                           "failure_count=0,dirty_fingerprint=?,feedback_detected_on=NULL,"
                           "pending_review_reason=NULL "
                           "WHERE singleton=1", (fingerprint,))
                return active
            block_start = (_day(active["block_start"])
                           if active and today < _day(active["block_end"]) else today)
            block_end = (_day(active["block_end"])
                         if active and today < _day(active["block_end"])
                         else today + timedelta(days=self.block_days))
            record = {
                "revision": active["revision"] + 1 if active else 1,
                "block_start": block_start.isoformat(), "block_end": block_end.isoformat(),
                "reviewed_on": today.isoformat(),
                "next_review_date": min(today + timedelta(days=self.review_days), block_end).isoformat(),
                "review": validated, "evidence": evidence,
                "source_fingerprint": fingerprint, "source_refs": evidence.get("refs", {}),
                "source_type": "model_proposal",
                "evidence_caveat": "Source-linked proposal, not a user fact or medical safety assessment.",
            }
            db.execute("INSERT INTO training_program_reviews VALUES (?,?,?,?)",
                       (record["revision"], _json(record), content_hash, instant))
            if request_key is not None:
                db.execute("INSERT INTO training_program_requests VALUES (?,?)",
                           (request_key, record["revision"]))
            db.execute("UPDATE training_program_state SET active_revision=?,dirty_fingerprint=?,"
                       "last_success_at=?,last_error=NULL,retry_after=NULL,failure_count=0,"
                       "last_attempt_at=?,feedback_detected_on=NULL,pending_review_reason=NULL WHERE singleton=1",
                       (record["revision"], fingerprint, instant, instant))
            return record

    def record_failure(self, error, now=None):
        instant = _timestamp(now)
        with self.connection() as db:
            db.execute("UPDATE training_program_state SET last_error=?,retry_after=?,"
                       "failure_count=failure_count+1,last_attempt_at=? WHERE singleton=1",
                       (str(error)[:2000] or "Programme review failed",
                        (instant + timedelta(minutes=30)).isoformat(),
                        instant.isoformat()))


def render_program(record):
    """Render a persisted record without claiming delivery or new user facts."""
    if not record:
        return "No training programme is available yet."
    review = record["review"]
    budget = review["weekly_training_days"]
    availability = "unknown — please clarify" if budget is None else f"usually {budget} days/week"
    lines = [
        "**\U0001F916 AgBot · Training programme**",
        f"**Goal:** {review['goal']}",
        f"**Block:** {record['block_start']} → {record['block_end']} (review/renewal boundary)",
        f"**Training target:** {availability} (adjustable, not a recovery test)",
        "**Session options** (not a promise to schedule every option each week)",
    ]
    hard = (record.get("evidence") or {}).get("hard_weekly_limit")
    if hard is not None:
        lines.insert(4, f"**Explicit scheduling maximum:** {hard} days per rolling seven days")
    for template in review["session_templates"]:
        names = ", ".join(move["name"] + (" [anchor]" if move["role"] == "anchor" else "")
                          for move in template["exercises"]) or "No prescribed exercises"
        label = template["id"].replace("-", " ").title()
        lines.append(f"- **{label}**: {template['purpose']}\n  {names}")
    lines.append("**Keep / change and why**")
    for decision in review["decisions"]:
        target = decision["exercise"]
        if decision["action"] == "replace":
            target += " → " + decision["replacement"]
        lines.append(f"- **{decision['action'].title()} {target}:** {decision['reason']}")
        if decision["action"] == "progress":
            rules = {move["progression_rule"] for template in review["session_templates"]
                     for move in template["exercises"]
                     if _identity(move["name"]) == _identity(decision["exercise"])}
            for rule in sorted(rules):
                lines.append("  Progression condition: " + rule)
    lines.extend([
        "**Recovery:** " + review["recovery_rule"],
        "**Success signals:** " + "; ".join(review["success_signals"]),
        "**Coverage / alternatives:** " + review["variety_review"],
    ])
    if review["questions"]:
        lines.append("**Questions:**")
        lines.extend("- " + question for question in review["questions"])
    lines.append("**Next review:** " + record["next_review_date"])
    return "\n".join(lines)
