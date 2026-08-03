# Athlete Profile — YOUR NAME

_This file personalizes the coach. Copy it to `profile.md` and edit it to describe
yourself — goals, the equipment you can train with, and any constraints. The bot
injects this file into every prompt, and you can also ask the bot to update it for
you ("AgBot: add that I tweaked my knee")._

> Copy this file: `cp profile.example.md profile.md` (Windows: `Copy-Item profile.example.md profile.md`).
> `profile.md` is git-ignored so your personal details never get committed.

## Goals
- **Primary:** _e.g. reduce body fat while building lean muscle (body recomposition)._
- **Protein is the priority macro:** target = `calorie_budget.protein_target_g` (2.2 g/kg ≈ 1 g
  per pound), never below `protein_floor_g` (1.8 g/kg). These recompute from current weight —
  always quote them from the snapshot rather than a fixed remembered number. If something has to
  give on a day, drop carbs or fat, not protein.
- **Judge muscle retention by strength, not the scale:** _most scales report weight only
  (`body_fat_pct` null), so use `logged_sets` progression as the proxy — load/reps climbing while
  weight falls means recomposition is working._
- **Secondary:** _e.g. progress cardio fitness — build, don't just maintain: raise VO2max
  and move Garmin Training Status toward Productive (not stuck at Maintaining)._
- **Weight tracking:** _e.g. I weigh in every morning under the same conditions (fasted), so my
  `weight_trend_30d` is low-noise — trust the trend as the headline fat-loss progress signal._

## Training availability
- **N days per week** of intentional training.
- _Note anything about how you like to structure the week (e.g. often stack two short
  sessions in a day, prefer mornings, long run on weekends)._

## Equipment / modalities available
_List everything you can realistically train with — the more specific, the better the
programming. Delete what doesn't apply, add what does._

**Cardio machines:**
- _e.g. treadmill, stationary bike, rowing machine, elliptical, stair climber, ski erg_

**Strength:**
- _e.g. adjustable dumbbells 5–25 kg, barbell + plates, cable machine, pull-up bar,
  kettlebells, resistance bands_

**Studio / floor:**
- _e.g. yoga mat, foam roller, bands_

**Outdoor:**
- _e.g. can run/cycle outdoors anytime_

> Programming note for the coach: _optionally spell out how you want sessions built
> around your kit — e.g. "max dumbbell is 25 kg so use tempo / unilateral work for legs;
> use the rower and bike for Zone 2 and intervals."_

## Preferences & style
- _e.g. enjoy mixing cardio + strength; prefer structured sessions with explicit
  sets/reps/intervals over vague advice; hate burpees; etc._

## Constraints / injuries
- _None recorded yet. Add anything ongoing here (e.g. left knee, lower back) and the
  coach will adapt. The bot also tracks injuries/illness you report in chat._

## Current capability & load anchors
_Optional but recommended — tell the coach what you can ACTUALLY do, so it scales
watts / weights / paces to **you** instead of to Garmin's stored (and often stale) numbers._
- _e.g. **Cycling:** my real working FTP is ~180W (Garmin's stored FTP is old / too high) —
  anchor interval watts to that, not the device value._
- _e.g. **Strength:** working sets around <your weights>, always leaving 1–2 reps in reserve._
- _e.g. **Phase:** rebuilding after time off — progress in small steps (~5%), no all-out or
  to-failure efforts; RPE 8 with a little in reserve is my ceiling for now._
- _The coach also learns from what you report you actually did and re-anchors to it._

## Coaching principles (for the bot)
_Optional — how you want the coach to think. Sensible defaults below; edit to taste._
- **Adapt to readiness daily:** when training readiness is LOW or HRV is below baseline,
  prefer Zone 2 cardio, mobility or a lighter day rather than hard intervals.
- **Protect recovery:** flag short sleep, elevated resting HR, or high ACWR (>1.5) and
  dial intensity back.
- Progressive overload for strength; keep nutrition advice sustainable, never crash tactics.
- **Strength programming logic (evidence-based hypertrophy — think in WEEKLY volume):**
  - **Weekly sets-per-muscle is the lever:** target ~10-12 hard sets per major muscle per week
    (as few as 6 for small or still-recovering muscles; ~20 is the ceiling — growth flattens past
    ~10). Reason in WEEKLY sets-per-muscle, not just today's session: use the last ~7 days of
    logged workouts + `recent_activities_7d` to estimate what each muscle already got this week,
    then program today to fill the muscles that are UNDER target, not ones already there.
  - **Spread the volume:** hit each muscle across 2-3 sessions/week — splitting a muscle's weekly
    sets over 2+ days beats cramming it into one (up to ~30% more growth). Don't stack the same
    muscle two days running.
  - **Progressive overload — climb the reps → sets → weight ladder, over WEEKS not sessions:**
    a good session is NOT a reason to jump weight next time. Move ONE rung at a time, anchored to
    what they ACTUALLY did last time (CAPABILITY ANCHORS + logged_sets), never to what was merely
    recommended: (1) add reps toward the top of the range; (2) once they hit the top on all sets
    with 1-2 in reserve, add a working SET at the same weight and hold it a session or two; (3)
    only after they handle the higher rep+set volume comfortably for ~2 sessions, raise load ~5%
    and drop back to the bottom of the range. So 12 kg done well ≠ 14 kg next time — it means more
    reps, then a set, then weeks later a small load bump. Same idea on the bike: hold watts and
    extend the interval or add a rep before raising wattage.
  - **Calibrate to recommended-vs-actual:** they'll say what they actually used vs what was
    prescribed. Met/beat it easily → progress one rung; matched it at the right effort → repeat to
    consolidate; reduced it to finish (said 180 W, held 150 W; said 14 kg, did 12 kg) → that lower
    number is the new anchor, prescribe from THERE. Never leave them repeating an identical session,
    and never leap two rungs at once.
  - **Effort standard:** every working set should be genuinely hard — the last rep visibly slows
    and they couldn't get more than ~2-3 extra reps — while respecting their reps-in-reserve
    ceiling. An easy set (4+ reps left in the tank) is junk volume — bump reps or load next time.
- **Build, don't just maintain (if that's your goal):** on genuinely good-recovery days
  (GREEN, or a train-ready AMBER), deliberately include vigorous aerobic / VO2max intervals
  aimed at whatever Load Focus is under target — that's what moves Garmin Training Status
  toward Productive; easy or strength-only days hold VO2max but won't lift it. Skip it on
  RED / rest days — recovery wins.
- Always end a workout suggestion with a one-line safety note (warm up, stop if pain).
