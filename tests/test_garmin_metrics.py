"""Offline synthetic tests. No Garmin login, production cache writes or third-party runner."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if importlib.util.find_spec("garminconnect") is None:
    import types
    stub = types.ModuleType("garminconnect")
    stub.Garmin = Mock
    sys.modules["garminconnect"] = stub

import garmin_coach as coach
import garmin_metrics as metrics


TODAY = date(2026, 9, 7)


def activity(day, kind="running", aid=1, **kwargs):
    return {"activityId": aid, "activityType": {"typeKey": kind},
            "startTimeLocal": day + " 12:00:00", **kwargs}


def raw_set(name, start, unit=None, weight=10000, set_type="ACTIVE"):
    return {"setType": set_type, "startTime": start, "duration": 30,
            "exercises": [{"name": name, "category": "SQUAT"}],
            "repetitionCount": 8, "weight": weight, "weightUnit": unit}


class SetTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"AGBOT_STRENGTH_WEIGHT_UNIT": "",
                                                   "AGBOT_STRENGTH_WEIGHT_UNIT_PROVENANCE": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_superset_sequence_is_not_grouped_order_and_keeps_rest(self):
        payload = {"exerciseSets": [
            raw_set("FRONT_SQUAT", "2026-09-01T12:00:00", "g"),
            raw_set("BENCH_PRESS", "2026-09-01T12:01:00", "g"),
            raw_set("", "2026-09-01T12:02:00", set_type="REST", weight=-1),
            raw_set("FRONT_SQUAT", "2026-09-01T12:03:00", "g"),
        ]}
        rows = coach._parse_exercise_sets(payload)
        self.assertEqual([r["exercise"] for r in rows], ["Front Squat", "Bench Press"])
        self.assertEqual(rows[0]["sets"], 2)
        self.assertEqual(rows[0]["set_indices"], [0, 3])
        self.assertEqual([r["set_type"] for r in rows[0]["set_sequence"]], ["ACTIVE", "ACTIVE", "REST", "ACTIVE"])
        self.assertEqual(rows[0]["set_sequence"][3]["start_time"], "2026-09-01T12:03:00")
        self.assertEqual(rows[0]["top_weight_kg"], 10)

    def test_unknown_unit_missing_sentinel_and_zero_are_distinct(self):
        for weight, status in ((None, "missing_or_sentinel"), (-1, "missing_or_sentinel"),
                               (0, "available"), (12000, "available")):
            rows = coach._parse_exercise_sets({"exerciseSets": [raw_set("SQUAT", None, weight=weight)]})
            point = rows[0]["set_sequence"][0]
            self.assertEqual(point["weight_status"], status)
            self.assertEqual(point["weight_raw"], weight)
            self.assertIsNone(point["weight_unit"])
            self.assertIsNone(rows[0]["top_weight_kg"])
        rows = coach._parse_exercise_sets({"exerciseSets": [raw_set("SQUAT", None, "lb", 10)]})
        self.assertAlmostEqual(rows[0]["top_weight_kg"], 4.536)

    def test_rest_only_sequence_and_empty(self):
        self.assertIsNone(coach._parse_exercise_sets({"exerciseSets": []}))
        rows = coach._parse_exercise_sets({"exerciseSets": [raw_set("", None, set_type="REST")]})
        self.assertEqual(rows[0]["sets"], 0)
        self.assertEqual(rows[0]["set_sequence"][0]["set_type"], "REST")

    def test_unsorted_sets_sort_by_timestamp_keep_source_indices(self):
        rows = coach._parse_exercise_sets({"exerciseSets": [
            raw_set("SQUAT", "2026-09-01T12:02:00"),
            raw_set("BENCH", "2026-09-01T12:00:00")]})
        self.assertEqual(rows[0]["sequence_order"], "chronological_start_time")
        self.assertEqual([s["source_index"] for s in rows[0]["set_sequence"]], [1, 0])
        self.assertEqual(rows[0]["exercise"], "Bench")

    def test_cache_upgrade_refetches_old_summary(self):
        g = Mock()
        g.get_activity_exercise_sets.return_value = {"exerciseSets": [raw_set("FRONT_SQUAT", None)]}
        cache = {"1": [{"exercise": "Squat", "sets": 1}]}
        out = coach._sets_for(g, 1, cache)
        g.get_activity_exercise_sets.assert_called_once_with(1)
        self.assertIn("set_sequence", out[0])
        self.assertEqual(cache["1"]["schema_version"], metrics.SCHEMA_VERSION)

    def test_empty_or_failed_refresh_returns_last_good_with_freshness(self):
        good = [{"exercise": "Squat", "sets": 1}]
        for response in ({}, {"exerciseSets": []}, {"__error__": "server failure"}):
            cache = {"1": good}
            g = Mock()
            g.get_activity_exercise_sets.return_value = response
            metadata = {}
            result = coach._sets_for(g, 1, cache, refresh=True, metadata=metadata)
            self.assertEqual(result[0]["exercise"], "Squat")
            self.assertEqual(metadata["status"], "stale")
            self.assertFalse(metadata["sequence_complete"])
            self.assertIs(cache["1"], good)
            self.assertNotIn("data_freshness", good[0])

    def test_budget_defers_not_silently_omits(self):
        meta = {}
        g = Mock()
        result = coach._sets_for(g, 1, {}, allow_fetch=False, metadata=meta)
        self.assertIsNone(result)
        self.assertEqual(meta["status"], "deferred")
        g.get_activity_exercise_sets.assert_not_called()

    def test_recent_good_cache_has_short_ttl(self):
        entry = {"schema_version": metrics.SCHEMA_VERSION,
                 "fetched_at": datetime.now(timezone.utc).isoformat(),
                 "weight_unit_policy": coach._strength_weight_policy(),
                 "summaries": [{"exercise": "Squat", "set_sequence": []}]}
        g = Mock()
        coach._sets_for(g, 1, {"1": entry}, refresh=True)
        g.get_activity_exercise_sets.assert_not_called()

    def test_explicit_private_unit_policy_restores_kg_with_provenance(self):
        with patch.dict(os.environ, {"AGBOT_STRENGTH_WEIGHT_UNIT": "g",
                                     "AGBOT_STRENGTH_WEIGHT_UNIT_PROVENANCE": "Synthetic matched-source evidence"}):
            rows = coach._parse_exercise_sets({"exerciseSets": [raw_set("SQUAT", None, weight=8000)]})
        detail = rows[0]["set_sequence"][0]
        self.assertIsNone(detail["weight_unit"])
        self.assertEqual(detail["effective_weight_unit"], "g")
        self.assertEqual(detail["weight_unit_status"], "configured")
        self.assertEqual(detail["weight_unit_source"], "explicit_private_configuration")
        self.assertEqual(detail["weight_unit_provenance"], "Synthetic matched-source evidence")
        self.assertEqual(rows[0]["top_weight_kg"], 8)

    def test_private_unit_policy_never_overrides_supplied_or_unrecognized_units(self):
        with patch.dict(os.environ, {"AGBOT_STRENGTH_WEIGHT_UNIT": "g"}):
            for unit, expected in (("kg", 8), ("unrecognized-unit", None)):
                rows = coach._parse_exercise_sets({"exerciseSets": [raw_set("SQUAT", None, unit, 8)]})
                self.assertEqual(rows[0]["top_weight_kg"], expected)
                self.assertNotEqual(rows[0]["set_sequence"][0]["weight_unit_source"], "explicit_private_configuration")

    def test_invalid_private_weight_unit_does_not_infer_kg(self):
        with patch.dict(os.environ, {"AGBOT_STRENGTH_WEIGHT_UNIT": "guess"}):
            rows = coach._parse_exercise_sets({"exerciseSets": [raw_set("SQUAT", None, weight=8000)]})
        self.assertIsNone(rows[0]["top_weight_kg"])
        self.assertEqual(rows[0]["set_sequence"][0]["weight_unit_status"], "unknown")

    def test_changed_private_weight_policy_invalidates_existing_normalized_cache(self):
        raw = {"exerciseSets": [raw_set("SQUAT", None, weight=8000)]}
        g = Mock()
        g.get_activity_exercise_sets.return_value = raw
        cache = {}
        first = coach._sets_for(g, 1, cache)
        self.assertIsNone(first[0]["top_weight_kg"])
        with patch.dict(os.environ, {"AGBOT_STRENGTH_WEIGHT_UNIT": "g"}):
            updated = coach._sets_for(g, 1, cache)
        self.assertEqual(updated[0]["top_weight_kg"], 8)
        self.assertEqual(g.get_activity_exercise_sets.call_count, 2)
        self.assertEqual(cache["1"]["weight_unit_policy"]["unit"], "g")

    def test_failed_policy_refresh_discloses_cached_and_requested_units(self):
        g = Mock()
        g.get_activity_exercise_sets.return_value = {"exerciseSets": [raw_set("SQUAT", None, weight=8000)]}
        cache = {}
        with patch.dict(os.environ, {"AGBOT_STRENGTH_WEIGHT_UNIT": "g"}):
            coach._sets_for(g, 1, cache)
        g.get_activity_exercise_sets.return_value = {}
        with patch.dict(os.environ, {"AGBOT_STRENGTH_WEIGHT_UNIT": "kg"}):
            result = coach._sets_for(g, 1, cache)
        meta = result[0]["data_freshness"]
        self.assertEqual(result[0]["top_weight_kg"], 8)
        self.assertEqual(meta["status"], "stale")
        self.assertFalse(meta["weight_unit_policy_current"])
        self.assertEqual(meta["weight_unit_policy"]["unit"], "g")
        self.assertEqual(meta["requested_weight_unit_policy"]["unit"], "kg")


class MetricTests(unittest.TestCase):
    def test_fitness_schema_and_bridge_activity_contract(self):
        class FakeGarmin:
            def __getattr__(self, _name):
                return lambda *_args, **_kwargs: {}

        with patch.object(coach, "_load_metric_cache", return_value={}), \
             patch.object(coach, "_save_metric_cache"):
            profile = coach.fitness_profile(TODAY, g=FakeGarmin())
        self.assertEqual(profile["schema_version"], 2)
        trimmed = coach._trim_activity(activity("2026-09-06", "strength_training", aid=7, duration=100))
        self.assertEqual({k: trimmed[k] for k in ("activity_id", "start", "type", "duration_s")},
                         {"activity_id": 7, "start": "2026-09-06 12:00:00",
                          "type": "strength_training", "duration_s": 100})

    def test_hill_unsorted_range_uses_latest_date_and_trend(self):
        raw = {"hillScoreDTOList": [
            {"calendarDate": "2026-09-06", "overallScore": 30, "strengthScore": 40},
            {"calendarDate": "2026-09-03", "overallScore": 20}]}
        out = metrics.score_metric(raw, "hill", TODAY)
        self.assertEqual(out["score"], 30)
        self.assertEqual(out["age_days"], 1)
        self.assertEqual(out["change_over_observed_period"], 10)
        self.assertEqual(out["strength"], 40)

    def test_endurance_daily_and_weekly_are_not_confused(self):
        daily = {"calendarDate": "2026-09-07", "overallScore": 5000,
                 "classificationLowerLimitTrained": 4000}
        history = {"enduranceScoreDTO": {"calendarDate": "2026-09-05", "overallScore": 4900},
                   "groupMap": {"2026-09-01": {"groupAverage": 4850, "groupMax": 5000}}}
        out = metrics.score_metric(daily, "endurance", TODAY, history)
        self.assertEqual(out["score"], 5000)
        self.assertEqual(out["level"], "Trained")
        self.assertEqual(out["weekly_aggregate_series"][0]["average"], 4850)

    def test_lactate_actual_wrapper_preserves_units_and_separate_dates(self):
        payload = {"speed_and_heart_rate": {"speed": .25, "heartRate": 170,
                                          "calendarDate": "2026-09-01"},
                   "power": {"functionalThresholdPower": 275, "powerToWeight": 3.4,
                             "calendarDate": "2026-09-06", "isStale": True}}
        out = metrics.lactate_metric(payload, TODAY, {"power": [
            {"from": "2026-09-01", "until": "2026-09-02", "updatedDate": "2026-09-01", "value": 260}]})
        self.assertNotIn("pace_s_per_km", out)
        self.assertIsNone(out["measurements"]["speed"]["unit"])
        self.assertEqual(out["measurements"]["heart_rate_bpm"]["age_days"], 6)
        self.assertEqual(out["measurements"]["power_w"]["age_days"], 1)
        self.assertTrue(out["measurements"]["power_w"]["device_marked_stale"])
        self.assertEqual(out["trends"]["power"][0]["value"], 260)
        payload["speed_and_heart_rate"].update(speed=4, speedUnit="m/s")
        self.assertEqual(metrics.lactate_metric(payload, TODAY)["pace_s_per_km"], 250)

    def test_empty_unsupported_and_error_endpoints(self):
        for payload, status in ((None, "unavailable"), ({}, "unavailable"),
                                ({"__error__": "404 Not Found"}, "not_supported"),
                                ({"__error__": "TimeoutError"}, "error")):
            self.assertEqual(metrics.score_metric(payload, "hill", TODAY)["status"], status)
            self.assertEqual(metrics.lactate_metric(payload, TODAY)["status"], status)
            self.assertEqual(metrics.activity_metrics(payload)["status"], status)

    def test_ciq_fuel_uses_verified_session_definition_not_chart_suffix(self):
        raw = {"summaryDTO": {"averagePower": 200, "groundContactTime": 250,
                              "beginPotentialStamina": 90, "directWorkoutRpe": 5},
               "connectIQMeasurements": [{"appID": metrics.FAT_BURNER_APP_ID,
                                         "developerFieldNumber": 2, "value": "12.5"}]}
        details = {"metricDescriptors": [{"key": "connectIQDeveloperField-02",
                                         "developerFieldNumber": 0, "unit": None}]}
        out = metrics.activity_metrics(raw, details)
        fuel = out["fuel_estimate"]
        self.assertEqual(fuel["source"], "Connect IQ")
        self.assertEqual(fuel["field_mapping_status"], "verified_fit_session_definition")
        self.assertEqual(fuel["fields"][0]["unit"], "g")
        self.assertEqual(fuel["fields"][0]["label"], "Total Fat")
        self.assertEqual(fuel["fat_g"], 12.5)
        self.assertIsNone(fuel["carbohydrate_g"])
        self.assertEqual(fuel["fields"][0]["developer_field_number"], 2)
        self.assertEqual(out["detail_descriptors"][0]["developerFieldNumber"], 0)
        self.assertIsNone(out["detail_descriptors"][0]["unit"])
        self.assertIn("Not measured body-fat loss", fuel["safeguard"])
        self.assertEqual(out["groups"]["self_evaluation"]["status"], "available")
        self.assertNotIn("activityDetailMetrics", out)

    def test_ciq_session_totals_preserve_raw_and_named_estimates(self):
        out = metrics.activity_metrics({"connectIQMeasurements": [
            {"appID": metrics.FAT_BURNER_APP_ID, "developerFieldNumber": 3, "value": "24.25"},
            {"appID": metrics.FAT_BURNER_APP_ID, "developerFieldNumber": 2, "value": "7.5"}]})
        self.assertEqual(out["fuel_estimate"]["fat_g"], 7.5)
        self.assertEqual(out["fuel_estimate"]["carbohydrate_g"], 24.25)
        self.assertEqual(out["custom_fields"][0]["value"], "24.25")
        self.assertEqual(out["custom_fields"][0]["field_namespace"], "session")
        self.assertEqual(out["schema_version"], metrics.ACTIVITY_METRICS_VERSION)

    def test_unknown_app_or_record_namespace_is_not_mapped_as_session_total(self):
        out = metrics.activity_metrics({"connectIQMeasurements": [
            {"appID": "synthetic-other-app", "developerFieldNumber": 2, "value": "8"},
            {"appID": metrics.FAT_BURNER_APP_ID, "developerFieldNumber": 0, "value": "8"}]})
        self.assertIsNone(out["fuel_estimate"]["fat_g"])
        self.assertEqual(out["fuel_estimate"]["field_mapping_status"], "unverified")
        self.assertTrue(all(f["unit"] is None for f in out["custom_fields"]))

    def test_conflicting_metadata_keeps_raw_unit_without_verified_estimate(self):
        for conflict in ({"unit": "kcal"}, {"label": "Different Quantity"}):
            out = metrics.activity_metrics({"connectIQMeasurements": [
                {"appID": metrics.FAT_BURNER_APP_ID, "developerFieldNumber": 2,
                 "value": "10", **conflict}]})
            self.assertIsNone(out["fuel_estimate"]["fat_g"])
            self.assertEqual(out["custom_fields"][0]["mapping_status"], "conflicts_with_verified_definition")

    def test_nonfinite_negative_or_duplicate_fuel_values_not_reported_as_total(self):
        for raw in ("nan", "inf", "-2", None, True):
            out = metrics.activity_metrics({"connectIQMeasurements": [
                {"appID": metrics.FAT_BURNER_APP_ID, "developerFieldNumber": 2, "value": raw}]})
            self.assertIsNone(out["fuel_estimate"]["fat_g"])
        field = {"appID": metrics.FAT_BURNER_APP_ID, "developerFieldNumber": 2, "value": "9"}
        out = metrics.activity_metrics({"connectIQMeasurements": [field, field]})
        self.assertIsNone(out["fuel_estimate"]["fat_g"])

    def test_metric_cache_reuses_and_falls_back_without_disk(self):
        stamp = datetime.now(timezone.utc).isoformat()
        entry = {"schema_version": metrics.SCHEMA_VERSION, "fetched_at": stamp,
                 "data": {"status": "available", "score": 20}}
        with patch.object(coach, "_load_metric_cache", return_value={"hill": entry}), \
             patch.object(coach, "_save_metric_cache") as save:
            loader = Mock(return_value={"status": "error"})
            self.assertEqual(coach._cached_metric("hill", loader)["score"], 20)
            loader.assert_not_called()
            out = coach._cached_metric("hill", loader, refresh=True)
            self.assertEqual(out["status"], "stale")
            self.assertEqual(out["score"], 20)
            self.assertEqual(out["cache"]["refresh_status"], "error")
            save.assert_not_called()

    def test_metric_cache_schema_upgrade(self):
        old = {"schema_version": 1, "data": {"status": "available", "score": 10}}
        with patch.object(coach, "_load_metric_cache", return_value={"hill": old}), \
             patch.object(coach, "_save_metric_cache") as save:
            loader = Mock(return_value={"status": "available", "score": 30})
            self.assertEqual(coach._cached_metric("hill", loader)["score"], 30)
            loader.assert_called_once()
            self.assertEqual(save.call_args.args[0]["hill"]["schema_version"], metrics.SCHEMA_VERSION)

    def test_optional_metric_cache_does_not_write_without_data_root(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(coach, "_atomic_write") as write:
            coach._save_metric_cache({"a": 1})
            write.assert_not_called()

    def test_cached_measurement_age_recomputed_not_mutating_cache(self):
        day = date.today() - timedelta(days=3)
        entry = {"schema_version": metrics.SCHEMA_VERSION,
                 "fetched_at": datetime.now(timezone.utc).isoformat(),
                 "data": {"status": "available", "as_of": day.isoformat(), "age_days": 0}}
        with patch.object(coach, "_load_metric_cache", return_value={"hill": entry}):
            out = coach._cached_metric("hill", Mock())
        self.assertEqual(out["age_days"], 3)
        self.assertEqual(entry["data"]["age_days"], 0)

    def test_empty_summary_and_error_details_preserve_status(self):
        self.assertEqual(metrics.activity_metrics({"summaryDTO": {}})["status"], "unavailable")
        g = Mock()
        g.get_activity.return_value = {"summaryDTO": {"averagePower": 100}}
        g.get_activity_details.side_effect = RuntimeError("unavailable")
        with patch.object(coach, "_load_metric_cache", return_value={}), \
             patch.object(coach, "_save_metric_cache") as save:
            out = coach.activity_detail_metrics(1, g=g)
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["details_coverage"]["status"], "error")
        save.assert_not_called()


class CoverageTests(unittest.TestCase):
    def test_programme_history_fetches_older_sessions_with_a_bounded_set_budget(self):
        g = Mock()
        g.get_activities.return_value = [
            activity("2026-09-06", "strength_training", aid=1),
            activity("2026-08-25", "strength_training", aid=2),
            activity("2026-08-20", "strength_training", aid=3),
        ]
        g.get_activity_exercise_sets.return_value = {"exerciseSets": [
            raw_set("TEST_PRESS", "2026-08-25T12:00:00", "kg", weight=4)]}
        with patch.object(coach, "_load_sets_cache", return_value={}), \
                patch.object(coach, "_save_sets_cache"):
            result = coach.program_history(TODAY, max_set_fetches=2, g=g)
        self.assertEqual(len(result["activities"]), 3)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(g.get_activity_exercise_sets.call_count, 2)
        self.assertEqual(result["coverage"]["strength_sets"]["without_detail"], 1)
        self.assertEqual(result["activities"][-1]["logged_sets_coverage"]["status"], "deferred")
        self.assertEqual(result["activities"][1]["start"][:10], "2026-08-25")

    def test_programme_history_failure_is_unknown_not_zero_training(self):
        g = Mock()
        g.get_activities.side_effect = RuntimeError("offline")
        with patch.object(coach, "_load_sets_cache", return_value={}):
            result = coach.program_history(TODAY, g=g)
        self.assertTrue(result["coverage"]["unknown"])
        self.assertIsNone(result["coverage"]["total"])

    def test_programme_history_rejects_unbounded_requests(self):
        with self.assertRaises(ValueError):
            coach.program_history(TODAY, max_set_fetches=100, g=Mock())

    def test_classifier_commute_strength_unknown_and_overrides(self):
        fixtures = [
            (activity("2026-09-07", "e_bike_fitness", activityTrainingLoad=100), "transport"),
            (activity("2026-09-07", "cycling"), "intentional_training"),
            (activity("2026-09-07", "cycling", activityName="To office"), "transport"),
            (activity("2026-09-07", "strength_training", duration=100, activityTrainingLoad=0), "intentional_training"),
            (activity("2026-09-07", "other", duration=1), "unknown"),
            (activity("2026-09-07", "walking", activityTrainingLoad=100), "active_recovery"),
            (activity("2026-09-07", "e_bike_fitness", activityName="[training] Hills"), "intentional_training"),
        ]
        for raw, expected in fixtures:
            self.assertEqual(coach.classify_activity(raw)["classification"], expected)
            self.assertEqual(coach._is_training_activity(raw), expected == "intentional_training")
        self.assertEqual(coach.classify_activity(fixtures[1][0], {"name_labels": {}, "type_labels": {
            "cycling": "transport"}})["classification"], "transport")

    def test_pagination_no_fifteen_item_truncation(self):
        rows = [activity("2026-09-07", aid=n) for n in range(80)]
        g = Mock()
        g.get_activities.side_effect = lambda start, limit: rows[start:start+limit]
        acts, coverage = coach._activity_history(g, TODAY - timedelta(days=7), TODAY)
        self.assertEqual(len(acts), 80)
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["total"], 80)
        self.assertEqual(coverage["omitted"], 0)

    def test_page_cap_does_not_invent_rest(self):
        g = Mock()
        g.get_activities.return_value = [activity("2026-09-07", aid=n) for n in range(50)]
        acts, coverage = coach._activity_history(g, TODAY - timedelta(days=7), TODAY, max_pages=1)
        self.assertTrue(coverage["unknown"])
        self.assertIsNone(coverage["total"])
        rhythm = coach._training_rhythm(g, TODAY, acts, coverage)
        self.assertIsNone(rhythm["rest_days_last_7"])
        self.assertIsNone(rhythm["last_rest_day"])
        self.assertEqual(rhythm["unknown_days_last_7"], 7)
        self.assertTrue(rhythm["trained_today"])

    def test_completed_days_streak_excludes_unfinished_today(self):
        acts = [activity("2026-09-06"), activity("2026-09-05", aid=2),
                activity("2026-09-07", "e_bike_fitness", aid=3)]
        coverage = {"complete_from": "2026-08-01", "complete_through": "2026-09-07"}
        result = metrics.training_rhythm(acts, TODAY, coverage)
        self.assertEqual(result["completed_days_streak"], 2)
        self.assertFalse(result["trained_today"])
        self.assertEqual(result["projected_streak_if_training"], 3)
        self.assertEqual(result["last_rest_day"], "2026-09-04")
        self.assertEqual(result["rest_days_last_7"], 5)

    def test_unknown_activity_prevents_rest_inference(self):
        coverage = {"complete_from": "2026-08-01", "complete_through": "2026-09-07"}
        result = metrics.training_rhythm([activity("2026-09-06", "other")], TODAY, coverage)
        self.assertEqual(result["unknown_days_last_7"], 1)
        self.assertIsNone(result["rest_days_last_7"])

    def test_empty_exhausted_history_confirms_days_no_api_error_does(self):
        g = Mock()
        g.get_activities.return_value = []
        acts, coverage = coach._activity_history(g, TODAY - timedelta(days=7), TODAY)
        self.assertEqual(metrics.training_rhythm(acts, TODAY, coverage)["rest_days_last_7"], 7)
        g.get_activities.side_effect = RuntimeError("offline")
        acts, coverage = coach._activity_history(g, TODAY - timedelta(days=7), TODAY)
        self.assertIsNone(metrics.training_rhythm(acts, TODAY, coverage)["rest_days_last_7"])

    def test_unordered_or_malformed_rows_do_not_prove_empty_days(self):
        g = Mock()
        g.get_activities.return_value = [activity("2026-09-01"), activity("2026-09-07", aid=2), {}]
        _acts, coverage = coach._activity_history(g, TODAY - timedelta(days=7), TODAY)
        self.assertTrue(coverage["unknown"])
        self.assertIsNone(coverage["complete_from"])

    def test_training_boolean_uses_shared_classifier(self):
        g = Mock()
        with patch.object(coach, "client", return_value=g):
            day = datetime.now(coach._local_zone()).date().isoformat()
            g.get_activities.return_value = [activity(day, "e_bike_fitness")]
            self.assertFalse(coach.trained_today())
            g.get_activities.return_value = [activity(day, "strength_training", duration=100)]
            self.assertTrue(coach.trained_today(min_duration_s=600))

    def test_snapshot_returns_all_activities_and_strength_coverage(self):
        class FakeGarmin:
            def __getattr__(self, _name):
                return lambda *_args, **_kwargs: {}

            def get_activities(self, start, limit):
                return [activity("2026-09-06", "strength_training", aid=n) for n in range(20)][start:start+limit]

            def get_activity_exercise_sets(self, _aid):
                return {"exerciseSets": [raw_set("SQUAT", None, "kg", 20)]}

        with patch.object(coach, "client", return_value=FakeGarmin()), \
             patch.object(coach, "_load_sets_cache", return_value={}), \
             patch.object(coach, "_save_sets_cache") as save:
            snapshot = coach.build_snapshot(TODAY)
        rows = snapshot["recent_activities_7d"]
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum("logged_sets" in a for a in rows), 8)
        self.assertEqual(sum(a["logged_sets_coverage"]["status"] == "deferred" for a in rows), 12)
        self.assertEqual(snapshot["recent_activities_7d_coverage"]["total"], 20)
        self.assertEqual(snapshot["recent_activities_7d_coverage"]["strength_sets"]["total"], 20)
        self.assertEqual(snapshot["training_rhythm"]["completed_days_streak"], 1)
        save.assert_called_once()


class NutritionGoalTests(unittest.TestCase):
    def budget(self, goal=None, weight_g=70000, birth_date="1990-01-01"):
        class FakeGarmin:
            def __getattr__(self, _name):
                return lambda *_args, **_kwargs: {}

            def get_activities(self, _start, _limit):
                return []

            def get_user_profile(self):
                return {"userData": {"gender": "FEMALE", "height": 175,
                                     "weight": weight_g, "birthDate": birth_date}}

        env = {} if goal is None else {"AGBOT_NUTRITION_GOAL": goal}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(coach, "client", return_value=FakeGarmin()), \
             patch.object(coach, "_load_sets_cache", return_value={}), \
             patch.object(coach, "_save_sets_cache"):
            return coach.build_snapshot(TODAY)["calorie_budget"]

    def test_default_estimates_maintenance_without_prescribing_diet(self):
        budget = self.budget()
        self.assertEqual(budget["nutrition_goal"], "unspecified")
        self.assertEqual(budget["target_status"], "not_configured")
        self.assertFalse(budget["goal_explicit"])
        self.assertGreater(budget["maintenance_kcal"], 0)
        self.assertTrue(budget["maintenance_is_estimate"])
        self.assertEqual(budget["activity_factor_source"], "provisional_default_not_observed")
        for key in ("target_kcal", "deficit_kcal", "deficit_cap_kcal",
                    "protein_target_g", "protein_floor_g", "protein_kcal"):
            self.assertIsNone(budget[key])

    def test_maintain_has_no_deficit_or_automatic_protein_prescription(self):
        budget = self.budget("maintain")
        self.assertEqual(budget["target_kcal"], budget["maintenance_kcal"])
        self.assertEqual(budget["deficit_kcal"], 0)
        self.assertIsNone(budget["protein_target_g"])
        self.assertTrue(budget["goal_explicit"])

    def test_explicit_fat_loss_preserves_existing_adult_recomposition_calculation(self):
        budget = self.budget("fat_loss")
        self.assertEqual(budget["target_status"], "configured_estimate")
        self.assertLess(budget["target_kcal"], budget["maintenance_kcal"])
        self.assertLessEqual(budget["deficit_kcal"], round(0.005 * 70 * 7700 / 7))
        self.assertEqual(budget["protein_target_g"], round(2.2 * 70))
        self.assertGreaterEqual(budget["target_kcal"], budget["bmr_kcal"])

    def test_unsupported_goal_does_not_silently_become_fat_loss(self):
        budget = self.budget("gain")
        self.assertEqual(budget["target_status"], "unsupported_goal")
        self.assertIsNone(budget["target_kcal"])
        self.assertIsNone(budget["protein_target_g"])

    def test_fat_loss_not_automatically_prescribed_for_minors_or_underweight(self):
        for kwargs in ({"birth_date": "2015-01-01"}, {"weight_g": 45000}):
            budget = self.budget("fat_loss", **kwargs)
            self.assertEqual(budget["target_status"], "not_appropriate_for_automatic_fat_loss_target")
            self.assertIsNone(budget["target_kcal"])


if __name__ == "__main__":
    unittest.main()
