import copy
import json
import shutil
import sqlite3
import unittest
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from coach_program import (
    ProgramStore, build_review_evidence, parse_program_review, render_program,
)


TODAY = date(2026, 5, 20)


def activity(aid="one", day=None, exercise="Synthetic press",
             classification="intentional_training", **payload):
    day = day or TODAY
    data = {
        "activity_id": aid, "start": day.isoformat() + "T10:00:00",
        "type": "strength_training",
        "logged_sets": [{"exercise": exercise, "sets": 2, "reps": "6-8",
                         "top_weight_kg": None,
                         "set_sequence": [{"weight_kg": None, "reps": 6}]}],
    }
    data.update(payload)
    return {"activity_id": aid, "date": day.isoformat(),
            "classification": classification, "payload": data}


def memory(mid=1, text="Synthetic press felt comfortable with 3 reps in reserve.",
           **updates):
    record = {
        "id": mid, "kind": "anchor", "key": "capability:synthetic_press",
        "text": text, "observed_on": (TODAY - timedelta(days=1)).isoformat(),
        "status": "active", "source_type": "user", "source_text": text,
        "verified": True, "source_metadata": {"channel": "synthetic"},
        "revision": 1, "created_at": "2026-05-19T08:00:00+00:00",
        "updated_at": "2026-05-19T08:00:00+00:00",
    }
    record.update(updates)
    return record


def evidence(outcomes=None, memories=None, profile="Four days per week are available for training.",
             **context):
    if outcomes is None:
        outcomes = [activity("one", TODAY - timedelta(days=3)),
                    activity("two", TODAY - timedelta(days=1))]
    if memories is None:
        memories = [memory()]
    return build_review_evidence({"recent_outcomes": outcomes, **context},
                                 memories, profile, TODAY)


def programme(**changes):
    result = {
        "goal": "Build a consistent, tolerable training routine.",
        "weekly_training_days": 4,
        "session_templates": [{
            "id": "strength-a", "kind": "strength", "purpose": "Practise the current anchor.",
            "exercises": [{
                "name": "Synthetic press", "role": "anchor",
                "progression_rule": "Hold the current dose; consider a small change only "
                                    "when effort and next-day tolerance support it.",
            }],
        }],
        "decisions": [{
            "exercise": "Synthetic press", "action": "keep",
            "reason": "Retain the observed movement while checking tolerance.",
            "evidence_refs": ["activity:one", "memory:1"],
        }],
        "recovery_rule": "Reduce or defer training with new symptoms or poor tolerance.",
        "success_signals": ["Consistent attendance", "User-reported manageable effort"],
        "variety_review": "Coverage is limited; consider other patterns only when goals, "
                          "equipment and tolerance justify them, not for novelty.",
        "questions": ["How did the anchor feel later that day?"],
    }
    result.update(changes)
    return result


def parse(review=None, sources=None, **kwargs):
    review = review if review is not None else programme()
    return parse_program_review("Review summary\n[[TRAINING_PROGRAM: " +
                                json.dumps(review) + "]]",
                                sources if sources is not None else evidence(), **kwargs)


