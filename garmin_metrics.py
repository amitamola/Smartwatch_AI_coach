"""Pure Garmin payload normalization; no login, network, or persistence."""
import math
import re
from datetime import date, timedelta

SCHEMA_VERSION = 2
ACTIVITY_METRICS_VERSION = 3
FAT_BURNER_APP_ID = "76ca8d3a-c186-4d65-8bf2-20971e02898b"
# Verified from FIT field_description + developer_data_id and exact session/JSON value
# matches. These are session field numbers, NOT descriptor-key suffixes or record fields.
FAT_BURNER_SESSION_FIELDS = {
    2: ("Total Fat", "fat_g"),
    3: ("Total Carbs", "carbohydrate_g"),
}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def payload_status(payload):
    if isinstance(payload, dict) and "__error__" in payload:
        error = str(payload["__error__"]).lower()
        return "not_supported" if any(x in error for x in ("notsupported", "notimplemented", "404", "not found", "attributeerror")) else "error"
    return "unavailable" if not payload else "available"


def freshness(as_of, today):
    try:
        age = (today - date.fromisoformat(str(as_of)[:10])).days
    except (TypeError, ValueError):
        age = None
    return {"as_of": as_of, "age_days": age, "measurement_date_known": age is not None}


def _dated_rows(payload, wrapper):
    if not isinstance(payload, (dict, list)):
        return []
    if isinstance(payload, dict):
        payload = payload.get(wrapper, payload)
    if isinstance(payload, dict):
        payload = [payload]
    return [r for r in payload if isinstance(r, dict) and number(r.get("overallScore"))]


def score_metric(payload, kind, today, history=None):
    """Single-day and range payloads differ; never select a list's last row blindly."""
    wrapper = "hillScoreDTOList" if kind == "hill" else "enduranceScoreDTO"
    rows = _dated_rows(history, wrapper) + _dated_rows(payload, wrapper)
    dated = {str(r["calendarDate"]): r for r in rows if r.get("calendarDate")}
    rows = [dated[k] for k in sorted(dated)] or rows
    out = {"status": payload_status(payload), "source": "garmin.get_" + kind + "_score",
           "unit": "Garmin score", "series": [], "latest_fetch_status": payload_status(payload)}
    if not rows:
        if out["status"] == "available":
            out["status"] = "unavailable"
        return out
    latest = rows[-1]
    out.update(status="available", score=latest["overallScore"],
               **freshness(latest.get("calendarDate"), today))
    for raw, name in (("strengthScore", "strength"), ("enduranceScore", "endurance"),
                      ("classification", "classification"), ("hillScoreClassificationId", "classification"),
                      ("hillScoreFeedbackPhraseId", "feedback"), ("feedbackPhrase", "feedback")):
        if latest.get(raw) is not None:
            out[name] = latest[raw]
    bands = [(v, k.replace("classificationLowerLimit", ""))
             for k, v in latest.items() if k.startswith("classificationLowerLimit") and number(v)]
    if bands:
        out["level"] = next((name for limit, name in sorted(bands, reverse=True)
                             if latest["overallScore"] >= limit), "Novice")
    out["series"] = [{"date": r.get("calendarDate"), "score": r["overallScore"]}
                     for r in rows if r.get("calendarDate")]
    out["change_over_observed_period"] = (rows[-1]["overallScore"] - rows[0]["overallScore"]
                                           if len(out["series"]) > 1 else None)
    if isinstance(history, dict) and kind == "endurance":
        # /stats uses weekly aggregation. Do not mislabel groupMap as daily observations.
        groups = history.get("groupMap")
        if isinstance(groups, dict):
            out["weekly_aggregate_series"] = [
                {"week": k, **({"average": v.get("groupAverage"), "max": v.get("groupMax")}
                               if isinstance(v, dict) else {"average": v})}
                for k, v in sorted(groups.items()) if isinstance(v, dict) or number(v)]
    out["history_status"] = payload_status(history) if history is not None else "not_requested"
    return out


