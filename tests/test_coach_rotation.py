import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

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
        return self.store.reconcile(training, today)

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

    def test_rotation_defaults_use_shared_athlete_date(self):
        local_day = TODAY + timedelta(days=1)
        with patch("coach_rotation.coach_clock.today", return_value=local_day):
            self.store.configure({"revision": 2, "review": {"rotation": ["pull", "push", "cardio"]}})
            context = self.store.context(self.training.context(local_day))
            self.assertEqual(context["started_on"], local_day.isoformat())
            future = self.plan(local_day + timedelta(days=1))
            projected, errors = project_rotation([future], context)
            self.assertFalse(errors)
            self.assertEqual(projected[0]["rotation_slot"], context["slot"])
            rest = self.plan(local_day, kind="rest", exercises=[])
            rest.pop("rotation_slot")
            rest["rotation_defer"] = {
                "slot": context["slot"], "reason": "Synthetic rest",
                "reassess_on": local_day.isoformat(), "resume_when": "Review tomorrow"}
            self.assertFalse(rotation_plan_errors([rest], context))

    def test_user_completion_is_idempotent_and_survives_review_and_restart(self):
        self.training.save([self.plan()])
        self.training.set_status(TODAY, "user_completed")
        self.assertEqual(self.context()["next_template_id"], "pull")
        self.store = RotationStore(self.path)
        self.store.configure({**self.record, "revision": 2}, TODAY)
        self.assertEqual(self.context()["slot"], "1:1")
        self.assertEqual(len(self.context()["last_completed_by_template"]), 1)

    def test_cosmetic_revision_of_a_completed_day_retains_credit(self):
        self.training.save([self.plan()])
        self.training.set_status(TODAY, "user_completed")
        self.training.save([self.plan(objective="Changed prescription")])
        self.assertEqual(self.context()["slot"], "1:1")

    def test_substantive_revision_of_a_completed_day_cannot_receive_old_credit(self):
        self.training.save([self.plan(exercises=[
            {"name": "Test press", "sets": [{"reps": 8, "weight_kg": 10}]},
            {"name": "Test fly", "sets": [{"reps": 8, "weight_kg": 5}]}])])
        self.training.set_status(TODAY, "user_completed")
        self.training.save([self.plan(exercises=[
            {"name": "Test press", "sets": [{"reps": 12, "weight_kg": 10}]},
            {"name": "Test fly", "sets": [{"reps": 8, "weight_kg": 5}]}])])
        self.assertEqual(self.context()["slot"], "1:0")

    def test_context_is_read_only_until_explicit_reconciliation(self):
        self.training.save([self.plan()])
        self.training.set_status(TODAY, "user_completed")
        training = self.training.context(TODAY)
        for _ in range(2):
            context = self.store.context(training, TODAY)
            self.assertEqual(context["slot"], "1:0")
            self.assertEqual(len(context["awaiting_reconciliation"]), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM training_rotation_completions").fetchone()[0], 0)
        self.assertEqual(self.store.reconcile(training, TODAY)["slot"], "1:1")
        self.assertEqual(self.store.context(training, TODAY)["slot"], "1:1")

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

    def test_multi_exercise_template_tolerates_garmin_qualifier_and_single_accessory_label(self):
        full_plan = self.plan(exercises=[
            {"name": "Wide Stance Goblet Squat", "role": "anchor"},
            {"name": "Dumbbell Bench Press", "role": "anchor"},
            {"name": "Seated Cable Row", "role": "anchor"},
            {"name": "Dumbbell Reverse Curl", "role": "accessory"},
            {"name": "Standing Calf Raise", "role": "accessory"},
            {"name": "Reverse Crunch", "role": "skill"},
        ])
        self.training.save([full_plan])
        activity = {
            "activity_id": "full-c", "start": TODAY.isoformat(), "type": "strength_training",
            "logged_sets": [
                {"exercise": "Wide Stance Goblet Squat", "sets": 2},
                {"exercise": "Dumbbell Bench Press", "sets": 2},
                {"exercise": "Seated Cable Row", "sets": 2},
                {"exercise": "Dumbbell Biceps Curl", "sets": 2},
                {"exercise": "Weighted Standing Calf Raise", "sets": 2},
                {"exercise": "Reverse Crunch On A Bench", "sets": 1},
            ],
        }
        self.training.record_activities([activity])
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
        candidate_context = self.context(day3)
        self.assertEqual(candidate_context["slot"], "1:2")
        self.assertEqual(candidate_context["cardio_participation_candidates"][0]["activity_id"], "run")
        self.assertEqual(candidate_context["cardio_participation_candidates"][0]["completion"],
                         "unconfirmed")
        self.training.set_status(day3, "user_completed")
        self.assertEqual(self.context(day3)["next_template_id"], "push")
        self.assertEqual(self.context(day3)["slot"], "1:3")

    def test_explicit_movement_aliases_never_erase_meaningful_variants(self):
        match = RotationStore._movement_matched
        self.assertTrue(match("Standing Calf Raise", {"Weighted Standing Calf Raise"}))
        self.assertTrue(match("Reverse Crunch", {"Reverse Crunch On A Bench"}))
        self.assertFalse(match("Biceps Curl", {"Dumbbell Hammer Biceps Curl"}))
        self.assertFalse(match("Dumbbell Biceps Curl", {"Dumbbell Hammer Biceps Curl"}))
        self.assertFalse(match("Romanian Deadlift", {"Single Leg Romanian Deadlift"}))
        self.assertFalse(match("Dumbbell Romanian Deadlift", {"Barbell Romanian Deadlift"}))

    def test_warmup_and_cooldown_do_not_expand_required_main_coverage(self):
        self.training.save([self.plan(exercises=[
            {"name": "Synthetic preparation", "phase": "warmup"},
            {"name": "Test press", "role": "anchor"}, {"name": "Test fly"},
            {"name": "Synthetic cool-down", "phase": "cooldown"}])])
        self.training.record_activities([{
            "activity_id": "main", "start": TODAY.isoformat(), "type": "strength_training",
            "logged_sets": [{"exercise": "Test press", "sets": 2},
                            {"exercise": "Test fly", "sets": 2}]}])
        self.assertEqual(self.context()["slot"], "1:1")

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

    def test_rest_shift_invalidates_all_affected_saved_forecasts_without_losing_payloads(self):
        tomorrow, later = TODAY + timedelta(days=1), TODAY + timedelta(days=3)
        future = [
            self.plan(tomorrow, program_template_id="pull", rotation_slot="1:1",
                      objective="Original pull", reason="Synthetic source", exercises=[
                          {"name": "Test row", "sets": [{"reps": 9, "weight_kg": 12}]}]),
            self.plan(later, kind="cardio", program_template_id="cardio", rotation_slot="1:2",
                      objective="Original cardio", reason="Synthetic source", exercises=[
                          {"name": "Synthetic bike", "blocks": [{"duration_seconds": 360}]}]),
        ]
        self.training.save([self.plan()] + future)
        rest = {"date": TODAY.isoformat(), "kind": "rest", "objective": "Rest",
                "reason": "User requested rest", "exercises": [],
                "rotation_defer": {"slot": "1:0", "reason": "User requested rest",
                                   "reassess_on": tomorrow.isoformat(), "resume_when": "Review tomorrow"}}
        projected, errors = project_rotation([rest], self.context(), TODAY)
        self.assertFalse(errors)
        self.assertEqual(len(projected), 3)
        for old, revised in zip(future, projected[1:]):
            self.assertEqual(revised["exercises"], old["exercises"])
            self.assertEqual(revised["objective"], old["objective"])
            self.assertEqual(revised["projection_status"], "needs_replanning")
            self.assertNotIn("rotation_slot", revised)
        self.assertNotIn("projection_status", future[0])
        self.training.save(projected)
        saved = self.training.context(TODAY)["plans"]
        self.assertEqual(saved[1]["payload"]["projection_status"], "needs_replanning")
        context = self.context()
        self.assertEqual(context["future_plans"], [])
        self.assertEqual(project_rotation([], context, TODAY), ([], []))

    def test_invalidated_plan_never_advances_or_offers_completion_candidates(self):
        invalidated = self.plan(kind="cardio", projection_status="needs_replanning")
        self.training.save([invalidated])
        self.training.set_status(TODAY, "user_completed")
        self.training.record_activities([{
            "activity_id": "candidate", "start": TODAY.isoformat(), "type": "running"}])
        context = self.context()
        self.assertEqual(context["slot"], "1:0")
        self.assertFalse(context["pending_today"])
        self.assertEqual(context["cardio_participation_candidates"], [])
        self.assertEqual(context["awaiting_reconciliation"], [])
        self.assertTrue(rotation_plan_errors([invalidated], context, TODAY))

    def test_projection_does_not_reactivate_explicit_invalidated_rows(self):
        future = self.plan(TODAY + timedelta(days=1), projection_status="needs_replanning")
        projected, errors = project_rotation([future], self.context(), TODAY)
        self.assertFalse(errors)
        self.assertEqual(projected[0]["projection_status"], "needs_replanning")
        self.assertNotIn("rotation_slot", projected[0])

    def test_epoch_rebase_returns_all_changed_full_saved_rows_for_persistence(self):
        tomorrow, later = TODAY + timedelta(days=1), TODAY + timedelta(days=2)
        self.training.save([
            self.plan(tomorrow, reason="Source-backed plan", exercises=[
                {"name": "Test press", "sets": [{"reps": 8, "weight_kg": 11}]}]),
            self.plan(later, program_template_id="cardio", rotation_slot="1:2",
                      objective="Synthetic cardio", exercises=[
                          {"name": "Synthetic bike", "blocks": [{"duration_seconds": 120}]}]),
        ])
        self.store.configure({"revision": 2, "review": {"rotation": ["push", "cardio", "pull"]}}, TODAY)
        projected, errors = project_rotation([], self.context(), TODAY)
        self.assertFalse(errors)
        self.assertEqual([p["rotation_slot"] for p in projected], ["2:0", "2:1"])
        self.assertEqual(projected[0]["exercises"][0]["sets"][0]["weight_kg"], 11)
        self.training.save(projected)
        self.assertEqual(project_rotation([], self.context(), TODAY), ([], []))

    def test_committed_future_conflict_is_not_silently_moved_or_invalidated(self):
        tomorrow = TODAY + timedelta(days=1)
        committed = self.plan(tomorrow, program_template_id="pull", rotation_slot="1:1",
                              start_time="17:00", objective="User appointment")
        self.training.save([committed])
        projected, errors = project_rotation([], self.context(), TODAY)
        self.assertTrue(errors)
        self.assertEqual(projected, [])
        self.assertEqual(self.training.context(TODAY)["plans"][0]["payload"], committed)

    def test_explicit_user_planned_day_is_a_commitment_without_a_start_time(self):
        tomorrow = TODAY + timedelta(days=1)
        committed = self.plan(tomorrow, program_template_id="pull", rotation_slot="1:1")
        self.training.save([committed])
        self.training.set_status(tomorrow, "user_planned")
        projected, errors = project_rotation([], self.context(), TODAY)
        self.assertTrue(errors)
        self.assertEqual(projected, [])

    def test_epoch_rebase_preserves_future_deferral_text_and_changes_its_slot(self):
        tomorrow = TODAY + timedelta(days=1)
        rest = self.plan(tomorrow, kind="rest", exercises=[], rotation_defer={
            "slot": "1:0", "reason": "Synthetic future rest",
            "reassess_on": tomorrow.isoformat(), "resume_when": "Review the next day"})
        rest.pop("rotation_slot")
        rest.pop("program_template_id")
        self.training.save([rest])
        self.store.configure({"revision": 2, "review": {"rotation": ["push", "cardio", "pull"]}}, TODAY)
        projected, errors = project_rotation([], self.context(), TODAY)
        self.assertFalse(errors)
        self.assertEqual(projected[0]["rotation_defer"]["slot"], "2:0")
        self.assertEqual(projected[0]["rotation_defer"]["reason"], "Synthetic future rest")

    def test_completed_slot_invalidates_stale_future_deferral_for_that_slot(self):
        tomorrow = TODAY + timedelta(days=1)
        rest = self.plan(tomorrow, kind="rest", exercises=[], rotation_defer={
            "slot": "1:0", "reason": "Waiting to clarify whether push was completed",
            "reassess_on": tomorrow.isoformat(), "resume_when": "Confirm push completion"})
        rest.pop("rotation_slot")
        rest.pop("program_template_id")
        self.training.save([self.plan(TODAY), rest])
        self.training.set_status(TODAY, "user_completed")
        projected, errors = project_rotation([], self.context(), TODAY)
        self.assertFalse(errors)
        self.assertEqual(projected[0]["projection_status"], "needs_replanning")
        self.assertNotIn("rotation_defer", projected[0])

    def test_removed_template_forecast_is_retained_and_explicitly_invalidated(self):
        tomorrow = TODAY + timedelta(days=1)
        self.training.save([self.plan(tomorrow, program_template_id="removed",
                                      rotation_slot="1:3", objective="Original session")])
        context = self.context()
        self.assertEqual(context["future_plans"][0]["program_template_id"], "removed")
        projected, errors = project_rotation([], context, TODAY)
        self.assertFalse(errors)
        self.assertEqual(projected[0]["projection_status"], "needs_replanning")
        self.assertEqual(projected[0]["program_template_id"], "removed")

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
