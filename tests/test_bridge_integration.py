import importlib
import json
import logging
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
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
        b._memory_store = b._training_store = b._runtime_store = b._program_store = None
        b._current_request = None
        b._send_sequence = b._generation_sequence = 0
        b._unavailable_models = {}
        b._llm_active_model = b._llm_last_error = None
        b._llm_last_outage = False
        for name, value in (("COPILOT_MODEL", "primary-test"), ("COPILOT_FALLBACK_MODELS", ()),
                            ("PROGRAM_ENABLED", False)):
            model_setting = patch.object(b, name, value)
            model_setting.start()
            self.addCleanup(model_setting.stop)
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

    def food_reply(self):
        return (
            "AgBot - A synthetic day\n\n**Meal estimate**\n"
            "- Fruit: ~60 kcal; P 1g, C 15g, F 0g\n"
            "- Yogurt: ~140 kcal; P 10g, C 9g, F 6g\n\n"
            "**Meal total**: ~200 kcal; protein 11g, carbs 24g, fat 6g.\n\n"
            "**Daily progress**\n"
            "- Calories: ~300 eaten / 2,100 kcal target; ~1,800 remaining.\n"
            "- Protein: ~21g eaten / 100g target; ~79g remaining.\n"
            "[[LOG: Fruit and yogurt (~200 kcal, ~11g protein)]]")

    def test_food_reply_repairs_missing_calories_and_daily_progress(self):
        b = self.bridge
        incomplete = "Protein looks good.\n[[LOG: Fruit and yogurt (~200 kcal, ~11g protein)]]"
        with patch.object(b, "run_llm", side_effect=[incomplete, self.food_reply()]) as model:
            result = b._generate_checked("food", source_text="I ate fruit and yogurt.")
        self.assertEqual(model.call_count, 2)
        self.assertIn("Meal total", result)
        self.assertIn("1,800 remaining", result)
        self.assertEqual(len(b.journal_entries()), 1)

    def test_food_receipt_is_app_owned_and_shown_once(self):
        b = self.bridge
        reply = self.food_reply() + "\n\n\U0001F37D\uFE0F logged \u2713\n\n\n\n\U0001F37D\uFE0F logged \u2713"
        with patch.object(b, "run_llm", return_value=reply):
            result = b._generate_checked("food", source_text="I ate fruit and yogurt.")
        self.assertEqual(result.count("logged \u2713"), 1)
        self.assertNotIn("\n\n\n", result)
        self.assertTrue(result.startswith("**\U0001F916 AgBot \u00b7"))

    def test_equivalent_readable_food_headings_do_not_trigger_an_extra_model_call(self):
        b = self.bridge
        reply = self.food_reply().replace("Daily progress", "Today's progress")
        with patch.object(b, "run_llm", return_value=reply) as model:
            b._generate_checked("food", source_text="I ate fruit and yogurt.")
        model.assert_called_once()

    def test_telegram_delivery_converts_tables_and_keeps_long_bold_chunks_balanced(self):
        b = self.bridge
        table = "| Food | Calories |\n|---|---|\n| Fruit | 60 kcal |\n"
        b.send_message(123, table + "\n**" + ("sample " * 700) + "**")
        messages = b.runtime_store().due_messages()
        self.assertGreater(len(messages), 1)
        visible = "".join(b._html_to_plain(m["payload"]["text"]) for m in messages)
        self.assertIn("60 kcal", visible)
        self.assertNotIn("|---", visible)
        for message in messages:
            chunk = message["payload"]["text"]
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))
            self.assertLessEqual(len(b._html_to_plain(chunk).encode("utf-16-le")) // 2, 4000)
        for message in messages[:-1]:
            self.assertTrue(b._html_to_plain(message["payload"]["text"])[-1].isspace())

    def test_meal_correction_replaces_active_entry_and_is_retry_safe(self):
        b = self.bridge
        b._current_request = "update:1"
        original = b.append_journal("Fruit and yogurt (~180 kcal)")
        b._current_request = "update:2"
        reply = self.food_reply() + "\n[[LOG_REPLACES: " + original["entry_id"] + "]]"
        with patch.object(b, "run_llm", return_value=reply):
            for _ in range(2):
                result = b._generate_checked("correction", source_text="There were two portions.")
        self.assertEqual(len(b.journal_entries()), 2)
        self.assertEqual(len(b.active_journal_entries()), 1)
        self.assertNotIn("180 kcal", b.recent_journal_text())
        self.assertIn("200 kcal", b.recent_journal_text())
        self.assertEqual(result.count("updated log \u2713"), 1)

    def test_meal_correction_preserves_original_day_without_explicit_new_date(self):
        b = self.bridge
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        original = b.append_journal("Fruit", on_date=yesterday)
        updated = b.append_journal("Two fruit", replaces_entry_id=original["entry_id"])
        self.assertEqual(updated["date"], yesterday)
        self.assertEqual(b.active_journal_entries(), [updated])

    def test_legacy_correction_link_is_not_an_extra_food_entry(self):
        b = self.bridge
        original = b.append_journal("First meal estimate")
        clarified = b.append_journal("Clarified meal estimate")
        link = {"entry_type": "supersession", "replaces_entry_id": original["entry_id"],
                "superseded_by": clarified["entry_id"], "text": "Audit link"}
        self.assertEqual(b.active_journal_entries([original, clarified, link]), [clarified])

    def test_unknown_meal_correction_is_not_silently_an_extra_meal(self):
        b = self.bridge
        reply = self.food_reply() + "\n[[LOG_REPLACES: note:missing]]"
        with patch.object(b, "run_llm", return_value=reply), self.assertRaises(ValueError):
            b._generate_checked("correction", source_text="It was two portions.")
        self.assertEqual(b.journal_entries(), [])

    def test_future_food_date_is_not_relabelled_today(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        reply = self.food_reply().replace("[[LOG:", "[[LOG " + tomorrow + ":")
        self.assertIn("Do not log future food consumption.",
                      self.bridge._food_reporting_errors(reply, "A food report"))

    def test_invalid_memory_quote_gets_one_repair_before_claiming_persistence(self):
        b = self.bridge
        source = "I prefer calories with every food report."
        marker = {"kind": "preference", "key": "nutrition_reporting",
                  "text": source, "source_quote": "Always show calories", "action": "upsert"}
        first = "I'll use that format.\n[[MEMORY: " + json.dumps(marker) + "]]"
        marker["source_quote"] = source
        second = "I'll use that format.\n[[MEMORY: " + json.dumps(marker) + "]]"
        with patch.object(b, "run_llm", side_effect=[first, second]) as model:
            result = b._generate_checked("preference", source_text=source)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len(b.memory_store().records("preference")), 1)
        self.assertNotIn("not saved", result)
        self.assertIn("Memory updated", result)

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

    def test_old_capabilities_keep_matching_sets_without_repeating_unrequested_full_sessions(self):
        b = self.bridge
        sequence = [{"sequence_index": 0, "exercise": "Synthetic press", "reps": 8},
                    {"sequence_index": 1, "exercise": "Synthetic row", "reps": 12}]
        old = {"activity_id": "old", "start": (date.today() - timedelta(days=20)).isoformat(),
               "logged_sets": [{"exercise": "Synthetic press", "sets": 1,
                               "set_indices": [0], "set_sequence": sequence}]}
        b.training_store().record_activities([old] + [
            {"activity_id": n, "start": date.today().isoformat(), "type": "running"}
            for n in range(13)])
        compact = b._compact_training_context(b.training_store().context(), {}, "GARMIN_JSON")
        move = compact["latest_observed_per_movement"]["synthetic press"]
        self.assertEqual(move["recorded_sets"]["set_sequence"], sequence[:1])
        self.assertIn("omitted", move["chronology_scope"])
        full = b._compact_training_context(b.training_store().context(), {}, "GARMIN_JSON",
                                          keep_older_sequences=True)
        self.assertEqual(full["latest_observed_per_movement"]["synthetic press"]
                         ["recorded_sets"]["set_sequence"], sequence)

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

    def test_unavailable_model_falls_back_with_images_and_rechecks_primary(self):
        b = self.bridge
        failure = SimpleNamespace(returncode=1, stdout="not an answer",
                                  stderr='Error: Model "primary-test" from --model flag is not available.')
        success = SimpleNamespace(returncode=0, stdout="Image answer", stderr="")
        with patch.object(b, "COPILOT_FALLBACK_MODELS", ("backup-test", "primary-test")), \
                patch.object(b.time, "monotonic", return_value=100) as clock, \
                patch.object(b.subprocess, "run", side_effect=[
                    failure, success, success, success]) as run:
            self.assertEqual(b._llm_copilot("synthetic", ["sample.png"]), "Image answer")
            health = b._health_report()
            self.assertEqual(health["model"], "primary-test")
            self.assertEqual(health["active_model"], "backup-test")
            self.assertIsNone(health["last_model_error"])
            b._llm_copilot("synthetic", ["sample.png"])
            clock.return_value = 100 + b._MODEL_RECHECK_SECONDS
            b._llm_copilot("synthetic", ["sample.png"])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([cmd[cmd.index("--model") + 1] for cmd in commands],
                         ["primary-test", "backup-test", "backup-test", "primary-test"])
        for call in run.call_args_list:
            self.assertIn("--available-tools=", call.args[0])
            self.assertIn("sample.png", call.args[0])
            self.assertEqual(call.kwargs["input"], "synthetic")
        self.assertEqual(b._llm_active_model, "primary-test")

    def test_all_models_unavailable_degrades_health_and_does_not_cache_failure(self):
        b = self.bridge
        b._current_request = "update:synthetic"
        errors = [SimpleNamespace(returncode=1, stdout="partial", stderr=
                                  f'Error: Model "{model}" from --model flag is not available.')
                  for model in ("primary-test", "backup-test")]
        with patch.object(b, "COPILOT_FALLBACK_MODELS", ("backup-test",)), \
                patch.object(b.subprocess, "run", side_effect=errors):
            self.assertIsNone(b.run_llm("synthetic"))
        self.assertEqual(b._health_report()["status"], "degraded")
        self.assertEqual(b._health_report()["last_model_error"], "model_unavailable")
        self.assertIsNone(b.runtime_store().saved_generation("update:synthetic:generation:1"))

    def test_other_cli_errors_never_switch_models(self):
        b = self.bridge
        for error in ("OAuth 503", "rate limit 429", "invalid argument --context",
                      'Error: Model "another-model" from --model flag is not available.'):
            with self.subTest(error=error), \
                    patch.object(b, "COPILOT_FALLBACK_MODELS", ("backup-test",)), \
                    patch.object(b.subprocess, "run", return_value=SimpleNamespace(
                        returncode=1, stdout="partial", stderr=error)) as run:
                self.assertIsNone(b._llm_copilot("synthetic", []))
                run.assert_called_once()
                self.assertEqual(b._health_report()["status"], "degraded")

    def test_timeout_never_switches_models_or_returns_partial_output(self):
        b = self.bridge
        with patch.object(b, "COPILOT_FALLBACK_MODELS", ("backup-test",)), \
                patch.object(b.subprocess, "run", side_effect=b.subprocess.TimeoutExpired(
                    "copilot", 1, output="partial")) as run:
            self.assertIsNone(b._llm_copilot("synthetic", []))
        run.assert_called_once()
        self.assertEqual(b._llm_last_error, "model_timeout")

    def test_optional_review_can_use_a_smaller_copilot_budget(self):
        b = self.bridge
        with patch.object(b.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="reply", stderr="")) as run:
            self.assertEqual(b._llm_copilot("synthetic", [], timeout=5), "reply")
        self.assertLessEqual(run.call_args.kwargs["timeout"], 5)
        self.assertGreater(run.call_args.kwargs["timeout"], 0)

    def test_success_clears_model_failure_health(self):
        b = self.bridge
        b._llm_last_error = "model_unavailable"
        with patch.object(b.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="reply", stderr="")):
            b._llm_copilot("synthetic", [])
        self.assertEqual(b._health_report()["status"], "ready")
        self.assertEqual(b._health_report()["active_model"], "primary-test")

    def test_recovered_photo_food_is_logged_once_on_original_message_day(self):
        b = self.bridge
        yesterday = date.today() - timedelta(days=1)
        b._set_msg_sent_at(datetime.combine(yesterday, datetime.min.time()).replace(
            hour=23, minute=58).timestamp())
        b._current_request = "update:synthetic-photo"
        with patch.object(b, "run_llm", return_value=self.food_reply()):
            for _ in range(2):
                result = b._generate_checked("photo", source_text="Having fruit and yogurt.")
        self.assertEqual(len(b.journal_entries()), 1)
        self.assertEqual(b.journal_entries()[0]["date"], yesterday.isoformat())
        self.assertIn("yesterday", result)

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