def lactate_metric(payload, today, history=None):
    out = {"status": payload_status(payload), "source": "garmin.get_lactate_threshold",
           "sport": "running", "measurements": {}}
    if out["status"] != "available" or not isinstance(payload, dict):
        return out
    shr = payload.get("speed_and_heart_rate") or payload
    power = payload.get("power") or {}
    if isinstance(shr, list):
        shr = max((r for r in shr if isinstance(r, dict)),
                  key=lambda r: str(r.get("calendarDate") or ""), default={})
    if not isinstance(shr, dict):
        shr = {}
    if isinstance(power, list):
        power = max((r for r in power if isinstance(r, dict)),
                    key=lambda r: str(r.get("calendarDate") or ""), default={})
    if not isinstance(power, dict):
        power = {}
    for raw, key, unit, row in (
        ("heartRate", "heart_rate_bpm", "bpm", shr),
        ("speed", "speed", shr.get("speedUnit"), shr),
        ("functionalThresholdPower", "power_w", "W", power),
        ("powerToWeight", "power_to_weight", "W/kg", power),
    ):
        value = row.get(raw)
        if key == "heart_rate_bpm" and value is None:
            value = row.get("hearRate")
        if not number(value) or value <= 0:
            continue
        measurement = {"value": value, "unit": unit, "unit_verified": unit is not None,
                       "source_field": raw, **freshness(row.get("calendarDate"), today)}
        if "isStale" in row:
            measurement["device_marked_stale"] = row["isStale"]
        out["measurements"][key] = measurement
    speed = out["measurements"].get("speed", {})
    conversion = {"m/s": 1, "mps": 1, "km/h": 1 / 3.6, "kph": 1 / 3.6, "km/min": 1000 / 60}
    factor = conversion.get(speed.get("unit")) if isinstance(speed.get("unit"), str) else None
    if factor and speed.get("value"):
        out["pace_s_per_km"] = round(1000 / (speed["value"] * factor), 1)
    out["status"] = "available" if out["measurements"] else "unavailable"
    out["speed_note"] = "Unlabelled API speed is raw; no magnitude-based unit inference or pace conversion."
    out["history_status"] = payload_status(history) if history is not None else "not_requested"
    out["trends"] = {}
    if isinstance(history, dict):
        for kind, unit in (("speed", None), ("heart_rate", "bpm"), ("power", "W")):
            rows = history.get(kind)
            if not isinstance(rows, list):
                continue
            points = [{"from": r.get("from"), "until": r.get("until"),
                       "updated_date": r.get("updatedDate"), "value": r["value"], "unit": unit}
                      for r in rows if isinstance(r, dict) and number(r.get("value"))]
            points.sort(key=lambda r: str(r.get("from") or ""))
            out["trends"][kind] = points
    return out


_ACTIVITY_FIELDS = {
    "power": ("averagePower", "maxPower", "minPower", "normalizedPower", "avgPower"),
    "running_dynamics": ("groundContactTime", "groundContactBalance", "strideLength",
                         "verticalOscillation", "verticalRatio", "averageRunCadence"),
    "stamina": ("beginPotentialStamina", "endPotentialStamina", "minAvailableStamina"),
    "self_evaluation": ("directWorkoutFeel", "directWorkoutRpe", "workoutFeel", "workoutRpe",
                        "selfEvaluation", "perceivedEffort"),
    "fuel_energy": ("calories", "bmrCalories"),
}


def _custom_field(field):
    record = {"app_id": field.get("appID"), "developer_field_number": field.get("developerFieldNumber"),
              "value": field.get("value"), "unit": field.get("unit"),
              "label": field.get("label") or field.get("name"),
              "source": "get_activity.connectIQMeasurements", "mapping_status": "unverified"}
    field_number = record["developer_field_number"]
    definition = (FAT_BURNER_SESSION_FIELDS.get(field_number)
                  if isinstance(field_number, int) and not isinstance(field_number, bool) else None)
    if record["app_id"] != FAT_BURNER_APP_ID or definition is None:
        return record
    name, quantity = definition
    unit = record["unit"]
    supplied_unit = unit.get("key") if isinstance(unit, dict) else unit
    if ((supplied_unit is not None and str(supplied_unit).lower() not in {"g", "gram", "grams"})
            or (record["label"] is not None and record["label"] != name)):
        record["mapping_status"] = "conflicts_with_verified_definition"
        return record
    record.update(
        label=name, unit="g", supplied_unit=unit, quantity=quantity,
        mapping_status="verified_fit_session_definition",
        unit_verified=True, field_namespace="session",
        mapping_source=("FIT field_description + developer_data_id.application_id; "
                        "session field identity and value matched to Connect IQ summary JSON"),
    )
    try:
        numeric = float(record["value"]) if not isinstance(record["value"], bool) else None
    except (TypeError, ValueError, OverflowError):
        numeric = None
    record["numeric_value"] = numeric if number(numeric) and numeric >= 0 else None
    record["value_status"] = "available" if record["numeric_value"] is not None else "invalid_or_missing"
    return record