class ReviewEvidenceTests(unittest.TestCase):
    def test_compact_source_linked_observations_do_not_invent_metrics(self):
        source = evidence()
        self.assertEqual(source["comparable_sessions"]["synthetic press"]["count"], 2)
        self.assertEqual(source["intentional_training_days"]["count"], 2)
        self.assertEqual(set(source["refs"]), {"activity:one", "activity:two", "memory:1", "profile"})
        group = source["observations"][0]["logged_sets"][0]
        self.assertEqual(group["sets"], 2)
        self.assertEqual(group["reps"], "6-8")
        self.assertIsNone(group["top_weight_kg"])
        self.assertNotIn("set_sequence", json.dumps(source))
        self.assertNotIn("hard_sets", json.dumps(source))
        self.assertNotIn("captured_at", source)
        self.assertFalse(source["coverage"]["complete_history"])
        self.assertIn("unknown", source["observations"][0]["effort_form_tolerability"])
        self.assertEqual(source["memory"][0]["source_metadata"], {"channel": "synthetic"})
        self.assertEqual(source, json.loads(json.dumps(source)))
        self.assertEqual(source, evidence())

    def test_proposals_legacy_statuses_and_latest_summaries_are_not_proof(self):
        source = evidence(outcomes=[], plans=[{"status": "user_completed", "payload": programme()}],
                          user_reported_day_status=[{"date": TODAY.isoformat(), "status": "user_completed"}],
                          latest_observed_per_movement={"synthetic press": {"date": TODAY.isoformat()}})
        self.assertEqual(source["comparable_sessions"], {})
        self.assertEqual(source["intentional_training_days"]["count"], 0)
        self.assertFalse(any(ref.startswith("activity:") for ref in source["refs"]))
        self.assertIn("legacy completion", " ".join(source["caveats"]))

    def test_unknown_transport_and_recovery_remain_observations_not_training_proof(self):
        outcomes = [activity(str(n), classification=kind)
                    for n, kind in enumerate(("transport", "unknown", "active_recovery", "invented"))]
        missing_class = activity("missing")
        missing_class.pop("classification")
        source = evidence(outcomes=outcomes + [missing_class])
        self.assertEqual(len(source["observations"]), 5)
        self.assertEqual(source["intentional_training_days"]["count"], 0)
        self.assertEqual(source["comparable_sessions"]["synthetic press"]["count"], 1)
        self.assertEqual(source["comparable_sessions"]["synthetic press"]["refs"], ["activity:2"])

    def test_distinct_dates_not_activities_and_canonical_movement_counts(self):
        source = evidence(outcomes=[
            activity("a", exercise="DB RDL"),
            activity("b", exercise="Romanian Deadlift With Dumbbells"),
            activity("c", TODAY - timedelta(days=1), exercise="Barbell Romanian Deadlift"),
            activity("d", exercise="Synthetic row"),
        ])
        summary = source["comparable_sessions"]["romanian deadlift"]
        self.assertEqual(summary["count"], 2)
        self.assertEqual(len(summary["refs"]), 3)
        self.assertEqual(source["intentional_training_days"]["count"], 2)
        self.assertEqual(source["comparable_sessions"]["synthetic row"]["count"], 1)

    def test_future_old_missing_and_duplicate_rows_do_not_inflate_counts(self):
        source = evidence(outcomes=[
            activity("current"), activity("current"),
            activity("boundary", TODAY - timedelta(days=28)),
            activity("old", TODAY - timedelta(days=29)),
            activity("future", TODAY + timedelta(days=1)),
            {"activity_id": "undated", "payload": {}},
            {"activity_id": "invalid", "date": "invalid", "payload": {}},
        ])
        self.assertEqual(source["intentional_training_days"]["count"], 2)
        self.assertEqual(source["coverage"]["duplicate_activity_rows_ignored"], 1)
        self.assertEqual(source["coverage"]["excluded_invalid_future_or_out_of_window"], 4)
        self.assertNotIn("activity:future", source["refs"])
        self.assertNotIn("activity:old", source["refs"])

    def test_truncation_is_visible_and_no_omitted_records_are_invented(self):
        source = evidence(recent_outcomes_omitted=15, outcome_count_28d=17)
        self.assertEqual(source["coverage"]["supplied_outcomes_omitted"], 15)
        self.assertEqual(source["coverage"]["observed_activity_count"], 2)
        self.assertEqual(source["intentional_training_days"]["count"], 2)

    def test_modality_can_be_observed_but_title_is_not_an_exact_variant(self):
        source = evidence(outcomes=[
            activity("bike", logged_sets=[], type="indoor_cycling", name="Synthetic morning ride"),
            activity("strength", logged_sets=[], type="strength_training", name="Press day"),
        ])
        self.assertIn("indoor cycling", source["comparable_sessions"])
        self.assertNotIn("press day", source["comparable_sessions"])
        self.assertNotIn("strength training", source["comparable_sessions"])

    def test_cardio_measurements_keep_supplied_values_units_and_unknown_effort(self):
        source = evidence(outcomes=[activity(
            "run", logged_sets=[], type="treadmill_running", duration_s=1200,
            distance_m=3000, avg_speed_mps=2.5, avg_pace_s_per_km=400,
            avg_power=180, max_power=250, avg_hr=130, max_hr=150,
            training_load=40, aerobic_te=2.0, anaerobic_te=None,
            training_effect_label="SYNTHETIC_BASE")])
        observation = source["observations"][0]
        for key, value in (("duration_s", 1200), ("distance_m", 3000), ("avg_speed_mps", 2.5),
                           ("avg_pace_s_per_km", 400), ("avg_power", 180), ("max_power", 250),
                           ("avg_hr", 130), ("max_hr", 150), ("training_load", 40),
                           ("aerobic_te", 2.0), ("anaerobic_te", None)):
            with self.subTest(metric=key):
                self.assertEqual(observation[key], value)
        self.assertEqual(observation["metric_units"]["duration_s"], "s")
        self.assertEqual(observation["metric_units"]["distance_m"], "m")
        self.assertEqual(observation["metric_units"]["avg_pace_s_per_km"], "s/km")
        self.assertEqual(observation["metric_units"]["avg_power"], "W")
        self.assertEqual(observation["metric_units"]["avg_hr"], "bpm")
        self.assertIn("not external weight", observation["metric_units"]["training_load"])
        self.assertIn("unknown", observation["effort_form_tolerability"])
        self.assertIn("treadmill running", observation["canonical_exercises"])
        self.assertEqual(observation["date"], TODAY.isoformat())
        self.assertNotIn("RPE", observation)

    def test_missing_pace_is_not_derived_and_ambiguous_pace_has_no_invented_units(self):
        source = evidence(outcomes=[
            activity("a", logged_sets=[], type="running", avg_speed_mps=3),
            activity("b", logged_sets=[], type="running", avg_pace=6),
            activity("c", logged_sets=[], type="running", avg_pace=8, pace_unit="min/mile"),
        ])
        first, second, third = source["observations"]
        self.assertNotIn("avg_pace", first)
        self.assertNotIn("avg_pace_s_per_km", first)
        self.assertNotIn("avg_hr", first)
        self.assertEqual(second["metric_units"]["avg_pace"], "unspecified")
        self.assertEqual(third["metric_units"]["avg_pace"], "min/mile")

    def test_explicit_profile_availability_and_unknown_cases(self):
        for text, expected in [
            ("Four days per week, split across strength and cardio.", 4),
            ("I can train 3 days a week.", 3),
            ("Availability: 2 days/week.", 2),
            ("I train seven days each week.", 7),
            ("Four days per week. Do not prescribe Synthetic row; cycling or walking is preferred.", 4),
            ("I enjoy strength training.", None),
            ("Maybe four days per week.", None),
            ("I cannot train four days per week.", None),
            ("Three to four days per week.", None),
            ("3-4 days per week.", None),
            ("Four days per week or two days per week.", None),
            ("Four or five days per week.", None),
            ("Could I do four days per week?", None),
            ("Nine days per week.", None),
        ]:
            with self.subTest(profile=text):
                self.assertEqual(evidence(profile=text)["weekly_training_days"], expected)

    def test_current_verified_schedule_overrides_profile_not_model_or_legacy(self):
        schedule = memory(2, "I can train three days per week.",
                          kind="preference", key="training_days")
        source = evidence(memories=[schedule])
        self.assertEqual(source["weekly_training_days"], 3)
        self.assertEqual(source["weekly_training_days_refs"], ["memory:2"])
        for update in ({"verified": False}, {"source_type": "legacy"},
                       {"source_type": "model_proposal"}, {"status": "resolved"},
                       {"source_text": "A different message."},
                       {"observed_on": (TODAY + timedelta(days=1)).isoformat()}):
            with self.subTest(update=update):
                self.assertEqual(evidence(memories=[{**schedule, **update}])
                                 ["weekly_training_days"], 4)

    def test_conflicting_latest_feedback_stays_unknown_but_newer_feedback_wins(self):
        first = memory(2, "I can train three days per week.", kind="preference", key="schedule")
        second = memory(3, "I can train two days per week.", kind="preference", key="availability")
        source = evidence(memories=[first, second])
        self.assertIsNone(source["weekly_training_days"])
        second["updated_at"] = "2026-05-19T09:00:00+00:00"
        self.assertEqual(evidence(memories=[first, second])["weekly_training_days"], 2)
        second["observed_on"] = TODAY.isoformat()
        second["text"] = second["source_text"] = "My training availability is uncertain now."
        self.assertIsNone(evidence(memories=[first, second])["weekly_training_days"])

    def test_unrelated_daily_preference_is_not_a_training_schedule(self):
        walking_to_work = memory(2, "I commute four days per week.",
                                 kind="preference", key="commuting")
        self.assertIsNone(evidence(memories=[walking_to_work], profile="")["weekly_training_days"])

    def test_old_active_constraints_remain_but_future_and_resolved_memory_do_not(self):
        old = memory(2, "Avoid Synthetic row.", kind="preference",
                     key="avoid_exercise:synthetic_row",
                     observed_on=(TODAY - timedelta(days=90)).isoformat())
        source = evidence(memories=[old, memory(3, status="resolved"),
                                    memory(4, observed_on=(TODAY + timedelta(days=1)).isoformat())])
        self.assertEqual([r["id"] for r in source["memory"]], [2])
        self.assertFalse(source["refs"]["memory:2"]["within_window"])


