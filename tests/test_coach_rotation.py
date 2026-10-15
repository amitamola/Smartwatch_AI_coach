import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from coach_plan import TrainingStore
from coach_rotation import (
    RotationStore, load_training_policy, project_rotation, rotation_plan_errors, validate_rotation,
)
from coach_program import build_review_evidence, parse_program_review
from tests.test_coach_program import programme, evidence, memory


TODAY = date(2026, 5, 20)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "coach.sqlite3"
        self.training = TrainingStore(self.path)
        self.store = RotationStore(self.path)
        self.record = {"revision": 1, "review": {"rotation": ["push", "pull", "cardio"]}}
        self.store.configure(self.record, TODAY)

    def context(self, today=TODAY):
        training = self.training.context(today, outcome_limit=None)
        for row in training["recent_outcomes"]:
            row["classification"] = "intentional_training"
        return self.store.context(training, today)

    def plan(self, day=TODAY, **changes):
        result = {"date": day.isoformat(), "kind": "strength", "program_template_id": "push",
                  "rotation_slot": "1:0", "exercises": [{"name": "Test press"}, {"name": "Test fly"}]}
        result.update(changes)
        return result

    def test_proposals_and_skips_never_advance_rotation(self):
        self.training.save([self.plan()])
        self.assertEqual(self.context()["slot"], "1:0")
        self.training.set_status(TODAY, "user_skipped")
        self.assertEqual(self.context()["slot"], "1:0")
        self.assertFalse(self.context()["pending_today"])
        projected, errors = project_rotation(
            [self.plan(TODAY + timedelta(days=1))], self.context(), TODAY)
        self.assertFalse(errors)
        self.assertEqual(projected[0]["rotation_slot"], "1:0")
        context = self.context(TODAY + timedelta(days=1))
        self.assertEqual(context["next_template_id"], "push")

    def test_user_completion_is_idempotent_and_survives_review_and_restart(self):
        self.training.save([self.plan()])
        self.training.set_status(TODAY, "user_completed")
        self.assertEqual(self.context()["next_template_id"], "pull")
        self.store = RotationStore(self.path)
        self.store.configure({**self.record, "revision": 2}, TODAY)
        self.assertEqual(self.context()["slot"], "1:1")
        self.assertEqual(len(self.context()["last_completed_by_template"]), 1)

    def test_new_revision_of_a_completed_day_cannot_receive_old_credit(self):
        self.training.save([self.plan()])
        self.training.set_status(TODAY, "user_completed")
        self.training.save([self.plan(objective="Changed prescription")])
        self.assertEqual(self.context()["slot"], "1:0")

    def test_complete_observed_movement_match_not_partial_or_transport(self):
        self.training.save([self.plan()])
        activity = {"activity_id": "a", "start": TODAY.isoformat(), "type": "strength_training",
                    "logged_sets": [{"exercise": "Test press", "sets": 3}]}
        self.training.record_activities([activity])
        self.assertEqual(self.context()["slot"], "1:0")
        activity["logged_sets"].append({"exercise": "Test fly", "sets": 2})
        self.training.record_activities([activity])
        context = self.training.context(TODAY)
        context["recent_outcomes"][0]["classification"] = "transport"
        self.assertEqual(self.store.context(context, TODAY)["slot"], "1:0")
        self.assertEqual(self.context()["slot"], "1:1")

    def test_cardio_modality_is_not_interval_completion(self):
        self.training.save([self.plan()])
        self.training.set_status(TODAY, "user_completed")
        self.context()
        day2 = TODAY + timedelta(days=1)
        self.training.save([self.plan(day2, program_template_id="pull", rotation_slot="1:1")])
        self.training.set_status(day2, "user_completed")
        self.context(day2)
        day3 = day2 + timedelta(days=1)
        self.training.save([self.plan(day3, kind="cardio", program_template_id="cardio",
                                     rotation_slot="1:2", exercises=[{"name": "running"}])])
        self.training.record_activities([{"activity_id": "run", "start": day3.isoformat(),
                                          "type": "running", "vigorous_intensity_min": 25}])
        self.assertEqual(self.context(day3)["slot"], "1:2")
        self.training.set_status(day3, "user_completed")
        self.assertEqual(self.context(day3)["next_template_id"], "push")
        self.assertEqual(self.context(day3)["slot"], "1:3")

    def test_deferral_is_timed_persistent_and_never_completion(self):
        context = self.context()
        rest = self.plan(kind="rest", exercises=[])
        self.assertTrue(rotation_plan_errors([rest], context, TODAY))
        rest.pop("rotation_slot")
        rest["rotation_defer"] = {"slot": "1:0", "reason": "Reported poor sleep",
                                  "reassess_on": (TODAY + timedelta(days=1)).isoformat(),
                                  "resume_when": "Reassess fresh symptoms and recovery"}
        self.assertFalse(rotation_plan_errors([rest], context, TODAY))
        self.store.save_deferrals([rest])
        self.assertEqual(self.context()["deferral"], rest["rotation_defer"])
        self.assertEqual(self.context()["slot"], "1:0")
        rest["rotation_defer"]["reassess_on"] = (TODAY + timedelta(days=8)).isoformat()
        self.assertTrue(rotation_plan_errors([rest], context, TODAY))
        rest["rotation_defer"] = "bad"
        # Malformed model fields must return a validation error, not crash generation.
        self.assertTrue(rotation_plan_errors([rest], context, TODAY))

    def test_template_or_rotation_change_preserves_pending_session(self):
        self.training.save([self.plan()])
        self.training.set_status(TODAY, "user_completed")
        self.context()
        self.store.configure({"revision": 2, "review": {"rotation": ["cardio", "push", "pull"]}}, TODAY)
        self.assertEqual(self.context()["next_template_id"], "pull")

    def test_future_projection_keeps_order_across_rest_without_completing_it(self):
        context = self.context()
        tomorrow = TODAY + timedelta(days=1)
        later = TODAY + timedelta(days=2)
        plans = [self.plan(), {"date": tomorrow.isoformat(), "kind": "rest"},
                 {"date": later.isoformat(), "kind": "strength", "program_template_id": "pull"}]
        projected, errors = project_rotation(plans, context, TODAY)
        self.assertFalse(errors)
        self.assertEqual(projected[-1]["rotation_slot"], "1:1")
        self.assertNotIn("rotation_slot", plans[-1])
        self.assertEqual(self.context()["slot"], "1:0")
        plans[-1]["program_template_id"] = "push"
        self.assertTrue(project_rotation(plans, context, TODAY)[1])

    def test_contract_requires_conventional_curl_and_cardio(self):
        review = {"session_templates": [
            {"id": "push", "kind": "strength", "exercises": [{"name": "Test press"}]},
            {"id": "pull", "kind": "strength", "exercises": [{"name": "Dumbbell Biceps Curl"}]},
            {"id": "cardio", "kind": "cardio", "exercises": [{"name": "running"}]}],
            "rotation": ["push", "cardio", "pull"]}
        policy = {"strength_order": ["push", "pull"], "cardio_templates": ["cardio"],
                  "required_exercises": ["Dumbbell Biceps Curl"]}
        validate_rotation(review, policy)
        missing = copy.deepcopy(review)
        missing["session_templates"][1]["exercises"] = [{"name": "Dumbbell Hammer Curl"}]
        with self.assertRaises(ValueError):
            validate_rotation(missing, policy)
        review["rotation"] = ["push", "pull"]
        with self.assertRaises(ValueError):
            validate_rotation(review, policy)

    def test_refining_later_session_preserves_earlier_saved_forecast_slots(self):
        tomorrow = TODAY + timedelta(days=1)
        later = TODAY + timedelta(days=3)
        self.training.save([self.plan(tomorrow),
                            self.plan(later, program_template_id="pull", rotation_slot="1:1")])
        update = self.plan(later, program_template_id="pull", objective="Refine the later session")
        update.pop("rotation_slot")
        projected, errors = project_rotation([update], self.context(), TODAY)
        self.assertFalse(errors)
        self.assertEqual(projected[0]["rotation_slot"], "1:1")
        self.assertEqual(self.context()["slot"], "1:0")
        self.training.set_status(tomorrow, "user_skipped")
        self.assertTrue(project_rotation([update], self.context(), TODAY)[1])

    def test_private_policy_missing_invalid_and_valid(self):
        path = Path(self.temp.name) / "training_policy.json"
        self.assertEqual(load_training_policy(path), {})
        path.write_text('{"frequency_mode":"propose"}')
        self.assertEqual(load_training_policy(path)["frequency_mode"], "propose")
        path.write_text('{"frequency_mode":"automatic-clearance"}')
        with self.assertRaises(ValueError):
            load_training_policy(path)

    def test_temporary_travel_does_not_replace_availability(self):
        source = "I have a flight today; sleep might be disrupted for two to three days."
        travel = memory(1, source, kind="preference", key="travel:flight_schedule")
        result = build_review_evidence({}, [travel], "Four days per week for training.", TODAY)
        self.assertEqual(result["weekly_training_days"], 4)
        self.assertEqual(result["weekly_training_days_refs"], ["profile"])

    def test_frequency_proposal_requires_permission_reason_and_honours_hard_limit(self):
        review = programme(weekly_training_days=5, rotation=["strength-a"],
                           frequency_reason="Revisable proposal based on recent tolerable participation.")
        sources = evidence()
        self.assertTrue(parse_program_review("[[TRAINING_PROGRAM: " + json.dumps(review) + "]]",
                                              sources)[2])
        sources["training_policy"] = {"frequency_mode": "propose"}
        self.assertFalse(parse_program_review("[[TRAINING_PROGRAM: " + json.dumps(review) + "]]",
                                               sources)[2])
        sources["hard_weekly_limit"] = 4
        self.assertTrue(parse_program_review("[[TRAINING_PROGRAM: " + json.dumps(review) + "]]",
                                              sources)[2])

    def test_two_cycles_across_multiple_weeks_keep_all_strength_and_cardio_slots(self):
        sequence = ["push", "pull", "aerobic-base", "legs", "upper", "aerobic-quality", "lower"]
        self.store.configure({"revision": 2, "review": {"rotation": sequence}}, TODAY)
        slots = set()
        for index in range(14):
            day = TODAY + timedelta(days=index * 2)
            context = self.context(day)
            self.assertEqual(context["next_template_id"], sequence[index % 7])
            self.assertNotIn(context["slot"], slots)
            slots.add(context["slot"])
            kind = "cardio" if "aerobic" in context["next_template_id"] else "strength"
            self.training.save([self.plan(day, kind=kind, program_template_id=context["next_template_id"],
                                         rotation_slot=context["slot"])])
            self.training.set_status(day, "user_completed")
            after = self.context(day)
            self.assertNotEqual(after["slot"], context["slot"])
            self.assertEqual(self.context(day + timedelta(days=1))["slot"], after["slot"])
        self.assertEqual(set(self.context(day)["last_completed_by_template"]), set(sequence))


if __name__ == "__main__":
    unittest.main()