def activity_metrics(summary, details=None):
    """Retain selected aggregates, not tracks, identity metadata or large chart series."""
    out = {"schema_version": ACTIVITY_METRICS_VERSION,
           "status": payload_status(summary), "source": "garmin.get_activity",
           "groups": {}, "custom_fields": [], "fuel_estimate": {"status": "unavailable"}}
    root = summary if isinstance(summary, dict) and "__error__" not in summary else {}
    dto = root.get("summaryDTO", root)
    if not isinstance(dto, dict):
        dto = {}
    descriptors = (details.get("metricDescriptors") or []) if isinstance(details, dict) else []
    for group, fields in _ACTIVITY_FIELDS.items():
        values = {k: {"value": dto[k], "unit": "W" if group == "power" else None,
                       "source_field": "summaryDTO." + k}
                  for k in fields if dto.get(k) is not None}
        out["groups"][group] = {"status": "available" if values else "unavailable", "values": values}
    for field in root.get("connectIQMeasurements") or []:
        if not isinstance(field, dict):
            continue
        # Chart developerFieldNumber is NOT the summary field number for this app.
        # Preserve the two namespaces separately instead of joining numeric suffixes.
        out["custom_fields"].append(_custom_field(field))
    fuel = [f for f in out["custom_fields"] if f["app_id"] == FAT_BURNER_APP_ID]
    if fuel:
        verified = [f for f in fuel if f["mapping_status"] == "verified_fit_session_definition"]
        quantities = {}
        for quantity in ("fat_g", "carbohydrate_g"):
            candidates = [f for f in verified if f.get("quantity") == quantity]
            quantities[quantity] = candidates[0]["numeric_value"] if len(candidates) == 1 else None
        out["fuel_estimate"] = {
            "status": "available", "app": "Fat Burner", "source": "Connect IQ",
            "app_id": FAT_BURNER_APP_ID, "fields": fuel,
            "field_mapping_status": ("verified_fit_session_definition" if len(verified) == len(fuel)
                                     else "partial" if verified else "unverified"),
            **quantities,
            "coverage": {"raw_fields": len(fuel), "verified_fields": len(verified),
                         "unavailable_named_totals": [k for k, v in quantities.items() if v is None]},
            "meaning": "App-estimated exercise fuel use from calories and configured heart-rate zones.",
            "safeguard": "Not measured body-fat loss; never subtract from weight or add to a calorie budget. "
                         "Named gram estimates require the verified app/session definition; "
                         "unknown or conflicting fields remain unverified.",
        }
    selected = [d for d in descriptors if isinstance(d, dict) and any(
        token in str(d.get("key", "")).lower()
        for token in ("power", "stamina", "vertical", "groundcontact", "stride", "workout", "connectiq"))]
    out["detail_descriptors"] = [
        {k: d[k] for k in ("key", "unit", "appID", "developerFieldNumber") if k in d}
        for d in selected]
    out["details_coverage"] = {
        "status": payload_status(details) if details is not None else "not_requested",
        **({k: details[k] for k in ("measurementCount", "metricsCount", "totalMetricsCount",
                                  "pendingData", "detailsAvailable") if k in details}
           if isinstance(details, dict) else {}),
        "chart_samples_exposed": False,
    }
    if out["status"] == "available" and not (
            any(group["values"] for group in out["groups"].values()) or out["custom_fields"]):
        out["status"] = "unavailable"
    return out


