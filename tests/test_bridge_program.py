import importlib
import json
import logging
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


class ProgrammeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"AGBOT_DATA_DIR": cls.root.name}):
            cls.bridge = importlib.import_module("telegram_bridge")

    @classmethod
    def tearDownClass(cls):
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler):
                handler.close()
                logging.getLogger().removeHandler(handler)
        cls.root.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        b = self.bridge
        b.STATE = str(self.state)
        b._memory_store = b._training_store = b._runtime_store = b._program_store = None
        b._current_request = None
        b._send_sequence = b._generation_sequence = 0
        b._set_msg_sent_at(None)
        for name, value in (
                ("PROGRAM_ENABLED", True), ("PROGRAM_REVIEW_DAYS", 7), ("PROGRAM_BLOCK_DAYS", 28),
                ("PROFILE_FILE", str(self.state / "profile.md")),
                ("HISTORY_FILE", str(self.state / "history.json")),
                ("JOURNAL_FILE", str(self.state / "journal.jsonl")),
                ("TODAYS_BRIEF_FILE", str(self.state / "brief.json")),
                ("EXERCISE_STATE_FILE", str(self.state / "exercise.json")),
                ("get_snapshot", lambda **kwargs: {}),
                ("load_fitness_profile", lambda: {}), ("owner", lambda: "123")):
            setting = patch.object(b, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        Path(b.PROFILE_FILE).write_text(
            "I can train four days per week. Goal: develop strength. Equipment: dumbbells.",
            encoding="utf-8")
        history = patch.object(b.garmin_coach, "program_history", return_value={
            "activities": [], "coverage": {"complete": False, "unknown": True}})
        history.start()
        self.addCleanup(history.stop)

    def review(self):
        return {
            "goal": "Develop strength with consistent tolerable practice",
            "weekly_training_days": 4,
            "session_templates": [{
                "id": "strength-a", "kind": "strength", "purpose": "Practise the main movement",
                "exercises": [{"name": "Test press", "role": "anchor",
                               "progression_rule": "Start with tolerable effort; repeat before "
                                                   "progressing and reduce if uncomfortable."}],
            }],
            "decisions": [{"exercise": "Test press", "action": "introduce",
                           "reason": "An introductory strength option with unknown capability",
                           "evidence_refs": ["profile"]}],
            "recovery_rule": "Space strength sessions and retain recovery days.",
            "success_signals": ["Reported tolerability and comparable completed repetitions"],
            "variety_review": "Start with one anchor; assess an accessory after tolerance feedback.",
            "questions": [],
        }

    def marker(self):
        return "[[TRAINING_PROGRAM: " + json.dumps(self.review()) + "]]"

    def initialise(self):
        with patch.object(self.bridge, "run_llm", return_value=self.marker()):
            return self.bridge.ensure_program_review()

    def test_classification_repair_is_not_reported_as_clinical_recovery(self):
        store = self.bridge.memory_store()
        quote = "My back felt good during and after glute bridges."
        record = store.upsert("health", "low_back", quote, source_text=quote, verified=True)
        store.reclassify_tolerance(record["id"], "exercise_tolerance:glute_bridges")
        self.assertEqual(self.bridge._resolved_health_reports(), [])
        self.assertTrue(store.records("anchor"))

    def plan(self, **changes):
        result = {
            "date": date.today().isoformat(), "kind": "strength", "objective": "Practise",
            "reason": "Follow the programme at tolerable effort", "program_revision": 1,
            "program_template_id": "strength-a",
            "exercises": [{"name": "Test press", "sets": [{"reps": 8, "weight_kg": None}]}],
        }
        result.update(changes)
        return result

    def test_first_daytime_review_is_automatic_and_announced_once(self):
        b = self.bridge
        daytime = datetime.now().replace(hour=10, minute=0)
        with patch.object(b, "run_llm", return_value=self.marker()) as model:
            b.maybe_program_review(daytime)
            first = b.runtime_store().due_messages()
            b.maybe_program_review(daytime)
        model.assert_called_once()
        self.assertTrue(first)
        self.assertEqual([item["id"] for item in first],
                         [item["id"] for item in b.runtime_store().due_messages()])
        active = b.program_store().context()["active"]
        self.assertEqual(active["review"], self.review())
        self.assertEqual(active["next_review_date"], (date.today() + timedelta(days=7)).isoformat())
        self.assertEqual(b.memory_store().records("anchor"), [])
        self.assertEqual(b._generation_sequence, 0)

    def test_no_automatic_review_outside_daytime_window_or_when_disabled(self):
        b = self.bridge
        with patch.object(b, "run_llm") as model:
            b.maybe_program_review(datetime.now().replace(hour=2))
            with patch.object(b, "PROGRAM_ENABLED", False):
                b.maybe_program_review(datetime.now().replace(hour=10))
                self.assertIsNone(b.ensure_program_review(force=True))
        model.assert_not_called()

    def test_failed_review_keeps_state_and_retries_without_disabling_summary(self):
        b = self.bridge
        rest = {"date": date.today().isoformat(), "kind": "rest", "objective": "Recover",
                "reason": "A conservative recovery day", "exercises": []}
        with patch.object(b, "run_llm", side_effect=[
                "invalid", "invalid", "[[SESSION_PLAN: " + json.dumps(rest) + "]]"]) as model:
            answer, _ = b.generate_summary()
        self.assertTrue(answer)
        self.assertEqual(model.call_count, 3)
        context = b.program_store().context()
        self.assertIsNone(context["active"])
        self.assertEqual(context["last_error"], "invalid_programme_review")
        self.assertIsNotNone(context["retry_after"])
        self.assertTrue(any("no programme change was saved" in item["payload"]["text"]
                            for item in b.runtime_store().due_messages()))
        with patch.object(b, "run_llm") as model:
            b.ensure_program_review()
        model.assert_not_called()

    def test_review_request_replay_returns_saved_result_without_another_model_call(self):
        b = self.bridge
        b._current_request = "update:synthetic"
        first = self.initialise()
        with patch.object(b, "run_llm") as model:
            replay = b.ensure_program_review(force=True)
        model.assert_not_called()
        self.assertEqual(replay["revision"], first["revision"])

    def test_review_launch_failure_is_deferred_without_aborting_the_summary(self):
        b = self.bridge
        rest = self.plan(kind="rest", exercises=[])
        with patch.object(b, "run_llm", side_effect=[
                OSError("temporary launch failure"),
                "[[SESSION_PLAN: " + json.dumps(rest) + "]]"]):
            answer, _ = b.generate_summary()
        self.assertTrue(answer)
        self.assertEqual(b.program_store().context()["last_error"], "model_launch_error")

    def test_saved_review_announcement_recovers_after_restart(self):
        b = self.bridge
        with patch.object(b, "_queue_program_update"):
            self.initialise()
        self.assertEqual(b.runtime_store().due_messages(), [])
        b._program_store = None
        with patch.object(b, "run_llm") as model:
            b.maybe_program_review(datetime.now().replace(hour=10))
        model.assert_not_called()
        self.assertTrue(b.runtime_store().due_messages())

    def test_scoped_review_cache_does_not_shift_normal_answer_retry_slots(self):
        b = self.bridge
        b._current_request = "update:cache"
        with patch.dict(b._LLM_BACKENDS, {b.LLM_BACKEND: lambda prompt, images: prompt}):
            self.assertEqual(b.run_llm("normal-one"), "normal-one")
            self.assertEqual(b.run_llm("review", cache_scope="program-review:0"), "review")
            self.assertEqual(b.run_llm("normal-two"), "normal-two")
            b._generation_sequence = 0
            self.assertEqual(b.run_llm("retry-one"), "normal-one")
            self.assertEqual(b.run_llm("retry-two"), "normal-two")

    def test_training_feedback_changes_review_fingerprint_but_food_reporting_does_not(self):
        b = self.bridge
        original = b._program_inputs()[2]
        b.memory_store().upsert("preference", "nutrition_reporting", "Show calories.")
        self.assertEqual(original, b._program_inputs()[2])
        b.memory_store().upsert("anchor", "test_press", "That load was too difficult.",
                                source_text="That load was too difficult.", verified=True)
        self.assertNotEqual(original, b._program_inputs()[2])

    def test_paraphrased_memory_keeps_its_verified_source_for_progression(self):
        b = self.bridge
        quote = "For Test press I finished eight reps with three reps in reserve."
        marker = {"kind": "anchor", "key": "test_press",
                  "text": "Reported comfortable eight-rep Test press sets.",
                  "source_quote": quote, "action": "upsert"}
        b.memory_store().process_markers("[[MEMORY: " + json.dumps(marker) + "]]", quote)
        record = b.memory_store().records("anchor")[0]
        b.training_store().record_activities([
            {"activity_id": str(n), "start": (date.today() - timedelta(days=n)).isoformat(),
             "type": "strength_training", "logged_sets": [{"exercise": "Test press", "sets": 2}]}
            for n in (1, 3)])
        profile, records, _ = b._program_inputs()
        evidence = b.build_review_evidence(b._program_training_context(), records, profile)
        reference = "memory:" + str(record["id"])
        self.assertTrue(evidence["refs"][reference]["verified"])
        self.assertEqual(evidence["refs"][reference]["text"], quote)
        review = self.review()
        review["decisions"][0].update(
            action="progress", evidence_refs=["activity:1", "activity:3", reference])
        _, parsed, errors = b.parse_program_review(
            "[[TRAINING_PROGRAM: " + json.dumps(review) + "]]", evidence)
        self.assertFalse(errors)
        self.assertIsNotNone(parsed)

    def test_schedule_uses_the_user_quote_not_a_conflicting_memory_summary(self):
        b = self.bridge
        quote = "I can train three days per week."
        marker = {"kind": "preference", "key": "availability",
                  "text": "Can train seven days weekly.", "source_quote": quote, "action": "upsert"}
        b.memory_store().process_markers("[[MEMORY: " + json.dumps(marker) + "]]", quote)
        profile, records, _ = b._program_inputs()
        evidence = b.build_review_evidence(b._program_training_context(), records, profile)
        self.assertEqual(evidence["weekly_training_days"], 3)

    def test_resolved_symptom_reports_are_context_not_reintroduced_active_flags(self):
        b = self.bridge
        b.memory_store().upsert("health", "low_back", "Lower back symptoms.")
        b.memory_store().resolve_health(["low_back"], source_text="My lower back is fine now.")
        prompt, _ = b._assemble(b.QA_PROMPT_FILE, question="My training?", data={})
        self.assertIn("My lower back is fine now.", prompt)
        self.assertIn("RESOLVED SYMPTOM REPORTS", prompt)
        self.assertEqual(b.memory_store().records("health"), [])

    def test_programme_is_in_training_context_but_nutrition_omits_templates(self):
        b = self.bridge
        self.initialise()
        prompt, _ = b._assemble(b.QA_PROMPT_FILE, question="My training programme?", data={})
        self.assertIn("PROGRAMME_STATE", prompt)
        self.assertIn("strength-a", prompt)
        context = b._program_context(nutrition_focused=True)
        self.assertEqual(context["active"]["review"]["weekly_training_days"], 4)
        self.assertNotIn("session_templates", context["active"]["review"])

    def test_review_prompt_deduplicates_sources_without_mutating_the_audit(self):
        b = self.bridge
        profile, records, _ = b._program_inputs()
        evidence = b.build_review_evidence(b._program_training_context(), records, profile)
        original = json.dumps(evidence, sort_keys=True)
        compact = json.loads(b._program_evidence_prompt(evidence))
        self.assertNotIn("profile", compact)
        self.assertNotIn("memory", compact)
        self.assertEqual(compact["refs"]["profile"]["text_reference"], "PROFILE")
        self.assertEqual(json.dumps(evidence, sort_keys=True), original)

    def test_explicit_stop_rule_accepts_a_legitimate_non_rpe_control_condition(self):
        b = self.bridge
        profile, records, _ = b._program_inputs()
        evidence = b.build_review_evidence(b._program_training_context(), records, profile)
        review = self.review()
        exercise = review["session_templates"][0]["exercises"][0]
        exercise["progression_rule"] = "Extend the lever only after all six repetitions stay smooth."
        exercise["stop_rule"] = "End the set if the lower back starts lifting away from the mat."
        _, result, errors = b.parse_program_review(
            "[[TRAINING_PROGRAM: " + json.dumps(review) + "]]", evidence)
        self.assertFalse(errors)
        self.assertIn("stop_rule", result["session_templates"][0]["exercises"][0])

    def test_programme_cannot_freeze_numeric_working_watts_or_claim_zero_spinal_load(self):
        b = self.bridge
        profile, records, _ = b._program_inputs()
        evidence = b.build_review_evidence(b._program_training_context(), records, profile)
        for reason in ("Keep sustainable 200-220W working power.",
                       "Choose this exercise without axial spinal loading."):
            review = self.review()
            review["decisions"][0]["reason"] = reason
            _, result, errors = b.parse_program_review(
                "[[TRAINING_PROGRAM: " + json.dumps(review) + "]]", evidence)
            self.assertTrue(errors)
            self.assertIsNone(result)

    def test_known_recovery_activity_can_be_retained_without_pretending_it_is_new_training(self):
        b = self.bridge
        b.training_store().record_activities([{
            "activity_id": "pilates", "type": "pilates",
            "start": date.today().isoformat()}])
        profile, records, _ = b._program_inputs()
        evidence = b.build_review_evidence(b._program_training_context(), records, profile)
        review = self.review()
        review["session_templates"][0].update(kind="recovery")
        review["session_templates"][0]["exercises"][0]["name"] = "pilates"
        review["decisions"][0].update(exercise="pilates", action="keep",
                                      evidence_refs=["activity:pilates"])
        _, result, errors = b.parse_program_review(
            "[[TRAINING_PROGRAM: " + json.dumps(review) + "]]", evidence)
        self.assertFalse(errors)
        self.assertIsNotNone(result)
        self.assertEqual(evidence["intentional_training_days"]["count"], 0)

    def test_daily_plan_must_follow_programme_or_explain_adjustment(self):
        b = self.bridge
        self.initialise()
        self.assertEqual(b._program_plan_errors([self.plan()]), [])
        changed = self.plan(exercises=[{"name": "Different press"}])
        self.assertTrue(b._program_plan_errors([changed]))
        changed["program_adjustment"] = "The user requested a different tolerable variation."
        self.assertEqual(b._program_plan_errors([changed]), [])
        self.assertTrue(b._program_plan_errors([self.plan(program_revision=99)]))
        self.assertTrue(b._program_plan_errors([self.plan(program_revision=True)]))
        self.assertTrue(b._program_plan_errors([self.plan(program_template_id=["strength-a"])]))

    def test_new_symptom_exclusion_still_guards_same_reply_under_older_programme(self):
        b = self.bridge
        self.initialise()
        source = "Please avoid Test press."
        memory = {"kind": "preference", "key": "avoid_exercise:test_press", "text": source,
                  "source_quote": source, "action": "upsert"}
        draft = ("[[MEMORY: " + json.dumps(memory) + "]]\n"
                 "[[SESSION_PLAN: " + json.dumps(self.plan()) + "]]")
        rest = self.plan(kind="rest", exercises=[], objective="Recovery")
        with patch.object(b, "run_llm", side_effect=[
                draft, "[[SESSION_PLAN: " + json.dumps(rest) + "]]"]) as model:
            b._generate_checked("synthetic", source_text=source, require_plan=True)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(b.training_store().context()["plans"][0]["payload"]["kind"], "rest")
        self.assertEqual(b.program_store().context()["active"]["revision"], 1)

    def test_todays_empty_outline_cannot_replace_a_morning_prescription(self):
        b = self.bridge
        self.initialise()
        outline = self.plan(detail_level="outline", exercises=[])
        with patch.object(b, "run_llm", return_value=
                          "[[SESSION_PLAN: " + json.dumps(outline) + "]]"), \
                self.assertRaises(ValueError):
            b._generate_checked("morning", require_plan=True)
        self.assertEqual(b.training_store().context()["plans"], [])

    def test_training_budget_counts_days_not_commutes_or_multiple_parts(self):
        b = self.bridge
        Path(b.PROFILE_FILE).write_text("I can train at most four days per week.", encoding="utf-8")
        self.initialise()
        today = date.today()
        activities = [
            {"activity_id": str(n), "start": (today - timedelta(days=n)).isoformat(),
             "type": "strength_training"} for n in range(1, 5)]
        b.training_store().record_activities(activities)
        self.assertTrue(b._program_plan_errors([self.plan()]))
        activities[-1]["type"] = "e_biking"
        b.training_store().record_activities(activities[-1:])
        self.assertEqual(b._program_plan_errors([self.plan()]), [])
        b.training_store().record_activities([{
            "activity_id": "second-part", "start": activities[0]["start"],
            "type": "running"}])
        self.assertEqual(b._program_plan_errors([self.plan()]), [])

    def test_today_cannot_overfill_an_already_planned_future_week(self):
        b = self.bridge
        Path(b.PROFILE_FILE).write_text("I can train at most four days per week.", encoding="utf-8")
        self.initialise()
        future = [self.plan(date=(date.today() + timedelta(days=n)).isoformat(),
                            detail_level="outline", exercises=[]) for n in range(1, 5)]
        b.training_store().save(future)
        self.assertTrue(b._program_plan_errors([self.plan()]))

    def test_completed_reported_training_days_count_even_before_watch_sync(self):
        b = self.bridge
        Path(b.PROFILE_FILE).write_text("I can train at most four days per week.", encoding="utf-8")
        self.initialise()
        for days_ago in range(1, 5):
            day = (date.today() - timedelta(days=days_ago)).isoformat()
            b.training_store().save([self.plan(date=day)])
            b.training_store().set_status(day, "user_completed")
        self.assertTrue(b._program_plan_errors([self.plan()]))

    def test_new_lower_availability_limits_the_same_reply_before_next_review(self):
        b = self.bridge
        self.initialise()
        Path(b.PROFILE_FILE).write_text("I can train at most two days per week.", encoding="utf-8")
        b.training_store().record_activities([
            {"activity_id": str(n), "start": (date.today() - timedelta(days=n)).isoformat(),
             "type": "strength_training"} for n in (1, 2)])
        self.assertTrue(b._program_plan_errors([self.plan()]))

    def test_new_availability_can_raise_an_unchanged_budget_with_an_explanation(self):
        b = self.bridge
        self.initialise()
        Path(b.PROFILE_FILE).write_text("I can train five days per week.", encoding="utf-8")
        b.training_store().record_activities([
            {"activity_id": str(n), "start": (date.today() - timedelta(days=n)).isoformat(),
             "type": "strength_training"} for n in range(1, 5)])
        self.assertEqual(b._program_plan_errors([self.plan()]), [])
        self.assertEqual(b._scheduling_context()["usual_target_days"], 5)
        adjusted = self.plan(program_adjustment="The user explicitly increased availability.")
        self.assertEqual(b._program_plan_errors([adjusted]), [])

    def test_more_availability_does_not_cancel_a_deliberately_reduced_budget(self):
        b = self.bridge
        reduced = self.review()
        reduced["weekly_training_days"] = 2
        with patch.object(self, "review", return_value=reduced):
            self.initialise()
        Path(b.PROFILE_FILE).write_text("I can train five days per week.", encoding="utf-8")
        b.training_store().record_activities([
            {"activity_id": str(n), "start": (date.today() - timedelta(days=n)).isoformat(),
             "type": "strength_training"} for n in (1, 2)])
        adjusted = self.plan(program_adjustment="The user has more available time.")
        self.assertEqual(b._program_plan_errors([adjusted]), [])
        self.assertEqual(b._scheduling_context()["usual_target_days"], 2)

    def test_programme_commands_are_views_or_explicit_reviews_not_generic_qa(self):
        b = self.bridge
        self.assertEqual(b.classify("AgBot: programme"), ("program", ""))
        self.assertEqual(b.classify("/review_program"), ("program_review", ""))
        self.initialise()
        with patch.object(b, "run_llm") as model:
            b._route_text(123, "programme")
        model.assert_not_called()
        health = b._health_report()["programme"]
        self.assertEqual(health["revision"], 1)
        self.assertNotIn("goal", health)

    def test_one_off_class_is_not_rejected_for_exceeding_a_starting_target(self):
        b = self.bridge
        self.initialise()
        b.training_store().record_activities([
            {"activity_id": str(n), "start": (date.today() - timedelta(days=n)).isoformat(),
             "type": "strength_training"} for n in range(4)])
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        future = self.plan(date=tomorrow, kind="mixed", exercises=[],
                           objective="Provisional group class",
                           reason="Requested one-off class; format and intensity need clarification")
        future.pop("program_revision")
        future.pop("program_template_id")
        reply = "What is the class format?\n[[SESSION_PLAN: " + json.dumps(future) + "]]"
        with patch.object(b, "run_llm", return_value=reply) as model:
            result = b._generate_checked("synthetic", source_text="Can I join the class tomorrow?",
                                         require_plan="any", show_schedule=True)
        model.assert_called_once()
        self.assertIn("5 training days", result)
        self.assertEqual(b.training_store().context()["plans"][0]["payload"]["kind"], "mixed")
        self.assertEqual(b.program_store().context()["active"]["review"]["weekly_training_days"], 4)

    def test_unsupported_reply_claim_is_repaired_without_reversing_class_choice(self):
        b = self.bridge
        self.initialise()
        plan = self.plan(date=(date.today() + timedelta(days=1)).isoformat(),
                         kind="mixed", exercises=[], objective="Class option")
        marker = "[[SESSION_PLAN: " + json.dumps(plan) + "]]"
        with patch.object(b, "run_llm", side_effect=[
                "Your muscular reserves are untouched.\n" + marker,
                "Class intensity remains to be clarified.\n" + marker]) as model:
            result = b._generate_checked("synthetic", source_text="Could I join the class?",
                                         require_plan="any")
        self.assertEqual(model.call_count, 2)
        self.assertNotIn("untouched", result)
        self.assertEqual(b.training_store().context()["plans"][0]["payload"]["kind"], "mixed")

    def test_mixed_message_correction_uses_app_debrief_refs_and_keeps_raw_source(self):
        b = self.bridge
        point = {"sequence_index": 0, "source_index": 0, "set_type": "ACTIVE",
                 "exercise": "TEST_PRESS", "reps": 21, "weight_kg": 4}
        group = {"exercise": "Test press", "sets": 1, "reps": "21",
                 "set_indices": [0], "set_sequence": [point]}
        recent = {"activity_id": "111", "start": date.today().isoformat(),
                  "type": "strength_training", "logged_sets": [group]}
        old = {**recent, "activity_id": "222",
               "start": (date.today() - timedelta(days=10)).isoformat()}
        b.training_store().record_activities([old, recent])
        b.append_history("agbot", "A question about the observed set.", activity_ids=["111"])
        b._current_request = "synthetic:correction"
        question = ("My watch recorded 21 reps by mistake; I corrected it to 15. "
                    "For another exercise I used 20kg. Can I join a class tomorrow?")
        with patch.object(b.garmin_coach, "exercise_sets", return_value=[group]) as refresh:
            result = b._prepare_workout_correction(question)
            replay = b._prepare_workout_correction(question)
        self.assertTrue(result["saved"])
        self.assertTrue(replay["replayed"])
        refresh.assert_called_once()
        self.assertTrue(refresh.call_args.kwargs["force_refresh"])
        self.assertEqual(result["correction"]["target"]["activity_id"], "111")
        raw = next(a for a in b.training_store().raw_activities() if a["activity_id"] == "111")
        self.assertEqual(raw["logged_sets"][0]["reps"], "21")
        effective = b.training_store().context()["latest_observed_per_movement"]["test press"]
        self.assertEqual(effective["recorded_sets"]["reps"], "15")
        self.assertIn("user_corrections", effective["recorded_sets"])
        with self.assertRaises(ValueError):
            b.training_store().record_activities(b.training_store().corrections.apply([raw]))
        with patch.object(b, "run_llm", return_value="The reported correction is available."):
            reply = b._generate_checked("synthetic", source_text=question)
        self.assertIn("21 \u2192 15 reps", reply)
        self.assertIn("user-reported", reply)
        self.assertIn("original Garmin data are preserved separately", reply)

    def test_todays_external_class_does_not_need_an_invented_exercise_list(self):
        b = self.bridge
        self.initialise()
        plan = self.plan(kind="mixed", exercises=[], delivery="instructor_led",
                         start_time="18:30", detail_level="outline",
                         participation_guidance=["Confirm format and intensity with the instructor.",
                                                 "Reassess symptoms and reduce overlapping work."],
                         program_adjustment="User requested an external class; content is unknown.")
        with patch.object(b, "run_llm", return_value="[[SESSION_PLAN: " + json.dumps(plan) + "]]"):
            reply = b._generate_checked("morning", require_plan=True)
        self.assertIn("Instructor-led session", reply)
        self.assertIn("18:30", reply)
        self.assertIn("content/intensity must be confirmed", reply)


if __name__ == "__main__":
    unittest.main()
