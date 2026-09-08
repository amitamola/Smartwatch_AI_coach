"""Synthetic, offline correction-domain tests; no user cache or Garmin account."""

import copy
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coach_corrections import CorrectionStore, correction_intent


DAY = "2026-09-08"
REPORT = "My watch recorded 21 reps by mistake; I corrected it to 15."
MIXED_CORRECTION = (
    "Sorry, that was recorded as 21 reps by mistake of my watch, "
    "I corrected it to 15 reps now.")
MIXED_REPORT = (
    MIXED_CORRECTION
    + " Also, for single arm cable row I did 20kg but it was difficult."
    + " Tomorrow there is a class; can I join?")


def activity(aid="101", reps=21, exercise="FRONT_SQUAT"):
    sequence = [
        {"sequence_index": 0, "source_index": 0, "set_type": "ACTIVE",
         "start_time": "2026-09-08T12:00:00", "exercise": exercise, "reps": reps,
         "weight_raw": 10000, "weight_unit": "g", "weight_kg": 10,
         "effective_weight_unit": "g", "weight_unit_status": "verified",
         "weight_unit_source": "payload", "duration_s": 30},
        {"sequence_index": 1, "source_index": 1, "set_type": "REST",
         "start_time": "2026-09-08T12:00:30", "exercise": "UNKNOWN", "reps": 21,
         "weight_raw": -1, "weight_unit": None, "weight_kg": None},
        {"sequence_index": 2, "source_index": 2, "set_type": "ACTIVE",
         "start_time": "2026-09-08T12:01:00", "exercise": "BENCH_PRESS", "reps": 8,
         "weight_raw": 12, "weight_unit": "lb", "weight_kg": 5.443,
         "weight_unit_status": "verified", "weight_unit_source": "payload"},
        {"sequence_index": 3, "source_index": 3, "set_type": "ACTIVE",
         "start_time": "2026-09-08T12:02:00", "exercise": exercise, "reps": 10,
         "weight_raw": 12000, "weight_unit": None, "weight_kg": None,
         "weight_unit_status": "unknown", "weight_unit_source": "unknown"},
    ]
    return {"activity_id": aid, "start": DAY + " 12:00:00", "type": "strength_training",
            "logged_sets": [
                {"order": 1, "exercise": exercise.replace("_", " ").title(),
                 "sets": 2, "reps": f"10-{reps}", "top_weight_kg": 10,
                 "set_indices": [0, 3], "set_sequence": sequence,
                 "sequence_order": "chronological_start_time",
                 "data_freshness": {"status": "available", "fetched": False,
                                    "fetched_at": DAY + "T12:30:00+00:00"}},
                {"order": 2, "exercise": "Bench Press", "sets": 1, "reps": "8",
                 "top_weight_kg": 5.443, "set_indices": [2]}]}


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent / (".correction-fixture-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = self.directory / "synthetic.sqlite3"
        self.store = CorrectionStore(self.path)
        self.activities = [activity()]

    def report(self, text=REPORT, activities=None, request_id="synthetic-request-1", **kwargs):
        return self.store.process_report(text, self.activities if activities is None else activities,
                                         request_id, observed_on=DAY, **kwargs)

    def test_unique_old_rep_set_saves_durable_source_quote_and_date(self):
        result = self.report()
        self.assertTrue(result["saved"])
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["candidate_activity_ids"], ["101"])
        record = CorrectionStore(self.path).history()[0]
        self.assertEqual(record["source_quote"], REPORT)
        self.assertEqual(record["observed_on"], DAY)
        self.assertEqual(record["original_set"]["reps"], 21)
        self.assertEqual(record["corrected_value"], 15)
        self.assertEqual(record["target"]["sequence_index"], 0)
        self.assertFalse(record["measurement_verified"])

    def test_mixed_audited_message_scopes_correction_not_row_or_future_question(self):
        row = activity("102", reps=12, exercise="SINGLE_ARM_CABLE_ROW")
        activities = [activity(), row]
        original = copy.deepcopy(activities)
        self.assertTrue(correction_intent(MIXED_REPORT))
        result = self.report(MIXED_REPORT, activities=activities)
        self.assertTrue(result["saved"])
        self.assertEqual(result["candidate_activity_ids"], ["101"])
        self.assertEqual(result["correction"]["source_quote"], MIXED_CORRECTION)
        self.assertEqual(result["correction"]["request_source_text"], MIXED_REPORT)
        applied = self.store.apply(activities)
        self.assertEqual(applied[0]["logged_sets"][0]["set_sequence"][0]["reps"], 15)
        self.assertEqual(applied[1], row)
        self.assertEqual(activities, original)
        annotation = applied[0]["logged_sets"][0]["set_sequence"][0]["user_correction"]
        self.assertEqual(annotation["source_quote"], MIXED_CORRECTION)
        self.assertNotIn("difficulty", annotation)
        record = CorrectionStore(self.path).history()[0]
        self.assertEqual(record["source_quote"], MIXED_CORRECTION)
        self.assertEqual(record["request_source_text"], MIXED_REPORT)
        with closing(sqlite3.connect(self.path)) as db:
            source = db.execute("SELECT source_text FROM workout_correction_requests").fetchone()[0]
        self.assertEqual(source, MIXED_REPORT)

    def test_mixed_question_and_load_before_or_after_do_not_infect_rep_report(self):
        for index, text in enumerate((
            "Could I join tomorrow's class? " + REPORT,
            REPORT + " Would tomorrow's 20kg row be safe?",
            "For single arm cable row I did 20kg. " + REPORT,
            REPORT + " Tomorrow I plan 20kg rows. Could I do that?",
            REPORT + " Tomorrow there is a class; could I join it?",
            "Tomorrow there is a class; could I join it? " + REPORT,
            REPORT + " Also, for single arm cable row I couldn't do 20kg.",
        )):
            with self.subTest(text=text):
                result = self.report(text, activities=[activity(str(index + 200))],
                                     request_id=str(index))
                self.assertTrue(result["saved"])
                self.assertEqual(result["correction"]["source_quote"], REPORT)

    def test_adjacent_recorded_antecedent_is_preserved_verbatim_but_unrelated_followup_is_not(self):
        quote = "My watch recorded 21 reps by mistake.\nI corrected it to 15 reps."
        text = quote + " Tomorrow, could I do a 20kg row?"
        result = self.report(text)
        self.assertTrue(result["saved"])
        self.assertEqual(result["correction"]["source_quote"], quote)

    def test_correction_question_negation_or_prospective_guard_cannot_be_stripped(self):
        examples = (
            "I did not correct it. " + MIXED_REPORT,
            "I haven't corrected this yet. " + MIXED_REPORT,
            "I have not done this. " + MIXED_REPORT,
            "This never happened. " + MIXED_REPORT,
            "This is hypothetical. " + MIXED_REPORT,
            "Suppose this happened. " + MIXED_REPORT,
            "I did not make that correction. My watch recorded 21 reps. I corrected it to 15.",
            "My watch recorded 21 reps. Did I say I corrected it to 15 reps?",
            "My watch recorded 21 reps. I would have corrected it to 15 reps.",
            "My watch recorded 21 reps. I did not correct it to 15 reps.",
            "My watch recorded 21 reps. I corrected it to 15 reps? Tomorrow is class.",
            REPORT + " Actually, I did not correct it.",
            REPORT + " Is that correction right?",
            REPORT + " Is that right?",
            REPORT + " I'm not sure that is true.",
            "For example. " + REPORT,
        )
        for text in examples:
            with self.subTest(text=text):
                result = self.report(text, request_id=text)
                self.assertFalse(result["saved"])
                self.assertFalse(correction_intent(text))
        self.assertEqual(self.store.history(), [])

    def test_unrelated_row_sentence_must_not_disambiguate_multiple_old_rep_matches(self):
        result = self.report(MIXED_REPORT, activities=[
            activity(), activity("102", exercise="SINGLE_ARM_CABLE_ROW")])
        self.assertFalse(result["saved"])
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["reason"], "multiple_matching_sets")
        self.assertEqual(result["candidate_activity_ids"], ["101", "102"])
        self.assertEqual(self.store.history(), [])

    def test_multiple_corrections_in_separate_sentences_or_same_pair_need_clarification(self):
        for index, text in enumerate((
            "I corrected squat reps from 21 to 15. I corrected bench reps from 8 to 6.",
            "I corrected squat reps from 21 to 15. I corrected bench reps from 21 to 15.",
            "I corrected squat reps from 21 to 15; I corrected bench reps from 21 to 15.",
        )):
            with self.subTest(text=text):
                result = self.report(text, request_id=str(index))
                self.assertFalse(result["saved"])
                self.assertTrue(result["needs_clarification"])
                self.assertEqual(result["reason"], "multiple_reports")
        self.assertEqual(self.store.history(), [])

    def test_overlay_preserves_raw_order_rest_units_and_grouped_summaries(self):
        self.report()
        baseline = copy.deepcopy(self.activities)
        applied = self.store.apply(self.activities)
        groups = applied[0]["logged_sets"]
        sequence = groups[0]["set_sequence"]
        self.assertEqual(self.activities, baseline)
        self.assertEqual([p["reps"] for p in sequence], [15, 21, 8, 10])
        self.assertEqual([p["source_index"] for p in sequence], [0, 1, 2, 3])
        self.assertEqual(groups[0]["set_indices"], [0, 3])
        self.assertEqual(groups[0]["reps"], "10-15")
        self.assertEqual(groups[0]["garmin_original_summary"]["reps"], "10-21")
        self.assertEqual(groups[0]["top_weight_kg"], 10)
        self.assertEqual(groups[1]["top_weight_kg"], 5.443)
        self.assertIsNone(sequence[3]["weight_kg"])
        self.assertIsNone(sequence[3]["weight_unit"])
        self.assertEqual(sequence[0]["garmin_original_set"]["reps"], 21)
        self.assertEqual(sequence[0]["user_correction"]["status"], "source_conflict")
        self.assertEqual(groups[0]["user_corrections"][0]["source_quote"], REPORT)
        self.assertNotIn("effort", sequence[0])
        self.assertNotIn("form", sequence[0])

    def test_reapplying_overlay_and_request_replay_are_idempotent(self):
        first = self.report()
        replay = self.report()
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["correction"]["id"], replay["correction"]["id"])
        applied = self.store.apply(self.activities)
        self.assertEqual(self.store.apply(applied), applied)
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(len(self.store.history()[0]["observations"]), 1)
        with self.assertRaises(ValueError):
            self.report("I corrected reps from 21 to 12.")

    def test_ambiguous_matching_sets_do_not_save_or_guess_latest(self):
        for activities in ([activity(), activity("102")],):
            result = self.report(activities=activities)
            self.assertFalse(result["saved"])
            self.assertTrue(result["needs_clarification"])
            self.assertEqual(result["candidate_activity_ids"], ["101", "102"])
            self.assertEqual(len(result["candidates"]), 2)
            self.assertEqual(self.store.history(), [])
            self.assertEqual(self.store.apply(activities), activities)

    def test_ambiguous_same_exercise_reps_require_exact_reference(self):
        self.activities[0]["logged_sets"][0]["set_sequence"][3]["reps"] = 21
        result = self.report()
        self.assertTrue(result["needs_clarification"])
        result = self.report(request_id="clarified",
                             reference={"activity_id": "101", "exercise": "Front Squat", "sequence_index": 3})
        self.assertTrue(result["saved"])
        sequence = self.store.apply(self.activities)[0]["logged_sets"][0]["set_sequence"]
        self.assertEqual([p["reps"] for p in sequence], [21, 21, 8, 15])

    def test_text_activity_exercise_and_set_number_resolve_only_scoped_target(self):
        self.activities.append(activity("102"))
        result = self.report("In activity 102, I corrected front squat reps from 21 to 15.")
        self.assertTrue(result["saved"])
        self.assertEqual(result["correction"]["target"]["activity_id"], "102")
        self.activities[0]["logged_sets"][0]["set_sequence"][3]["reps"] = 21
        result = self.report("In activity 101, I corrected front squat reps from 21 to 12 in set 2.",
                             request_id="second-set")
        self.assertTrue(result["saved"])
        self.assertEqual(result["correction"]["target"]["sequence_index"], 3)

    def test_unrelated_named_exercise_does_not_fall_back_to_unique_other_set(self):
        for text in (
            "I corrected biceps curl reps from 21 to 15.",
            "I corrected reps from 21 to 15 for biceps curl.",
            "Biceps curl: my watch recorded 21 reps; I corrected it to 15.",
            "For bench press, my watch recorded 21 reps; I corrected it to 15.",
            "My biceps curl reps were recorded as 21 reps; I corrected it to 15.",
        ):
            with self.subTest(text=text):
                result = self.report(text, request_id=text)
                self.assertFalse(result["saved"])
                self.assertTrue(result["needs_clarification"])
        self.assertEqual(self.store.history(), [])

    def test_negated_conditional_future_questions_and_plans_are_not_reports(self):
        examples = [
            "The watch recorded 21 reps. I did not correct it to 15.",
            "The watch recorded 21 reps; I didn't correct it to 15.",
            "If I corrected reps from 21 to 15, would it change the plan?",
            "I will correct reps from 21 to 15.",
            "I am going to say I corrected reps from 21 to 15.",
            "I plan 15 reps instead of 21 tomorrow.",
            "I should have corrected reps from 21 to 15.",
            "Did I correct reps from 21 to 15?",
            "Have I corrected reps from 21 to 15",
            "Can you check if I corrected reps from 21 to 15",
            "Why did it say I corrected reps from 21 to 15",
            "Tomorrow I'll report that I corrected reps from 21 to 15.",
            "What happens when I corrected reps from 21 to 15?",
            "Let's do 15 reps instead of 21.",
            "Watch recorded 21 reps; perhaps I corrected it to 15.",
            "I did 15 reps today.",
            "Please correct 21 reps to 15.",
            "I changed weight from 21 kg to 15 kg, doing 8 reps.",
            "I corrected reps from 21 to 15.5.",
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertFalse(correction_intent(text))
                result = self.report(text, request_id=text)
                self.assertFalse(result["saved"])
                self.assertFalse(result["needs_clarification"])
        self.assertEqual(self.store.history(), [])

    def test_multiple_report_pairs_require_clarification(self):
        text = "I corrected squat reps from 21 to 15; I corrected bench reps from 8 to 6."
        self.assertTrue(correction_intent(text))
        result = self.report(text)
        self.assertEqual(result["reason"], "multiple_reports")
        self.assertFalse(result["saved"])
        result = self.report("I corrected reps from 21 to 15 or 12.", request_id="uncertain-value")
        self.assertTrue(result["needs_clarification"])
        self.assertFalse(result["saved"])

    def test_conflicting_duplicate_source_views_require_clarification(self):
        result = self.report(activities=[activity(), activity(reps=15)])
        self.assertFalse(result["saved"])
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["reason"], "ambiguous_source_identity")
        self.assertEqual(self.store.history(), [])

    def test_refresh_failure_and_unchanged_data_retain_user_evidence_without_verification(self):
        self.report()
        for freshness in (
            {"status": "available", "fetched": True, "data_changed": False, "refresh_status": "unchanged"},
            {"status": "stale", "fetched": True, "data_changed": None, "refresh_status": "error"},
        ):
            self.activities[0]["logged_sets"][0]["data_freshness"] = freshness
            sequence = self.store.apply(self.activities)[0]["logged_sets"][0]["set_sequence"]
            self.assertEqual(sequence[0]["reps"], 15)
            correction = sequence[0]["user_correction"]
            self.assertEqual(correction["current_garmin_value"], 21)
            self.assertFalse(correction["source_record_matches"])
            self.assertFalse(correction["measurement_verified"])
            self.assertEqual(correction["source_freshness"], freshness)
            self.assertIn("source_conflict", correction["status"])

    def test_garmin_match_reconciles_but_never_claims_measurement_verified(self):
        self.report()
        groups = self.activities[0]["logged_sets"]
        groups[0]["set_sequence"][0]["reps"] = 15
        groups[0]["reps"] = "10-15"
        groups[0]["data_freshness"].update(fetched=True, data_changed=True)
        correction = self.store.apply(self.activities)[0]["logged_sets"][0]["set_sequence"][0]["user_correction"]
        self.assertEqual(correction["status"], "reconciled")
        self.assertEqual(correction["current_garmin_value"], 15)
        self.assertTrue(correction["source_record_matches"])
        self.assertFalse(correction["measurement_verified"])
        self.assertEqual(self.store.history()[0]["original_set"]["reps"], 21)
        self.assertEqual(self.store.history()[0]["observations"][0]["status"], "reconciled")

    def test_intent_survives_already_corrected_server_value_but_requires_link(self):
        self.assertTrue(correction_intent(REPORT))
        self.activities[0]["logged_sets"][0]["set_sequence"][0]["reps"] = 15
        self.assertTrue(self.report()["needs_clarification"])
        result = self.report(request_id="exact",
                             reference={"activity_id": "101", "exercise": "Front Squat", "sequence_index": 0})
        self.assertTrue(result["saved"])
        self.assertEqual(result["correction"]["original_set"]["reps"], 15)
        self.assertEqual(result["correction"]["original_value"], 21)

    def test_supersession_keeps_history_and_latest_effective_value(self):
        first = self.report()
        second = self.report("I corrected front squat reps from 15 to 12.", request_id="revision-2")
        self.assertTrue(second["saved"])
        self.assertEqual(second["correction"]["supersedes"], first["correction"]["id"])
        records = CorrectionStore(self.path).history()
        self.assertEqual([r["status"] for r in records], ["superseded", "active"])
        self.assertEqual([r["original_set"]["reps"] for r in records], [21, 21])
        sequence = self.store.apply(self.activities)[0]["logged_sets"][0]["set_sequence"]
        self.assertEqual(sequence[0]["reps"], 12)

    def test_payload_wrappers_are_supported(self):
        wrapped = [{"activity_id": "101", "date": DAY, "payload": activity()}]
        self.assertTrue(self.report(activities=wrapped)["saved"])
        applied = self.store.apply(wrapped)
        self.assertEqual(applied[0]["payload"]["logged_sets"][0]["reps"], "10-15")
        self.assertEqual(wrapped[0]["payload"]["logged_sets"][0]["reps"], "10-21")

    def test_missing_complete_sequence_cannot_be_corrected(self):
        self.activities[0]["logged_sets"][0].pop("set_sequence")
        result = self.report()
        self.assertFalse(result["saved"])
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(self.store.history(), [])

    def test_changed_identity_is_not_silently_applied_to_other_set(self):
        self.report()
        self.activities[0]["logged_sets"][0]["set_sequence"][0]["start_time"] = "2026-09-08T17:00:00"
        result = self.store.apply(self.activities)
        self.assertEqual(result[0]["logged_sets"][0]["set_sequence"][0]["reps"], 21)
        self.assertEqual(result[0]["correction_warnings"][0]["status"], "source_set_unresolved")

    def test_same_database_does_not_modify_other_namespaces(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE training_synthetic (payload TEXT)")
            db.execute("INSERT INTO training_synthetic VALUES (?)", (json.dumps(activity()),))
            db.commit()
        self.report()
        self.store.apply(self.activities)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(json.loads(db.execute("SELECT payload FROM training_synthetic").fetchone()[0]),
                             activity())


if __name__ == "__main__":
    unittest.main()