class ReviewValidationTests(unittest.TestCase):
    def assertRejected(self, review, message=None, **kwargs):
        _, result, errors = parse(review, **kwargs)
        self.assertIsNone(result)
        self.assertTrue(errors)
        if message:
            self.assertIn(message, " ".join(errors))

    def test_exactly_one_valid_marker_returns_clean_text_and_complete_review(self):
        clean, result, errors = parse()
        self.assertEqual(clean, "Review summary")
        self.assertEqual(result, programme())
        self.assertEqual(errors, [])

    def test_absent_multiple_malformed_and_unsafe_markers_are_atomic(self):
        marker = "[[TRAINING_PROGRAM: " + json.dumps(programme()) + "]]"
        for text in ("No proposal", marker + marker, marker + "[[TRAINING_PROGRAM: broken]]",
                     "[[TRAINING_PROGRAM: {broken}]]", "[[TRAINING_PROGRAM {}]]",
                     "[[TRAINING_PROGRAM: {}", "[[TRAINING_PROGRAM: []]]"):
            with self.subTest(text=text[:65]):
                _, review, errors = parse_program_review(text, evidence())
                self.assertIsNone(review)
                self.assertTrue(errors)
        _, review, errors = parse_program_review("Guaranteed back-safe.\n" + marker, evidence())
        self.assertIsNone(review)
        self.assertTrue(errors)

    def test_json_string_containing_closing_brackets_is_not_truncated(self):
        review = programme(goal="A synthetic goal with ]] inside the text.")
        self.assertEqual(parse(review)[1], review)

    def test_duplicate_json_keys_and_non_json_numbers_are_rejected(self):
        for raw in (json.dumps(programme()).replace('"weekly_training_days": 4',
                                                   '"weekly_training_days": 4, "weekly_training_days": 2'),
                    json.dumps(programme()).replace('"weekly_training_days": 4',
                                                   '"weekly_training_days": NaN')):
            with self.subTest(raw=raw[:70]):
                _, review, errors = parse_program_review("[[TRAINING_PROGRAM: " + raw + "]]", evidence())
                self.assertIsNone(review)
                self.assertTrue(errors)

    def test_availability_ceiling_reduction_unknown_and_boolean(self):
        self.assertIsNotNone(parse(programme(weekly_training_days=2))[1])
        self.assertIsNotNone(parse(programme(weekly_training_days=None))[1])
        self.assertRejected(programme(weekly_training_days=5), "availability")
        for value in (0, 8, True, 4.0, "four"):
            with self.subTest(value=value):
                self.assertRejected(programme(weekly_training_days=value), "integer")
        unknown = evidence(profile="")
        self.assertRejected(programme(), "invented", sources=unknown)
        self.assertIsNotNone(parse(programme(weekly_training_days=None), unknown)[1])

    def test_model_cannot_choose_dates_deadlines_or_add_set_prescriptions(self):
        for field in ("reviewed_on", "next_review_date", "block_end", "date"):
            with self.subTest(field=field):
                self.assertRejected(programme(**{field: "2026-06-01"}), "application-owned")
        review = programme()
        review["session_templates"][0]["exercises"][0]["sets"] = [{"reps": 8, "weight_kg": 10}]
        self.assertRejected(review, "unsupported fields")

    def test_all_required_fields_shape_limits_and_action_validation(self):
        for field in programme():
            review = programme()
            del review[field]
            with self.subTest(missing=field):
                self.assertRejected(review, "missing fields")
        for field, value in (("goal", ""), ("goal", "x" * 601),
                             ("session_templates", []), ("decisions", []),
                             ("success_signals", []), ("questions", ["One?", "Two?", "Three?"]),
                             ("questions", [None]), ("variety_review", " ")):
            with self.subTest(field=field, value=str(value)[:40]):
                self.assertRejected(programme(**{field: value}))
        review = programme()
        review["decisions"][0]["action"] = "rotate"
        self.assertRejected(review, "unknown decision action")
        review["decisions"][0]["action"] = []
        self.assertRejected(review)

    def test_duplicate_ids_names_and_competing_decisions_fail(self):
        review = programme()
        review["session_templates"].append(copy.deepcopy(review["session_templates"][0]))
        self.assertRejected(review, "unique")
        review = programme()
        review["session_templates"][0]["id"] = "Unstable Name"
        self.assertRejected(review, "slug")
        review = programme()
        review["session_templates"][0]["exercises"].append(
            copy.deepcopy(review["session_templates"][0]["exercises"][0]))
        self.assertRejected(review, "distinct")
        review = programme()
        review["decisions"].append(copy.deepcopy(review["decisions"][0]))
        self.assertRejected(review, "duplicate decisions")

    def test_same_exercise_can_be_an_option_in_multiple_templates(self):
        review = programme()
        second = copy.deepcopy(review["session_templates"][0])
        second["id"] = "strength-b"
        review["session_templates"].append(second)
        self.assertIsNotNone(parse(review)[1])
        self.assertEqual(len(parse(review)[1]["decisions"]), 1)

    def test_invalid_empty_or_duplicate_refs_fail(self):
        for refs in ([], ["activity:invented"], ["profile", "bad"],
                     ["activity:one", "activity:one"], [True]):
            review = programme()
            review["decisions"][0]["evidence_refs"] = refs
            with self.subTest(refs=refs):
                self.assertRejected(review, "evidence")

    def test_unsupported_certainty_and_unconditional_progression_fail(self):
        self.assertRejected(programme(goal="A zero-spinal-load exercise."), "certainty")
        self.assertIsNotNone(parse(programme(goal="Do not claim a zero-spinal-load exercise."))[1])
        review = programme()
        review["session_templates"][0]["exercises"][0]["progression_rule"] = "Add weight every week."
        self.assertRejected(review, "effort/tolerance")
        review["session_templates"][0]["exercises"][0]["progression_rule"] = "Always increase despite pain."
        self.assertRejected(review, "must not force")

    def test_progress_needs_two_distinct_dates_same_movement_and_verified_anchor(self):
        review = programme()
        review["decisions"][0].update(
            action="progress", evidence_refs=["activity:one", "activity:two", "memory:1"])
        self.assertIsNotNone(parse(review)[1])
        same_day = evidence(outcomes=[activity("one"), activity("two")])
        self.assertRejected(review, "two distinct", sources=same_day)
        wrong_movement = evidence(outcomes=[activity("one"), activity("two", TODAY - timedelta(days=1),
                                                                        exercise="Synthetic row")])
        self.assertRejected(review, "same canonical exercise", sources=wrong_movement)
        review["decisions"][0]["evidence_refs"] = ["activity:one", "activity:two", "profile"]
        self.assertRejected(review, "verified user capability")

    def test_exact_cardio_modality_can_progress_with_dates_and_user_effort_anchor(self):
        review = programme()
        review["session_templates"][0]["kind"] = "cardio"
        review["session_templates"][0]["exercises"][0]["name"] = "Treadmill running"
        review["decisions"][0].update(
            exercise="Treadmill running", action="progress",
            evidence_refs=["activity:one", "activity:two", "memory:1"])
        report = memory(text="Treadmill running felt comfortable at conversational effort.",
                        key="capability:treadmill_running")
        outcomes = [
            activity("one", TODAY - timedelta(days=3), logged_sets=[], type="treadmill_running",
                     duration_s=1200, avg_hr=125, avg_power=150),
            activity("two", TODAY - timedelta(days=1), logged_sets=[], type="treadmill_running",
                     duration_s=1400, avg_hr=165, avg_power=240),
        ]
        source = evidence(outcomes=outcomes, memories=[report])
        self.assertIsNotNone(parse(review, source)[1])
        comparison = source["comparable_sessions"]["treadmill running"]
        self.assertEqual(comparison["count"], 2)
        self.assertIn("intensity", comparison["comparison_basis"])
        self.assertIn("unverified", comparison["comparison_basis"])
        self.assertEqual(source["observations"][0]["avg_hr"], 125)
        self.assertEqual(source["observations"][1]["avg_hr"], 165)
        outcomes[1]["payload"]["type"] = "running"
        self.assertRejected(review, "same canonical exercise",
                            sources=evidence(outcomes=outcomes, memories=[report]))
        outcomes[1]["payload"]["type"] = "treadmill_running"
        outcomes[1]["date"] = outcomes[0]["date"]
        self.assertRejected(review, "two distinct",
                            sources=evidence(outcomes=outcomes, memories=[report]))

    def test_device_snapshots_are_known_audit_refs_not_progression_proof(self):
        source = evidence()
        source["refs"]["snapshot"] = {
            "kind": "device_snapshot", "data": {"date": TODAY.isoformat(), "readiness": 70}}
        source["refs"]["fitness_metrics"] = {
            "kind": "device_metrics", "data": {"date": TODAY.isoformat(), "fitness_score": 40}}
        review = programme()
        review["decisions"][0]["evidence_refs"] = ["snapshot", "fitness_metrics"]
        self.assertIsNotNone(parse(review, source)[1])
        review["decisions"][0]["action"] = "progress"
        self.assertRejected(review, "two distinct", sources=source)
        review["decisions"][0]["evidence_refs"] += ["activity:one", "activity:two"]
        self.assertRejected(review, "verified user capability", sources=source)
        review["decisions"][0]["evidence_refs"].append("memory:1")
        self.assertIsNotNone(parse(review, source)[1])

    def test_watch_only_reasons_cannot_claim_good_form_or_pain_free_execution(self):
        review = programme()
        review["decisions"][0]["evidence_refs"] = ["activity:one", "activity:two"]
        for reason in ("Repeated recordings demonstrate solid tolerability and control.",
                       "Completed with stable execution and good reserve.",
                       "Observed with comfortable squat mechanics and good depth.",
                       "Regularly completed without back strain."):
            with self.subTest(reason=reason):
                review["decisions"][0]["reason"] = reason
                self.assertRejected(review, "verified user feedback")
        review["decisions"][0]["reason"] = (
            "Recorded on multiple dates; form and tolerance require user feedback.")
        self.assertIsNotNone(parse(review)[1])

    def test_transport_unknown_future_and_old_sessions_are_not_progression_proof(self):
        review = programme()
        review["decisions"][0].update(
            action="progress", evidence_refs=["activity:one", "activity:two", "memory:1"])
        for classification in ("transport", "unknown"):
            with self.subTest(classification=classification):
                source = evidence(outcomes=[activity("one", TODAY - timedelta(days=1)),
                                            activity("two", classification=classification)])
                self.assertRejected(review, "two distinct", sources=source)
        for day in (TODAY + timedelta(days=1), TODAY - timedelta(days=29)):
            with self.subTest(day=day):
                self.assertRejected(review, "unknown evidence", sources=evidence(
                    outcomes=[activity("one"), activity("two", day)]))

    def test_recovery_capability_can_progress_without_becoming_training_days(self):
        review = programme()
        review["session_templates"][0]["kind"] = "recovery"
        review["session_templates"][0]["exercises"][0]["name"] = "pilates"
        review["decisions"][0].update(
            exercise="pilates", action="progress",
            evidence_refs=["activity:one", "activity:two", "memory:1"])
        outcomes = [
            activity("one", TODAY - timedelta(days=3), classification="active_recovery",
                     type="pilates", logged_sets=[]),
            activity("two", TODAY - timedelta(days=1), classification="active_recovery",
                     type="pilates", logged_sets=[]),
        ]
        source = evidence(outcomes=outcomes, memories=[
            memory(text="Pilates felt easy and controlled.", key="capability:pilates")])
        self.assertIsNotNone(parse(review, source)[1])
        self.assertEqual(source["intentional_training_days"]["count"], 0)
        self.assertEqual(source["comparable_sessions"]["pilates"]["count"], 2)
        source["refs"]["memory:1"]["verified"] = False
        self.assertRejected(review, "verified user capability", sources=source)

    def test_legacy_proposed_unverified_old_or_wrong_movement_anchor_cannot_prove_progress(self):
        review = programme()
        review["decisions"][0].update(
            action="progress", evidence_refs=["activity:one", "activity:two", "memory:1"])
        for anchor in (
            memory(verified=False),
            memory(source_type="legacy"),
            memory(source_type="model_proposal"),
            memory(kind="health"),
            memory(observed_on=(TODAY - timedelta(days=29)).isoformat()),
            memory(text="Synthetic row felt comfortable.", key="capability:synthetic_row"),
            memory(text="I like this exercise."),
        ):
            with self.subTest(anchor=anchor["text"], source=anchor["source_type"]):
                self.assertRejected(review, "verified user capability",
                                    sources=evidence(memories=[anchor]))

    def test_new_unobserved_moves_need_introduce_and_no_fake_load_fields(self):
        source = evidence(outcomes=[])
        review = programme()
        review["decisions"][0]["evidence_refs"] = ["profile"]
        self.assertRejected(review, "new unobserved", sources=source)
        review["decisions"][0]["action"] = "introduce"
        result = parse(review, source)[1]
        self.assertIsNotNone(result)
        self.assertNotIn("weight", json.dumps(result))
        self.assertNotIn("completed", json.dumps(result))

    def test_known_move_can_be_kept_on_initialization_and_prior_option_is_not_history(self):
        self.assertIsNotNone(parse()[1])
        review = programme()
        review["decisions"][0]["evidence_refs"] = ["profile"]
        self.assertIsNotNone(parse(review, evidence(outcomes=[]), previous=programme())[1])
        review["decisions"][0]["action"] = "progress"
        self.assertRejected(review, "two distinct", sources=evidence(outcomes=[]), previous=programme())

    def test_every_distinct_template_exercise_needs_a_decision(self):
        review = programme()
        row = copy.deepcopy(review["session_templates"][0]["exercises"][0])
        row["name"] = "Synthetic row"
        review["session_templates"][0]["exercises"].append(row)
        self.assertRejected(review, "missing decision")

    def test_previous_anchor_cannot_be_silently_dropped_or_demoted(self):
        previous = programme()
        review = programme()
        review["session_templates"][0]["exercises"][0]["name"] = "Synthetic row"
        review["decisions"][0].update(exercise="Synthetic row", action="introduce")
        self.assertRejected(review, "previous anchor", previous=previous)
        review["decisions"].append({
            "exercise": "Synthetic press", "action": "remove",
            "reason": "Remove the old option after the user's changed preference.",
            "evidence_refs": ["memory:1"],
        })
        self.assertIsNotNone(parse(review, previous={"review": previous})[1])
        review = programme()
        review["session_templates"][0]["exercises"][0]["role"] = "accessory"
        self.assertRejected(review, "changed anchor role", previous=previous)
        review["decisions"][0]["action"] = "reduce"
        self.assertIsNotNone(parse(review, previous=previous)[1])

    def test_active_exact_exclusions_and_resolved_exclusion(self):
        preference = {"key": "avoid_exercise:synthetic_press", "status": "active"}
        self.assertRejected(programme(), "excluded movement", preferences=[preference])
        self.assertIsNotNone(parse(preferences=[{**preference, "status": "resolved"}])[1])
        source = evidence(memories=[memory(2, "Avoid the synthetic press.", kind="preference",
                                          key="avoid_exercise:synthetic_press")])
        self.assertRejected(programme(), "excluded movement", sources=source)

    def test_canonical_deadlift_exclusion_covers_alias_not_all_variants(self):
        preference = {"key": "avoid_exercise:normal_RDL", "status": "active"}
        for name in ("DB RDL", "Romanian Deadlift With Dumbbells", "Barbell Romanian Deadlift"):
            review = programme()
            review["session_templates"][0]["exercises"][0]["name"] = name
            review["decisions"][0].update(exercise=name, action="introduce")
            with self.subTest(name=name):
                self.assertRejected(review, "excluded movement", preferences=[preference])
        review["session_templates"][0]["exercises"][0]["name"] = "Single-leg RDL"
        review["decisions"][0]["exercise"] = "Single-leg RDL"
        self.assertIsNotNone(parse(review, preferences=[preference])[1])

    def test_old_excluded_exercise_can_be_explicitly_replaced_or_removed(self):
        previous = programme()
        preference = {"key": "avoid_exercise:synthetic_press", "status": "active"}
        review = programme()
        review["session_templates"][0]["exercises"][0]["name"] = "Synthetic row"
        review["decisions"][0].update(action="replace", replacement="Synthetic row")
        self.assertIsNotNone(parse(review, preferences=[preference], previous=previous)[1])
        review["decisions"][0].pop("replacement")
        review["decisions"][0]["action"] = "remove"
        review["decisions"].append({
            "exercise": "Synthetic row", "action": "introduce",
            "reason": "Consider this option without assuming tolerance.",
            "evidence_refs": ["profile"],
        })
        self.assertIsNotNone(parse(review, preferences=[preference], previous=previous)[1])

    def test_replacement_must_be_different_present_and_nonexcluded(self):
        for replacement in (None, "Synthetic press", "Absent row"):
            review = programme()
            review["session_templates"][0]["exercises"][0]["name"] = "Synthetic row"
            review["decisions"][0].update(action="replace", replacement=replacement)
            with self.subTest(replacement=replacement):
                self.assertRejected(review, "replacement")
        review = programme()
        review["decisions"][0].update(action="remove")
        self.assertRejected(review, "still prescribed")

    def test_alias_only_change_cannot_masquerade_as_replacement(self):
        review = programme()
        review["session_templates"][0]["exercises"][0]["name"] = "Romanian deadlift"
        review["decisions"][0].update(exercise="RDL", action="replace", replacement="Romanian deadlift")
        self.assertRejected(review, "alias-only")