def classify_activity(activity, labels=None):
    """Return {classification, reason}; labels override semantic type defaults."""
    labels = labels if isinstance(labels, dict) else {}
    allowed = {"intentional_training", "active_recovery", "transport", "unknown"}
    if not isinstance(activity, dict):
        return {"classification": "unknown", "reason": "invalid_activity"}
    type_info = activity.get("activityType") or activity.get("activityTypeDTO") or {}
    typ = (type_info.get("typeKey") if isinstance(type_info, dict) else None) or activity.get("type") or ""
    typ = str(typ).lower()
    name = str(activity.get("activityName") or activity.get("name") or "").lower()
    aid = str(activity.get("activityId", activity.get("activity_id")))
    label_maps = {key: value if isinstance(value, dict) else {}
                  for key, value in ((key, labels.get(key)) for key in ("activity_ids", "name_labels", "type_labels"))}
    direct = label_maps["activity_ids"].get(aid)
    if direct in allowed:
        return {"classification": direct, "reason": "configured_activity_id"}
    for text, value in label_maps["name_labels"].items():
        if value in allowed and text and str(text).lower() in name:
            return {"classification": value, "reason": "configured_name_label"}
    for marker, value in (("[training]", "intentional_training"), ("[commute]", "transport"),
                          ("[recovery]", "active_recovery")):
        if marker in name:
            return {"classification": value, "reason": "explicit_name_tag"}
    direct = label_maps["type_labels"].get(typ)
    if direct in allowed:
        return {"classification": direct, "reason": "configured_type_label"}
    if re.search(r"\b(commute|commuting)\b|\b(to|from) (the )?(office|work)\b", name):
        return {"classification": "transport", "reason": "commute_name_label"}
    if typ in {"e_bike_fitness", "e_biking", "e_bike_mountain"}:
        return {"classification": "transport", "reason": "assisted_cycling_default_overrideable"}
    if typ in {"walking", "indoor_walking", "yoga", "pilates", "stretching"}:
        return {"classification": "active_recovery", "reason": "recovery_type_default_overrideable"}
    if typ in {"other", "unknown", "transition", ""}:
        return {"classification": "unknown", "reason": "unclassified_activity_not_assumed_training"}
    training = {"running", "treadmill_running", "trail_running", "track_running", "indoor_running",
                "cycling", "indoor_cycling", "virtual_ride", "road_biking", "mountain_biking",
                "strength_training", "cardio", "fitness_equipment", "elliptical",
                "stair_climbing", "rowing", "indoor_rowing", "swimming", "lap_swimming",
                "open_water_swimming", "hiking", "hiit", "multi_sport"}
    if typ in training:
        return {"classification": "intentional_training", "reason": "recognized_training_type_no_load_threshold"}
    return {"classification": "unknown", "reason": "unrecognized_type"}


def training_rhythm(activities, today, coverage, labels=None):
    active, ambiguous, observed = set(), set(), set()
    for a in activities:
        day = str(a.get("startTimeLocal") or a.get("start") or "")[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        if day > today.isoformat():
            continue
        observed.add(day)
        kind = classify_activity(a, labels)["classification"]
        if kind == "intentional_training":
            active.add(day)
        elif kind == "unknown":
            ambiguous.add(day)
    complete_start = coverage.get("complete_from")
    complete_end = coverage.get("complete_through")

    def state(day):
        key = day.isoformat()
        if key in active:
            return "training"
        if key in ambiguous:
            return "unknown"
        if complete_start and complete_end and complete_start <= key <= complete_end:
            return "rest"
        return "unknown"

    cursor, streak = today - timedelta(days=1), 0
    while state(cursor) == "training":
        streak += 1
        cursor -= timedelta(days=1)
    statuses = { (today - timedelta(days=i)).isoformat(): state(today - timedelta(days=i))
                 for i in range(1, 8)}
    trained = state(today)
    trained = True if trained == "training" else False if trained == "rest" else None
    known_rests = [day for day, status in statuses.items() if status == "rest"]
    return {
        "completed_days_streak": streak,
        "completed_days_streak_is_lower_bound": state(cursor) == "unknown",
        "trained_today": trained,
        "projected_streak_if_training": streak + 1,
        "consecutive_training_days": streak + (1 if trained else 0),
        "last_rest_day": cursor.isoformat() if state(cursor) == "rest" else (max(known_rests) if known_rests else None),
        "rest_days_last_7": len(known_rests) if "unknown" not in statuses.values() else None,
        "confirmed_rest_days_last_7": len(known_rests),
        "unknown_days_last_7": sum(s == "unknown" for s in statuses.values()),
        "completed_day_statuses": statuses, "today_is_completed_day": False,
        "observed_activity_days": sorted(observed),
        "observed_range": [min(observed), max(observed)] if observed else None,
        "coverage": coverage,
    }
