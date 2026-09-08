"""Garmin Coach data layer.

Reads OAuth tokens from ~/.garminconnect (created by garmin-mcp-auth) and provides:
  * should-brief      -> decide whether this morning's brief should be sent yet
  * mark-brief-sent   -> record that today's brief was delivered
  * dump [date]       -> print a rich consolidated JSON snapshot for the LLM

Runs in an ephemeral `uv run --with garminconnect` env, so nothing pollutes the
base conda install. All network calls are defensive: one failing endpoint never
breaks the whole snapshot.

Exit codes for `should-brief`:
  0 = SEND        (sleep for today is logged and not yet sent)
  2 = ALREADY_SENT
  3 = WAITING     (no finalized sleep record yet)
  4 = ERROR
"""
import os
import sys
import json
import copy
import tempfile
import argparse
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo

from garminconnect import Garmin
import garmin_metrics as metrics

STATE_DIR = os.path.join(os.environ.get("AGBOT_DATA_DIR", os.path.dirname(os.path.abspath(__file__))), "state")
BRIEF_STATE = os.path.join(STATE_DIR, "last_brief_date.txt")
SETS_CACHE = os.path.join(STATE_DIR, "exercise_sets_cache.json")
METRICS_CACHE = os.path.join(STATE_DIR, "metrics_cache.json")
# A recently-finished session can still be EDITED in Garmin Connect (fixing a
# mis-detected exercise type, reps, etc). Within this many days we refresh its
# logged sets on a bounded TTL; explicit correction checks bypass that TTL.
# Older sessions are treated as immutable
# and served from the by-id cache.
EDITABLE_DAYS = 3
# A night is considered "logged" once at least this much sleep is recorded.
MIN_SLEEP_SECONDS = 90 * 60

# The user's real local timezone (IANA name from their Garmin profile), cached at module
# level during build_snapshot so date resolution + local wall-time parsing stay correct even
# when the server clock runs in a different timezone than the user. None -> fall back to the
# host's local timezone (a no-op when host tz == user tz, e.g. running on the user's own box).
_LOCAL_TZ_NAME = None


def _set_local_tz(name):
    global _LOCAL_TZ_NAME
    if name:
        _LOCAL_TZ_NAME = name


def _local_zone():
    """The user's local timezone (Garmin profile) when known and loadable, else host local."""
    if _LOCAL_TZ_NAME:
        try:
            return ZoneInfo(_LOCAL_TZ_NAME)
        except Exception:  # noqa: BLE001 - missing tzdata / bad name -> host local
            pass
    return datetime.now().astimezone().tzinfo


def _atomic_write(path, text):
    """Write text to path atomically (temp file in the same dir + os.replace) so a crash or
    concurrent read can never see a half-written / corrupt file."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".swp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def token_dir():
    return os.path.expanduser(os.environ.get("GARMINTOKENS", "~/.garminconnect"))


def client():
    g = Garmin()
    g.login(token_dir())
    return g


def iso(d):
    return d.isoformat()


def safe(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def sleep_dto(g, d):
    data = safe(lambda: g.get_sleep_data(iso(d)))
    if not isinstance(data, dict) or "__error__" in data:
        return None, data
    return (data.get("dailySleepDTO") or {}), data


def is_sleep_logged(g, d):
    dto, _ = sleep_dto(g, d)
    if not dto:
        return False
    secs = dto.get("sleepTimeSeconds")
    return bool(secs) and secs >= MIN_SLEEP_SECONDS


# ---------------------------------------------------------------- should-brief
def cmd_should_brief(args):
    today = date.today()
    already = ""
    if os.path.exists(BRIEF_STATE):
        with open(BRIEF_STATE, "r", encoding="utf-8") as fh:
            already = fh.read().strip()
    if already == iso(today):
        print("ALREADY_SENT")
        return 2
    try:
        g = client()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: login failed: {exc}")
        return 4
    if is_sleep_logged(g, today):
        print("SEND")
        return 0
    print("WAITING")
    return 3


def cmd_mark_sent(args):
    _atomic_write(BRIEF_STATE, iso(date.today()))
    print(f"marked {iso(date.today())}")
    return 0


# ------------------------------------------------------------------------ dump
def _trim_activity(a):
    out = {
        "activity_id": a.get("activityId"),
        "name": a.get("activityName"),
        "type": (a.get("activityType") or {}).get("typeKey"),
        "start": a.get("startTimeLocal"),
        "duration_s": a.get("duration"),
        "distance_m": a.get("distance"),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "calories": a.get("calories"),
        "training_load": a.get("activityTrainingLoad"),
        "aerobic_te": a.get("aerobicTrainingEffect"),
        "anaerobic_te": a.get("anaerobicTrainingEffect"),
        "avg_power": a.get("avgPower"),
    }
    # Extra per-activity signals; dropped when absent so strength/cardio rows stay lean.
    extras = {
        "training_effect_label": a.get("trainingEffectLabel"),
        "bb_cost": a.get("differenceBodyBattery"),
        "sweat_loss_ml": _round(a.get("waterEstimated")),
        "moderate_intensity_min": a.get("moderateIntensityMinutes"),
        "vigorous_intensity_min": a.get("vigorousIntensityMinutes"),
        "elevation_gain_m": _round(a.get("elevationGain"), 1),
        "steps": a.get("steps"),
        "avg_cadence_spm": _round(a.get("averageRunningCadenceInStepsPerMinute")),
        "avg_speed_mps": _round(a.get("averageSpeed"), 2),
    }
    out.update({k: v for k, v in extras.items() if v is not None})
    out["classification"] = classify_activity(a)
    return out


def _count_breathing_disruptions(sleep_raw):
    """Count real breathing-disruption events during sleep. Garmin uses 255 as a
    'no reading' sentinel (shows as '--' in Connect), so those are ignored."""
    data = (sleep_raw or {}).get("breathingDisruptionData")
    if not isinstance(data, list):
        return None
    return sum(1 for e in data
               if isinstance(e, dict)
               and isinstance(e.get("value"), (int, float))
               and 0 < e["value"] < 255)


def _nap_local(ts_gmt, offset_min):
    """Convert a Garmin GMT nap timestamp + minute offset to a local ISO 'HH:MM' string."""
    try:
        return (datetime.fromisoformat(ts_gmt)
               + timedelta(minutes=offset_min or 0)).isoformat(timespec="minutes")
    except Exception:  # noqa: BLE001
        return ts_gmt


def _round(v, n=0):
    """Round numbers for compact output; leave non-numbers (and bools) untouched."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return round(v) if n == 0 else round(v, n)


