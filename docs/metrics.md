# Garmin metric inventory and data contracts

This inventory describes the read-only data layer, not promises that every device or
account supplies every metric. Normalizers were checked against garminconnect **0.3.6**
and bounded API schema probes. No personal payloads, routes, account identifiers or
individual measurements belong in this document or test fixtures.

## Collection paths

| Path | Frequency and cost | Coverage |
|---|---|---|
| `build_snapshot()` | Existing daily/wellness endpoints; one shared client and bounded activity history | Up to six pages of 50 activities, then a disclosed boundary; no 15-activity output cutoff |
| Strength enrichment in snapshot | At most eight exercise-set refreshes; cached sessions beyond that remain available | Every returned strength activity has `logged_sets_coverage`; no silent three-session cutoff |
| `fitness_profile(d_today=None, g=None, refresh=False)` | Slow-changing profile. Hill, endurance and running LT normalized aggregates cached for 24 hours | Each score uses a precise single-day endpoint plus 28-day history; LT history contains separate HR/speed/power series |
| `activity_detail_metrics(id, g=None, refresh=False, include_details=True)` | On demand, cached 24 hours; one summary and optionally one bounded details call | 100 requested chart points and **zero** polyline points; only selected descriptors and aggregates are exposed |
| `activity_extras(...)` | On-demand detail metrics plus existing HR zones/weather | Client is shared within the call; weather/zone calls remain defensive |
| `build_weekly()` | On demand; bounded shared history helper | All fetched activities returned, with `activities_coverage` |
| `latest_activity()` / `trained_today()` | Minimal polling / training check | No daily performance endpoints added to polling; `trained_today` uses at most two activity pages |

New optional `metrics_cache.json` writes occur only when `AGBOT_DATA_DIR` is explicitly
set. State paths, including exercise sets and brief state, are resolved as
`os.path.join(os.environ.get("AGBOT_DATA_DIR", module_directory), "state")`.
`GARMINTOKENS` continues to select the existing credential directory; default
`~/.garminconnect`. None of these read endpoints updates Garmin or sends Telegram messages.

Metric cache schema is version 2. Old schemas are refetched. Empty/error refreshes never
replace last-good aggregates: callers receive the previous values with `status: stale`,
`cache.status: stale_fallback`, original `fetched_at`, current cache age and
`refresh_status`. Current measurement ages are recalculated even for cached values.
An activity-detail partial refresh can retain a prior complete result. Without a
previous result, partial/unavailable/error states remain visible.

Successful `fitness_profile()` results also carry top-level `schema_version: 2`, so
outer daily caches can reject older thin profiles. `build_snapshot(d_today=None)` keeps
its existing signature. Trimmed activity rows retain `activity_id`, `start`, `type`
and `duration_s`; classification and set coverage are additive.

## Metric inventory

“Collected” means code extracts the field **when present**, not that it is guaranteed.
“Device-dependent” means a compatible device, activity, sensor, enabled feature and/or
sufficient history is required. Missing values are not zero and do not establish rest.

