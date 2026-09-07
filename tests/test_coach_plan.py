import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from coach_plan import TrainingStore, parse_plans, render_plans, unsupported_claims


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

    def test_future_outline_cannot_masquerade_as_placeholder_working_sets(self):
        _, _, errors = self.parse(self.plan(kind="cardio", detail_level="outline", exercises=[
            {"name": "Synthetic bike", "sets": [{"reps": "outline only"}]}]))
        self.assertTrue(errors)
        outline = self.plan(kind="cardio", detail_level="outline")
        _, _, errors = self.parse(outline)
        self.assertFalse(errors)
        self.assertIn("provisional outline", render_plans([outline]))

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


if __name__ == "__main__":
    unittest.main()