def _epoch_ms_local(ms):
    """Garmin *Local* epoch-millis already encode wall-clock time as if UTC -> 'YYYY-MM-DDTHH:MM'."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    try:
        return datetime.utcfromtimestamp(ms / 1000).isoformat(timespec="minutes")
    except Exception:  # noqa: BLE001
        return None


def _activity_labels():
    try:
        value = json.loads(os.environ.get("AGBOT_ACTIVITY_LABELS", "{}"))
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def classify_activity(a, labels=None):
    """Public {classification, reason} contract shared by snapshots and training checks."""
    return metrics.classify_activity(a, _activity_labels() if labels is None else labels)


def _is_training_activity(a):
    return classify_activity(a)["classification"] == "intentional_training"


def _activity_history(g, start, end, page_size=50, max_pages=6):
    """Bounded descending pagination. Missing days are only rest after proven coverage."""
    rows, seen, all_dates = [], set(), []
    complete, malformed, stop = False, False, "page_limit"
    scanned = 0
    for page in range(max_pages):
        batch = safe(lambda: g.get_activities(page * page_size, page_size))
        if not isinstance(batch, list):
            stop = metrics.payload_status(batch)
            break
        scanned += len(batch)
        dates, new = [], 0
        for a in batch:
            if not isinstance(a, dict):
                malformed = True
                continue
            day = str(a.get("startTimeLocal") or "")[:10]
            try:
                date.fromisoformat(day)
            except ValueError:
                malformed = True
                continue
            dates.append(day)
            key = str(a.get("activityId")) if a.get("activityId") is not None else json.dumps(a, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            new += 1
            if iso(start) <= day <= iso(end):
                rows.append(a)
        all_dates.extend(dates)
        ordered = all_dates == sorted(all_dates, reverse=True)
        if len(batch) < page_size:
            complete, stop = True, "exhausted"
            break
        if dates and min(dates) < iso(start) and ordered:
            complete, stop = True, "range_boundary"
            break
        if batch and not new:
            stop = "repeated_page"
            break
    ordered = all_dates == sorted(all_dates, reverse=True)
    complete = complete and not malformed and ordered
    complete_from = iso(start) if complete else (
        (date.fromisoformat(min(all_dates)) + timedelta(days=1)).isoformat()
        if all_dates and ordered and not malformed else None)
    if complete_from and complete_from > iso(end):
        complete_from = None
    coverage = {
        "requested_from": iso(start), "requested_through": iso(end),
        "returned": len(rows), "total": len(rows) if complete else None,
        "omitted": 0 if complete else None, "unknown": not complete,
        "complete": complete, "complete_from": max(iso(start), complete_from) if complete_from else None,
        "complete_through": iso(end) if complete_from else None,
        "stop_reason": stop, "pages_requested": page + 1, "rows_scanned": scanned,
        "malformed_rows": malformed, "observed_order_descending": ordered,
    }
    return rows, coverage


def _training_rhythm(g, d_today, activities=None, coverage=None):
    if activities is None:
        activities, coverage = _activity_history(g, d_today - timedelta(days=28), d_today)
    return metrics.training_rhythm(activities, d_today, coverage or {}, _activity_labels())


def program_history(d_today=None, days=28, max_set_fetches=8, g=None):
    """Bounded programme lookback, including workouts predating the local bot store."""
    d_today = d_today or date.today()
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise ValueError("Programme history days must be between 1 and 90.")
    if (isinstance(max_set_fetches, bool) or not isinstance(max_set_fetches, int)
            or not 0 <= max_set_fetches <= 20):
        raise ValueError("Programme set-fetch budget must be between 0 and 20.")
    g = safe(client) if g is None else g
    if isinstance(g, dict) and "__error__" in g:
        return {"activities": [], "coverage": {
            "complete": False, "unknown": True, "total": None,
            "stop_reason": "history_client_unavailable",
        }}
    raw, coverage = _activity_history(g, d_today - timedelta(days=days), d_today)
    activities = [_trim_activity(activity) for activity in raw]
    cache = _load_sets_cache()
    fetched = available = strength = 0
    for activity in activities:
        if "strength" not in str(activity.get("type") or "") or activity.get("activity_id") is None:
            continue
        strength += 1
        metadata = {}
        sets = _sets_for(g, activity["activity_id"], cache,
                        refresh=_within_days(activity.get("start"), EDITABLE_DAYS),
                        metadata=metadata, allow_fetch=fetched < max_set_fetches)
        fetched += int(metadata.get("fetched", False))
        activity["logged_sets_coverage"] = metadata
        if sets:
            activity["logged_sets"] = sets
            available += 1
    if fetched:
        _save_sets_cache(cache)
    coverage["strength_sets"] = {
        "activities": strength, "with_available_or_cached_detail": available,
        "without_detail": strength - available, "fetches": fetched,
        "max_fetches": max_set_fetches,
        "interpretation": "Per-activity freshness applies; missing or deferred detail is unknown.",
    }
    return {"activities": activities, "coverage": coverage}


def _load_metric_cache():
    # New optional caches are isolated only when an explicit data root is supplied.
    if not os.environ.get("AGBOT_DATA_DIR"):
        return {}
    try:
        with open(METRICS_CACHE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_metric_cache(cache):
    if os.environ.get("AGBOT_DATA_DIR"):
        try:
            _atomic_write(METRICS_CACHE, json.dumps(cache, ensure_ascii=False))
        except OSError:
            pass


def _cached_metric(key, loader, ttl_hours=24, refresh=False, context=None):
    """Cache normalized aggregates only; retain last good values after empty/error refresh."""
    cache = _load_metric_cache()
    entry = cache.get(key) or {}
    if not isinstance(entry, dict):
        entry = {}
    now = datetime.now(timezone.utc)
    try:
        age = (now - datetime.fromisoformat(entry["fetched_at"])).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        age = None
    current = (entry.get("schema_version") == metrics.SCHEMA_VERSION
               and isinstance(entry.get("data"), dict)
               and (context is None or entry.get("context") == context))
    if current and not refresh and age is not None and 0 <= age < ttl_hours:
        result = copy.deepcopy(entry["data"])
        _update_metric_ages(result, now.date())
        result["cache"] = {"status": "fresh", "fetched_at": entry["fetched_at"], "age_hours": round(age, 2)}
        return result
    result = loader()
    if result.get("status") == "available":
        cache[key] = {"schema_version": metrics.SCHEMA_VERSION, "fetched_at": now.isoformat(),
                      "context": context, "data": result}
        _save_metric_cache(cache)
        result["cache"] = {"status": "fresh", "fetched_at": now.isoformat(), "age_hours": 0}
    elif (isinstance(entry.get("data"), dict) and entry["data"]
          and (context is None or entry.get("context") is None or entry["context"] <= context)):
        status = result.get("status")
        result = copy.deepcopy(entry["data"])
        _update_metric_ages(result, now.date())
        result["status"] = "stale"
        result["cache"] = {"status": "stale_fallback", "fetched_at": entry.get("fetched_at"),
                           "age_hours": round(age, 2) if age is not None else None,
                           "refresh_status": status, "schema_upgrade_pending": not current}
    else:
        result["cache"] = {"status": "miss", "refresh_status": result.get("status")}
    return result


def _update_metric_ages(value, today):
    if isinstance(value, dict):
        if "as_of" in value:
            value.update(metrics.freshness(value["as_of"], today))
        for child in value.values():
            _update_metric_ages(child, today)
    elif isinstance(value, list):
        for child in value:
            _update_metric_ages(child, today)


def build_snapshot(d_today=None):
    if d_today is None:
        d_today = date.today()
    elif isinstance(d_today, str):
        d_today = date.fromisoformat(d_today)
    d_yest = d_today - timedelta(days=1)
    week_start = d_today - timedelta(days=7)

    try:
        g = client()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"login failed: {exc}"}

    try:
        full_name = g.get_full_name()
    except Exception:
        full_name = None

    dto_today, sleep_raw = sleep_dto(g, d_today)
    sleep = None
    if dto_today:
        scores = dto_today.get("sleepScores") or {}
        overall = scores.get("overall") or {} if isinstance(scores, dict) else {}
        # Per-dimension sleep sub-scores (which PART of the night was good/poor vs its optimal band).
        _sub_dims = ("totalDuration", "stress", "awakeCount", "remPercentage",
                     "restlessness", "lightPercentage", "deepPercentage")
        sub_scores = {}
        if isinstance(scores, dict):
            for _dim in _sub_dims:
                _sc = scores.get(_dim)
                if isinstance(_sc, dict):
                    sub_scores[_dim] = {k: v for k, v in {
                        "value": _sc.get("value"),
                        "qualifier": _sc.get("qualifierKey"),
                        "optimal_low": _sc.get("optimalStart"),
                        "optimal_high": _sc.get("optimalEnd"),
                    }.items() if v is not None}
        # Garmin's personalised sleep-need model (minutes -> hours). nextSleepNeed = TONIGHT's target.
        _sn = dto_today.get("sleepNeed") or {}
        _nsn = dto_today.get("nextSleepNeed") or {}

        def _need_h(v):
            return round(v / 60, 1) if isinstance(v, (int, float)) else None

        sleep_need_adjust = {k: v for k, v in {
            "sleep_history": _nsn.get("sleepHistoryAdjustment"),
            "hrv": _nsn.get("hrvAdjustment"),
            "nap": _nsn.get("napAdjustment"),
        }.items() if v and v != "NO_CHANGE"}
        sleep = {
            "date": iso(d_today),
            "score": overall.get("value"),
            "quality": overall.get("qualifierKey"),
            "time_asleep_s": dto_today.get("sleepTimeSeconds"),
            "deep_s": dto_today.get("deepSleepSeconds"),
            "light_s": dto_today.get("lightSleepSeconds"),
            "rem_s": dto_today.get("remSleepSeconds"),
            "awake_s": dto_today.get("awakeSleepSeconds"),
            "resting_hr": (sleep_raw or {}).get("restingHeartRate"),
            "avg_overnight_hrv": (sleep_raw or {}).get("avgOvernightHrv"),
            "body_battery_change": (sleep_raw or {}).get("bodyBatteryChange"),
            "avg_spo2": dto_today.get("averageSpO2Value"),
            "lowest_spo2": dto_today.get("lowestSpO2Value"),
            "avg_respiration": dto_today.get("averageRespirationValue"),
            "restless_moments": (sleep_raw or {}).get("restlessMomentsCount"),
            "breathing_disruptions": _count_breathing_disruptions(sleep_raw),
            "skin_temp_deviation_c": (sleep_raw or {}).get("avgSkinTempDeviationC"),
            "skin_temp_deviation_f": (sleep_raw or {}).get("avgSkinTempDeviationF"),
            "skin_temp_calibration_days": (sleep_raw or {}).get("skinTempCalibrationDays"),
            "score_feedback": (dto_today.get("sleepScores") or {}).get("overall", {}).get("qualifierKey")
            if isinstance(dto_today.get("sleepScores"), dict) else None,
            "score_feedback_garmin": dto_today.get("sleepScoreFeedback"),
            "score_personalized_insight": dto_today.get("sleepScorePersonalizedInsight"),
            "sub_scores": sub_scores or None,
            "avg_sleep_stress": dto_today.get("avgSleepStress"),
            "avg_sleep_hr": dto_today.get("avgHeartRate"),
            "awake_count": dto_today.get("awakeCount"),
            "bed_time_local": _epoch_ms_local(dto_today.get("sleepStartTimestampLocal")),
            "wake_time_local": _epoch_ms_local(dto_today.get("sleepEndTimestampLocal")),
            "sleep_need_last_night_h": _need_h(_sn.get("actual")),
            "sleep_need_tonight_h": _need_h(_nsn.get("actual")),
            "sleep_need_baseline_h": _need_h(_nsn.get("baseline")),
            "sleep_need_tonight_feedback": _nsn.get("feedback"),
            "sleep_need_tonight_adjustments": sleep_need_adjust or None,
        }

    # Daytime naps (Garmin nap detection) - separate from overnight sleep; they add recovery
    # through the day. Pulled from the SAME sleep DTO already fetched (no extra API call).
    naps_today = None
    if dto_today:
        nap_list = dto_today.get("dailyNapDTOS") or []
        total_nap_s = dto_today.get("napTimeSeconds")
        if nap_list or total_nap_s:
            naps = []
            for n in nap_list:
                if not isinstance(n, dict):
                    continue
                dur = n.get("napTimeSec")
                naps.append({
                    "start_local": _nap_local(n.get("napStartTimestampGMT"), n.get("napStartTimeOffset")),
                    "end_local": _nap_local(n.get("napEndTimestampGMT"), n.get("napEndTimeOffset")),
                    "duration_min": round(dur / 60) if isinstance(dur, (int, float)) else None,
                    "feedback": n.get("napFeedback"),
                })
            naps_today = {
                "total_nap_min": round(total_nap_s / 60) if isinstance(total_nap_s, (int, float)) else None,
                "count": len(naps),
                "naps": naps,
            }

    def day_stats(d):
        s = safe(lambda: g.get_stats(iso(d)))
        if not isinstance(s, dict) or "__error__" in s:
            return {"__error__": s.get("__error__") if isinstance(s, dict) else str(s)}
        return {
            "date": iso(d),
            "total_steps": s.get("totalSteps"),
            "step_goal": s.get("dailyStepGoal"),
            "distance_m": s.get("totalDistanceMeters"),
            "active_kcal": s.get("activeKilocalories"),
            "total_kcal": s.get("totalKilocalories"),
            "bmr_kcal": s.get("bmrKilocalories"),
            "net_kcal_remaining": (s.get("remainingKilocalories")
                                   if s.get("includesCalorieConsumedData") else None),
            "resting_hr": s.get("restingHeartRate"),
            "min_hr": s.get("minHeartRate"),
            "max_hr": s.get("maxHeartRate"),
            "avg_stress": s.get("averageStressLevel"),
            "max_stress": s.get("maxStressLevel"),
            "body_battery_high": s.get("bodyBatteryHighestValue"),
            "body_battery_low": s.get("bodyBatteryLowestValue"),
            "body_battery_most_recent": s.get("bodyBatteryMostRecentValue"),
            "moderate_intensity_min": s.get("moderateIntensityMinutes"),
            "vigorous_intensity_min": s.get("vigorousIntensityMinutes"),
            "floors_up": s.get("floorsAscended"),
            "avg_spo2": s.get("averageSpo2"),
        }

    readiness = safe(lambda: g.get_training_readiness(iso(d_today)))
    if isinstance(readiness, list) and readiness:
        r0 = readiness[0]
        rt_min = r0.get("recoveryTime")  # Garmin reports recovery time in MINUTES
        readiness = {
            "score": r0.get("score"),
            "level": r0.get("level"),
            "feedback": r0.get("feedbackShort"),
            "feedback_long": r0.get("feedbackLong"),
            "recovery_time_min": rt_min,
            "recovery_time_h": round(rt_min / 60, 1) if isinstance(rt_min, (int, float)) else None,
            "recovery_change": r0.get("recoveryTimeChangePhrase"),
            "sleep_score_factor": r0.get("sleepScoreFactorPercent"),
            "recovery_time_factor": r0.get("recoveryTimeFactorPercent"),
            "hrv_factor": r0.get("hrvFactorPercent"),
            "acwr_factor": r0.get("acwrFactorPercent"),
            "stress_history_factor": r0.get("stressHistoryFactorPercent"),
            "sleep_history_factor": r0.get("sleepHistoryFactorPercent"),
            "acute_load": r0.get("acuteLoad"),
        }

    # Wake-time ("this morning") readiness snapshot - cleaner for the 09:30 brief
    # than the current-moment readiness above, which decays through the day.
    morning = safe(lambda: g.get_morning_training_readiness(iso(d_today)))
    morning_readiness = None
    if isinstance(morning, dict) and "__error__" not in morning:
        morning_readiness = {
            "score": morning.get("score"),
            "level": morning.get("level"),
            "feedback": morning.get("feedbackShort"),
            "measured_at_local": morning.get("timestampLocal"),
            "sleep_score": morning.get("sleepScore"),
            "hrv_weekly_avg": morning.get("hrvWeeklyAverage"),
            "input_context": morning.get("inputContext"),
        }

    hrv = safe(lambda: g.get_hrv_data(iso(d_today)))
    if isinstance(hrv, dict) and "__error__" not in hrv:
        summ = hrv.get("hrvSummary") or {}
        hrv = {
            "last_night_avg": summ.get("lastNightAvg"),
            "last_night_5min_high": summ.get("lastNight5MinHigh"),
            "weekly_avg": summ.get("weeklyAvg"),
            "status": summ.get("status"),
            "baseline_low_upper": (summ.get("baseline") or {}).get("lowUpper"),
            "baseline_balanced_low": (summ.get("baseline") or {}).get("balancedLow"),
            "baseline_balanced_upper": (summ.get("baseline") or {}).get("balancedUpper"),
        }

    status = safe(lambda: g.get_training_status(iso(d_today)))
    train_status = None
    if isinstance(status, dict) and "__error__" not in status:
        most_recent = status.get("mostRecentTrainingStatus") or {}
        latest = None
        map_ = most_recent.get("latestTrainingStatusData") or {}
        if isinstance(map_, dict):
            for _k, v in map_.items():
                latest = v
                break
        vo2 = status.get("mostRecentVO2Max") or {}
        generic = (vo2.get("generic") or {}) if isinstance(vo2, dict) else {}
        acute_dto = (latest or {}).get("acuteTrainingLoadDTO") or {}
        train_status = {
            "training_status": (latest or {}).get("trainingStatus"),
            "training_status_key": (latest or {}).get("trainingStatusFeedbackPhrase"),
            "acwr": acute_dto.get("dailyAcuteChronicWorkloadRatio"),
            "acwr_status": acute_dto.get("acwrStatus"),
            "acute_load": acute_dto.get("dailyTrainingLoadAcute"),
            "chronic_load": acute_dto.get("dailyTrainingLoadChronic"),
            "load_trend": (latest or {}).get("loadLevelTrend"),
            "fitness_trend": (latest or {}).get("fitnessTrend"),
            "vo2max": generic.get("vo2MaxValue"),
            "vo2max_date": generic.get("calendarDate"),
        }
        # Load Focus: 4-week aerobic-low / aerobic-high / anaerobic load split vs
        # Garmin's target ranges, plus its balance verdict (e.g. AEROBIC_LOW_FOCUS).
        lb_map = ((status.get("mostRecentTrainingLoadBalance") or {})
                  .get("metricsTrainingLoadBalanceDTOMap") or {})
        lb = next(iter(lb_map.values()), {}) if isinstance(lb_map, dict) else {}
        if lb:
            def _r(x):
                return round(x) if isinstance(x, (int, float)) else None
            train_status["load_focus"] = {
                "aerobic_low": _r(lb.get("monthlyLoadAerobicLow")),
                "aerobic_high": _r(lb.get("monthlyLoadAerobicHigh")),
                "anaerobic": _r(lb.get("monthlyLoadAnaerobic")),
                "aerobic_low_target": [lb.get("monthlyLoadAerobicLowTargetMin"),
                                       lb.get("monthlyLoadAerobicLowTargetMax")],
                "aerobic_high_target": [lb.get("monthlyLoadAerobicHighTargetMin"),
                                        lb.get("monthlyLoadAerobicHighTargetMax")],
                "anaerobic_target": [lb.get("monthlyLoadAnaerobicTargetMin"),
                                     lb.get("monthlyLoadAnaerobicTargetMax")],
                "feedback": lb.get("trainingBalanceFeedbackPhrase"),
            }

    history, history_coverage = _activity_history(g, d_today - timedelta(days=28), d_today)
    activities = [_trim_activity(a) for a in history
                  if str(a.get("startTimeLocal") or "")[:10] >= iso(week_start)]
    activities_coverage = dict(history_coverage, requested_from=iso(week_start), returned=len(activities))
    week_complete = (history_coverage.get("complete_from") is not None
                     and history_coverage["complete_from"] <= iso(week_start))
    activities_coverage.update(complete=week_complete, unknown=not week_complete,
                               total=len(activities) if week_complete else None,
                               omitted=0 if week_complete else None)
    cache = _load_sets_cache()
    refresh_budget = 8
    strength_total = strength_available = strength_stale = 0
    for a in activities:
        if "strength" not in (a.get("type") or "") or a.get("activity_id") is None:
            continue
        strength_total += 1
        meta = {}
        logged = _sets_for(g, a["activity_id"], cache,
                           refresh=_within_days(a.get("start"), EDITABLE_DAYS),
                           metadata=meta, allow_fetch=refresh_budget > 0)
        refresh_budget -= int(meta.get("fetched", False))
        a["logged_sets_coverage"] = meta
        if logged:
            a["logged_sets"] = logged
            strength_available += 1
            strength_stale += int(meta.get("status") == "stale")
    if refresh_budget < 8:
        _save_sets_cache(cache)
    activities_coverage["strength_sets"] = {
        "total": strength_total, "returned": strength_available,
        "unavailable_or_deferred": strength_total - strength_available,
        "stale": strength_stale, "max_refresh_calls": 8,
    }

    body_battery = safe(lambda: g.get_body_battery(iso(d_yest), iso(d_today)))
    bb = None
    if isinstance(body_battery, list):
        bb = []
        for entry in body_battery:
            bb.append({
                "date": entry.get("date"),
                "charged": entry.get("charged"),
                "drained": entry.get("drained"),
                "highest": entry.get("bodyBatteryStatList", [{}])[0].get("statsValue")
                if entry.get("bodyBatteryStatList") else None,
            })

    # Latest synced body-battery sample from today's series (value + timestamp), used only
    # for the "as of" freshness stamp. The reported current value prefers Garmin's canonical
    # bodyBatteryMostRecentValue scalar (see below) - the exact number the Connect app/web show.
    bb_series_val = None
    bb_series_ts = None
    if isinstance(body_battery, list) and body_battery:
        arr = body_battery[-1].get("bodyBatteryValuesArray") or []
        for point in reversed(arr):
            if (isinstance(point, list) and len(point) >= 2
                    and isinstance(point[1], (int, float))):
                bb_series_val = point[1]
                bb_series_ts = point[0]
                break

    weight = safe(lambda: g.get_body_composition(iso(d_today - timedelta(days=30)), iso(d_today)))
    latest_weight = None
    if isinstance(weight, dict):
        dwl = weight.get("dateWeightList") or []
        if dwl:
            # Garmin returns dateWeightList newest-first; pick the most recent by date
            # explicitly rather than trusting order, so we never report a stale weigh-in.
            last = max(dwl, key=lambda e: e.get("calendarDate") or "")
            latest_weight = {
                "date": last.get("calendarDate"),
                "weight_g": last.get("weight"),
                "bmi": last.get("bmi"),
                "body_fat_pct": last.get("bodyFat"),
                "muscle_mass_g": last.get("muscleMass"),
                "body_water_pct": last.get("bodyWater"),
                "bone_mass_g": last.get("boneMass"),
            }

    # Weight TREND (fat-loss progress signal) - built from the SAME 30-day body-comp pull
    # above, so no extra API call. Oldest->newest series (kg) + net change + direction, so
    # the daily brief can actually track PROGRESS, not just report a single weigh-in.
    weight_trend = None
    if isinstance(weight, dict):
        _pts = []
        for w in sorted((weight.get("dateWeightList") or []),
                        key=lambda e: e.get("calendarDate") or ""):
            _g = w.get("weight")
            if not isinstance(_g, (int, float)):
                continue
            _pts.append({
                "date": w.get("calendarDate"),
                "weight_kg": round(_g / 1000.0, 1),
                "body_fat_pct": w.get("bodyFat"),
            })
        if _pts:
            _pts = _pts[-14:]  # last ~2 weeks is the meaningful daily-brief window
            _latest, _earliest = _pts[-1], _pts[0]

            def _closest_before(days):
                try:
                    target = date.fromisoformat(_latest["date"][:10]) - timedelta(days=days)
                except (ValueError, TypeError):
                    return None
                best, best_gap = None, None
                for _p in _pts[:-1]:
                    try:
                        _pd = date.fromisoformat((_p["date"] or "")[:10])
                    except ValueError:
                        continue
                    _gap = abs((_pd - target).days)
                    if best_gap is None or _gap < best_gap:
                        best, best_gap = _p, _gap
                return best if (best is not None and best_gap is not None and best_gap <= 4) else None

            def _chg(ref):
                if ref and isinstance(ref.get("weight_kg"), (int, float)):
                    return round(_latest["weight_kg"] - ref["weight_kg"], 1)
                return None

            _net = _chg(_earliest) if _earliest is not _latest else None
            _c7 = _chg(_closest_before(7))
            _dir = None
            if isinstance(_net, (int, float)):
                _dir = "down" if _net <= -0.3 else ("up" if _net >= 0.3 else "flat")
            try:
                _span = (date.fromisoformat(_latest["date"][:10])
                         - date.fromisoformat(_earliest["date"][:10])).days
            except (ValueError, TypeError):
                _span = None
            weight_trend = {
                "series": _pts,
                "count": len(_pts),
                "latest_kg": _latest["weight_kg"],
                "latest_date": _latest["date"],
                "change_last_7d_kg": _c7,
                "net_change_kg": _net,
                "span_days": _span,
                "direction": _dir,
            }

    # ---- Whole-day wellness (get_user_summary is Garmin's richest single call) ----
    usumm = safe(lambda: g.get_user_summary(iso(d_today)))
    resp = safe(lambda: g.get_respiration_data(iso(d_today)))
    hydr = safe(lambda: g.get_hydration_data(iso(d_today)))
    ads = safe(lambda: g.get_all_day_stress(iso(d_today)))
    wellness = None
    last_sync_gmt = None
    last_sync_age_min = None
    if isinstance(usumm, dict) and "__error__" not in usumm:
        def _h(sec):
            return round(sec / 3600, 1) if isinstance(sec, (int, float)) else None

        def _m(sec):
            return round(sec / 60) if isinstance(sec, (int, float)) else None

        wellness = {
            "sedentary_h": _h(usumm.get("sedentarySeconds")),
            "active_h": _h(usumm.get("activeSeconds")),
            "highly_active_h": _h(usumm.get("highlyActiveSeconds")),
            "sleeping_h": _h(usumm.get("sleepingSeconds")),
            "stress_qualifier": usumm.get("stressQualifier"),
            "avg_stress": usumm.get("averageStressLevel"),
            "rest_stress_min": _m(usumm.get("restStressDuration")),
            "low_stress_min": _m(usumm.get("lowStressDuration")),
            "medium_stress_min": _m(usumm.get("mediumStressDuration")),
            "high_stress_min": _m(usumm.get("highStressDuration")),
            "activity_stress_min": _m(usumm.get("activityStressDuration")),
            "rhr_today": usumm.get("restingHeartRate"),
            "rhr_7d_avg": usumm.get("lastSevenDaysAvgRestingHeartRate"),
            "body_battery_charged": usumm.get("bodyBatteryChargedValue"),
            "body_battery_drained": usumm.get("bodyBatteryDrainedValue"),
            "body_battery_during_sleep": usumm.get("bodyBatteryDuringSleep"),
            "body_battery_at_wake": usumm.get("bodyBatteryAtWakeTime"),
            "abnormal_hr_alerts": usumm.get("abnormalHeartRateAlertsCount"),
            # Garmin's remainingKilocalories / netCalorieGoal are only meaningful if food is
            # logged IN GARMIN. The user logs meals in the bot instead, so includesCalorieConsumedData
            # is False and Garmin's "remaining" is just netCalorieGoal + active burn - it ignores
            # everything they ate and even GROWS as they train, which made the coach quote a bogus
            # multi-thousand-kcal "calorie room". Only surface it when Garmin truly has their intake;
            # otherwise give honest expenditure and flag that intake must come from the bot's log.
            "calorie_intake_tracked_in_garmin": bool(usumm.get("includesCalorieConsumedData")),
            "energy_burned_so_far_kcal": (round(usumm.get("totalKilocalories"))
                                          if isinstance(usumm.get("totalKilocalories"), (int, float)) else None),
            "remaining_kcal": (round(usumm.get("remainingKilocalories"))
                               if usumm.get("includesCalorieConsumedData")
                               and isinstance(usumm.get("remainingKilocalories"), (int, float)) else None),
            "net_calorie_goal": (usumm.get("netCalorieGoal")
                                 if usumm.get("includesCalorieConsumedData") else None),
        }
        if isinstance(resp, dict) and "__error__" not in resp:
            wellness["respiration_sleep_avg"] = resp.get("avgSleepRespirationValue")
            wellness["respiration_waking_avg"] = resp.get("avgWakingRespirationValue")
            wellness["respiration_high"] = resp.get("highestRespirationValue")
            wellness["respiration_low"] = resp.get("lowestRespirationValue")
        if isinstance(hydr, dict) and "__error__" not in hydr:
            wellness["sweat_loss_ml"] = hydr.get("sweatLossInML")
            wellness["hydration_logged_ml"] = hydr.get("valueInML")
            wellness["hydration_goal_ml"] = (round(hydr.get("goalInML"))
                                             if isinstance(hydr.get("goalInML"), (int, float)) else None)
        # Garmin's own plain-English Body Battery read + daytime recovery/nap events.
        _dfe = usumm.get("bodyBatteryDynamicFeedbackEvent")
        if isinstance(_dfe, dict):
            wellness["body_battery_feedback"] = _dfe.get("feedbackShortType")
            wellness["body_battery_feedback_detail"] = _dfe.get("feedbackLongType")
            wellness["body_battery_level_label"] = _dfe.get("bodyBatteryLevel")
        _bb_events = usumm.get("bodyBatteryActivityEventList")
        if isinstance(_bb_events, list):
            evs = []
            for _e in _bb_events:
                if not isinstance(_e, dict) or _e.get("eventType") == "SLEEP":
                    continue  # overnight charge already captured in last_night_sleep
                _fb = _e.get("shortFeedback")
                _dur = _e.get("durationInMilliseconds")
                evs.append({k: v for k, v in {
                    "type": _e.get("eventType"),
                    "bb_impact": _e.get("bodyBatteryImpact"),
                    "feedback": _fb if (_fb and _fb != "NONE") else None,
                    "start_gmt": _e.get("eventStartTimeGmt"),
                    "duration_min": round(_dur / 60000) if isinstance(_dur, (int, float)) else None,
                }.items() if v is not None})
            if evs:
                wellness["body_battery_events"] = evs
        # Hourly stress curve (downsampled) - shows WHEN stress peaked, not just the daily split.
        if isinstance(ads, dict) and "__error__" not in ads:
            _arr = ads.get("stressValuesArray") or []
            _off_ms = 0
            try:
                _sg = datetime.fromisoformat((ads.get("startTimestampGMT") or "").replace("Z", ""))
                _sl = datetime.fromisoformat((ads.get("startTimestampLocal") or "").replace("Z", ""))
                _off_ms = (_sl - _sg).total_seconds() * 1000
            except Exception:  # noqa: BLE001
                _off_ms = 0
            _buckets = {}
            for _pt in _arr:
                if not (isinstance(_pt, (list, tuple)) and len(_pt) >= 2):
                    continue
                _ts, _lvl = _pt[0], _pt[1]
                if not isinstance(_ts, (int, float)) or not isinstance(_lvl, (int, float)) or _lvl < 0:
                    continue  # skip -1/-2 no-reading sentinels
                _hr = datetime.utcfromtimestamp((_ts + _off_ms) / 1000).hour
                _buckets.setdefault(_hr, []).append(_lvl)
            if _buckets:
                wellness["stress_curve_hourly"] = {
                    f"{_h:02d}:00": round(sum(_v) / len(_v))
                    for _h, _v in sorted(_buckets.items())
                }
        sync = usumm.get("lastSyncTimestampGMT")
        if sync:
            try:
                _st = datetime.fromisoformat(sync.replace("Z", ""))
                last_sync_gmt = _st.isoformat(timespec="minutes")
                last_sync_age_min = round((datetime.utcnow() - _st).total_seconds() / 60)
            except Exception:  # noqa: BLE001
                last_sync_gmt = sync[:16]

    # Whole-day strain from YESTERDAY (a completed day) - the morning brief's "how hard
    # was yesterday" read. today's wellness_today counters are still accumulating in the AM.
    ysumm = safe(lambda: g.get_user_summary(iso(d_yest)))
    strain_yesterday = None
    if isinstance(ysumm, dict) and "__error__" not in ysumm:
        strain_yesterday = {
            "sedentary_h": (round(ysumm.get("sedentarySeconds") / 3600, 1)
                            if isinstance(ysumm.get("sedentarySeconds"), (int, float)) else None),
            "active_h": (round(ysumm.get("activeSeconds") / 3600, 1)
                         if isinstance(ysumm.get("activeSeconds"), (int, float)) else None),
            "highly_active_h": (round(ysumm.get("highlyActiveSeconds") / 3600, 1)
                                if isinstance(ysumm.get("highlyActiveSeconds"), (int, float)) else None),
            "avg_stress": ysumm.get("averageStressLevel"),
            "stress_qualifier": ysumm.get("stressQualifier"),
            "high_stress_min": (round(ysumm.get("highStressDuration") / 60)
                                if isinstance(ysumm.get("highStressDuration"), (int, float)) else None),
            "rest_stress_min": (round(ysumm.get("restStressDuration") / 60)
                                if isinstance(ysumm.get("restStressDuration"), (int, float)) else None),
        }

    today_stats = day_stats(d_today)
    # Prefer Garmin's canonical "most recent" scalar (identical to what the Connect app/web
    # display); fall back to the latest series sample only if the scalar is unavailable.
    bb_current = None
    if isinstance(today_stats, dict):
        bb_current = today_stats.get("body_battery_most_recent")
    if bb_current is None:
        bb_current = bb_series_val
    bb_current_as_of = None
    bb_current_age_min = None
    if bb_series_ts:
        _bb_ts = datetime.fromtimestamp(bb_series_ts / 1000)
        bb_current_as_of = _bb_ts.isoformat(timespec="minutes")
        bb_current_age_min = round((datetime.now() - _bb_ts).total_seconds() / 60)

    # ---- Static-ish profile config: timezone (for correct time-of-day reasoning), habitual
    # sleep window, and training-day preferences. From settings + user profile (userData). ----
    ups = safe(lambda: g.get_userprofile_settings())
    uprof = safe(lambda: g.get_user_profile())
    _ud = (uprof.get("userData") if isinstance(uprof, dict) else None) or {}
    _ups = ups if isinstance(ups, dict) and "__error__" not in ups else {}
    profile_config = None
    if _ups or _ud:
        def _hhmm(sec):
            if not isinstance(sec, (int, float)):
                return None
            return f"{int(sec // 3600) % 24:02d}:{int((sec % 3600) // 60):02d}"

        _win = None
        _windows = uprof.get("userSleepWindows") if isinstance(uprof, dict) else None
        if isinstance(_windows, list) and _windows:
            _daily = next((w for w in _windows if isinstance(w, dict)
                           and w.get("sleepWindowFrequency") == "DAILY"), _windows[0])
            if isinstance(_daily, dict):
                _win = {
                    "bedtime": _hhmm(_daily.get("startSleepTimeSecondsFromMidnight")),
                    "waketime": _hhmm(_daily.get("endSleepTimeSecondsFromMidnight")),
                }
        _age = None
        try:
            _by = date.fromisoformat(_ud.get("birthDate"))
            _age = d_today.year - _by.year - ((d_today.month, d_today.day) < (_by.month, _by.day))
        except Exception:  # noqa: BLE001
            _age = None
        profile_config = {k: v for k, v in {
            "timezone": _ups.get("timeZone"),
            "measurement_system": _ups.get("measurementSystem") or _ud.get("measurementSystem"),
            "first_day_of_week": (_ups.get("firstDayOfWeek") or {}).get("dayName"),
            "age": _age,
            "available_training_days": _ud.get("availableTrainingDays"),
            "preferred_long_training_days": _ud.get("preferredLongTrainingDays"),
            "habitual_sleep_window": _win,
        }.items() if v is not None} or None
        if profile_config:
            _set_local_tz(profile_config.get("timezone"))

    # Maintenance is an estimate, not an intake prescription. Targets require an explicit
    # nutrition goal; food logged outside Garmin must not be ignored in a remaining budget.
    calorie_budget = None
    try:
        _sex = (_ud.get("gender") or "").upper()
        _ht_cm = _ud.get("height")
        _wt_kg = None
        if latest_weight and isinstance(latest_weight.get("weight_g"), (int, float)):
            _wt_kg = latest_weight["weight_g"] / 1000.0
        elif isinstance(_ud.get("weight"), (int, float)):
            _wt_kg = _ud["weight"] / 1000.0
        _cb_age = None
        try:
            _bd = date.fromisoformat(_ud.get("birthDate"))
            _cb_age = d_today.year - _bd.year - ((d_today.month, d_today.day) < (_bd.month, _bd.day))
        except Exception:  # noqa: BLE001
            _cb_age = None
        if _wt_kg and isinstance(_ht_cm, (int, float)) and _cb_age and _sex in ("MALE", "FEMALE"):
            _bmi = _wt_kg / ((_ht_cm / 100.0) ** 2)
            _bmr = 10 * _wt_kg + 6.25 * _ht_cm - 5 * _cb_age + (5 if _sex == "MALE" else -161)
            _al = _ud.get("activityLevel")
            if isinstance(_al, (int, float)):
                _af = (1.2 if _al <= 2 else 1.375 if _al <= 4 else 1.55 if _al <= 6
                       else 1.725 if _al <= 8 else 1.9)
            else:
                _af = 1.55  # provisional estimate, disclosed below rather than asserted as observed
            _maint = _bmr * _af
            _bmi_cat = ("underweight" if _bmi < 18.5 else "normal" if _bmi < 25
                        else "overweight" if _bmi < 30 else "obese")
            _requested_goal = os.environ.get("AGBOT_NUTRITION_GOAL", "unspecified").strip().lower()
            _goal = _requested_goal if _requested_goal in {"maintain", "fat_loss"} else "unspecified"
            _target = _deficit = _deficit_cap = None
            _protein_target = _protein_floor = _protein_kcal = None
            _target_status = "not_configured" if _requested_goal in {"", "unspecified"} else "unsupported_goal"
            _protein_basis = "No protein target configured; do not infer a mandatory intake from maintenance."
            if _goal == "maintain":
                _target, _deficit = round(_maint), 0
                _target_status = "configured_estimate"
            elif _goal == "fat_loss":
                if _cb_age < 18 or _bmi < 18.5:
                    _target_status = "not_appropriate_for_automatic_fat_loss_target"
                else:
                    _target_status = "configured_estimate"
                    _band_deficit = (300 if _bmi < 23 else 400 if _bmi < 25 else 500 if _bmi < 30 else 600)
                    _deficit_cap = round(0.005 * _wt_kg * 7700 / 7)
                    _deficit = min(_band_deficit, _deficit_cap)
                    _target = max(round(_maint - _deficit), round(_bmr), 1500)
                    _protein_target, _protein_floor = round(2.2 * _wt_kg), round(1.8 * _wt_kg)
                    _protein_kcal = _protein_target * 4
                    _protein_basis = ("Explicit fat-loss goal: provisional resistance-training protein estimate "
                                      "2.2 g/kg, lower reference 1.8 g/kg; individual needs may differ.")
            calorie_budget = {
                "nutrition_goal": _goal,
                "goal_explicit": _goal != "unspecified",
                "target_status": _target_status,
                "weight_kg": round(_wt_kg, 1),
                "height_cm": round(_ht_cm),
                "age": _cb_age,
                "bmi": round(_bmi, 1),
                "bmi_category": _bmi_cat,
                "bmr_kcal": round(_bmr),
                "activity_factor": _af,
                "activity_factor_source": ("garmin_activity_level_band" if isinstance(_al, (int, float))
                                           else "provisional_default_not_observed"),
                "maintenance_kcal": round(_maint),
                "maintenance_is_estimate": True,
                "deficit_kcal": _deficit,
                "deficit_cap_kcal": _deficit_cap,
                "target_kcal": _target,
                "protein_target_g": _protein_target,
                "protein_floor_g": _protein_floor,
                "protein_kcal": _protein_kcal,
                "protein_basis": _protein_basis,
                "basis": ("Mifflin-St Jeor BMR x activity factor estimates maintenance, not measured expenditure. "
                          "Only an explicit maintain/fat_loss goal enables an intake target. Fat-loss estimates "
                          "use a BMI-scaled deficit capped at ~0.5% bodyweight/week, with BMR/1500 kcal guards. "
                          "No remaining-calorie budget exists when target_kcal is null; otherwise account for "
                          "all logged intake. These estimates are not a binding diet prescription."),
            }
    except Exception:  # noqa: BLE001
        calorie_budget = None

    # ---- Weekly intensity minutes vs Garmin's goal (WHO 150-min target; vigorous counts double) ----
    wim = safe(lambda: g.get_weekly_intensity_minutes(iso(week_start), iso(d_today)))
    weekly_intensity = None
    if isinstance(wim, list) and wim and isinstance(wim[-1], dict):
        _cur = wim[-1]
        _mod = _cur.get("moderateValue")
        _vig = _cur.get("vigorousValue")
        _tot = (_mod + 2 * _vig) if isinstance(_mod, (int, float)) and isinstance(_vig, (int, float)) else None
        weekly_intensity = {k: v for k, v in {
            "week_start": _cur.get("calendarDate"),
            "moderate_min": _mod,
            "vigorous_min": _vig,
            "total_toward_goal": _tot,
            "weekly_goal": _cur.get("weeklyGoal"),
        }.items() if v is not None} or None

    # ---- Multi-week trend lines (last 8 weeks) for direction, kept compact to limit payload ----
    wsteps = safe(lambda: g.get_weekly_steps(iso(d_today)))
    wstress = safe(lambda: g.get_weekly_stress(iso(d_today)))
    weekly_trends = {}
    if isinstance(wsteps, list) and wsteps:
        _rows = []
        for _w in wsteps[-8:]:
            if not isinstance(_w, dict):
                continue
            _avg = (_w.get("values") or {}).get("averageSteps")
            if isinstance(_avg, (int, float)):
                _rows.append({"week": _w.get("calendarDate"), "avg_steps": round(_avg)})
        if _rows:
            weekly_trends["avg_daily_steps_by_week"] = _rows
    if isinstance(wstress, list) and wstress:
        _rows = []
        for _w in wstress[-8:]:
            if isinstance(_w, dict) and _w.get("value") is not None:
                _rows.append({"week": _w.get("calendarDate"), "avg_stress": _w.get("value")})
        if _rows:
            weekly_trends["avg_stress_by_week"] = _rows
    weekly_trends = weekly_trends or None

    snapshot = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "athlete": full_name,
        "sleep_date": iso(d_today),
        "activity_date": iso(d_yest),
        "last_night_sleep": sleep,
        "naps_today": naps_today,
        "yesterday_full_day": day_stats(d_yest),
        "today_so_far": today_stats,
        "training_readiness": readiness,
        "morning_readiness": morning_readiness,
        "hrv": hrv,
        "training_status": train_status,
        "recent_activities_7d": activities,
        "recent_activities_7d_coverage": activities_coverage,
        "training_rhythm": _training_rhythm(g, d_today, history, history_coverage),
        "wellness_today": wellness,
        "strain_yesterday": strain_yesterday,
        "body_battery_current": bb_current,
        "body_battery_current_as_of": bb_current_as_of,
        "body_battery_current_age_min": bb_current_age_min,
        "body_battery_2d": bb,
        "latest_weigh_in_30d": latest_weight,
        "weight_trend_30d": weight_trend,
        "calorie_budget": calorie_budget,
        "last_sync_gmt": last_sync_gmt,
        "last_sync_age_min": last_sync_age_min,
        "profile_config": profile_config,
        "weekly_intensity": weekly_intensity,
        "weekly_trends": weekly_trends,
    }
    return snapshot


def latest_activity():
    """Cheapest possible fetch of the single most recent activity (for the
    proactive post-workout debrief). Returns a trimmed dict, None, or an error."""
    try:
        g = client()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"login failed: {exc}"}
    acts = safe(lambda: g.get_activities(0, 1))
    if isinstance(acts, list) and acts:
        return _trim_activity(acts[0])
    if isinstance(acts, dict) and "__error__" in acts:
        return acts
    return None


def trained_today(min_duration_s=None):
    """Compatibility boolean using classify_activity; errors/unknown return False.

    min_duration_s is retained for call compatibility, not an all-sports threshold.
    Coverage-aware callers should consume snapshot.training_rhythm.trained_today.
    """
    try:
        g = client()
    except Exception:  # noqa: BLE001
        return False
    today = datetime.now(_local_zone()).date()
    acts, _coverage = _activity_history(g, today, today, max_pages=2)
    return any(_is_training_activity(a) for a in acts)


def hydration_today(d_today=None):
    """Lightweight fetch of today's hydration as (logged_ml, goal_ml). Returns
    (None, None) on any login/API error or missing field. Used by the bridge's
    hydration nudges to skip a reminder when the user is already on pace."""
    d = d_today or date.today()
    try:
        g = client()
    except Exception:  # noqa: BLE001
        return (None, None)
    hydr = safe(lambda: g.get_hydration_data(iso(d)))
    if not isinstance(hydr, dict) or "__error__" in hydr:
        return (None, None)
    logged = hydr.get("valueInML")
    goal = hydr.get("goalInML")
    logged = logged if isinstance(logged, (int, float)) else None
    goal = round(goal) if isinstance(goal, (int, float)) else None
    return (logged, goal)


def _recent_hr(g, now_utc):
    """Most recent intraday heart-rate sample as (bpm, age_min, resting_hr), or (None, None, rhr).
    Lets the mover-nudge detect that the user is CURRENTLY exercising even when the workout is
    step-less (indoor bike, rowing, lifting) and not yet saved as an activity - HR stays high
    while step buckets read 'sedentary'."""
    local_today = now_utc.astimezone(_local_zone()).date().isoformat()
    hr = safe(lambda: g.get_heart_rates(local_today))
    if not isinstance(hr, dict) or "__error__" in hr:
        return None, None, None
    resting = hr.get("restingHeartRate")
    last_ts = last_bpm = None
    for pair in (hr.get("heartRateValues") or []):
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        ts, bpm = pair[0], pair[1]
        if bpm is None or not isinstance(ts, (int, float)):
            continue
        if last_ts is None or ts > last_ts:
            last_ts, last_bpm = ts, bpm
    if last_bpm is None:
        return None, None, resting
    age_min = round((now_utc.timestamp() - last_ts / 1000.0) / 60)
    return last_bpm, age_min, resting


def recent_inactivity(now_utc=None):
    """Look at today's 15-min intraday step buckets (Garmin) and report how long the user
    has been CONTINUOUSLY sedentary up to the most recent synced bucket. Lets the bridge send
    a 'get up and move' nudge ONLY when recent inactivity is confirmed (fail-closed - returns
    None on any error so the caller stays quiet). Bucket timestamps are GMT/UTC ('endGMT'),
    compared against UTC now.

    Step buckets alone are NOT enough: Garmin derives primaryActivityLevel largely from step
    count, so step-less work - cycling, e-bike commuting, rowing, lifting - lands in buckets
    labelled 'sedentary' even though the user was moving hard. The intraday feed also lags a
    sync behind. So today's ACTIVITIES are read too, and the sedentary run is never allowed to
    reach back past the end of the last one. Returns {data_age_min, sedentary_run_min,
    last_hour_steps, last_level, since_activity_min} or None."""
    try:
        g = client()
    except Exception:  # noqa: BLE001
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    local_today = now_utc.astimezone(_local_zone()).date().isoformat()
    buckets = safe(lambda: g.get_steps_data(local_today))
    if not isinstance(buckets, list) or not buckets:
        return None
    parsed = []
    for b in buckets:
        try:
            end = datetime.fromisoformat(b["endGMT"]).replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        parsed.append((end, b))
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    last_end, last_b = parsed[-1]
    data_age_min = round((now_utc - last_end).total_seconds() / 60)
    run_min = 0
    for _end, b in reversed(parsed):
        if b.get("primaryActivityLevel") == "sedentary":
            run_min += 15
        else:
            break
    hour_cutoff = last_end - timedelta(minutes=60)
    last_hour_steps = sum((b.get("steps") or 0) for _end, b in parsed if _end > hour_cutoff)
    since_activity_min = _minutes_since_last_activity(g, now_utc, local_today)
    if since_activity_min is not None:
        # Can't have been sitting longer than the time since the last workout/commute ended.
        run_min = min(run_min, since_activity_min)
    last_hr, last_hr_age_min, resting_hr = _recent_hr(g, now_utc)
    return {
        "data_age_min": data_age_min,
        "sedentary_run_min": run_min,
        "last_hour_steps": last_hour_steps,
        "last_level": last_b.get("primaryActivityLevel"),
        "since_activity_min": since_activity_min,
        "last_hr": last_hr,
        "last_hr_age_min": last_hr_age_min,
        "resting_hr": resting_hr,
    }


def _minutes_since_last_activity(g, now_utc, local_today):
    """Minutes since the END of the most recent activity that STARTED today, or None when
    there is none (or the lookup fails). Used to stop the movement nudge treating a
    step-less workout - an e-bike commute, a ride, a lifting session - as sitting still."""
    acts = safe(lambda: g.get_activities(0, 8))
    if not isinstance(acts, list):
        return None
    latest_end = None
    for a in acts:
        if str(a.get("startTimeLocal") or "")[:10] != local_today:
            continue
        start = _parse_local_epoch(a.get("startTimeLocal"))
        if start is None:
            continue
        end = start + (a.get("duration") or 0)
        if latest_end is None or end > latest_end:
            latest_end = end
    if latest_end is None:
        return None
    return max(0, round((now_utc.timestamp() - latest_end) / 60))


def _parse_local_epoch(s):
    """startTimeLocal ('YYYY-MM-DD HH:MM:SS') -> POSIX epoch, interpreting the naive wall
    time in the user's real local timezone (not the host's), or None."""
    try:
        dt = datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=_local_zone()).timestamp()
    except (ValueError, TypeError):
        return None


def session_block(gap_secs=3600, lookback=15):
    """The trailing cluster of just-finished activities that together form ONE workout
    session. The user logs each part of a session as a separate Garmin activity (a cardio
    warm-up, the Strength block, a run, a Pilates/stretch cool-down); this walks the most
    recent activities backward and groups every activity whose finish is within gap_secs of
    the next part's start, so a debrief can judge the whole session at once instead of
    nagging after each part. Returns a newest-last list of trimmed activities, [] if none,
    or an {'__error__': ...} dict."""
    try:
        g = client()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"login failed: {exc}"}
    acts = safe(lambda: g.get_activities(0, lookback))
    if isinstance(acts, dict) and "__error__" in acts:
        return acts
    if not isinstance(acts, list) or not acts:
        return []
    rows = []
    for a in acts:
        t = _trim_activity(a)
        st = _parse_local_epoch(t.get("start"))
        if st is None:
            continue
        dur = t.get("duration_s")
        end = st + (dur if isinstance(dur, (int, float)) else 0)
        rows.append((st, end, t))
    if not rows:
        return []
    rows.sort(key=lambda r: r[0])            # ascending by start time
    block = [rows[-1]]                        # newest activity anchors the block
    for i in range(len(rows) - 2, -1, -1):
        earliest_start = block[0][0]
        if earliest_start - rows[i][1] <= gap_secs:   # small gap -> same session
            block.insert(0, rows[i])
        else:
            break
    return [r[2] for r in block]


def activity_extras(activity_id, g=None, refresh=False, include_details=True):
    """Per-activity enrichment for the post-workout debrief: time-in-HR-zone and,
    for outdoor sessions, the weather at activity time. Weather comes back all-null
    for indoor activities (temp is None) and is skipped."""
    if activity_id is None:
        return {}
    try:
        g = g or client()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"login failed: {exc}"}
    out = {"metrics": activity_detail_metrics(activity_id, g=g, refresh=refresh,
                                             include_details=include_details)}
    hz = safe(lambda: g.get_activity_hr_in_timezones(activity_id))
    if isinstance(hz, list):
        zones = []
        for z in hz:
            secs = z.get("secsInZone")
            zones.append({
                "zone": z.get("zoneNumber"),
                "min": round(secs / 60, 1) if isinstance(secs, (int, float)) else None,
                "low_bpm": z.get("zoneLowBoundary"),
            })
        if any(zn["min"] for zn in zones):
            out["hr_time_in_zones"] = zones
    wx = safe(lambda: g.get_activity_weather(activity_id))
    if isinstance(wx, dict) and wx.get("temp") is not None:
        wtype = wx.get("weatherTypeDTO") or {}
        out["weather"] = {
            "temp_f": wx.get("temp"),
            "feels_like_f": wx.get("apparentTemp"),
            "humidity_pct": wx.get("relativeHumidity"),
            "wind_mph": wx.get("windSpeed"),
            "wind_dir": wx.get("windDirectionCompassPoint"),
            "conditions": wtype.get("desc") if isinstance(wtype, dict) else None,
        }
    return out


def activity_detail_metrics(activity_id, g=None, refresh=False, include_details=True):
    """On-demand aggregate enrichment, cached 24h; never expose tracks or chart samples."""
    try:
        g = g or client()
    except Exception as exc:
        return {"status": "error", "__error__": type(exc).__name__}

    def load():
        summary = safe(lambda: g.get_activity(activity_id))
        details = (safe(lambda: g.get_activity_details(activity_id, maxchart=100, maxpoly=0))
                   if include_details else None)
        result = metrics.activity_metrics(summary, details)
        if (result["status"] == "available" and include_details
                and metrics.payload_status(details) in {"error", "not_supported", "unavailable"}):
            result["status"] = "partial"
        return result

    return _cached_metric(
        f"activity:{activity_id}:details:{include_details}:v{metrics.ACTIVITY_METRICS_VERSION}",
        load, refresh=refresh)


def build_weekly(days=7, d_today=None):
    """Per-day trends over the last `days` days plus weight series, the week's
    activities and latest training status. Heavier than build_snapshot, on demand."""
    if d_today is None:
        d_today = date.today()
    elif isinstance(d_today, str):
        d_today = date.fromisoformat(d_today)
    try:
        g = client()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"login failed: {exc}"}
    start = d_today - timedelta(days=days)
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "range": iso(start) + " .. " + iso(d_today),
        "days": [],
    }
    for i in range(days, 0, -1):
        d = d_today - timedelta(days=i)
        s = safe(lambda d=d: g.get_stats(iso(d)))
        s = s if isinstance(s, dict) and "__error__" not in s else {}
        dto, _raw = sleep_dto(g, d)
        sc = (dto.get("sleepScores") or {}) if dto else {}
        overall = (sc.get("overall") or {}) if isinstance(sc, dict) else {}
        secs = dto.get("sleepTimeSeconds") if dto else None
        out["days"].append({
            "date": iso(d),
            "sleep_h": round(secs / 3600, 1) if secs else None,
            "sleep_score": overall.get("value"),
            "resting_hr": s.get("restingHeartRate"),
            "steps": s.get("totalSteps"),
            "active_kcal": s.get("activeKilocalories"),
            "body_battery_high": s.get("bodyBatteryHighestValue"),
            "body_battery_low": s.get("bodyBatteryLowestValue"),
            "avg_stress": s.get("averageStressLevel"),
        })
    weight = safe(lambda: g.get_body_composition(iso(start), iso(d_today)))
    wseries = []
    if isinstance(weight, dict):
        for w in sorted((weight.get("dateWeightList") or []),
                        key=lambda e: e.get("calendarDate") or ""):
            wseries.append({
                "date": w.get("calendarDate"),
                "weight_g": w.get("weight"),
                "body_fat_pct": w.get("bodyFat"),
            })
    out["weight_series"] = wseries
    acts, coverage = _activity_history(g, start, d_today)
    out["activities"] = [_trim_activity(a) for a in acts]
    out["activities_coverage"] = coverage
    out["training_rhythm"] = _training_rhythm(g, d_today, acts, coverage)
    status = safe(lambda: g.get_training_status(iso(d_today)))
    if isinstance(status, dict) and "__error__" not in status:
        mr = status.get("mostRecentTrainingStatus") or {}
        mp = mr.get("latestTrainingStatusData") or {}
        latest = None
        if isinstance(mp, dict):
            for _k, v in mp.items():
                latest = v
                break
        vo2 = status.get("mostRecentVO2Max") or {}
        generic = (vo2.get("generic") or {}) if isinstance(vo2, dict) else {}
        out["training_status"] = {
            "status": (latest or {}).get("trainingStatus"),
            "acute_load": (latest or {}).get("acwrPercent")
            or (latest or {}).get("acuteTrainingLoadDTO"),
            "vo2max": generic.get("vo2MaxValue"),
        }
    return out


def _fmt_secs(s):
    """Seconds -> 'h:mm:ss' or 'm:ss' (for race-prediction times)."""
    if not isinstance(s, (int, float)) or s <= 0:
        return None
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# Garmin personal-record typeId -> (label, value-kind). kind drives formatting:
# 'time' = seconds, 'dist' = metres, 'count' = raw number.
_PR_TYPES = {
    1: ("Fastest 1 km", "time"),
    2: ("Fastest 1 mile", "time"),
    3: ("Fastest 5 km", "time"),
    4: ("Fastest 10 km", "time"),
    5: ("Fastest Half Marathon", "time"),
    6: ("Fastest Marathon", "time"),
    7: ("Longest Run", "dist"),
    8: ("Longest Ride", "dist"),
    9: ("Longest e-Bike Ride", "dist"),
    10: ("Best Avg Power", "count"),
    11: ("Most Ascent (Ride)", "dist"),
    12: ("Most Steps in a Day", "count"),
    13: ("Most Steps in a Week", "count"),
    14: ("Most Steps in a Month", "count"),
    15: ("Longest Goal Streak (days)", "count"),
    16: ("Longest Activity Streak (days)", "count"),
}


def _format_prs(pr_list):
    """Turn Garmin's raw personal-record list into labelled, human-readable records."""
    out = []
    for p in pr_list:
        if not isinstance(p, dict):
            continue
        tid = p.get("typeId")
        val = p.get("value")
        label, kind = _PR_TYPES.get(tid, (f"Record #{tid}", "count"))
        pretty = val
        if isinstance(val, (int, float)):
            if kind == "time":
                pretty = _fmt_secs(val)
            elif kind == "dist":
                pretty = f"{val / 1000:.2f} km"
            else:
                pretty = int(round(val))
        out.append({
            "record": label,
            "value": pretty,
            "date": (p.get("prStartTimeGmtFormatted") or "")[:10] or None,
        })
    return out