| Metric / status | Source → output | Meaning, units and safeguards |
|---|---|---|
| Hill Score — collected, device-dependent | `get_hill_score(day)` and `hillScoreDTOList` from range → `fitness_profile.hill_score` | Garmin overall, strength and endurance scores; classification/feedback IDs remain raw. Latest **dated** observation is chosen, not last list position. Sorted dated series, observed-period change, measurement age and fetch statuses included. |
| Endurance Score — collected, device-dependent | `get_endurance_score(day)`, range `enduranceScoreDTO`, `groupMap` → `endurance_score` | Single-day exact score plus Garmin-supplied classification thresholds. Weekly `groupAverage`/`groupMax` are explicitly weekly aggregates, never relabelled daily measurements. |
| Running LT heart rate — collected, device-dependent | `get_lactate_threshold().speed_and_heart_rate.heartRate` → `running_lactate_threshold.measurements.heart_rate_bpm` | bpm, dated and source-labelled. Legacy `lactate_threshold_hr` scalar retained. Library also handles Garmin's historical `hearRate` typo. |
| Running LT speed / pace — raw speed collected; unlabelled pace currently unavailable | `speed_and_heart_rate.speed` | Observed endpoint has no speed unit. **Do not infer m/s from the field name or numeric magnitude.** Raw speed has `unit: null`, `unit_verified: false`. Only an explicitly recognized payload unit enables `pace_s_per_km`. |
| Running threshold power — collected, device-dependent | `power.functionalThresholdPower`, `powerToWeight` | W and W/kg, separately dated from speed/HR; Garmin `isStale` preserved. Latest HR, speed and power need not describe the same test. The client merges HR and speed under one date; that is API/client provenance, not proof of contemporaneous measurements. |
| Running LT trends — collected, device-dependent | Range `speed`, `heart_rate`, `power` lists with `from`, `until`, `value`, `updatedDate` | Separate series retain the source interval and update date. Speed unit remains unverified. No lab-test accuracy or causal improvement claim. |
| Fat Burner / “Fat Burned” — collected **Connect IQ estimate**, not native body composition | `get_activity().connectIQMeasurements` → `activity_detail_metrics.fuel_estimate` | Public app UUID `76ca8d3a-c186-4d65-8bf2-20971e02898b` identifies **Fat Burner**. FIT developer definitions verify session field 2 as **Total Fat (g)** and field 3 as **Total Carbs (g)**. Exact app/session-field identity and FIT/JSON value matches verify this mapping, not numeric suffixes. Output `fat_g` / `carbohydrate_g` are app-estimated exercise fuel use, never body-fat loss. Raw values are retained. |
| Other Connect IQ fields — collected raw, device/app-dependent | `connectIQMeasurements`, selected detail descriptors → `custom_fields`, `detail_descriptors` | Preserve app ID, field number, raw value, supplied label/unit and source. Summary developer field numbers and chart developer field numbers are **different namespaces**; matching a numeric suffix is unsafe. |
| Activity power — collected, sensor/device-dependent | `summaryDTO.averagePower`, `maxPower`, `minPower`, `normalizedPower` → `groups.power` | W. No power manufactured for sessions without a meter/watch estimate. Raw provider field names retained. |
| Running dynamics — collected, sensor/device-dependent | `groundContactTime`, `groundContactBalance`, `strideLength`, `verticalOscillation`, `verticalRatio`, `averageRunCadence` → `groups.running_dynamics` | Aggregate values retained. Unlabelled summary units stay unknown; detail descriptor units are separately preserved, not blindly assigned across endpoints. No gait diagnosis. |
| Stamina — collected, device-dependent | `beginPotentialStamina`, `endPotentialStamina`, `minAvailableStamina` → `groups.stamina` | Garmin modeled stamina indicators, not direct physiological capacity measurements. Missing values not interpreted as exhausted. |
| Self-evaluation — collected when supplied; often unavailable | `directWorkoutFeel`, `directWorkoutRpe`, `workoutFeel`, `workoutRpe`, `selfEvaluation`, `perceivedEffort` → `groups.self_evaluation` | User-entered/provider raw values. No assumed scale or fabricated RPE/RIR when absent. |
| Activity calories — collected | List summary plus `summaryDTO.calories`, `bmrCalories` → `groups.fuel_energy` | Raw source estimates; retain source fields. Do not equate calories with measured fat oxidation, add both sources together, or “eat back” a custom fuel estimate. |
| Strength sets — collected, activity-dependent | `get_activity_exercise_sets` → backward-compatible `logged_sets` list | Specific names, category alternatives, full ACTIVE/REST chronology, timestamps, reps, duration and raw weight/unit. Details below. |
| Training rhythm — derived with coverage | Activity history plus shared classifier → `training_rhythm` | Completed-day streak, confirmed rest and unknown days; no load threshold across all sports and no assuming missing pages are rest. |
| Activity basics — collected | Activity list → `recent_activities_7d` | Type/name/date, duration s, distance m, HR, calories, load, training effects, power, intensity minutes, estimated sweat, elevation, steps, cadence and speed when present. Each row includes classification/reason. |
| HR zones and weather — collected, activity-dependent | `get_activity_hr_in_timezones`, `get_activity_weather` → `activity_extras` | Zone minutes and low boundary bpm; weather absent indoors. Existing weather fields label Fahrenheit/mph. Not added to every snapshot. |
| Sleep / naps — collected, device-dependent | Sleep DTO → `last_night_sleep`, `naps_today` | Stages/duration, scores/subscores, need adjustments, stress, HR/HRV, breathing events, SpO2, respiration and skin-temperature deviation. Sentinels/missing readings are not real events. |
| Recovery / training load — collected, device-dependent | Readiness, HRV, status endpoints | Current vs morning readiness, recovery minutes/hours, acute/chronic load and ratio, load focus targets, VO2max. Device scores are contextual signals, not diagnoses. |
| Daily wellness — collected | Stats, summary, hydration, respiration, stress, Body Battery | Steps, energy expenditure, sedentary/activity totals, stress curve, respiration, hydration, Body Battery events and sync freshness. Cumulative sedentary hours are **not** proof of continuous recent sitting. |
| Weight/body composition — collected, scale-dependent | `get_body_composition` → weigh-in and trend | Latest weight, BMI, body-fat percentage, mass estimates, water percentage, observed weight change. Weight trend is not direct fat-loss measurement; short-term water variation matters. |
| Profile/performance — collected when supplied | Fitness age, race predictions, cycling FTP, PRs, intensity minutes, profile settings | Existing separate profile fields; FTP date/staleness, modeled race times, user training preferences and weekly goals. Predictions are not guaranteed outcomes. |
| Raw GPS/route, full charts — deliberately not exposed | Activity details may contain these | No polyline requested; raw payload not cached or included in prompts. Selected descriptor metadata only. |
| Direct body-fat loss from a workout — unavailable | No such validated measurement | Never derive body-fat change from Fat Burner, calories or training effect. |

