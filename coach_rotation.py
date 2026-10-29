"""Persistent session rotation. Proposals never count as completed training."""

import json
import copy
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from coach_plan import canonical_exercise


def load_training_policy(path):
    path = Path(path)
    if not path.exists():
        return {}
    policy = json.loads(path.read_text(encoding="utf-8"))
    allowed = {"strength_order", "cardio_templates", "required_exercises", "frequency_mode"}
    if not isinstance(policy, dict) or set(policy) - allowed:
        raise ValueError("training_policy.json contains unsupported fields")
    for field in ("strength_order", "cardio_templates", "required_exercises"):
        items = policy.get(field, [])
        if (not isinstance(items, list) or len(items) > 12
                or any(not isinstance(x, str) or not x.strip() for x in items)
                or len(set(items)) != len(items)):
            raise ValueError("training_policy.json: invalid " + field)
    if policy.get("frequency_mode", "availability") not in ("availability", "propose"):
        raise ValueError("training_policy.json: invalid frequency_mode")
    return policy


def validate_rotation(review, policy):
    """Validate balanced coverage, not medical suitability or user acceptance."""
    sequence = review.get("rotation", [])
    templates = {t["id"]: t for t in review["session_templates"]}
    intentional = {key for key, t in templates.items() if t["kind"] != "recovery"}
    if (not isinstance(sequence, list) or not sequence or len(sequence) > 24
            or any(not isinstance(x, str) for x in sequence)
            or set(sequence) != intentional):
        raise ValueError("rotation must cover every non-recovery template, and only those templates")
    strength = policy.get("strength_order", [])
    if strength and [key for key in sequence if templates[key]["kind"] == "strength"] != strength:
        raise ValueError("rotation must preserve the configured strength_order exactly")
    cardio = policy.get("cardio_templates", [])
    if any(key not in sequence or templates[key]["kind"] != "cardio" for key in cardio):
        raise ValueError("rotation must include each configured cardio template")
    names = {canonical_exercise(m["name"]) for t in templates.values() for m in t["exercises"]}
    if any(canonical_exercise(name) not in names for name in policy.get("required_exercises", [])):
        raise ValueError("programme omitted a configured required exercise; clarify conflicts instead")


class RotationStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS training_rotation (
                    id INTEGER PRIMARY KEY, sequence_json TEXT NOT NULL,
                    started_on TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
                    program_revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS training_rotation_completions (
                    slot TEXT PRIMARY KEY, completed_on TEXT NOT NULL,
                    template_id TEXT NOT NULL, source_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS training_rotation_deferrals (
                    slot TEXT NOT NULL, plan_date TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(slot,plan_date));
            """)

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
    def _slot(row, position=None):
        return f"{row['id']}:{row['position'] if position is None else position}"

    def configure(self, programme, today=None):
        sequence = programme["review"].get("rotation")
        if not sequence:
            return
        encoded = json.dumps(sequence)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM training_rotation ORDER BY id DESC LIMIT 1").fetchone()
            if old and old["sequence_json"] == encoded:
                db.execute("UPDATE training_rotation SET program_revision=? WHERE id=?",
                           (programme["revision"], old["id"]))
                return
            position = 0
            if old:
                previous = json.loads(old["sequence_json"])
                pending = previous[old["position"] % len(previous)]
                if pending in sequence:
                    position = sequence.index(pending)
            db.execute("INSERT INTO training_rotation(sequence_json,started_on,position,program_revision) "
                       "VALUES (?,?,?,?)",
                       (encoded, (today or date.today()).isoformat(), position, programme["revision"]))

    @staticmethod
    def _movement_matched(planned, actual_names):
        if planned in actual_names:
            return True
        tokens = set(planned.split())
        if len(tokens) >= 2:
            return any(tokens <= set(act.split()) for act in actual_names)
        return False

    @staticmethod
    def _completion_evidence(plan, day, outcomes, reported):
        """Require a linked user completion or sufficient movement coverage in one observation."""
        if reported and reported.get("plan_revision_at_report") == plan["revision"]:
            return {"type": "user_reported_session_completion", "date": day}
        payload = plan["payload"]
        if payload.get("delivery") == "instructor_led":
            return None
        main_moves = [m for m in payload.get("exercises", [])
                      if m.get("role") not in ("warmup", "cooldown")]
        names = {canonical_exercise(m["name"]) for m in main_moves}
        anchors = {canonical_exercise(m["name"]) for m in main_moves if m.get("role") == "anchor"}
        for outcome in outcomes:
            activity = outcome["payload"]
            if outcome["date"] != day or outcome.get("classification") != "intentional_training":
                continue
            if payload["kind"] == "strength" and len(names) >= 2:
                actual = {canonical_exercise(m.get("exercise", "")) for m in activity.get("logged_sets", [])
                          if isinstance(m.get("sets"), int) and m["sets"] > 0}
                matched = {n for n in names if RotationStore._movement_matched(n, actual)}
                required = len(names) if len(names) < 4 else max(3, len(names) - 1)
                if len(matched) >= required and anchors <= matched:
                    return {"type": "observed_session_movements", "activity_id": outcome["activity_id"],
                            "caveat": "Movement participation, not proof of prescribed dose, form or effort."}
            # Quality intensity/interval execution cannot be inferred from modality or HR.
        return None

    def context(self, training, today=None):
        today = today or date.today()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM training_rotation ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return {"enabled": False}
            row = dict(row)
            sequence = json.loads(row["sequence_json"])
            reports = {r["date"]: r for r in training.get("user_reported_day_status", [])
                       if r["status"] == "user_completed"}
            plans = sorted(training.get("plans", []), key=lambda p: p["date"])
            unmatched = []
            for plan in plans:
                day = plan["date"]
                if not row["started_on"] <= day <= today.isoformat():
                    continue
                if plan["payload"].get("rotation_slot") != self._slot(row):
                    continue
                if reports.get(day) is None and plan.get("status") == "user_skipped":
                    continue
                evidence = self._completion_evidence(
                    plan, day, training.get("recent_outcomes", []), reports.get(day))
                if evidence:
                    db.execute("INSERT INTO training_rotation_completions VALUES (?,?,?,?)",
                               (self._slot(row), day, sequence[row["position"] % len(sequence)],
                                json.dumps(evidence)))
                    row["position"] += 1
                    db.execute("UPDATE training_rotation SET position=? WHERE id=?",
                               (row["position"], row["id"]))
                elif day < today.isoformat():
                    unmatched.append({"date": day, "slot": self._slot(row),
                                      "reason": "Completion unconfirmed; ask, do not infer rest or completion."})
            slot = self._slot(row)
            defer = db.execute("SELECT payload FROM training_rotation_deferrals WHERE slot=? "
                               "ORDER BY plan_date DESC LIMIT 1", (slot,)).fetchone()
            completed = [dict(r) for r in db.execute(
                "SELECT slot,completed_on,template_id FROM training_rotation_completions "
                "ORDER BY completed_on DESC, rowid DESC LIMIT 24")]
        latest = {}
        for item in completed:
            latest.setdefault(item["template_id"], item["completed_on"])
        return {
            "enabled": True, "slot": slot, "next_template_id": sequence[row["position"] % len(sequence)],
            "sequence": sequence, "started_on": row["started_on"],
            "upcoming": [sequence[(row["position"] + n) % len(sequence)] for n in range(len(sequence))],
            "last_completed_by_template": latest,
            "completed_today": [r for r in completed if r["completed_on"] == today.isoformat()],
            "deferral": json.loads(defer["payload"]) if defer else None,
            "unconfirmed_sessions": unmatched,
            "pending_today": any(p["date"] == today.isoformat()
                                 and p.get("status") != "user_skipped"
                                 and p["payload"].get("rotation_slot") == slot for p in plans),
            "future_plans": [
                {key: p["payload"][key] for key in (
                    "date", "kind", "program_template_id", "program_adjustment", "rotation_slot")
                 if key in p["payload"]}
                for p in plans if p["date"] > today.isoformat()
                and p.get("status") != "user_skipped"
                and p["payload"].get("program_template_id") in sequence],
            "interpretation": "Sequence of sessions, NOT consecutive calendar days. Rest does not advance "
                              "it. Completion is participation only, never proof of safe effort or full dose.",
        }

    def save_deferrals(self, plans):
        with self.connection() as db:
            for plan in plans:
                defer = plan.get("rotation_defer")
                if defer:
                    db.execute("INSERT INTO training_rotation_deferrals VALUES (?,?,?) "
                               "ON CONFLICT(slot,plan_date) DO UPDATE SET payload=excluded.payload",
                               (defer["slot"], plan["date"], json.dumps(defer)))


def rotation_plan_errors(plans, context, today=None):
    if not context.get("enabled"):
        return []
    today = today or date.today()
    errors = []
    for plan in plans:
        if plan["date"] != today.isoformat():
            continue
        matches = (plan.get("program_template_id") == context["next_template_id"]
                   and plan.get("rotation_slot") == context["slot"])
        if matches and plan["kind"] not in ("rest", "recovery"):
            if plan.get("rotation_defer"):
                errors.append("A session cannot both fulfil and defer the same rotation slot.")
            continue
        defer = plan.get("rotation_defer") or {}
        if not isinstance(defer, dict):
            errors.append("rotation_defer must be an object with slot, reason, reassess_on and resume_when")
            continue
        try:
            reassess = date.fromisoformat(defer.get("reassess_on", ""))
        except (ValueError, TypeError):
            reassess = None
        if (defer.get("slot") != context["slot"]
                or not reassess or not today <= reassess <= today + timedelta(days=7)
                or any(not isinstance(defer.get(k), str) or not defer[k].strip()
                       for k in ("reason", "resume_when"))):
            errors.append("Use the pending ROTATION_STATE template and rotation_slot, or supply "
                          "rotation_defer {slot, reason, reassess_on (within 7 days), resume_when}. "
                          "Do not lose the pending session when resting or substituting.")
        if plan.get("rotation_slot"):
            errors.append("A deferred or substitute session must not claim completion credit "
                          "for the pending rotation_slot.")
    _, future_errors = project_rotation(plans, context, today)
    errors.extend(future_errors)
    return errors


def project_rotation(plans, context, today=None):
    """Attach forecast slot identities without advancing actual progress."""
    result = copy.deepcopy(plans)
    if not context.get("enabled"):
        return result, []
    today = today or date.today()
    current = next((p for p in result if p["date"] == today.isoformat()), None)
    scheduled_today = (current.get("rotation_slot") == context["slot"] if current
                       else context.get("pending_today", False))
    offset = int(scheduled_today)
    epoch, position = map(int, context["slot"].split(":"))
    errors = []
    last_requested = max((p["date"] for p in result), default=today.isoformat())
    forecast = {p["date"]: copy.deepcopy(p) for p in context.get("future_plans", [])
                if p["date"] <= last_requested}
    forecast.update({p["date"]: p for p in result})
    for plan in sorted(forecast.values(), key=lambda p: p["date"]):
        if plan["date"] <= today.isoformat() or plan["kind"] in ("rest", "recovery"):
            continue
        expected = context["upcoming"][offset % len(context["upcoming"])]
        if plan.get("program_template_id") == expected:
            plan["rotation_slot"] = f"{epoch}:{position + offset}"
            offset += 1
        elif isinstance(plan.get("program_adjustment"), str) and plan["program_adjustment"].strip():
            plan.pop("rotation_slot", None)
        else:
            errors.append(f"Future {plan['date']} should name program_template_id={expected}, "
                          "or explain an off-rotation substitute with program_adjustment. "
                          "Recovery days do not skip pending sessions.")
    return result, errors