def fitness_profile(d_today=None, g=None, refresh=False):
    """Slow-changing performance metrics compatible Garmin devices compute but the daily
    snapshot skips: fitness age, race predictions, endurance & hill score,
    VO2max, lactate-threshold HR, cycling FTP, weekly intensity minutes.
    Meant to be cached ~daily by the bot, not fetched on every message."""
    if d_today is None:
        d_today = date.today()
    elif isinstance(d_today, str):
        d_today = date.fromisoformat(d_today)
    try:
        g = g or client()
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"login failed: {exc}"}
    today = iso(d_today)
    week = iso(d_today - timedelta(days=7))
    prof = {"schema_version": 2, "generated_at": datetime.now().isoformat(timespec="seconds")}

    fa = safe(lambda: g.get_fitnessage_data(today))
    if isinstance(fa, dict) and "__error__" not in fa:
        f_age = fa.get("fitnessAge")
        ach = fa.get("achievableFitnessAge")
        prof["fitness_age"] = {
            "chronological": fa.get("chronologicalAge"),
            "fitness_age": round(f_age, 1) if isinstance(f_age, (int, float)) else None,
            "achievable": round(ach, 1) if isinstance(ach, (int, float)) else None,
        }

    rp = safe(lambda: g.get_race_predictions())
    if isinstance(rp, dict) and "__error__" not in rp:
        prof["race_predictions"] = {
            "5K": _fmt_secs(rp.get("time5K")),
            "10K": _fmt_secs(rp.get("time10K")),
            "half_marathon": _fmt_secs(rp.get("timeHalfMarathon")),
            "marathon": _fmt_secs(rp.get("timeMarathon")),
        }

    history_start = iso(d_today - timedelta(days=28))
    for kind in ("hill", "endurance"):
        def load_score(kind=kind):
            daily = safe(lambda: getattr(g, "get_" + kind + "_score")(today))
            history = safe(lambda: getattr(g, "get_" + kind + "_score")(history_start, today))
            return metrics.score_metric(daily, kind, d_today, history)
        prof[kind + "_score"] = _cached_metric(
            f"{kind}_score", load_score, refresh=refresh, context=today)

    def load_lactate():
        latest = safe(lambda: g.get_lactate_threshold())
        history = safe(lambda: g.get_lactate_threshold(latest=False, start_date=history_start, end_date=today))
        return metrics.lactate_metric(latest, d_today, history)

    prof["running_lactate_threshold"] = _cached_metric(
        "running_lactate_threshold", load_lactate, refresh=refresh, context=today)
    shr = prof["running_lactate_threshold"].get("measurements", {}).get("heart_rate_bpm", {})
    if shr.get("value") is not None:
        prof["lactate_threshold_hr"] = shr["value"]

    ftp = safe(lambda: g.get_cycling_ftp())
    if isinstance(ftp, dict) and ftp.get("functionalThresholdPower"):
        prof["cycling_ftp"] = {
            "watts": ftp.get("functionalThresholdPower"),
            "stale": ftp.get("isStale"),
            "as_of": (ftp.get("calendarDate") or "")[:10],
        }

    im = safe(lambda: g.get_intensity_minutes_data(today))
    if isinstance(im, dict) and "__error__" not in im:
        prof["weekly_intensity_minutes"] = {
            "total": im.get("weeklyTotal"),
            "goal": im.get("weekGoal"),
            "moderate": im.get("weeklyModerate"),
            "vigorous": im.get("weeklyVigorous"),
            "goal_met_on": im.get("dayOfGoalMet") or None,
        }

    pr = safe(lambda: g.get_personal_record())
    if isinstance(pr, list) and pr:
        prof["personal_records"] = _format_prs(pr)
    return prof