class ProgramStoreTests(unittest.TestCase):
    def setUp(self):
        # Synthetic, project-local scratch space; never access an actual coach database.
        self.folder = Path.cwd() / (".program-tests-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.path = self.folder / "synthetic.sqlite3"
        self.store = ProgramStore(self.path)
        self.sources = evidence()

    def save(self, day=TODAY, fingerprint="feedback-a", request_key=None, review=None):
        return self.store.save_review(review or programme(), self.sources, fingerprint,
                                      today=day, request_key=request_key)

    def event_count(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("SELECT COUNT(*) FROM training_program_reviews").fetchone()[0]

    def test_initial_record_full_shape_and_weekly_cadence(self):
        initial = self.store.context(today=TODAY, fingerprint="feedback-a")
        self.assertTrue(initial["review_due"])
        self.assertEqual(initial["due_reason"], "initial")
        self.assertIsNone(initial["active"])
        self.assertIsNone(initial["block_baseline"])
        record = self.save()
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["block_start"], TODAY.isoformat())
        self.assertEqual(record["block_end"], (TODAY + timedelta(days=28)).isoformat())
        self.assertEqual(record["next_review_date"], (TODAY + timedelta(days=7)).isoformat())
        self.assertEqual(record["source_type"], "model_proposal")
        self.assertEqual(record["source_refs"], self.sources["refs"])
        self.assertEqual(record["evidence"], self.sources)
        for offset in (0, 1, 6):
            self.assertFalse(self.store.context(TODAY + timedelta(days=offset))["review_due"])
        due = self.store.context(TODAY + timedelta(days=7))
        self.assertTrue(due["review_due"])
        self.assertEqual(due["due_reason"], "weekly")

    def test_feedback_review_waits_until_next_day_and_dirty_state_survives_restart(self):
        self.save()
        same_day = self.store.context(TODAY, fingerprint="feedback-b")
        self.assertFalse(same_day["review_due"])
        self.assertEqual(same_day["next_review_date"], (TODAY + timedelta(days=1)).isoformat())
        restarted = ProgramStore(self.path)
        next_day = restarted.context(TODAY + timedelta(days=1))
        self.assertTrue(next_day["review_due"])
        self.assertEqual(next_day["due_reason"], "feedback_changed")
        second = self.save(TODAY + timedelta(days=1), fingerprint="feedback-b")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["block_start"], TODAY.isoformat())
        self.assertEqual(second["next_review_date"], (TODAY + timedelta(days=8)).isoformat())
        self.assertFalse(self.store.context(TODAY + timedelta(days=1), "feedback-c")["review_due"])
        self.assertTrue(self.store.context(TODAY + timedelta(days=2))["review_due"])

    def test_reverted_feedback_does_not_trigger_review(self):
        self.save()
        self.store.context(TODAY, "feedback-b")
        self.store.context(TODAY, "feedback-a")
        self.assertFalse(self.store.context(TODAY + timedelta(days=1))["review_due"])

    def test_feedback_days_after_review_uses_detection_day_and_does_not_postpone(self):
        self.save()
        changed_day = TODAY + timedelta(days=3)
        context = self.store.context(changed_day, "feedback-b")
        self.assertFalse(context["review_due"])
        self.assertEqual(context["next_review_date"], (changed_day + timedelta(days=1)).isoformat())
        restarted = ProgramStore(self.path)
        next_day = restarted.context(changed_day + timedelta(days=1), "feedback-c")
        self.assertTrue(next_day["review_due"])
        self.assertEqual(next_day["due_reason"], "feedback_changed")
        self.assertEqual(next_day["feedback_detected_on"], changed_day.isoformat())

    def test_failed_forced_review_retries_even_before_the_regular_due_date(self):
        self.save()
        failed_at = datetime.combine(TODAY + timedelta(days=1), datetime.min.time()).replace(
            hour=12, tzinfo=timezone.utc)
        self.store.record_failure("model unavailable", now=failed_at)
        self.assertFalse(self.store.context(now=failed_at + timedelta(minutes=5))["review_due"])
        retry = ProgramStore(self.path).context(now=failed_at + timedelta(minutes=31))
        self.assertTrue(retry["review_due"])
        self.assertEqual(retry["due_reason"], "retry")
        self.assertEqual(retry["active"]["revision"], 1)

    def test_weekly_review_preserves_block_and_caps_next_date_at_boundary(self):
        initial = self.save()
        weekly = self.save(TODAY + timedelta(days=7))
        late = self.save(TODAY + timedelta(days=26))
        self.assertEqual(weekly["block_start"], initial["block_start"])
        self.assertEqual(weekly["block_end"], initial["block_end"])
        self.assertEqual(late["next_review_date"], initial["block_end"])
        self.assertEqual(self.store.context(TODAY + timedelta(days=28))["due_reason"], "block")

    def test_block_baseline_keeps_first_observations_not_latest_review_or_proposals(self):
        first = self.save(request_key="baseline-request")
        first_context = self.store.context(TODAY)
        self.assertEqual(first_context["block_baseline"], first)
        self.assertIsNot(first_context["block_baseline"], first_context["active"])
        weekly_day = TODAY + timedelta(days=7)
        updated_sources = build_review_evidence({
            "recent_outcomes": [
                activity("one", TODAY - timedelta(days=3)),
                activity("two", TODAY - timedelta(days=1)),
                activity("three", TODAY + timedelta(days=6)),
            ],
            "plans": [{"payload": {"exercises": [{"name": "Invented planned movement"}]},
                       "status": "user_completed"}],
        }, [memory()], "Four days per week are available for training.", weekly_day)
        latest = self.store.save_review(programme(goal="Refine the same tolerable routine."),
                                        updated_sources, "feedback-a", weekly_day)
        context = ProgramStore(self.path).context(weekly_day)
        self.assertEqual(context["active"], latest)
        self.assertEqual(context["block_baseline"], first)
        self.assertEqual(len(context["block_baseline"]["evidence"]["observations"]), 2)
        self.assertEqual(len(context["active"]["evidence"]["observations"]), 3)
        self.assertNotIn("Invented planned movement",
                         json.dumps(context["block_baseline"]["evidence"]))
        context["block_baseline"]["evidence"]["observations"].clear()
        self.assertEqual(self.store.context(weekly_day)["block_baseline"], first)
        self.assertEqual(self.store.review_for_request("baseline-request"), first)

    def test_block_rollover_opens_new_baseline_without_deleting_prior_successes(self):
        first = self.save(request_key="first-block")
        self.save(TODAY + timedelta(days=7))
        rollover_day = TODAY + timedelta(days=40)
        new_block = self.save(rollover_day, request_key="second-block")
        self.assertEqual(self.store.context(rollover_day)["block_baseline"], new_block)
        self.save(rollover_day + timedelta(days=7))
        context = ProgramStore(self.path).context(rollover_day + timedelta(days=7))
        self.assertEqual(context["block_baseline"], new_block)
        self.assertEqual(context["active"]["revision"], 4)
        self.assertEqual(self.event_count(), 4)
        self.assertEqual(self.store.review_for_request("first-block"), first)

    def test_overdue_restart_rolls_new_block_from_actual_day_without_missed_weeks(self):
        self.save()
        restarted = ProgramStore(self.path)
        day = TODAY + timedelta(days=60)
        self.assertEqual(restarted.context(day)["due_reason"], "block")
        reviewed = restarted.save_review(programme(), self.sources, "feedback-a", day)
        self.assertEqual(reviewed["revision"], 2)
        self.assertEqual(reviewed["block_start"], day.isoformat())
        self.assertEqual(reviewed["block_end"], (day + timedelta(days=28)).isoformat())
        self.assertEqual(self.event_count(), 2)
        self.assertFalse(ProgramStore(self.path).context(day)["review_due"])

    def test_repeated_request_key_returns_original_success_without_revalidation_or_reset(self):
        first = self.save(request_key="request-1")
        second = self.save(TODAY + timedelta(days=7), request_key="request-2")
        replay = self.store.save_review({"invalid": True}, {}, "different",
                                        TODAY + timedelta(days=15), request_key="request-1")
        self.assertEqual(replay, first)
        self.assertEqual(self.store.review_for_request("request-1"), first)
        self.assertEqual(self.store.context(TODAY + timedelta(days=7))["active"], second)
        self.assertEqual(self.event_count(), 2)
        self.assertIsNone(self.store.review_for_request("missing"))
        self.assertIsNone(self.store.review_for_request(None))

    def test_same_content_same_day_is_idempotent_and_new_request_alias_is_replayable(self):
        first = self.save(request_key="request-1")
        self.assertEqual(self.save(), first)
        self.assertEqual(self.save(request_key="request-2"), first)
        self.assertEqual(self.store.review_for_request("request-2"), first)
        self.assertEqual(self.event_count(), 1)
        weekly = self.save(TODAY + timedelta(days=7))
        self.assertEqual(weekly["revision"], 2)
        self.assertEqual(self.event_count(), 2)

    def test_failure_backoff_is_durable_and_never_advances_success_or_review(self):
        active = self.save()
        before = self.store.context(TODAY)["last_success_at"]
        failure_day = TODAY + timedelta(days=7)
        instant = datetime.combine(failure_day, datetime.min.time(), tzinfo=timezone.utc)
        self.store.record_failure("Synthetic model response was invalid.", now=instant)
        restarted = ProgramStore(self.path)
        blocked = restarted.context(failure_day, now=instant + timedelta(minutes=29))
        self.assertFalse(blocked["review_due"])
        self.assertEqual(blocked["due_reason"], "weekly")
        self.assertEqual(blocked["active"], active)
        self.assertEqual(blocked["last_success_at"], before)
        self.assertEqual(blocked["failure_count"], 1)
        self.assertIn("invalid", blocked["last_error"])
        self.assertEqual(self.event_count(), 1)
        retry = restarted.context(failure_day, now=instant + timedelta(minutes=30))
        self.assertTrue(retry["review_due"])
        saved = restarted.save_review(programme(), self.sources, "feedback-a", failure_day)
        after = restarted.context(failure_day, now=instant + timedelta(minutes=31))
        self.assertEqual(after["active"], saved)
        self.assertIsNone(after["last_error"])
        self.assertIsNone(after["retry_after"])
        self.assertEqual(after["failure_count"], 0)

    def test_initial_failure_has_backoff_but_no_active_programme(self):
        instant = datetime(2026, 5, 20, 9, tzinfo=timezone.utc)
        self.store.record_failure("Synthetic outage", now=instant)
        context = self.store.context(now=instant)
        self.assertEqual(context["due_reason"], "initial")
        self.assertFalse(context["review_due"])
        self.assertIsNone(context["active"])
        self.assertIsNone(context["last_success_at"])
        self.assertEqual(self.event_count(), 0)
        self.assertTrue(self.store.context(now=instant + timedelta(minutes=31))["review_due"])

    def test_invalid_save_is_atomic_and_preserves_success_and_request_ledger(self):
        first = self.save()
        before = self.store.context(TODAY)
        with self.assertRaisesRegex(ValueError, "not saved"):
            self.save(TODAY + timedelta(days=7), review=programme(goal=""), request_key="bad-request")
        self.assertEqual(self.store.context(TODAY), before)
        self.assertEqual(self.store.context(TODAY)["active"], first)
        self.assertEqual(self.event_count(), 1)
        self.assertIsNone(self.store.review_for_request("bad-request"))

    def test_append_and_active_update_roll_back_together_on_database_failure(self):
        self.save()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER synthetic_block_state BEFORE UPDATE ON training_program_state "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save(TODAY + timedelta(days=7), request_key="failed-request")
        self.assertEqual(self.event_count(), 1)
        self.assertIsNone(self.store.review_for_request("failed-request"))
        self.assertEqual(self.store.context(TODAY)["active"]["revision"], 1)

    def test_configurable_cadence_backdate_rejection_and_namespaced_tables(self):
        store = ProgramStore(self.path, review_days=3, block_days=10)
        record = store.save_review(programme(), self.sources, "feedback-a", TODAY)
        self.assertEqual(record["next_review_date"], (TODAY + timedelta(days=3)).isoformat())
        self.assertEqual(record["block_end"], (TODAY + timedelta(days=10)).isoformat())
        with self.assertRaisesRegex(ValueError, "backdate"):
            store.save_review(programme(), self.sources, "feedback-a", TODAY - timedelta(days=1))
        with closing(sqlite3.connect(self.path)) as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertTrue(all(name.startswith("training_program_") for name in tables))
        for kwargs in ({"review_days": 0}, {"block_days": True}, {"review_days": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ProgramStore(self.path, **kwargs)

    def test_render_visible_shape_rationale_progress_conditions_and_exact_review_date(self):
        review = programme()
        review["decisions"][0].update(
            action="progress", evidence_refs=["activity:one", "activity:two", "memory:1"])
        record = self.save(review=review)
        text = render_program(record)
        for expected in ("**Goal:**", "**Block:**", "up to 4 days/week", "Strength A",
                         "not a promise", "Synthetic press", "Progress", "Retain the observed",
                         "Progression condition:", "**Recovery:**", "**Success signals:**",
                         "**Coverage / alternatives:**", "**Questions:**", record["next_review_date"]):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        self.assertNotIn("|", text)
        self.assertNotIn("saved", text.lower())
        self.assertNotIn("[[TRAINING_PROGRAM", text)
        self.assertNotIn("Set 1", text)
        self.assertLess(len(text), 3500)

    def test_render_unknown_availability_replacement_and_empty_context(self):
        self.assertIn("No training programme", render_program(None))
        review = programme(weekly_training_days=None, questions=[])
        review["session_templates"][0]["exercises"][0]["name"] = "Synthetic row"
        review["decisions"][0].update(action="replace", replacement="Synthetic row")
        text = render_program(self.save(review=review))
        self.assertIn("unknown", text)
        self.assertIn("Synthetic press → Synthetic row", text)
        self.assertNotIn("**Questions:**", text)


if __name__ == "__main__":
    unittest.main()