Fat Burner provenance:
[Connect IQ listing](https://apps.garmin.com/apps/76ca8d3a-c186-4d65-8bf2-20971e02898b)
and [developer explanation](https://starttorun.info/fat-burner/).
The developer explains configurable energy/HR-zone assumptions. Field identity and
units were separately verified from ORIGINAL/FIT `field_description` messages linked
through `developer_data_id.application_id`, then matched to actual session records and
the corresponding JSON app ID, field definition number and identical value.

For this app, FIT **session** fields 2/3 are `Total Fat` / `Total Carbs`, both `g`.
FIT **record** fields 0/1 are `Fat Burned` / `Carbs Burned`, also `g`. The latter names
come from developer definitions; chart suffixes are not used to join these namespaces.
Only the verified session contract maps summary values to named gram estimates. Other
apps/field numbers or conflicting supplied names/units stay unverified. Missing,
negative, non-finite and duplicate values do not become named totals.

This source-verified mapping is implemented without a runtime FIT dependency or
runtime activity exports. The activity metric payload/cache key is version 3, so
older unlabelled cached results are refreshed; the top-level fitness profile and
generic persistence envelope remain version 2. No raw FIT files or account readings
are distributed with the code. A same-activity strength comparison can establish
account-scoped JSON unit evidence: match unique sets by exact timestamp, repetition
count and set type, corroborate duration, then compare the JSON weight to the named,
unit-bearing FIT weight field. A unit in the FIT profile or plausible numeric magnitude
alone is insufficient. This does not establish the unit for every client/account;
the generic missing-unit default remains unknown.

## Strength sequence and cache contract

`_parse_exercise_sets(payload)` still returns a list of per-exercise summaries:
`order`, `exercise`, `sets`, `reps`, `top_weight_kg` remain. `order` is **first appearance
of an exercise**, not the full workout sequence.

Additions:

* Each summary has `set_indices` into the full sequence.
* The **first summary only** owns `set_sequence`, containing every ACTIVE, REST or
  unknown set. This avoids duplicating the full workout once per exercise.
* Every detail retains `sequence_index`, original `source_index`, `set_type`,
  `start_time`, `duration_s`, specific `exercise`, name/category alternatives in
  `exercises`, `reps`, `weight_raw`, `weight_unit`, optional `weight_kg`, workout step
  and message indices.
* `sequence_order` is `chronological_start_time` when all timestamps parse; otherwise
  `api_order_timestamps_incomplete` explicitly declines to invent chronology.
* Missing and negative sentinel weights remain distinct from a recorded zero.
  Explicit recognized g/kg/lb payload units permit conversion. **A missing weight unit
  is not automatically grams**; absent the optional account-scoped configuration below,
  `top_weight_kg` stays null while the raw weight survives.
* REST-only payloads retain a zero-active-set summary, rather than lose the sequence.
* First-summary `data_freshness` and activity sibling `logged_sets_coverage` expose
  status, original fetch date, cache age, sequence coverage, schema version and
  refresh/defer outcome. Old grouped-only cache rows are refetched; failed migration
  returns the old summary explicitly marked stale and sequence-incomplete.

Recent edited sessions use a six-hour refresh TTL; older version-2 set sequences are
reused. A caller may force an on-demand metric refresh; strength edit refresh remains
TTL-bounded. Empty/failed set refresh does not erase the cache or return null in place
of a previously good list.

### Optional private strength unit policy

After verifying matched source records for the particular account, a private deployment
may set `AGBOT_STRENGTH_WEIGHT_UNIT` to `g`, `kg` or `lb` and optionally set
`AGBOT_STRENGTH_WEIGHT_UNIT_PROVENANCE` to its verification basis. For example, **only
for an account where the JSON-to-FIT comparison established grams**, configure `g`.
This is not the generic default and requires no runtime FIT dependency or export.

The policy fills **missing** JSON units only; supplied or unrecognized payload units
are never silently overwritten. The original `weight_unit` is preserved, while
`effective_weight_unit`, `weight_unit_source: explicit_private_configuration`,
`weight_unit_status: configured` and `weight_unit_provenance` explain the conversion.
`weight_kg` and the backward-compatible summary `top_weight_kg` then become usable for
progression. Invalid configuration values leave weights unconverted.

Set-cache entries include the unit/provenance policy. Enabling, changing or removing
it invalidates previously normalized summaries and triggers bounded refetching.
Fallback freshness separately reports the cached `weight_unit_policy`, the
`requested_weight_unit_policy`, and `weight_unit_policy_current`, so a stale result
cannot disguise the policy under which its weights were normalized.

## Activity classification and observed-day coverage

Public API: `classify_activity(activity, labels=None)` returns exactly:

```json
{"classification": "intentional_training", "reason": "recognized_training_type_no_load_threshold"}
```

`classification` is one of `intentional_training`, `active_recovery`, `transport`,
`unknown`. The reason is a machine-readable rule name. Raw and trimmed activities
are accepted. `_is_training_activity`, `_training_rhythm` and `trained_today` all use
the same rules.

Priority: configured activity ID → configured name substring → explicit `[training]`,
`[commute]`, `[recovery]` name tag → configured type → commute name → type default.
`AGBOT_ACTIVITY_LABELS` accepts JSON of this form:

```json
{
  "activity_ids": {"example-id": "intentional_training"},
  "name_labels": {"station commute": "transport"},
  "type_labels": {"pilates": "intentional_training"}
}
```

An e-bike ride defaults to transport, overrideable for deliberate workouts. Ordinary
cycling defaults to training **without claiming a commute is inferable from cycling
type**; explicit names/config are how commute purpose is conveyed. Walking, yoga,
Pilates and stretching default to active recovery, also overrideable. Recognized
strength counts without needing load or a universal duration minimum. Unrecognized/
`other`/transition recordings, including accidental short unknown activities, remain
unknown rather than automatically training. No arbitrary 50-load exception exists.

`recent_activities_7d_coverage` and `activities_coverage` contain requested range,
`returned`, `total`, `omitted`, `unknown`, `complete_from`, `complete_through`,
stop reason and pagination counts. `total`/`omitted` are null when unknown. Exhaustion
or a verified descending date boundary establishes coverage; caps, malformed rows,
repeated pages and errors do not establish missing days as rest. Strength detail
coverage separately reports total/returned/stale/unavailable-or-deferred.

`training_rhythm`:

* `completed_days_streak` counts backward from **yesterday**.
* `completed_days_streak_is_lower_bound` is true if an unknown day ends that count.
* `trained_today` is true / false / null (unknown), not proof today is a completed day.
* `projected_streak_if_training` adds one to the completed-day streak.
* Compatibility `consecutive_training_days` adds today only if training is observed.
* `rest_days_last_7` covers the seven **completed** days, and is null if any are unknown;
  confirmed-rest count and unknown-day count are separate.
* Active-recovery/transport-only days can be non-training rest days, not proof of zero
  exertion. Unknown activity classifications prevent rest inference.
* Observed activity days/range and source coverage are retained. A sync-empty day is
  only “no recorded training in this covered query”, not a claim nothing occurred.

Legacy `trained_today(min_duration_s=None)` returns a boolean, with unknown/error as
false. The old duration argument remains callable but no longer imposes an all-sports
minimum. Coverage-aware planners should use the tri-state snapshot value instead.

## Opt-in nutrition targets

`AGBOT_NUTRITION_GOAL` supports `maintain` and `fat_loss`; default `unspecified`.
Unsupported values (including `gain`) safely leave the goal unspecified and expose
`target_status: unsupported_goal`, rather than silently selecting weight loss.

`calorie_budget.maintenance_kcal` remains a separate provisional expenditure estimate,
with `maintenance_is_estimate: true` and `activity_factor_source`. It is **not** an
intake target. If the Garmin activity level is absent, the activity multiplier is
explicitly marked `provisional_default_not_observed`.

* Unspecified: `target_kcal`, deficit and protein target/floor/calories are **null**;
  `target_status: not_configured`. Do not calculate calorie “room” from maintenance.
* Maintain: `target_kcal` equals estimated maintenance, deficit zero, no automatic
  protein target. `goal_explicit: true`, `target_status: configured_estimate`.
* Fat loss: explicitly enables the prior guarded deficit and provisional
  resistance-training protein calculation. No automatic fat-loss intake target is
  generated for minors or an underweight BMI. Targets remain estimates, not mandatory
  diet prescriptions or a substitute for individualized clinical advice.

`nutrition_goal`, `goal_explicit` and `target_status` are additive snapshot fields.
Private deployments wanting a fat-loss target must explicitly configure that goal;
it is never the public default.

## Validation

Offline, synthetic stdlib tests (no login or production state writes):

```powershell
python -B -m unittest discover -s tests -p test_garmin_metrics.py -v
```