def _parse_exercise_sets(xs):
    """Backward-compatible grouped summaries; first row owns the complete set_sequence.

    `order` means first appearance, not chronology. set_sequence preserves every ACTIVE,
    REST and unknown set, sorted when all timestamps parse; set_indices indexes it.
    """
    if not isinstance(xs, dict) or "__error__" in xs:
        return None
    unit_policy = _strength_weight_policy()
    sets = xs.get("exerciseSets") or []
    if not isinstance(sets, list):
        return None
    indexed = list(enumerate(sets))
    chronology = "api_order_timestamps_incomplete"
    try:
        def start_epoch(pair):
            dt = datetime.fromisoformat(pair[1]["startTime"].replace("Z", "+00:00"))
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        indexed.sort(key=start_epoch)
        chronology = "chronological_start_time"
    except (ValueError, TypeError, KeyError, AttributeError):
        indexed = list(enumerate(sets))
    agg, order, sequence = {}, [], []
    for index, (source_index, s) in enumerate(indexed):
        if not isinstance(s, dict):
            sequence.append({"sequence_index": index, "source_index": source_index, "status": "invalid_set"})
            continue
        exs = [e for e in (s.get("exercises") or []) if isinstance(e, dict)]
        name = ((exs[0].get("name") or exs[0].get("category")) if exs else None) or "UNKNOWN"
        unit = s.get("weightUnit")
        if unit is None or unit == "":
            unit = xs.get("weightUnit")
        unit_key = unit.get("key") if isinstance(unit, dict) else unit
        missing_unit = unit is None or unit == ""
        effective_unit = unit_policy["unit"] if missing_unit else unit_key
        wt = s.get("weight")
        valid_weight = metrics.number(wt) and wt >= 0
        scale = {"g": .001, "gram": .001, "grams": .001, "kg": 1, "kilogram": 1,
                 "kilograms": 1, "lb": .45359237, "lbs": .45359237, "pound": .45359237}.get(
                     str(effective_unit).lower())
        configured = missing_unit and scale is not None
        kg = round(wt * scale, 3) if valid_weight and scale is not None else None
        detail = {
            "sequence_index": index, "source_index": source_index, "set_type": s.get("setType"),
            "start_time": s.get("startTime"), "duration_s": s.get("duration"),
            "exercise": name, "exercises": [{k: e[k] for k in ("category", "name", "probability") if k in e} for e in exs],
            "reps": s.get("repetitionCount"), "weight_raw": wt, "weight_unit": unit,
            "effective_weight_unit": effective_unit,
            "weight_kg": kg, "weight_status": "available" if valid_weight else "missing_or_sentinel",
            "weight_unit_status": "configured" if configured else "verified" if scale is not None else "unknown",
            "weight_unit_source": ("explicit_private_configuration" if configured else
                                   "payload" if scale is not None else "unknown"),
            "weight_unit_provenance": unit_policy["provenance"] if configured else None,
            "workout_step_index": s.get("wktStepIndex"), "message_index": s.get("messageIndex"),
        }
        sequence.append(detail)
        if s.get("setType") != "ACTIVE":
            continue
        if name not in agg:
            agg[name] = {"sets": 0, "reps": [], "top_kg": None, "indices": []}
            order.append(name)
        a = agg[name]
        a["sets"] += 1
        a["indices"].append(index)
        reps = s.get("repetitionCount")
        if metrics.number(reps) and reps >= 0:
            a["reps"].append(int(reps))
        if kg is not None:
            a["top_kg"] = kg if a["top_kg"] is None else max(a["top_kg"], kg)
    out = []
    for name in order:
        a = agg[name]
        reps = a["reps"]
        out.append({
            "order": len(out) + 1,
            "exercise": name.replace("_", " ").title(),
            "sets": a["sets"],
            "reps": (f"{min(reps)}-{max(reps)}" if reps and min(reps) != max(reps)
                     else (str(reps[0]) if reps else None)),
            "top_weight_kg": a["top_kg"],
            "set_indices": a["indices"],
        })
    if not out and sequence:
        out = [{"order": 1, "exercise": "No active sets", "sets": 0, "reps": None,
                "top_weight_kg": None, "set_indices": []}]
    if out:
        out[0]["set_sequence"] = sequence
        out[0]["sequence_order"] = chronology
    return out or None


