import importlib
import json
import logging
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class BridgeTests(unittest.TestCase):
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
        b = self.bridge
        self.state = Path(self.temp.name)
        b.STATE = str(self.state)
        b._memory_store = b._training_store = b._runtime_store = None
        b._current_request = None
        b._send_sequence = b._generation_sequence = 0
        b._set_msg_sent_at(None)
        b.INCOMING = str(self.state)
        for variable, name in (
                ("JOURNAL_FILE", "journal.jsonl"), ("HISTORY_FILE", "conversation.json"),
                ("EXERCISE_STATE_FILE", "exercise.json"), ("TODAYS_BRIEF_FILE", "brief.json"),
                ("LAST_SUMMARY_FILE", "summary.txt"), ("PENDING_DEBRIEF_FILE", "pending.json"),
                ("RED_FLAGS_FILE", "redflags.txt")):
            setattr(b, variable, str(self.state / name))

    def rest_reply(self):
        plan = {"date": date.today().isoformat(), "kind": "rest",
                "objective": "Planned recovery", "reason": "User's agreed schedule",
                "exercises": []}
        return "A recovery day is planned.\n[[SESSION_PLAN: " + json.dumps(plan) + "]]"

    def test_current_plan_and_reminders_share_rest_state(self):
        b = self.bridge
        with patch.object(b, "run_llm", return_value=self.rest_reply()):
            result = b._generate_checked("synthetic", require_plan=True)
        self.assertNotIn("SESSION_PLAN", result)
        state = json.loads(Path(b.EXERCISE_STATE_FILE).read_text())
        self.assertTrue(state["coach_rest"])
        self.assertEqual(b.training_store().context()["plans"][0]["payload"]["kind"], "rest")

    def test_repeated_sequences_are_referenced_without_losing_source_detail(self):
        b = self.bridge
        sequence = [{"sequence_index": 1, "exercise": "Synthetic press", "reps": 8}]
        activity = {"activity_id": 1, "start": date.today().isoformat() + " 10:00:00",
                    "logged_sets": [{"exercise": "Synthetic press", "sets": 1,
                                     "set_sequence": sequence}]}
        b.training_store().record_activities([activity])
        compact = b._compact_training_context(
            b.training_store().context(), {"recent_activities_7d": [activity]}, "GARMIN_JSON")
        self.assertEqual(compact["recent_outcomes"][0]["payload"], {})
        movement = compact["latest_observed_per_movement"]["synthetic press"]
        self.assertNotIn("set_sequence", movement["recorded_sets"])
        self.assertIn("GARMIN_JSON.", movement["chronology_reference"])
        self.assertEqual(activity["logged_sets"][0]["set_sequence"], sequence)

    def test_context_keeps_sequence_when_snapshot_has_no_details(self):
        b = self.bridge
        sequence = [{"sequence_index": 1, "exercise": "Synthetic press", "reps": 8}]
        activity = {"activity_id": 1, "start": date.today().isoformat() + " 10:00:00",
                    "logged_sets": [{"exercise": "Synthetic press", "sets": 1,
                                     "set_sequence": sequence}]}
        b.training_store().record_activities([activity])
        compact = b._compact_training_context(b.training_store().context(), {}, "GARMIN_JSON")
        self.assertEqual(compact["recent_outcomes"][0]["payload"]["logged_sets"][0]
                         ["set_sequence"], sequence)
        self.assertIn("TRAINING_STATE.", compact["latest_observed_per_movement"]
                      ["synthetic press"]["chronology_reference"])

    def test_requested_workout_metrics_reach_context_and_durable_outcomes(self):
        b = self.bridge
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        activity = {"activity_id": 1, "start": yesterday + " 10:00:00", "type": "running"}
        data = {"recent_activities_7d": [activity]}
        metrics = {"fuel_estimate": {"status": "unverified", "unit": None}}
        with patch.object(b.garmin_coach, "activity_detail_metrics", return_value=metrics) as get:
            b._enrich_activity_context(data, summary=True)
        get.assert_called_once_with(1)
        self.assertEqual(activity["detail_metrics"], metrics)
        self.assertEqual(data["activity_metric_coverage"]["returned"], 1)
        self.assertEqual(b.training_store().context()["recent_outcomes"][0]
                         ["payload"]["detail_metrics"], metrics)

    def test_nutrition_context_omits_only_irrelevant_workout_detail(self):
        b = self.bridge
        activity = {"activity_id": 1, "start": date.today().isoformat() + " 10:00:00",
                    "calories": 200, "logged_sets": [{"exercise": "Synthetic press", "sets": 1}]}
        b.training_store().record_activities([activity])
        data, training = b._nutrition_context(
            {"recent_activities_7d": [activity], "calorie_budget": {"target_kcal": 2300}},
            b.training_store().context())
        self.assertNotIn("logged_sets", data["recent_activities_7d"][0])
        self.assertIn("logged_sets", activity)
        self.assertEqual(data["calorie_budget"]["target_kcal"], 2300)
        self.assertEqual(training["latest_observed_per_movement"], {})
        self.assertIn("omitted, not absent", training["detail_selection"])
        self.assertTrue(b._nutrition_focused(b.QA_PROMPT_FILE, "Today's protein target?"))
        self.assertFalse(b._nutrition_focused(b.QA_PROMPT_FILE,
                                             "Does protein affect my workout sets?"))

    def test_metric_context_is_bounded_and_does_not_substitute_other_dates(self):
        b = self.bridge
        activities = [{"activity_id": n, "start": date.today().isoformat() + " 10:00:00",
                       "type": "running"} for n in range(5)]
        data = {"recent_activities_7d": activities}
        with patch.object(b.garmin_coach, "activity_detail_metrics", return_value={}) as get:
            b._enrich_activity_context(data, "today's workout")
            self.assertEqual(get.call_count, 3)
            self.assertEqual(data["activity_metric_coverage"]["omitted"], 2)
            get.reset_mock()
            b._enrich_activity_context(data, "yesterday's workout")
            get.assert_not_called()
            self.assertEqual(data["activity_metric_coverage"]["returned"], 0)

    def test_tomorrows_request_does_not_force_a_plan_for_today(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        reply = self.rest_reply().replace(date.today().isoformat(), tomorrow)
        with patch.object(self.bridge, "run_llm", return_value=reply):
            self.bridge._generate_checked("Plan tomorrow", require_plan="any")
        self.assertEqual(self.bridge.training_store().context()["plans"][0]["date"], tomorrow)

    def test_no_source_means_no_verified_automatic_memory(self):
        b = self.bridge
        response = ('Observation.\n[[MEMORY: {"kind":"anchor","key":"test_press",'
                    '"text":"The press was easy","source_quote":"The press was easy",'
                    '"action":"upsert"}]]')
        with patch.object(b, "run_llm", return_value=response):
            result = b._generate_checked("automated debrief")
        self.assertEqual(b.memory_store().records("anchor"), [])
        self.assertIn("not saved", result)

    def test_new_avoidance_guards_same_answer_and_keeps_save_receipt_after_repair(self):
        b = self.bridge
        source = "Please avoid Romanian deadlift."
        memory = {"kind": "preference", "key": "avoid_exercise:romanian_deadlift",
                  "text": source, "source_quote": source, "action": "upsert"}
        plan = {"date": date.today().isoformat(), "kind": "strength",
                "objective": "A synthetic proposal", "reason": "A synthetic draft",
                "exercises": [{"name": "Dumbbell Romanian Deadlift"}]}
        draft = ("A draft.\n[[MEMORY: " + json.dumps(memory) + "]]"
                 "\n[[SESSION_PLAN: " + json.dumps(plan) + "]]")
        with patch.object(b, "run_llm", side_effect=[draft, self.rest_reply()]) as model:
            result = b._generate_checked("Synthetic request", source_text=source,
                                         require_plan=True)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len(b.memory_store().records("preference")), 1)
        self.assertIn("Memory updated:", result)
        self.assertIn(source, result)
        self.assertEqual(b.training_store().context()["plans"][0]["payload"]["kind"], "rest")

    def test_failed_workout_review_keeps_pending_debrief(self):
        b = self.bridge
        with patch.object(b, "_assemble", return_value=("synthetic", {})), \
                patch.object(b, "_generate_checked", return_value=None), \
                patch.object(b, "_is_workout_review", return_value=True), \
                patch.object(b, "_clear_pending") as clear:
            b.generate_qa("How was my workout?")
        clear.assert_not_called()

    def test_model_is_not_granted_general_tool_access(self):
        b = self.bridge
        with patch.object(b.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="reply", stderr="")) as run:
            self.assertEqual(b._llm_copilot("synthetic", []), "reply")
        command = run.call_args.args[0]
        self.assertIn("--available-tools=", command)
        self.assertNotIn("--deny-tool=*", command)
        self.assertNotIn("--allow-all-tools", command)
        self.assertEqual(command[command.index("--context") + 1], "default")

    def test_failed_cli_output_is_not_treated_as_an_answer(self):
        with patch.object(self.bridge.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="partial response", stderr="upstream unavailable")):
            self.assertIsNone(self.bridge._llm_copilot("synthetic", []))

    def test_album_downloads_have_distinct_paths_without_token_global(self):
        b = self.bridge
        with patch.object(b, "tg", return_value={"result": {"file_path": "photos/sample.jpg"}}), \
                patch.object(b, "load_token", return_value="synthetic-test-token"), \
                patch.object(b.urllib.request, "urlretrieve"):
            first = b.download_file("first")
            second = b.download_file("second")
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).is_file())
        self.assertTrue(Path(second).is_file())

    def test_ambiguous_improvement_does_not_clear_every_health_flag(self):
        self.assertEqual(self.bridge.classify("I'm feeling better now")[0], "qa")
        self.assertEqual(self.bridge.classify("recovered")[0], "recovered")

    def test_checkin_uses_shared_training_classification(self):
        b = self.bridge
        with patch.object(b.garmin_coach, "trained_today", return_value=False) as trained, \
                patch.object(b.garmin_coach, "latest_activity") as latest:
            message = b._exercise_checkin_message()
        trained.assert_called_once()
        latest.assert_not_called()
        self.assertNotIn("I can see you've trained", message)

    def test_explicit_recovered_command_clears_health_not_preferences(self):
        b = self.bridge
        store = b.memory_store()
        store.upsert("health", "left_knee", "Left knee symptoms.")
        store.upsert("preference", "avoid_exercise:test_hinge", "Avoid test hinge.")
        self.assertEqual(len(b.resolve_health("recovered")), 1)
        self.assertEqual(store.records("health"), [])
        self.assertEqual(len(store.records("preference")), 1)

    def test_summary_is_marked_only_after_delivery(self):
        b = self.bridge
        day = date.today().isoformat()
        b.send_message(123, "Synthetic summary", summary_date=day)
        self.assertFalse(Path(b.LAST_SUMMARY_FILE).exists())
        with patch.object(b, "_post", return_value={"ok": False, "description": "offline"}):
            b.flush_outbox()
        self.assertTrue(b.runtime_store().summary_pending(day))
        self.assertFalse(Path(b.LAST_SUMMARY_FILE).exists())
        with b.runtime_store().connection() as db:
            db.execute("UPDATE outbox SET available_at=0")
        with patch.object(b, "_post", return_value={"ok": True, "result": {"message_id": 9}}):
            b.flush_outbox()
        self.assertEqual(Path(b.LAST_SUMMARY_FILE).read_text(), day)

    def test_queued_summary_suppresses_redundant_early_alert(self):
        b = self.bridge
        day = date.today().isoformat()
        b.send_message(123, "Synthetic summary", summary_date=day)
        with patch.object(b, "owner", return_value=123), \
                patch.object(b, "get_snapshot") as snapshot:
            b.maybe_red_flags()
        snapshot.assert_not_called()
        self.assertEqual(Path(b.RED_FLAGS_FILE).read_text(), day)

    def test_every_generation_route_uses_shared_finalizer(self):
        b = self.bridge
        snapshot = {"recent_activities_7d": []}
        with patch.object(b, "_assemble", return_value=("synthetic", snapshot)), \
                patch.object(b, "_generate_checked", return_value="response") as checked, \
                patch.object(b.garmin_coach, "build_weekly", return_value={}), \
                patch.object(b, "get_snapshot", return_value=snapshot), \
                patch.object(b.garmin_coach, "activity_extras", return_value={}):
            b.generate_summary()
            b.generate_qa("A synthetic question")
            b.generate_images([], "A synthetic caption")
            b.generate_weekly()
            b.generate_nutrition()
            b.generate_performance()
            b.generate_debrief([{"activity_id": 1, "start": day_string(),
                                 "type": "running", "name": "Synthetic run"}])
        self.assertEqual(checked.call_count, 7)


def day_string():
    return date.today().isoformat()


if __name__ == "__main__":
    unittest.main()
