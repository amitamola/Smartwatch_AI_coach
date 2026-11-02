import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from coach_plan import (
    TrainingStore, canonical_exercise, completion_matches, parse_plans,
    prescription_identity, render_plans, unsupported_claims,
)


class PlanTests(unittest.TestCase):
    def plan(self, **changes):
        result = {"date": "2026-01-01", "kind": "rest", "objective": "Recover",
                  "reason": "Agreed recovery day", "exercises": []}
        result.update(changes)
        return result

    def parse(self, plan, preferences=()):
        return parse_plans("[[SESSION_PLAN: " + json.dumps(plan) + "]]",
                           preferences, today=date(2026, 1, 1))

    def test_rest_day_cannot_hide_working_sets(self):
        _, _, errors = self.parse(self.plan(exercises=[{"name": "Squat"}]))
        self.assertTrue(errors)

    def test_default_dates_and_persistence_use_shared_athlete_clock(self):
        instant = datetime(2026, 1, 1, 0, 15, tzinfo=timezone(timedelta(hours=10)))
        with patch("coach_plan.coach_clock.today", return_value=instant.date()), \
                patch("coach_plan.coach_clock.now", return_value=instant), \
                tempfile.TemporaryDirectory() as folder:
            plan = self.plan()
            _, parsed, errors = parse_plans("[[SESSION_PLAN: " + json.dumps(plan) + "]]")
            self.assertFalse(errors)
            self.assertIn("Today", render_plans(parsed))
            store = TrainingStore(Path(folder) / "plans.db")
            store.save(parsed)
            store.set_status(plan["date"], "user_completed")
            store.record_activities([{"activity_id": "clock", "start": plan["date"]}])
            self.assertEqual(len(store.raw_activities()), 1)
            context = store.context()
            self.assertEqual(context["plans"][0]["date"], plan["date"])
            self.assertEqual(context["user_reported_day_status"][0]["updated_at"],
                             instant.isoformat(timespec="seconds"))

    def test_structured_sets_are_visible_with_units_and_rest(self):
        text = render_plans([self.plan(kind="strength", exercises=[{
            "name": "Synthetic press", "weight_basis": "per_hand",
            "sets": [{"reps": "8-10", "weight_kg": 7, "rest_seconds": 90},
                     {"reps": 8, "weight_kg": None, "rest_seconds": 90}],
            "effort": "Keep some reserve"}])])
        self.assertIn("Set 1: 8-10 reps; 7 kg (per hand); rest 90 s", text)
        self.assertIn("Set 2: 8 reps; load unspecified", text)
        self.assertIn("Keep some reserve", text)

    def test_malformed_set_list_is_rejected_before_rendering(self):
        _, _, errors = self.parse(self.plan(kind="strength", exercises=[
            {"name": "Synthetic press", "sets": [4]}]))
        self.assertTrue(errors)

    def test_finite_doses_units_and_null_unknowns(self):
        invalid = [
            {"reps": -1}, {"reps": True}, {"reps": 2.5}, {"reps": "10-8"},
            {"reps": "unknown"}, {"reps": 8, "weight_kg": float("nan")},
            {"reps": 8, "weight_kg": -1}, {"reps": 8, "rest_seconds": -1},
            {"duration_seconds": float("inf")}, {"duration_seconds": "five minutes"},
            {"duration_seconds": 0}, {"duration_seconds": 60, "rounds": False},
            {"duration_seconds": 60, "duration_minutes": 1},
            {"duration": 60}, {"reps": 8, "weight_lbs": 12}, {},
            {"effort": "gentle"}, {"duration_seconds": 60, "rpe": 11},
        ]
        for field in ("sets", "blocks"):
            for dose in invalid:
                with self.subTest(field=field, dose=dose):
                    self.assertTrue(self.parse(self.plan(kind="mixed", exercises=[
                        {"name": "Synthetic movement", field: [dose]}]))[2])
            self.assertFalse(self.parse(self.plan(kind="mixed", exercises=[
                {"name": "Synthetic movement", field: [
                    {"reps": None, "weight_kg": None, "rest_seconds": None}]}]))[2])
        for valid_reps in ("10", "8-10 each side", "10 per side"):
            with self.subTest(valid_reps=valid_reps):
                self.assertFalse(self.parse(self.plan(kind="strength", exercises=[
                    {"name": "Synthetic movement", "sets": [{"reps": valid_reps}]}]))[2])
        future_outline = {"date": "2026-01-02", "kind": "cardio", "detail_level": "outline",
                          "objective": "Provisional aerobic ride", "exercises": []}
        self.assertFalse(self.parse(future_outline)[2])
        self.assertTrue(self.parse(self.plan(kind="cardio", exercises=[
            {"name": "Synthetic walk", "blocks": ["10 minutes"]}]))[2])

    def detailed_plan(self, **changes):
        return self.plan(kind="strength", exercises=[
            {"name": "Synthetic preparation", "phase": "warmup",
             "blocks": [{"duration_seconds": 60}]},
            {"name": "Synthetic press", "phase": "main", "equipment": "dumbbell",
             "sets": [{"reps": 8, "weight_kg": None, "rest_seconds": 60}]},
            {"name": "Synthetic cool-down", "phase": "cooldown",
             "blocks": [{"duration_seconds": 60}]},
        ], **changes)

    def test_strict_phase_contract_is_opt_in_and_does_not_invent_doses(self):
        plan = self.detailed_plan()
        def strict(value, **kwargs):
            return parse_plans("[[SESSION_PLAN: " + json.dumps(value) + "]]",
                               today=date(2026, 1, 1), require_phases=True, **kwargs)
        self.assertFalse(strict(plan)[2])
        missing = {**plan, "exercises": plan["exercises"][1:]}
        self.assertTrue(strict(missing)[2])
        self.assertFalse(self.parse(missing)[2])
        self.assertTrue(strict({**plan, "exercises": list(reversed(plan["exercises"]))})[2])
        self.assertFalse(strict(self.plan())[2])
        self.assertFalse(strict(self.plan(kind="recovery", exercises=[
            {"name": "Synthetic walk", "blocks": [{"duration_minutes": 5}]}]))[2])
        self.assertFalse(strict(self.plan(kind="strength", detail_level="outline"))[2])
        self.assertFalse(strict(self.plan(kind="cardio", delivery="instructor_led",
                                         participation_guidance=["Confirm class content first"]))[2])
        rendered = render_plans([plan])
        for heading in ("Warm-up / ramp-up", "Main session", "Cool-down", "Working sets"):
            self.assertIn(heading, rendered)

    def test_known_duration_and_equipment_constraints_are_enforced(self):
        text = "[[SESSION_PLAN: " + json.dumps(self.detailed_plan()) + "]]"
        def validate(constraints):
            return parse_plans(text, today=date(2026, 1, 1), require_phases=True,
                               constraints=constraints)[2]
        self.assertFalse(validate({"max_duration_minutes": 5, "available_equipment": ["dumbbell"]}))
        self.assertTrue(validate({"max_duration_minutes": 2}))
        self.assertTrue(validate({"available_equipment": ["cable"]}))
        self.assertFalse(validate({}))

    def test_unsupported_claims_in_rendered_structured_fields_are_rejected(self):
        for field in ("objective", "reason"):
            with self.subTest(field=field):
                self.assertTrue(self.parse(self.plan(**{
                    field: "You are cleared to train."}))[2])
        plan = self.detailed_plan()
        plan["exercises"][1]["effort"] = "This is guaranteed back-safe."
        self.assertTrue(self.parse(plan)[2])

    def test_missing_optional_food_logs_never_justify_training_changes(self):
        for reason in ("No food logs, so reduce training.",
                       "Take a rest day because no food entries.",
                       "Food was not logged, therefore underfueling is likely."):
            self.assertTrue(self.parse(self.plan(reason=reason))[2])
        self.assertFalse(self.parse(self.plan(
            reason="Food logging is optional. No food logs does not mean poor intake."))[2])

    def test_apparatus_remains_part_of_movement_identity(self):
        self.assertNotEqual(canonical_exercise("DB RDL"), canonical_exercise("Barbell RDL"))
        self.assertEqual(canonical_exercise("DB RDL"),
                         canonical_exercise("Dumbbell Romanian Deadlift"))

    def test_completion_identity_survives_cosmetics_not_substantive_revisions(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TrainingStore(Path(folder) / "plans.db")
            original = self.detailed_plan()
            store.save([original], source_text="Synthetic source")
            returned = store.set_status("2026-01-01", "user_completed",
                                        source="explicit_user", session_id="synthetic-session")
            cosmetic = {**original, "reason": "A rephrased reason", "objective": "Reworded title"}
            store.save([cosmetic])
            context = store.context(date(2026, 1, 1))
            report = context["user_reported_day_status"][0]
            self.assertTrue(completion_matches(context["plans"][0], report))
            self.assertEqual(returned["prescription_id_at_report"], prescription_identity(original))
            self.assertEqual(report["completion_session_id"], "synthetic-session")
            changed = json.loads(json.dumps(cosmetic))
            changed["exercises"][1]["sets"][0]["reps"] = 9
            store.save([changed])
            self.assertFalse(completion_matches(store.context(date(2026, 1, 1))["plans"][0], report))
            with store.connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM training_plan_events").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT source_text FROM training_plan_sources "
                                            "WHERE revision=1").fetchone()[0], "Synthetic source")

    def test_legacy_completion_identity_uses_original_event_not_latest_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plans.db"
            store = TrainingStore(path)
            original = self.detailed_plan()
            store.save([original])
            store.set_status("2026-01-01", "user_completed")
            with store.connection() as db:
                db.execute("DELETE FROM training_completion_identity")
            store.save([{**original, "reason": "Cosmetic update"}])
            migrated = TrainingStore(path).context(date(2026, 1, 1))
            self.assertEqual(migrated["user_reported_day_status"][0]["prescription_id_at_report"],
                             prescription_identity(original))
            self.assertTrue(completion_matches(migrated["plans"][0],
                                               migrated["user_reported_day_status"][0]))

    def test_future_outline_cannot_masquerade_as_placeholder_working_sets(self):
        _, _, errors = self.parse(self.plan(kind="cardio", detail_level="outline", exercises=[
            {"name": "Synthetic bike", "sets": [{"reps": "outline only"}]}]))
        self.assertTrue(errors)
        outline = self.plan(kind="cardio", detail_level="outline")
        _, _, errors = self.parse(outline)
        self.assertFalse(errors)
        self.assertIn("provisional outline", render_plans([outline]))

    def test_future_schedule_is_a_compact_card_not_repeated_reason_blocks(self):
        text = render_plans([self.plan(kind="strength", detail_level="outline"),
                             self.plan(date="2026-01-02", detail_level="outline")])
        self.assertEqual(text.count("Coming up"), 1)
        self.assertNotIn("Reason:", text)
        self.assertNotIn("Session plan -", text)
        self.assertEqual(text.count("provisional outline"), 2)

    def test_invalidated_plans_render_only_history_warning_not_prescription(self):
        detailed = self.detailed_plan(projection_status="needs_replanning",
                                      projection_reason="The pending rotation changed.")
        rendered = render_plans([detailed])
        self.assertIn("Needs replanning", rendered)
        self.assertIn("not an active prescription", rendered)
        self.assertNotIn("Synthetic press", rendered)
        self.assertNotIn("Set 1:", rendered)
        self.assertNotIn("Block 1:", rendered)
        outline = {**detailed, "detail_level": "outline", "exercises": []}
        self.assertNotIn("provisional outline", render_plans([outline]))

    def test_avoidance_survives_symptom_clear(self):
        preference = {"key": "avoid_exercise:normal RDL", "status": "active"}
        _, _, errors = self.parse(
            self.plan(kind="strength", exercises=[{"name": "Romanian Deadlift"}]),
            [preference])
        self.assertTrue(errors)
        _, _, errors = self.parse(
            self.plan(kind="strength", exercises=[{"name": "Single-leg RDL"}]),
            [preference])
        self.assertFalse(errors)  # Not equivalent to a claim that the variant is safe.
        for name in ("DB RDL", "Romanian Deadlift With Dumbbells", "Barbell Romanian Deadlift"):
            with self.subTest(name=name):
                _, _, errors = self.parse(
                    self.plan(kind="strength", exercises=[{"name": name}]), [preference])
                self.assertTrue(errors)

    def test_plan_revisions_are_not_performance_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TrainingStore(Path(folder) / "plans.db")
            store.save([self.plan()])
            store.save([self.plan()])
            store.save([self.plan(objective="Updated recovery")], source="user_revision")
            context = store.context(date(2026, 1, 1))
            self.assertEqual(context["plans"][0]["revision"], 2)
            self.assertEqual(context["recent_outcomes"], [])
            store.set_status("2026-01-01", "user_completed")
            self.assertEqual(store.context(date(2026, 1, 1))["plans"][0]["status"],
                             "user_completed")
            store.save([self.plan(objective="A later proposal")])
            latest = store.context(date(2026, 1, 1))["plans"][0]
            self.assertEqual(latest["status"], "user_completed")
            self.assertEqual(latest["proposal_status"], "proposed")

    def test_explicitly_rejecting_false_claim_is_not_a_violation(self):
        self.assertTrue(unsupported_claims("This is a zero-spinal-load exercise."))
        self.assertFalse(unsupported_claims("This is not a zero-spinal-load exercise."))

    def test_user_status_is_kept_without_a_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TrainingStore(Path(folder) / "plans.db")
            store.set_status("2026-01-01", "user_skipped")
            self.assertEqual(store.context(date(2026, 1, 1))["user_reported_day_status"][0]
                             ["status"], "user_skipped")
            store.save([self.plan()])
            self.assertEqual(store.context(date(2026, 1, 1))["plans"][0]["status"],
                             "user_skipped")

    def test_completion_context_does_not_change_with_a_later_plan_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TrainingStore(Path(folder) / "plans.db")
            store.save([self.plan(kind="strength")])
            store.set_status("2026-01-01", "user_completed")
            store.save([self.plan(kind="recovery")])
            report = store.context(date(2026, 1, 1))["user_reported_day_status"][0]
            self.assertEqual(report["plan_kind_at_report"], "strength")
            self.assertEqual(report["plan_revision_at_report"], 1)
            store.set_status("2026-01-01", "user_skipped")
            self.assertIsNone(store.context(date(2026, 1, 1))["user_reported_day_status"][0]
                              ["plan_kind_at_report"])

    def test_completion_without_a_plan_does_not_infer_a_later_proposal_was_done(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TrainingStore(Path(folder) / "plans.db")
            store.set_status("2026-01-01", "user_completed")
            store.save([self.plan(kind="strength")])
            self.assertIsNone(store.context(date(2026, 1, 1))["user_reported_day_status"][0]
                              ["plan_kind_at_report"])

    def test_latest_movement_uses_workout_time_not_upload_identifier(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TrainingStore(Path(folder) / "plans.db")
            store.record_activities([
                {"activity_id": "20", "start": "2026-01-01 08:00:00",
                 "logged_sets": [{"exercise": "Test press", "sets": 2}]},
                {"activity_id": "10", "start": "2026-01-01 18:00:00",
                 "logged_sets": [{"exercise": "Test press", "sets": 3}]},
            ])
            movement = store.context(date(2026, 1, 1))["latest_observed_per_movement"]
            self.assertEqual(movement["test press"]["activity_id"], "10")

    def test_programme_reviews_can_get_all_observed_sessions_not_just_last_twelve(self):
        today = date(2026, 1, 28)
        with tempfile.TemporaryDirectory() as folder:
            store = TrainingStore(Path(folder) / "plans.db")
            store.record_activities([
                {"activity_id": str(n), "start": (today - timedelta(days=n)).isoformat()}
                for n in range(20)
            ] + [{"activity_id": "future", "start": "2026-01-29"}])
            self.assertEqual(len(store.context(today)["recent_outcomes"]), 12)
            all_context = store.context(today, outcome_limit=None)
            self.assertEqual(len(all_context["recent_outcomes"]), 20)
            self.assertEqual(all_context["recent_outcomes_omitted"], 0)
            self.assertNotIn("future", [r["activity_id"] for r in all_context["recent_outcomes"]])
            with self.assertRaises(ValueError):
                store.context(today, outcome_limit=0)


if __name__ == "__main__":
    unittest.main()
