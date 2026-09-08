import copy
import json
import unittest
from datetime import date, timedelta

from coach_context import compact_workouts, plan_request_scope, schedule_claim_errors, strip_schedule_rendering
from coach_plan import exercise_excluded, parse_plans, unsupported_claims
from coach_program import build_review_evidence


class CoachingContextTests(unittest.TestCase):
    def test_target_and_explicit_maximum_are_distinct(self):
        for text, limit in [
            ("Four days per week is my starting training availability.", None),
            ("I can train at most four days per week.", 4),
            ("I can only train three days per week.", 3),
            ("No more than two training days per week.", 2),
            ("I don't want a maximum of four days per week.", None),
        ]:
            with self.subTest(text=text):
                evidence = build_review_evidence({}, [], text)
                self.assertEqual(evidence["hard_weekly_limit"], limit)

    def test_variant_patch_keeps_the_other_exercises(self):
        today = date.today().isoformat()
        original = {"date": today, "kind": "strength", "objective": "Practice",
                    "reason": "Existing proposal", "exercises": [
                        {"name": "Synthetic extension", "sets": [{"reps": 10, "weight_kg": None}]},
                        {"name": "Synthetic row", "sets": [{"reps": 8, "weight_kg": 5}]}]}
        before = copy.deepcopy(original)
        patch = {"date": today, "exercise": "Synthetic extension", "replacement": {
            "name": "Cable synthetic extension", "weight_basis": "machine_stack",
            "sets": [{"reps": 10, "weight_kg": None}], "effort": "Assess tolerable effort"},
            "reason": "Clarify apparatus without transferring an unknown load", "program_revision": 2}
        text, plans, errors = parse_plans("[[SESSION_PATCH: " + json.dumps(patch) + "]]",
                                           existing_plans=[original])
        self.assertFalse(errors)
        self.assertEqual(text, "")
        self.assertEqual(plans[0]["exercises"][1], original["exercises"][1])
        self.assertEqual(plans[0]["exercises"][0]["name"], "Cable synthetic extension")
        self.assertIsNone(plans[0]["exercises"][0]["sets"][0]["weight_kg"])
        self.assertEqual(original, before)
        patch["exercise"] = "Missing movement"
        self.assertTrue(parse_plans("[[SESSION_PATCH: " + json.dumps(patch) + "]]",
                                    existing_plans=[original])[2])

    def test_family_exclusion_does_not_collapse_unrelated_variants(self):
        standard = [{"key": "avoid_exercise:romanian_deadlift"}]
        family = [{"key": "avoid_family:romanian_deadlift"}]
        self.assertTrue(exercise_excluded("Dumbbell RDL", standard))
        self.assertFalse(exercise_excluded("Single-leg RDL", standard))
        self.assertTrue(exercise_excluded("Cable RDL", family))
        self.assertTrue(exercise_excluded("Single-leg RDL", family))
        self.assertFalse(exercise_excluded("Cable row", family))
        self.assertFalse(exercise_excluded("Cable RDL", [{**family[0], "status": "resolved"}]))

    def test_audited_false_claims_are_rejected_across_reply_types(self):
        for text in [
            "This exercise works without spinal strain.",
            "Your muscular reserves are untouched.",
            "Deep neural recovery is running behind.",
            "Productive only occurs when chronic load continually increases.",
            "Maintaining confirms your muscular capacity is preserved.",
            "Moderate readiness indicates capacity for light activity.",
            "Thirty is irrelevant: 30 hours is insufficient for complete muscular recovery.",
            "That set keeps 1-2 reps in reserve.",
            "Holding Thursday as rest ensures your back fully recovers.",
            "You are cleared to join the evening class.",
            "Systemic recovery should be solid.",
            "Moving to Productive simply requires ratcheting acute training load higher.",
            "Your recovery time should clear before the class.",
            "A fifth rolling session is fine given your energy.",
            "Maintaining is a strong outcome that protects muscle and current capacity.",
        ]:
            with self.subTest(text=text):
                self.assertTrue(unsupported_claims(text))
        for text in [
            "You reported no discomfort during that session.",
            "Sixteen repetitions do not establish your reps in reserve.",
            "I cannot claim that this exercise works without spinal strain.",
            "Readiness is moderate: good to go, not clearance for every intensity.",
            "Thirty hours does not guarantee complete recovery.",
            "Productive does not simply require increasing training load.",
            "A fifth session is not automatically safe because you feel energetic.",
            "Maintaining does not itself protect muscle in a calorie deficit.",
        ]:
            with self.subTest(text=text):
                self.assertFalse(unsupported_claims(text))

    def test_schedule_promises_must_match_returned_or_saved_plan(self):
        today = date(2026, 5, 19)
        thursday = today + timedelta(days=2)
        rest = {"date": thursday.isoformat(), "kind": "rest"}
        promise = "Thursday's lower-body or cardio work follows recovery."
        self.assertTrue(schedule_claim_errors(promise, [], [rest], today))
        self.assertFalse(schedule_claim_errors(
            promise, [{"date": thursday.isoformat(), "kind": "strength"}], [rest], today))
        self.assertFalse(schedule_claim_errors(
            "We could consider Thursday for cardio, depending on symptoms.", [], [rest], today))
        self.assertFalse(schedule_claim_errors(
            "The earlier outline showed Thursday for cardio.", [], [rest], today))

    def test_compaction_keeps_per_set_values_and_explicit_full_chronology(self):
        value = {"logged_sets": [
            {"exercise": "Synthetic press", "set_indices": [1, 3], "sets": 2,
             "set_sequence": [
                 {"sequence_index": 1, "set_type": "ACTIVE", "reps": 8, "weight_kg": 4},
                 {"sequence_index": 2, "set_type": "REST", "duration_s": 60},
                 {"sequence_index": 3, "set_type": "ACTIVE", "reps": 7, "weight_kg": 4}]}]}
        original = copy.deepcopy(value)
        concise = compact_workouts(value)
        self.assertNotIn("set_sequence", concise["logged_sets"][0])
        self.assertEqual([s["reps"] for s in concise["logged_sets"][0]["observed_sets"]], [8, 7])
        self.assertIn("Do not infer", concise["chronology_scope"])
        full = compact_workouts(value, full_chronology=True)
        self.assertEqual(len(full["logged_sets"][0]["set_sequence"]), 3)
        self.assertEqual(value, original)

    def test_plan_intents_and_single_calendar_owner(self):
        self.assertEqual(plan_request_scope(
            "The extension you recommended today, how exactly to do it? What machine?"), "today")
        self.assertFalse(plan_request_scope(
            "The extension you recommended today, how exactly to do it? What machine?",
            completed_today=True))
        self.assertEqual(plan_request_scope(
            "Could I join the full-body class tomorrow and keep Thursday as rest?"), "any")
        self.assertFalse(plan_request_scope("What does training readiness mean?"))
        self.assertFalse(plan_request_scope("Can I have oats for breakfast tomorrow?"))
        text = "Explanation.\n\n**Coming up**\n- Wed: Class\n- Thu: Rest\n\nA useful question."
        self.assertEqual(strip_schedule_rendering(text), "Explanation.\n\nA useful question.")


if __name__ == "__main__":
    unittest.main()
