"""Persistent session rotation. Proposals never count as completed training."""

import json
import copy
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import coach_clock
from coach_plan import canonical_exercise, completion_matches, prescription_identity


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
    names = {canonical_exercise(m["name"]) for t in templates.values() for m in t["exercises"]
             if m.get("role") not in ("warmup", "cooldown", "preparation", "recovery")
             and m.get("phase") not in ("warmup", "cooldown")}
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
                       (encoded, (today or coach_clock.today()).isoformat(), position, programme["revision"]))

    @staticmethod
    def _movement_matched(planned, actual_names):
        aliases = {
            "weighted standing calf raise": "standing calf raise",
            "reverse crunch on a bench": "reverse crunch",
            "reverse crunch on bench": "reverse crunch",
        }
        planned = canonical_exercise(planned)
        planned = aliases.get(planned, planned)
        return any(aliases.get(canonical_exercise(actual), canonical_exercise(actual)) == planned
                   for actual in actual_names)

    @staticmethod
    def _completion_evidence(plan, day, outcomes, reported):
        """Require a linked user completion or sufficient movement coverage in one observation."""
        if plan["payload"].get("projection_status") == "needs_replanning":
            return None
        if completion_matches(plan, reported):
            return {"type": "user_reported_session_completion", "date": day,
                    "prescription_id": plan.get("prescription_id") or prescription_identity(plan["payload"]),
                    "source": reported.get("completion_source"),
                    "session_id": reported.get("completion_session_id")}
        if reported:
            return None
        payload = plan["payload"]
        if payload.get("delivery") == "instructor_led":
            return None
        main_moves = [m for m in payload.get("exercises", [])
                      if m.get("role") not in ("warmup", "cooldown", "preparation", "recovery")
                      and m.get("phase") not in ("warmup", "cooldown")]
        names = {canonical_exercise(m["name"]) for m in main_moves}
        anchors = {canonical_exercise(m["name"]) for m in main_moves if m.get("role") == "anchor"}
        for outcome in outcomes:
            activity = outcome["payload"]
            if outcome["date"] != day or outcome.get("classification") != "intentional_training":
                continue
            if payload["kind"] == "strength" and len(names) >= 2:
                actual = {canonical_exercise(m.get("exercise", "")) for m in activity.get("logged_sets", [])
                          if isinstance(m.get("sets"), int) and not isinstance(m["sets"], bool)
                          and m["sets"] > 0}
                matched = {n for n in names if RotationStore._movement_matched(n, actual)}
                required = len(names) if len(names) < 4 else max(3, len(names) - 1)
                if len(matched) >= required and anchors <= matched:
                    return {"type": "observed_session_movements", "activity_id": outcome["activity_id"],
                            "caveat": "Movement participation, not proof of prescribed dose, form or effort."}
            # Quality intensity/interval execution cannot be inferred from modality or HR.
        return None

    def context(self, training, today=None):
        """Read-only rotation view. Call reconcile explicitly after ingesting evidence."""
        return self._context(training, today, reconcile=False)

    def preview(self, training, reports=(), today=None):
        """Simulate rotation state after candidate user reports without mutating SQLite."""
        if not reports:
            return self.context(training, today)
        staged = copy.deepcopy(training)
        plans_by_day = {p["date"]: p for p in staged.get("plans", [])}
        by_day = {r["date"]: dict(r) for r in staged.get("user_reported_day_status", [])}
        for report in reports:
            day = report["date"]
            status = report["status"]
            plan = plans_by_day.get(day)
            if plan is not None:
                plan["status"] = status
            payload = plan["payload"] if plan else None
            by_day[day] = {
                "date": day,
                "status": status,
                "plan_kind_at_report": payload.get("kind") if payload else None,
                "plan_revision_at_report": plan.get("revision") if plan else None,
                "prescription_id_at_report": (
                    plan.get("prescription_id") or prescription_identity(payload)
                ) if payload else None,
                "rotation_slot_at_report": payload.get("rotation_slot") if payload else None,
                "completion_source": report.get("source_quote"),
                "completion_session_id": None,
            }
        staged["user_reported_day_status"] = list(by_day.values())
        return self._context(staged, today, reconcile=False, simulate=True)

    def reconcile(self, training, today=None):
        """Persist newly supported participation, idempotently; return the resulting view."""
        return self._context(training, today, reconcile=True)

    @staticmethod
    def _cardio_candidates(plan, outcomes):
        if plan["payload"].get("kind") not in ("cardio", "mixed"):
            return []
        candidates = []
        for outcome in outcomes:
            activity = outcome["payload"]
            modality = str(activity.get("type", "")).lower()
            if (outcome["date"] != plan["date"]
                    or outcome.get("classification") != "intentional_training"
                    or not any(word in modality for word in
                               ("run", "cycl", "bik", "walk", "swim", "row", "ellipt", "cardio"))):
                continue
            candidates.append({
                "date": plan["date"], "slot": plan["payload"].get("rotation_slot"),
                "activity_id": outcome["activity_id"], "activity_type": activity.get("type"),
                "completion": "unconfirmed",
                "interpretation": "Candidate participation only. Ask whether this was the planned "
                                  "session; modality, duration and HR do not prove intervals or dose.",
            })
        return candidates

    def _context(self, training, today=None, reconcile=False, simulate=False):
        today = today or coach_clock.today()
        with self.connection() as db:
            if reconcile:
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
            cardio_candidates = []
            awaiting_reconciliation = []
            simulated_completed = []
            for plan in plans:
                day = plan["date"]
                if not row["started_on"] <= day <= today.isoformat():
                    continue
                if plan["payload"].get("projection_status") == "needs_replanning":
                    continue
                if plan["payload"].get("rotation_slot") != self._slot(row):
                    continue
                if reports.get(day) is None and plan.get("status") == "user_skipped":
                    continue
                evidence = self._completion_evidence(
                    plan, day, training.get("recent_outcomes", []), reports.get(day))
                if evidence and reconcile:
                    db.execute("INSERT INTO training_rotation_completions VALUES (?,?,?,?)",
                               (self._slot(row), day, sequence[row["position"] % len(sequence)],
                                json.dumps(evidence)))
                    row["position"] += 1
                    db.execute("UPDATE training_rotation SET position=? WHERE id=?",
                               (row["position"], row["id"]))
                elif evidence and simulate:
                    simulated_completed.append({
                        "slot": self._slot(row),
                        "completed_on": day,
                        "template_id": sequence[row["position"] % len(sequence)],
                    })
                    row["position"] += 1
                elif evidence:
                    awaiting_reconciliation.append({"date": day, "slot": self._slot(row),
                                                    "evidence": evidence})
                else:
                    cardio_candidates.extend(self._cardio_candidates(
                        plan, training.get("recent_outcomes", [])))
                if not evidence and day < today.isoformat():
                    unmatched.append({"date": day, "slot": self._slot(row),
                                      "reason": "Completion unconfirmed; ask, do not infer rest or completion."})
            slot = self._slot(row)
            defer = db.execute("SELECT payload FROM training_rotation_deferrals WHERE slot=? "
                               "ORDER BY plan_date DESC LIMIT 1", (slot,)).fetchone()
            completed = list(reversed(simulated_completed)) + [dict(r) for r in db.execute(
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
            "cardio_participation_candidates": cardio_candidates,
            "awaiting_reconciliation": awaiting_reconciliation,
            "pending_today": any(p["date"] == today.isoformat()
                                 and p.get("status") != "user_skipped"
                                 and p["payload"].get("projection_status") != "needs_replanning"
                                 and p["payload"].get("rotation_slot") == slot for p in plans),
            "committed_future_dates": [p["date"] for p in plans
                                       if p["date"] > today.isoformat()
                                       and p.get("status") == "user_planned"],
            "future_plans": [
                copy.deepcopy(p["payload"])
                for p in plans if p["date"] > today.isoformat()
                and p.get("status") != "user_skipped"
                and p["payload"].get("projection_status") != "needs_replanning"
                and (p["payload"].get("program_template_id")
                     or p["payload"].get("rotation_slot")
                     or p["payload"].get("rotation_defer"))],
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
    today = today or coach_clock.today()
    errors = []
    for plan in plans:
        if plan["date"] != today.isoformat():
            continue
        if plan.get("projection_status") == "needs_replanning":
            errors.append("An invalidated forecast is retained history, not today's active prescription.")
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
    """Return incoming plans AND every changed saved forecast, with complete payloads.

    Stale provisional rows are explicitly invalidated, not silently rewritten into a
    different workout. Explicit incoming conflicts and fixed commitments require review.
    No mutation is persisted here: callers must save the entire returned list on success.
    """
    result = copy.deepcopy(plans)
    if not context.get("enabled"):
        return result, []
    today = today or coach_clock.today()
    current = next((p for p in result if p["date"] == today.isoformat()), None)
    scheduled_today = (current.get("rotation_slot") == context["slot"]
                       and current.get("kind") not in ("rest", "recovery") if current
                       else context.get("pending_today", False))
    if current and current.get("projection_status") == "needs_replanning":
        scheduled_today = False
    offset = int(scheduled_today)
    epoch, position = map(int, context["slot"].split(":"))
    errors = []
    old = {p["date"]: copy.deepcopy(p) for p in context.get("future_plans", [])}
    incoming_dates = {p["date"] for p in result}
    forecast = copy.deepcopy(old)
    forecast.update({p["date"]: p for p in result})
    for plan in sorted(forecast.values(), key=lambda p: p["date"]):
        if plan["date"] <= today.isoformat():
            continue
        if plan.get("projection_status") == "needs_replanning":
            plan.pop("rotation_slot", None)
            continue
        if plan["kind"] in ("rest", "recovery"):
            plan.pop("rotation_slot", None)
            if isinstance(plan.get("rotation_defer"), dict):
                old_slot = str(plan["rotation_defer"].get("slot", ""))
                parts = old_slot.split(":")
                if (len(parts) == 2 and all(p.isdigit() for p in parts)
                        and int(parts[0]) == epoch and int(parts[1]) < position + offset
                        and plan["date"] not in incoming_dates):
                    plan.pop("rotation_defer", None)
                    plan["projection_status"] = "needs_replanning"
                    plan["projection_reason"] = (
                        f"Deferred slot {old_slot} is now completed; replan {plan['date']} "
                        f"for {context['upcoming'][offset % len(context['upcoming'])]}.")
                else:
                    plan["rotation_defer"]["slot"] = f"{epoch}:{position + offset}"
            continue
        expected = context["upcoming"][offset % len(context["upcoming"])]
        if plan.get("program_template_id") == expected:
            plan["rotation_slot"] = f"{epoch}:{position + offset}"
            plan.pop("projection_status", None)
            plan.pop("projection_reason", None)
            offset += 1
        elif isinstance(plan.get("program_adjustment"), str) and plan["program_adjustment"].strip():
            plan.pop("rotation_slot", None)
        else:
            message = (f"Future {plan['date']} should name program_template_id={expected}, "
                       "or explain an off-rotation substitute with program_adjustment. "
                       "Recovery days do not skip pending sessions.")
            committed = (plan.get("delivery") == "instructor_led" or plan.get("start_time")
                         or plan.get("commitment") or plan.get("user_committed")
                         or plan["date"] in context.get("committed_future_dates", []))
            if plan["date"] in incoming_dates or committed:
                errors.append(message)
            else:
                plan.pop("rotation_slot", None)
                plan["projection_status"] = "needs_replanning"
                plan["projection_reason"] = message
    for day, plan in sorted(forecast.items()):
        if day not in incoming_dates and plan != old.get(day):
            result.append(plan)
    return result, errors