def _strength_weight_policy():
    """Only an explicit account-scoped setting may supply a missing JSON weight unit."""
    supplied = os.environ.get("AGBOT_STRENGTH_WEIGHT_UNIT", "").strip().lower()
    unit = {"g": "g", "gram": "g", "grams": "g", "kg": "kg", "kilogram": "kg", "kilograms": "kg",
            "lb": "lb", "lbs": "lb", "pound": "lb"}.get(supplied)
    return {
        "unit": unit,
        "provenance": (os.environ.get("AGBOT_STRENGTH_WEIGHT_UNIT_PROVENANCE", "").strip()
                       or "Explicit private configuration; applicability must be verified for this account")
                      if unit is not None else None,
    }


def _load_sets_cache():
    try:
        with open(SETS_CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_sets_cache(cache):
    try:
        _atomic_write(SETS_CACHE, json.dumps(cache, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def _within_days(start_local, days):
    """True if a 'YYYY-MM-DD ...' local timestamp falls within the last `days` days.
    Unparseable/absent dates return True so we err on the side of refetching."""
    try:
        d = date.fromisoformat(str(start_local)[:10])
    except ValueError:
        return True
    return 0 <= (date.today() - d).days <= days


def _set_source_values(summaries):
    """Compare source measurements, not freshness or configured unit conversion."""
    sequence = (summaries or [{}])[0].get("set_sequence")
    if not isinstance(sequence, list) or not sequence:
        return None
    fields = ("source_index", "set_type", "start_time", "duration_s", "exercise",
              "exercises", "reps", "weight_raw", "weight_unit", "workout_step_index",
              "message_index")
    return [{key: row.get(key) for key in fields} for row in sequence]


def _sets_for(g, activity_id, cache=None, refresh=False, metadata=None, allow_fetch=True,
              force_refresh=False):
    """Return cached sets with provenance.

    refresh=True permits a six-hour TTL for editable workouts. force_refresh=True
    always requests this activity from Garmin, unless allow_fetch=False explicitly
    forbids network access. Empty/failed responses retain the last good source.
    data_changed is None when comparison is unavailable or no refresh succeeded.
    """
    key, now = str(activity_id), datetime.now(timezone.utc)
    unit_policy = _strength_weight_policy()
    entry = (cache or {}).get(key)
    legacy = isinstance(entry, list)
    previous = entry if legacy else entry.get("summaries") if isinstance(entry, dict) else None
    current = (isinstance(entry, dict) and entry.get("schema_version") == metrics.SCHEMA_VERSION
               and entry.get("weight_unit_policy") == unit_policy)
    fetched_at = entry.get("fetched_at") if isinstance(entry, dict) else None
    try:
        age = (now - datetime.fromisoformat(fetched_at)).total_seconds() / 3600
    except (ValueError, TypeError):
        age = None
    meta = {"status": "unavailable", "fetched": False, "fetched_at": fetched_at,
            "force_refresh": bool(force_refresh), "data_changed": None,
            "refresh_status": "not_requested",
            "cache_age_hours": round(age, 2) if age is not None else None,
            "schema_version": metrics.SCHEMA_VERSION, "sequence_complete": current and bool(previous),
            "weight_unit_policy": entry.get("weight_unit_policy") if isinstance(entry, dict) else None,
            "requested_weight_unit_policy": unit_policy, "weight_unit_policy_current": current}
    parsed = None
    # Recently editable entries have a short TTL, not one API request per chat message.
    cache_fresh = (not force_refresh and current
                   and (not refresh or (age is not None and 0 <= age < 6)))
    if previous and cache_fresh:
        parsed = previous
        meta["status"] = "available"
    elif not allow_fetch:
        parsed = previous
        meta.update(status="stale" if previous else "deferred", refresh_status="budget_deferred")
    else:
        xs = safe(lambda: g.get_activity_exercise_sets(activity_id))
        parsed = _parse_exercise_sets(xs)
        meta["fetched"] = True
        status = metrics.payload_status(xs)
        if parsed:
            fetched_at = now.isoformat()
            before, after = _set_source_values(previous), _set_source_values(parsed)
            changed = before != after if before is not None and after is not None else None
            meta.update(status="available", fetched_at=fetched_at, cache_age_hours=0, sequence_complete=True,
                        weight_unit_policy=unit_policy, weight_unit_policy_current=True,
                        data_changed=changed,
                        refresh_status=("changed" if changed else "unchanged")
                        if changed is not None else "fetched_without_comparable_baseline")
            if cache is not None:
                cache[key] = {"schema_version": metrics.SCHEMA_VERSION,
                              "fetched_at": fetched_at, "summaries": parsed,
                              "weight_unit_policy": unit_policy}
        else:
            meta.update(status="stale" if previous else (status if status != "available" else "unavailable"),
                        refresh_status=status if status != "available" else "unavailable",
                        schema_upgrade_pending=not current)
            parsed = previous
    if metadata is not None:
        metadata.update(meta)
    if parsed:
        # Annotate a copy; never contaminate the persisted last-good source with fallback state.
        parsed = copy.deepcopy(parsed)
        parsed[0]["data_freshness"] = dict(meta)
    return parsed


def exercise_sets(activity_id, force_refresh=False, metadata=None):
    """Per-exercise set summary for a strength activity (exercise, #sets, rep
    range, top weight in kg). None when the activity logs no sets.

    force_refresh checks this activity even inside the six-hour cache TTL. metadata
    receives refresh/fallback provenance and data_changed (True, False, or unknown).
    """
    login_error = None
    try:
        g = client()
    except Exception as exc:  # noqa: BLE001
        # Login failures follow the same last-good fallback contract as endpoint failures.
        class UnavailableClient:
            def get_activity_exercise_sets(self, _activity_id):
                return {"__error__": f"login failed: {login_error}"}
        login_error = str(exc)
        g = UnavailableClient()
    cache = _load_sets_cache()
    out = _sets_for(g, activity_id, cache, refresh=True, force_refresh=force_refresh,
                    metadata=metadata)
    _save_sets_cache(cache)
    return out if out or not login_error else {"__error__": f"login failed: {login_error}"}


def cmd_dump(args):
    snap = build_snapshot(args.date)
    print(json.dumps(snap, indent=2, default=str))
    return 4 if isinstance(snap, dict) and "__error__" in snap else 0


def cmd_fitness(args):
    prof = fitness_profile(args.date)
    print(json.dumps(prof, indent=2, default=str))
    return 4 if isinstance(prof, dict) and "__error__" in prof else 0


def cmd_sets(args):
    print(json.dumps(exercise_sets(args.activity_id), indent=2, default=str))
    return 0


def main():
    p = argparse.ArgumentParser(description="Garmin Coach data layer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("should-brief")
    sub.add_parser("mark-brief-sent")
    d = sub.add_parser("dump")
    d.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD (default today)")
    f = sub.add_parser("fitness")
    f.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD (default today)")
    st = sub.add_parser("sets")
    st.add_argument("activity_id", help="Garmin activityId of a strength workout")
    args = p.parse_args()

    if args.cmd == "should-brief":
        sys.exit(cmd_should_brief(args))
    elif args.cmd == "mark-brief-sent":
        sys.exit(cmd_mark_sent(args))
    elif args.cmd == "dump":
        sys.exit(cmd_dump(args))
    elif args.cmd == "fitness":
        sys.exit(cmd_fitness(args))
    elif args.cmd == "sets":
        sys.exit(cmd_sets(args))


if __name__ == "__main__":
    main()
