#!/usr/bin/env python3
"""AgBot - a personal Garmin coaching Telegram bot.

Long-polls Telegram and, for each message from the owner:
  * "GMS" / "AgBot: GMS" / "summary" / "/gms"  -> morning summary
  * anything else                              -> a coaching Q&A answer

Both are produced by pulling a fresh Garmin snapshot (garmin_coach.build_snapshot)
and asking a language-model backend (run_llm - the GitHub Copilot CLI by default;
swap in OpenAI / Anthropic / Ollama, see run_llm below) to write the reply. The
reply text is sent back to Telegram. No cloud services beyond the model provider
and Telegram are required.

If the owner has not received a summary by ~09:30 local time, one is pushed
automatically (once per day).

Runs as a single persistent process (see scripts/run_bridge.ps1 / run_bridge.sh).
A localhost port lock guarantees only one instance polls at a time.

Manual test modes (no Telegram needed):
  python telegram_bridge.py --selftest-summary
  python telegram_bridge.py --selftest-qa "how did I sleep?"
"""
import os
import re
import sys
import json
import hashlib
import time
import socket
import logging
import tempfile
import subprocess
import threading
import http.client
from logging.handlers import RotatingFileHandler
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.expanduser(os.environ.get("AGBOT_DATA_DIR", BASE)))
# Test-mode isolation is established before any directories, logs or state are opened.
_selftest_dir = None
if any(arg.startswith("--selftest-") for arg in sys.argv):
    _selftest_dir = tempfile.TemporaryDirectory(prefix="coach-selftest-")
    original_profile = os.environ.get("AGBOT_PROFILE", os.path.join(DATA_DIR, "profile.md"))
    os.environ["AGBOT_PROFILE"] = original_profile
    DATA_DIR = _selftest_dir.name
    os.environ["AGBOT_DATA_DIR"] = DATA_DIR
STATE = os.path.join(DATA_DIR, "state")
LOGS = os.path.join(DATA_DIR, "logs")
os.makedirs(STATE, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)

TOKEN_FILE = os.path.join(STATE, "telegram_token.txt")
OWNER_FILE = os.path.join(STATE, "telegram_chat_id.txt")
OFFSET_FILE = os.path.join(STATE, "tg_offset.txt")
LAST_SUMMARY_FILE = os.path.join(STATE, "last_summary_date.txt")
HISTORY_FILE = os.path.join(STATE, "conversation.json")
TODAYS_BRIEF_FILE = os.path.join(STATE, "todays_brief.json")
PROMPTS = os.path.join(BASE, "prompts")
PROFILE_FILE = os.environ.get("AGBOT_PROFILE", os.path.join(DATA_DIR, "profile.md"))
SUMMARY_PROMPT_FILE = os.path.join(PROMPTS, "summary_prompt.md")
QA_PROMPT_FILE = os.path.join(PROMPTS, "qa_prompt.md")
IMAGE_PROMPT_FILE = os.path.join(PROMPTS, "image_prompt.md")
WEEKLY_PROMPT_FILE = os.path.join(PROMPTS, "weekly_prompt.md")
NUTRITION_PROMPT_FILE = os.path.join(PROMPTS, "nutrition_prompt.md")
DEBRIEF_PROMPT_FILE = os.path.join(PROMPTS, "debrief_prompt.md")
PERF_PROMPT_FILE = os.path.join(PROMPTS, "performance_prompt.md")
PROGRAM_PROMPT_FILE = os.path.join(PROMPTS, "program_review_prompt.md")
JOURNAL_FILE = os.path.join(STATE, "journal.jsonl")
HEALTH_FILE = os.path.join(STATE, "health.jsonl")
ANCHOR_FILE = os.path.join(STATE, "anchors.jsonl")
LAST_ACTIVITY_FILE = os.path.join(STATE, "last_activity_id.txt")
PENDING_DEBRIEF_FILE = os.path.join(STATE, "pending_debrief.json")
RED_FLAGS_FILE = os.path.join(STATE, "red_flags_date.txt")
MEAL_STATE_FILE = os.path.join(STATE, "meal_reminders.json")
HYDRATION_STATE_FILE = os.path.join(STATE, "hydration_reminders.json")
EXERCISE_STATE_FILE = os.path.join(STATE, "exercise_adherence.json")
FITNESS_FILE = os.path.join(STATE, "fitness_profile.json")
INCOMING = os.path.join(STATE, "incoming")
os.makedirs(INCOMING, exist_ok=True)

COPILOT_EXE = os.environ.get("COPILOT_EXE", "copilot")
COPILOT_HOME = os.environ.get("COPILOT_HOME", os.path.expanduser("~/.copilot"))

# Which model/agent backend generates replies. "copilot" = GitHub Copilot CLI
# (default, works out of the box). See run_llm() to enable openai / anthropic / ollama.
LLM_BACKEND = os.environ.get("AGBOT_LLM", "copilot")
# Optional: the user's first name, used only so the coach can greet by name. All real
# personalization lives in profile.md - this is just a convenience override.
USER_NAME = os.environ.get("AGBOT_USER_NAME", "").strip()
# Pin the Copilot CLI model + reasoning effort (only used by the "copilot" backend).
# Leave AGBOT_MODEL unset to use the CLI's default model; set it to any id shown by
# `/model` in an interactive `copilot` session (e.g. "gpt-5.6-luna", "claude-sonnet-4.5",
# or "auto"). AGBOT_REASONING_EFFORT is one of none|minimal|low|medium|high|xhigh|max.
COPILOT_MODEL = os.environ.get("AGBOT_MODEL", "").strip()
COPILOT_FALLBACK_MODELS = tuple(dict.fromkeys(
    model.strip() for model in os.environ.get("AGBOT_FALLBACK_MODELS", "").split(",")
    if model.strip()))
COPILOT_REASONING_EFFORT = (os.environ.get("AGBOT_REASONING_EFFORT", "").strip() or "medium")
PROGRAM_ENABLED = os.environ.get("AGBOT_PROGRAM_ENABLED", "false").strip().lower() == "true"
PROGRAM_REVIEW_DAYS = int(os.environ.get("AGBOT_PROGRAM_REVIEW_DAYS", "7"))
PROGRAM_BLOCK_DAYS = int(os.environ.get("AGBOT_PROGRAM_BLOCK_DAYS", "28"))
PROGRAM_REVIEW_TIMEOUT = int(os.environ.get("AGBOT_PROGRAM_REVIEW_TIMEOUT", "180"))
PROGRAM_POLICY_VERSION = 3
NUTRITION_OPTIONAL = os.environ.get("AGBOT_NUTRITION_OPTIONAL", "true").lower() == "true"
MEAL_REMINDERS_ENABLED = os.environ.get("AGBOT_MEAL_REMINDERS", "false").lower() == "true"
PROMPT_MAX_CHARS = int(os.environ.get("AGBOT_PROMPT_MAX_CHARS", "200000"))
NUTRITION_POLICY = (
    "Food logging is optional. Missing photos, meals, calories or protein logs are NOT "
    "evidence of inadequate intake. Never alter workouts, activity, recovery or other "
    "recommendations based on calorie estimates, food-log coverage or an apparent logged "
    "protein shortfall. Do not chase meals, require a complete diary, or prescribe calorie "
    "compensation. Provide nutritional estimates when requested or food is voluntarily "
    "reported. A general assurance of healthy eating is a preference, not measured daily "
    "intake. Independent symptoms, actual effort and user requests still matter."
)

# Auto-summary window (local time). If no summary has been sent yet today and the
# current time falls in this window, one is pushed automatically. Between AUTO_START
# and AUTO_SLEEP_WAIT_UNTIL we wait for overnight sleep to sync first.
AUTO_START = (9, 30)
AUTO_SLEEP_WAIT_UNTIL = (11, 30)
AUTO_SLEEP_POLL_SECS = 300
AUTO_END = (20, 0)

# Daily meal-logging reminders (local time): (slot, hour, minute, message). Each fires
# once per day, guarded by a per-slot date marker in MEAL_STATE_FILE. If the bot was down
# at the scheduled minute it still nudges within MEAL_CATCHUP_MIN; past that it skips the
# slot for the day so you never get a stale ping hours late.
MEAL_REMINDERS = (
    ("breakfast", 8, 30,
     "\U0001F373 AgBot reminder \u00B7 Breakfast - log what you eat so I can track your "
     "fuel & protein through the day. Reply e.g. 'log: 3 eggs, oats, coffee'."),
    ("lunch", 12, 15,
     "\U0001F957 AgBot reminder \u00B7 Lunch - time to log your midday meal. "
     "Reply e.g. 'log: chicken, rice & salad'."),
    ("dinner", 21, 0,
     "\U0001F37D\uFE0F AgBot reminder \u00B7 Dinner - log tonight's meal so today's intake "
     "is complete. Reply e.g. 'log: salmon, potatoes, veg'."),
)
MEAL_CATCHUP_MIN = 90  # still nudge if the bot came online within this window after the time

# Hydration nudges: a light "drink water" reminder every HYDRATION_EVERY_H hours, on the hour,
# from HYDRATION_START to HYDRATION_END local (inclusive). Logging is done on the watch, so
# these are pure nudges - deduped per slot per day in HYDRATION_STATE_FILE, with a catch-up
# guard (shorter than the gap) so a restart never double-pings or fires a stale slot late.
HYDRATION_START = 8
HYDRATION_END = 22
HYDRATION_EVERY_H = 2
HYDRATION_CATCHUP_MIN = 55
HYDRATION_MESSAGES = (
    "\U0001F4A7 AgBot \u00B7 Water check - take a few good sips now. Staying topped up "
    "helps energy, recovery and focus.",
    "\U0001F4A7 AgBot \u00B7 Hydration nudge - grab a glass of water. Little and often "
    "beats gulping it all at once.",
    "\U0001F6B0 AgBot \u00B7 Time to hydrate - a cup of water now keeps you ahead of thirst.",
)

POLL_TIMEOUT = 50          # long-poll seconds
COPILOT_TIMEOUT = int(os.environ.get("AGBOT_LLM_TIMEOUT", "180"))  # seconds/generation (raise for slow high-reasoning models)
SINGLETON_PORT = 49517     # localhost lock so only one instance polls
MAX_STORED_TURNS = 500     # hard ceiling of conversation turns kept on disk (time-prune below keeps ~a week)
HISTORY_KEEP_DAYS = 8      # keep conversation turns on disk for this many days (covers the 7-day prompt window)
FITNESS_MAX_AGE_H = 20     # refresh cached fitness profile if older than this
HISTORY_PROMPT_DAYS = 7    # inject the last week of conversation into prompts (food, water, workouts, mood, plans)
HISTORY_PROMPT_TURNS = 300  # safety ceiling on injected turns after the day-window filter
HISTORY_PROMPT_CHARS = 16000  # char budget for injected history (~a week of chat; oldest truncated if over)
JOURNAL_PROMPT_CHARS = 6000  # durable notes injected into every prompt (a week+ of food / coach-plan notes)
JOURNAL_KEEP = 120          # journal lines kept on disk
PREF_FILE = os.path.join(STATE, "preferences.jsonl")
ACTIVITY_CHECK_SECS = 300    # how often to poll for a finished workout
DEBRIEF_QUIET_SECS = 90 * 60  # after the LAST logged activity, wait this long (no new
                              # activity) before the single collective debrief - a session is
                              # several back-to-back activities; debrief the whole thing once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(os.path.join(LOGS, "telegram_bridge.log"), encoding="utf-8",
                            maxBytes=5_000_000, backupCount=4),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("agbot")


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        rendered = super().format(record)
        rendered = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", rendered)
        return re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+)\b",
                      "[REDACTED]", rendered)


for _handler in logging.getLogger().handlers:
    _handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))

sys.path.insert(0, BASE)
import garmin_coach  # noqa: E402
import coach_clock
from coach_memory import MemoryStore
from coach_nutrition import NutritionStore, parse_food, midpoint, render_receipt
from coach_plan import TrainingStore, canonical_exercise, parse_plans, render_plans, unsupported_claims
from coach_program import ProgramStore, build_review_evidence, parse_program_review, render_program
from coach_runtime import RuntimeStore, SnapshotCache, message_key
from coach_context import (
    compact_workouts, plan_request_scope, schedule_claim_errors, strip_schedule_rendering,
)
from coach_corrections import correction_intent
from coach_rotation import RotationStore, load_training_policy, rotation_plan_errors, project_rotation
from telegram_formatting import md_to_html, html_chunks, html_to_plain as _html_to_plain

_memory_store = None
_training_store = None
_program_store = None
_runtime_store = None
_snapshot_cache = SnapshotCache(garmin_coach.build_snapshot,
                                int(os.environ.get("AGBOT_SNAPSHOT_TTL", "120")))
_current_request = None
_send_sequence = 0
_generation_sequence = 0
_worker_started = None
_last_progress = time.time()
_stop = threading.Event()
_delivery_lock = threading.Lock()
_poll_progress = None
_publisher_progress = None
_request_deadline = None
_scheduled_job = None
_draining = threading.Event()


def memory_store():
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore(os.path.join(STATE, "coach.sqlite3"))
        _memory_store.migrate_legacy(STATE)
    return _memory_store


def training_store():
    global _training_store
    if _training_store is None:
        _training_store = TrainingStore(os.path.join(STATE, "coach.sqlite3"))
    return _training_store


def nutrition_store():
    return NutritionStore(os.path.join(STATE, "coach.sqlite3"))


def runtime_store():
    global _runtime_store
    if _runtime_store is None:
        _runtime_store = RuntimeStore(os.path.join(STATE, "transport.sqlite3"))
    return _runtime_store


def program_store():
    global _program_store
    if _program_store is None:
        _program_store = ProgramStore(os.path.join(STATE, "coach.sqlite3"),
                                     review_days=PROGRAM_REVIEW_DAYS, block_days=PROGRAM_BLOCK_DAYS)
    return _program_store


def get_snapshot(force=False):
    started = time.monotonic()
    result = _snapshot_cache.get(force=force, allow_stale=True)
    if (isinstance(result, dict) and "__error__" not in result
            and not result.get("snapshot_cache", {}).get("stale")):
        training_store().record_activities(result.get("recent_activities_7d"))
        if PROGRAM_ENABLED:
            _rotation_context(reconcile=True)
    log.info("stage=snapshot seconds=%.2f cached_age=%s", time.monotonic() - started,
             (result.get("snapshot_cache") or {}).get("age_seconds")
             if isinstance(result, dict) else None)
    return result


def _available_snapshot(data):
    if not isinstance(data, dict) or "__error__" in data:
        log.warning("stage=snapshot unavailable; user reporting remains enabled")
        return {"sensor_status": "unavailable",
                "coverage_note": "Garmin could not be refreshed. Missing observations are unknown, not rest."}
    return data


def _correction_request_id(question):
    return _current_request or "correction:" + hashlib.sha256(
        (coach_clock.today().isoformat() + question).encode("utf-8")).hexdigest()


def _prepare_workout_correction(question):
    if not correction_intent(question):
        return None
    store = training_store()
    # Preserve old source values before a normal snapshot or targeted refresh can replace them.
    before = {str(a["activity_id"]): a for a in store.raw_activities()}
    get_snapshot()
    sources = {str(a["activity_id"]): a for a in store.raw_activities()}
    sources.update({aid: a for aid, a in before.items() if a.get("logged_sets")})
    history = load_history()
    previous = history[-1] if history else {}
    referents = previous.get("activity_ids") if previous.get("role") == "agbot" else None
    if referents and re.search(r"\b(?:that|it|those|them)\b", question, re.I):
        # These identifiers were attached by the application, not inferred from model prose.
        sources = {aid: a for aid, a in sources.items() if aid in referents}
    request_id = _correction_request_id(question)
    result = store.corrections.process_report(
        question, list(sources.values()), request_id=request_id,
        observed_on=_sent_backdate() or coach_clock.today().isoformat())
    refreshes = {}
    candidates = result["candidate_activity_ids"]
    if not result["replayed"] and len(candidates) <= 3:
        for aid in candidates:
            metadata = {}
            sets = garmin_coach.exercise_sets(aid, force_refresh=True, metadata=metadata)
            refreshes[aid] = metadata
            if isinstance(sets, list) and sets and aid in sources:
                updated = dict(sources[aid], logged_sets=sets, logged_sets_coverage=metadata)
                store.record_activities([updated])
        if candidates:
            _snapshot_cache.invalidate()
    elif len(candidates) > 3:
        refreshes["deferred"] = "More than three possible activities; clarify the workout first."
    log.info("stage=workout_correction saved=%s clarification=%s reason=%s",
             result["saved"], result["needs_clarification"], result["reason"])
    return {**result, "refreshes": refreshes}


# --------------------------------------------------------------------- helpers
def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


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


def write_file(path, text):
    _atomic_write(path, text)


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _defang_markers(text):
    """Neutralize bracketed control-marker syntax typed by the USER so it can never be
    reflected back through history and mis-parsed as a machine command (e.g. a user literally
    typing '[[HEALTH_CLEAR: all]]'). Inserts a zero-width space so the text looks unchanged
    but the marker regexes ('\\[\\[NAME...') no longer match."""
    if not text:
        return text
    return text.replace("[[", "[\u200b[").replace("]]", "]\u200b]")


def append_history(role, text, activity_ids=None):
    if role == "user":
        text = _defang_markers(text)
    hist = load_history()
    if _current_request and any(t.get("request_id") == _current_request
                               and t.get("role") == role and t.get("text") == text for t in hist):
        return
    hist.append({"role": role, "text": text,
                 "request_id": _current_request,
                 "ts": ((_MSG_SENT_AT if role == "user" and _MSG_SENT_AT else coach_clock.now())
                        .isoformat(timespec="seconds"))})
    if activity_ids:
        hist[-1]["activity_ids"] = [str(aid) for aid in activity_ids]
    hist = [t for t in hist if _within_days(t.get("ts"), HISTORY_KEEP_DAYS)]
    hist = hist[-MAX_STORED_TURNS:]
    try:
        _atomic_write(HISTORY_FILE, json.dumps(hist, ensure_ascii=False, indent=1))
    except Exception as exc:  # noqa: BLE001
        log.error("history save failed: %s", exc)


def clear_history():
    try:
        os.remove(HISTORY_FILE)
    except FileNotFoundError:
        pass


def save_todays_brief(text):
    """Persist today's morning brief (recovery read + workout/recommendations) so the
    coach can still reference 'the plan/recommendations' later in the day even after the
    volatile chat window has scrolled past it."""
    try:
        _atomic_write(TODAYS_BRIEF_FILE,
                      json.dumps({"date": coach_clock.today().isoformat(), "text": text},
                                 ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        log.error("brief save failed: %s", exc)


def todays_brief_text():
    """The brief sent earlier TODAY, or '' if none yet today (auto-expires by date)."""
    try:
        with open(TODAYS_BRIEF_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
    if isinstance(d, dict) and d.get("date") == coach_clock.today().isoformat():
        return (d.get("text") or "").strip()
    return ""


def _pending_jobs():
    """Keep dated jobs; the head remains readable by the previous release."""
    try:
        with open(PENDING_DEBRIEF_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except FileNotFoundError:
        return []
    if not isinstance(d, dict):
        raise ValueError("Invalid pending debrief state.")
    queued = d.pop("queued", [])
    if not d:
        return queued
    d.setdefault("date", datetime.fromtimestamp(
        d.get("last_ts", time.time()), coach_clock.zone()).date().isoformat())
    return [d, *queued]


def _load_pending():
    jobs = _pending_jobs()
    return jobs[0] if jobs else {}


def _write_pending_jobs(jobs):
    if jobs:
        jobs.sort(key=lambda job: job.get("last_ts", 0))
        _atomic_write(PENDING_DEBRIEF_FILE, json.dumps(dict(jobs[0], queued=jobs[1:])))
    else:
        try:
            os.remove(PENDING_DEBRIEF_FILE)
        except FileNotFoundError:
            pass


def _save_pending(d):
    jobs = {job["date"]: job for job in _pending_jobs()}
    day = d.get("date") or coach_clock.today().isoformat()
    previous = jobs.get(day, {})
    jobs[day] = {**previous, **d, "date": day}
    jobs[day]["user_requested"] = bool(previous.get("user_requested") or d.get("user_requested"))
    _write_pending_jobs(list(jobs.values()))


def _clear_pending(day=None):
    day = day or coach_clock.today().isoformat()
    _write_pending_jobs([job for job in _pending_jobs() if job["date"] != day])


_REVIEW_CUES = (
    "how did i do", "how'd i do", "how did that go", "how did it go", "did i do well",
    "did i follow", "follow the plan", "follow your plan", "stick to the plan",
    "did i stick", "follow the recommendation", "follow your recommendation",
    "how was my", "how was that", "how was the workout", "how was the session",
    "how did my workout", "how did my session", "rate my", "grade my", "assess my",
    "review my", "debrief", "did i hit", "did i nail", "did i complete", "did i cover",
    "how was training", "was that a good",
)


def _is_workout_review(q):
    """True if the user is asking how they did / whether they followed the plan, so we can
    cancel the pending auto-debrief and not repeat the same feedback 90 min later."""
    ql = " " + (q or "").lower() + " "
    return any(cue in ql for cue in _REVIEW_CUES)


def _day_tag(date_str):
    """Relative-day label so the model never has to do date math to place a dated entry
    ('today' / 'yesterday' / 'N days ago'). Empty for unparseable or future dates."""
    try:
        d = date.fromisoformat((date_str or "")[:10])
    except ValueError:
        return ""
    delta = (coach_clock.today() - d).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "yesterday"
    if delta > 1:
        return str(delta) + " days ago"
    return ""


def _within_days(ts_str, days):
    """True if an ISO date/timestamp falls within the last `days` days (today = 0)."""
    try:
        d = date.fromisoformat((ts_str or "")[:10])
    except ValueError:
        return False
    return 0 <= (coach_clock.today() - d).days <= days


def recent_history_text():
    hist = load_history()
    hist = [t for t in hist if _within_days(t.get("ts"), HISTORY_PROMPT_DAYS)]
    hist = hist[-HISTORY_PROMPT_TURNS:]
    # Older user statements are more useful than repeatedly injecting long bot answers.
    last_assistants = {id(t) for t in [t for t in hist if t.get("role") != "user"][-2:]}
    hist = [t for t in hist if t.get("role") == "user" or id(t) in last_assistants]
    lines, used, omitted = [], 0, 0
    for turn in reversed(hist):
        who = ("You" if turn.get("role") == "user"
               else "AgBot (historical model advice, not evidence; may be incorrect)")
        raw = turn.get("ts") or ""
        short = raw[:16].replace("T", " ")
        tag = _day_tag(raw)
        stamp = ("[" + short + ((" " + tag) if tag else "") + "] ") if raw else ""
        line = stamp + who + ": " + (turn.get("text") or "").strip()
        if used + len(line) + 1 > HISTORY_PROMPT_CHARS:
            omitted += 1
            continue
        lines.append(line)
        used += len(line) + 1
    prefix = ("[User reports are evidence; past model explanations are not. "
              "Use the saved schedule and current metric definitions over old bot claims.]\n")
    if omitted:
        prefix += f"[Bounded context: {omitted} older/oversized turns omitted.]\n"
    return prefix + "\n".join(reversed(lines))


def journal_entry_id(entry):
    identity = [entry.get("request_id") or entry.get("ts"), entry.get("date"), entry.get("text")]
    return entry.get("entry_id") or "note:" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def journal_entries():
    legacy = [json.loads(line) for line in read_file(JOURNAL_FILE).splitlines() if line.strip()]
    return legacy + nutrition_store().entries()


def active_journal_entries(entries=None):
    entries = journal_entries() if entries is None else entries
    replaced = {e["replaces_entry_id"] for e in entries if e.get("replaces_entry_id")}
    return [e for e in entries if journal_entry_id(e) not in replaced
            and e.get("entry_type") != "supersession"]


def append_journal(text, on_date=None, replaces_entry_id=None):
    """Journal a durable note. `on_date` ('YYYY-MM-DD') back-dates the entry - used when the
    user reports a meal from an earlier day ('last night's dinner', shared the morning after).
    Defaults to today. The stored `ts` always stays the real capture time."""
    existing = journal_entries()
    target = next((e for e in existing if journal_entry_id(e) == replaces_entry_id), None)
    if replaces_entry_id and target is None:
        raise ValueError("The meal selected for correction was not found.")
    entry = {"date": on_date or (target["date"] if target else coach_clock.today().isoformat()),
             "text": text.strip(),
             "ts": coach_clock.now().isoformat(timespec="seconds"), "request_id": _current_request}
    if date.fromisoformat(entry["date"]) > coach_clock.today():
        raise ValueError("Future food consumption cannot be logged.")
    if replaces_entry_id:
        entry["replaces_entry_id"] = replaces_entry_id
    if _current_request:
        for old in existing:
            if (old.get("request_id") == _current_request and old.get("date") == entry["date"]
                    and old.get("text") == entry["text"]
                    and old.get("replaces_entry_id") == replaces_entry_id):
                return old
    if replaces_entry_id and replaces_entry_id not in {
            journal_entry_id(e) for e in active_journal_entries(existing)}:
        raise ValueError("This meal already has a correction; use its latest entry.")
    entry["entry_id"] = journal_entry_id(entry)
    with open(JOURNAL_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


_LOG_MARKER_RE = re.compile(r"\[\[LOG(?:\s+(\d{4}-\d{2}-\d{2}))?:\s*(.*?)\]\]",
                            re.IGNORECASE | re.DOTALL)
_LOG_REPLACES_RE = re.compile(r"\[\[LOG_REPLACES:\s*([A-Za-z0-9:_-]+)\s*\]\]", re.IGNORECASE)
_MODEL_LOG_RECEIPT_RE = re.compile(
    r"(?mi)^[ \t]*(?:\U0001F37D\uFE0F?[ \t]*)?(?:logged|updated log)"
    r"[ \t]*\u2713(?:[ \t]*\u00b7[^\n]*)?[ \t]*$")


def _extract_log_marker(text):
    """Pull a model-emitted [[LOG: ...]] food marker out of a reply.

    The qa / image prompts tell the model to append this marker whenever the user
    REPORTS actually eating or drinking something (any phrasing - no 'log:' prefix
    needed). An optional date - [[LOG 2026-07-29: ...]] - back-dates the entry to the
    day it was actually eaten, so a dinner reported the next morning doesn't land on
    today's tally. We strip it from the reply the user sees and return the note so the
    bridge can journal it. Returns (clean_text, note_or_None, on_date_or_None).
    """
    if not text:
        return text, None, None
    found = _LOG_MARKER_RE.findall(text)
    if not found:
        return text, None, None
    clean = _LOG_MARKER_RE.sub("", text).rstrip()
    note = " ".join(" ".join(n for _d, n in found).split()).strip()
    on_date = next((d for d, _n in found if d), None)
    return clean, (note or None), on_date


def _logged_confirmation(on_date=None, updated=False):
    """The 'logged' line appended after a meal is saved. Names the day whenever the entry
    was back-dated, so it's obvious it did NOT land on today's tally."""
    base = "\n\n\U0001F37D\uFE0F " + ("updated log" if updated else "logged") + " \u2713"
    today = coach_clock.today().isoformat()
    if not on_date or on_date == today:
        return base
    try:
        d = date.fromisoformat(on_date)
    except ValueError:
        return base
    label = "yesterday" if (coach_clock.today() - d).days == 1 else d.strftime("%a %d %b")
    return base + " \u00B7 " + label


# When the machine sleeps or drops off the network, Telegram holds the backlog and replays it
# on reconnect - so a photo sent at 23:57 can arrive hours later, the NEXT day. Processing
# time is therefore not when the user ate. We remember when the message being handled was
# actually SENT (Telegram's 'date' field) and treat that, not the wall clock, as "when".
# Only the serial coaching worker modifies this; polling/publishing do not.
_MSG_SENT_AT = None


def _set_msg_sent_at(ts):
    """Record the send time (unix seconds) of the message about to be handled."""
    global _MSG_SENT_AT
    try:
        _MSG_SENT_AT = datetime.fromtimestamp(ts, coach_clock.zone()) if ts else None
    except (TypeError, ValueError, OSError):
        _MSG_SENT_AT = None


def _sent_backdate():
    """'YYYY-MM-DD' when the current message was SENT on an earlier day than today - i.e. it
    was delivered late - else None. Used to file its food on the day it was actually eaten."""
    if not _MSG_SENT_AT:
        return None
    d = _MSG_SENT_AT.date()
    return d.isoformat() if d < coach_clock.today() else None


def _delayed_message_note():
    """A prompt block warning the model that this message is arriving late, so 'today',
    'tonight' and 'just had' inside it refer to the day it was SENT, not to now."""
    back = _sent_backdate()
    if not back:
        return None
    tag = _day_tag(back) or back
    return ("\nDELAYED MESSAGE - READ THIS FIRST: the message below was SENT at "
            + _MSG_SENT_AT.strftime("%Y-%m-%d %H:%M") + " (" + tag + ") and only reached "
            "you now, because the bot was offline in between. Interpret EVERY time word in "
            "it - 'today', 'tonight', 'this evening', 'just had' - RELATIVE TO WHEN IT WAS "
            "SENT, not to TODAY. Any food in it was eaten on " + back + ", so date the log "
            "marker `[[LOG " + back + ": ...]]` and do the calorie/protein maths against "
            "THAT day. Say which day you've credited it to.\n")


_REST_MARKER_RE = re.compile(r"\[\[REST_DAY\]\]", re.IGNORECASE)


def _extract_rest_marker(text):
    """Pull the model-emitted [[REST_DAY]] marker off the morning brief. The summary
    prompt appends it only when today's plan is a genuine rest / recovery day, so the
    bridge can switch the day's exercise check-ins to a gentle rest-aware note instead
    of nagging 'did you do your exercise?'. Returns (clean_text, is_rest)."""
    if not text or not _REST_MARKER_RE.search(text):
        return text, False
    return _REST_MARKER_RE.sub("", text).rstrip(), True


_PLAN_MARKER_RE = re.compile(r"\[\[PLAN:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)

_EXPLAN_MARKER_RE = re.compile(r"\[\[EXERCISE_PLAN:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)


def _extract_exercise_plan_marker(text):
    """Pull a model-emitted [[EXERCISE_PLAN: ...]] marker off a reply. The prompts append it
    when the user STATES what they intend to do about exercise today - including a tentative
    'planning to rest but might bike later, will see'. Returns (clean_text, plan_or_None)."""
    if not text:
        return text, None
    plans = _EXPLAN_MARKER_RE.findall(text)
    if not plans:
        return text, None
    clean = _EXPLAN_MARKER_RE.sub("", text).rstrip()
    plan = " ".join(" ".join(plans).split()).strip()
    return clean, (plan or None)


def _harvest_exercise_plan(text):
    """Mark today's exercise intent as STATED once the user has told the coach their plan, so
    the adherence check-ins stop asking something already answered in chat. Deliberately does
    NOT clear the pending auto-debrief: a tentative plan ('might ride this evening') may still
    become a real session, and that session should still get its wrap-up. Only upgrades from
    'pending' so a later done/skip is never overwritten. Returns the cleaned reply text."""
    text, plan = _extract_exercise_plan_marker(text)
    if plan and _exercise_status() == "pending":
        _set_exercise_status("stated")
        log.info("Exercise intent stated - check-ins off for today: %s", plan[:120])
    return text


def recent_journal_text():
    entries = []
    active = active_journal_entries()
    for e in active[-JOURNAL_KEEP:]:
        d = e.get("date", "")
        tag = _day_tag(d)
        label = d + ((" (" + tag + ")") if tag else "")
        if not (e.get("text") or "").startswith("[coach plan]"):
            entries.append((d, ("[entry_id=" + journal_entry_id(e) + "] " +
                               label + ": " + (e.get("text") or "")).strip()))
    selected, size, omitted = [], 0, 0
    for day, line in reversed(entries):
        if day != coach_clock.today().isoformat() and size + len(line) > JOURNAL_PROMPT_CHARS:
            omitted += 1
            continue
        selected.append(line)
        size += len(line) + 1
    prefix = "ACTIVE JOURNAL: superseded corrections are excluded; count each meal once.\n"
    if omitted:
        prefix += f"[{omitted} older journal records omitted; plans are in TRAINING_STATE.]\n"
    return prefix + "\n".join(reversed(selected)) if selected else ""

def active_health_text():
    """Injuries/illness the user reported and hasn't marked recovered - injected into EVERY
    prompt so no recommendation ignores them, and they don't scroll out of chat."""
    return memory_store().render("health")


def resolve_health(source_text="recovered"):
    return [r["text"] for r in memory_store().resolve_health(
        ["all"], source_text=source_text, allow_all=True)]
# Legacy markers are recognized only to prevent old control text leaking to Telegram.
_HEALTH_CLEAR_RE = re.compile(r"\[\[HEALTH_CLEAR:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)
_HEALTH_FLAG_RE = re.compile(r"\[\[HEALTH_FLAG:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)


def active_anchors_text(query=""):
    """Retrieve source-labelled capability records by entity, not just recency."""
    return memory_store().render("anchor", query=query, max_chars=8000, verified_only=True)


_ANCHOR_MARKER_RE = re.compile(r"\[\[ANCHOR:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)
# Defensive catch-all for ANY bracketed uppercase control tag (incl. markers added in future),
# used only as a final egress strip so nothing like [[FOO: ...]] can ever leak to the user.
_ANY_CONTROL_MARKER_RE = re.compile(r"\[\[[A-Z][A-Z0-9_]*(?:\s+\d{4}-\d{2}-\d{2})?(?:\s*:[^\]]*)?\]\]")

def active_prefs_text():
    """Active standing constraints are never silently omitted."""
    return memory_store().render("preference")


_PREF_MARKER_RE = re.compile(r"\[\[PREF:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)

def load_fitness_profile():
    """Return the cached slow-changing fitness profile (fitness age, race
    predictions, endurance/hill score, VO2max-driven metrics, FTP, weekly
    intensity minutes), refreshing at most once per FITNESS_MAX_AGE_H via a
    Garmin fetch. Falls back to the stale cache (or None) and never raises."""
    cached = None
    try:
        with open(FITNESS_FILE, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
    except Exception:  # noqa: BLE001
        cached = None
    if isinstance(cached, dict) and cached.get("schema_version") == 2:
        try:
            age_h = ((coach_clock.now()
                      - datetime.fromisoformat(cached.get("generated_at")))
                     .total_seconds() / 3600)
            if age_h < FITNESS_MAX_AGE_H:
                return cached
        except Exception:  # noqa: BLE001
            pass
    prof = garmin_coach.fitness_profile()
    if isinstance(prof, dict) and "__error__" not in prof:
        try:
            _atomic_write(FITNESS_FILE, json.dumps(prof, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            log.error("fitness cache save failed: %s", exc)
        return prof
    if isinstance(cached, dict):
        cached["cache_status"] = "stale: refresh failed"
        cached["refresh_error"] = prof.get("__error__") if isinstance(prof, dict) else "no response"
    return cached


def load_token():
    raw = os.environ.get("AGBOT_TELEGRAM_TOKEN", "").strip() or read_file(TOKEN_FILE)
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", raw)
    if not m:
        raise SystemExit(
            "No valid Telegram bot token found. Set AGBOT_TELEGRAM_TOKEN or put the "
            "token in " + TOKEN_FILE)
    return m.group(0)


def telegram_api():
    return "https://api.telegram.org/bot" + load_token()


def tg(method, params=None, timeout=60):
    url = telegram_api() + "/" + method
    data = urllib.parse.urlencode(params).encode("utf-8") if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(method, params, timeout=30):
    """POST that returns Telegram's JSON even on HTTP 4xx (never raises)."""
    url = telegram_api() + "/" + method
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error_code": getattr(exc, "code", 0),
                    "description": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "description": str(exc)}


def _strip_control_markers(text):
    """Final egress safety net: remove EVERY machine control marker ([[LOG]], [[REST_DAY]],
    [[PLAN]], [[EXERCISE_PLAN]], [[HEALTH_CLEAR]], [[ANCHOR]], and any future [[UPPER: ...]]
    tag) so none can ever leak into a user-facing Telegram message. Harvesting happens
    upstream; by the time we send, no marker should remain - this catches any path that
    forgot to strip one (e.g. the summary path)."""
    if not text:
        return text
    for rgx in (_LOG_MARKER_RE, _REST_MARKER_RE, _PLAN_MARKER_RE,
                _EXPLAN_MARKER_RE, _HEALTH_CLEAR_RE, _HEALTH_FLAG_RE, _ANCHOR_MARKER_RE,
                _PREF_MARKER_RE):
        text = rgx.sub("", text)
    text = _ANY_CONTROL_MARKER_RE.sub("", text)  # defensive catch-all for future markers
    return text.rstrip()


def send_message(chat_id, text, summary_date=None, expires_at=None, delivery_key=None):
    global _send_sequence
    text = _strip_control_markers(text)
    rendered = md_to_html(text)
    keys = []
    chunks = [chunk for chunk in html_chunks(rendered, 4000) if _html_to_plain(chunk).strip()]
    group = delivery_key or message_key(_current_request, _send_sequence + 1, chat_id, text)
    for index, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        _send_sequence += 1
        key = (f"{delivery_key}:{index}" if delivery_key
               else message_key(_current_request, _send_sequence, chat_id, chunk))
        payload = {"text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if summary_date:
            payload.update(_summary_date=summary_date, _summary_complete=index == len(chunks) - 1)
        runtime_store().enqueue(key, chat_id, payload, group_id=group, expires_at=expires_at)
        keys.append(key)
    return keys


def flush_outbox():
    if not _delivery_lock.acquire(blocking=False):
        return
    try:
        blocked_groups = set()
        for item in runtime_store().due_messages():
            if item.get("group_id") in blocked_groups:
                continue
            params = {k: v for k, v in item["payload"].items() if not k.startswith("_")}
            params["chat_id"] = item["chat_id"]
            response = _post("sendMessage", params)
            if (not response.get("ok") and response.get("error_code") == 400
                    and "parse" in str(response.get("description", "")).lower()):
                params.pop("parse_mode", None)
                params["text"] = _html_to_plain(params["text"])
                response = _post("sendMessage", params)
            if response.get("ok"):
                runtime_store().delivered(item["id"], response.get("result", {}).get("message_id"))
                if item["payload"].get("_summary_complete"):
                    write_file(LAST_SUMMARY_FILE, item["payload"]["_summary_date"])
            else:
                description = response.get("description", "Telegram rejected delivery")
                retry = (response.get("parameters") or {}).get("retry_after")
                permanent = response.get("error_code") in (400, 401, 403, 404)
                runtime_store().delivery_failed(item["id"], description, retry,
                                               permanent=permanent)
                log.error("stage=delivery permanent=%s error_code=%s",
                          permanent, response.get("error_code"))
                blocked_groups.add(item.get("group_id"))
                if response.get("error_code") == 429:
                    break
    finally:
        _delivery_lock.release()


def owner():
    v = os.environ.get("AGBOT_OWNER_CHAT_ID", "").strip() or read_file(OWNER_FILE).strip()
    return v or None


def set_owner(chat_id):
    write_file(OWNER_FILE, str(chat_id))
    log.info("Owner locked to chat_id %s", chat_id)


def get_offset():
    v = read_file(OFFSET_FILE).strip()
    return int(v) if v.lstrip("-").isdigit() else None


def set_offset(update_id):
    write_file(OFFSET_FILE, str(update_id))


def download_file(file_id):
    info = tg("getFile", {"file_id": file_id})
    fp = (info.get("result") or {}).get("file_path")
    if not fp:
        return None
    url = "https://api.telegram.org/file/bot" + load_token() + "/" + fp
    ext = os.path.splitext(fp)[1] or ".jpg"
    fd, dest = tempfile.mkstemp(prefix="attachment_", suffix=ext, dir=INCOMING)
    os.close(fd)
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception:
        os.remove(dest)
        raise
    return dest


# ---------------------------------------------------------------- transcription
WHISPER_MODEL_NAME = os.environ.get("AGBOT_WHISPER_MODEL", "base")
# Language hint: set to your spoken language ("en", "es", ...); "" to auto-detect.
WHISPER_LANG = os.environ.get("AGBOT_WHISPER_LANG", "en")
_whisper_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel  # lazy import keeps startup light
        _whisper_model = WhisperModel(WHISPER_MODEL_NAME, device="cpu",
                                      compute_type="int8")
    return _whisper_model


def transcribe_audio(path):
    """Local Whisper transcription of a voice/audio file. Returns the text, or
    None if nothing intelligible was found or the model is unavailable."""
    try:
        model = _get_whisper()
    except Exception as exc:  # noqa: BLE001 - e.g. faster-whisper not installed
        log.error("whisper unavailable: %s", exc)
        return None
    try:
        kwargs = {"beam_size": 5, "vad_filter": True}
        if WHISPER_LANG:
            kwargs["language"] = WHISPER_LANG
        segments, _info = model.transcribe(path, **kwargs)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        log.error("transcription failed: %s", exc)
        return None


# ------------------------------------------------------------------- video frames
VIDEO_MAX_FRAMES = int(os.environ.get("AGBOT_VIDEO_FRAMES", "6"))
VIDEO_FRAME_MAXDIM = 1024


def _downscale(img, max_dim=VIDEO_FRAME_MAXDIM):
    w, h = img.size
    m = max(w, h)
    if m <= max_dim:
        return img
    s = max_dim / float(m)
    return img.resize((max(1, int(w * s)), max(1, int(h * s))))


def extract_video_frames(path, max_frames=VIDEO_MAX_FRAMES):
    """Sample up to max_frames evenly-spaced JPEG frames from a video with PyAV.

    Returns a list of file paths (possibly empty). No external ffmpeg needed -
    PyAV bundles the decoders (the same reason voice transcription works)."""
    try:
        import av  # noqa: PLC0415 - heavy optional dep, imported lazily
    except Exception as exc:  # noqa: BLE001
        log.error("PyAV unavailable, cannot read video: %s", exc)
        return []
    try:
        container = av.open(path)
    except Exception as exc:  # noqa: BLE001
        log.error("could not open video: %s", exc)
        return []
    out = []
    try:
        vs = next((s for s in container.streams if s.type == "video"), None)
        if vs is None:
            return []
        if container.duration:
            total = container.duration / 1_000_000.0
        elif vs.duration and vs.time_base:
            total = float(vs.duration * vs.time_base)
        else:
            total = 0.0
        stamp = int(time.time())
        if total and total > 0.6 and vs.time_base:
            targets = [total * (i + 0.5) / max_frames for i in range(max_frames)]
            for i, t in enumerate(targets):
                try:
                    container.seek(int(t / vs.time_base), stream=vs, backward=True)
                    frame = next(container.decode(vs), None)
                    if frame is None:
                        continue
                    fp = os.path.join(INCOMING, "vf_%d_%02d.jpg" % (stamp, i))
                    _downscale(frame.to_image()).save(fp, "JPEG", quality=85)
                    out.append(fp)
                except Exception as exc:  # noqa: BLE001
                    log.warning("frame at %.1fs failed: %s", t, exc)
        else:  # short or unknown-duration clip: take the first frames decoded
            for frame in container.decode(vs):
                fp = os.path.join(INCOMING, "vf_%d_%02d.jpg" % (stamp, len(out)))
                _downscale(frame.to_image()).save(fp, "JPEG", quality=85)
                out.append(fp)
                if len(out) >= max_frames:
                    break
    finally:
        try:
            container.close()
        except Exception:  # noqa: BLE001
            pass
    return out


# ------------------------------------------------------------------ generation
def _scrub(text):
    """Drop any stray Copilot stats footer that slips past -s."""
    lines = text.rstrip().split("\n")
    for i, ln in enumerate(lines):
        s = ln.strip()
        if (s.startswith("AI Credits") or s.startswith("Resume ")
                or s.startswith("Changes ") or s.startswith("Tokens ")):
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


# Keep provider failures separate from an explicitly unavailable model identifier.
_LLM_OUTAGE_RE = re.compile(
    r"could not be validated|OAuth|No server is currently available|\b503\b|"
    r"service unavailable|temporarily unavailable|Authentication token|Failed to fetch",
    re.IGNORECASE)
_llm_last_outage = False
_MODEL_UNAVAILABLE_RE = re.compile(
    r"""^\s*(?:Error:\s*)?Model ["']([^"'\r\n]+)["'] """
    r"(?:from --model flag )?is not available\.?\s*$", re.IGNORECASE | re.MULTILINE)
_MODEL_RECHECK_SECONDS = 300
_unavailable_models = {}
_llm_active_model = None
_llm_last_error = None


def _is_llm_outage(stderr):
    """Identify a likely model-service outage for diagnostics."""
    return bool(stderr) and bool(_LLM_OUTAGE_RE.search(stderr))


def _llm_fail_notice(chat_id, what="answer"):
    raise RuntimeError("Model unavailable while trying to " + what)


def run_llm(prompt, image=None, images=None, cache_scope=None, timeout=None):
    """Generate a reply from the configured model/agent backend.

    *** THIS IS THE ONE PLACE TO CHANGE TO USE A DIFFERENT MODEL. ***
    Select a backend with the AGBOT_LLM env var (default "copilot"). To add your
    own, write a _llm_<name>(prompt, images) -> str and register it in _LLM_BACKENDS
    below. `images` is a list of local image file paths; only vision-capable
    backends use them (text-only backends simply ignore them).
    """
    global _llm_last_outage, _generation_sequence
    _llm_last_outage = False
    if cache_scope is None:
        _generation_sequence += 1
        generation_key = f"generation:{_generation_sequence}"
    else:
        # Optional review calls must not shift the answer's retry-cache slots.
        generation_key = "scope:" + cache_scope
    cache_key = f"{_current_request}:{generation_key}" if _current_request else None
    if cache_key:
        artifact_key = cache_key + ":input"
        frozen = runtime_store().get_artifact(artifact_key)
        if frozen is None:
            runtime_store().save_artifact(artifact_key, {"prompt": prompt})
        else:
            prompt = frozen["prompt"]
    cached = runtime_store().saved_generation(cache_key)
    if cached is not None:
        return cached
    if len(prompt) > PROMPT_MAX_CHARS:
        raise ValueError("Context exceeds the configured budget; no safety facts were silently removed.")
    if _request_deadline is not None:
        remaining = _request_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The overall coaching request deadline was reached.")
        timeout = min(timeout or COPILOT_TIMEOUT, remaining)
    imgs = list(images or [])
    if image:
        imgs.insert(0, image)
    backend = _LLM_BACKENDS.get(LLM_BACKEND)
    if backend is None:
        log.error("Unknown AGBOT_LLM backend %r (available: %s)",
                  LLM_BACKEND, ", ".join(sorted(_LLM_BACKENDS)))
        return None
    started = time.monotonic()
    try:
        result = (backend(prompt, imgs, timeout=timeout)
                  if timeout is not None and LLM_BACKEND == "copilot" else backend(prompt, imgs))
        if result:
            runtime_store().save_generation(cache_key, result)
        return result
    finally:
        log.info("stage=llm configured=%s active=%s seconds=%.2f prompt_chars=%d images=%d",
                 COPILOT_MODEL, _llm_active_model,
                 time.monotonic() - started, len(prompt), len(imgs))


def _llm_copilot(prompt, images, timeout=None):
    """Default backend: the GitHub Copilot CLI (`copilot`). The prompt is piped via
    stdin (so the OS command-line length limit never applies); images are passed as
    --attachment (Copilot is vision-capable)."""
    global _llm_last_outage, _llm_active_model, _llm_last_error
    env = dict(os.environ)
    for k in ("AGENCY_ENGINE", "AGENCY_SESSION_ID", "AGENCY_OPERATION_ID",
              "AGENCY_LOG_SESSION_DIR", "COPILOT_AGENT_SESSION_ID",
              "COPILOT_LOADER_PID", "COPILOT_CLI", "MSFT_AGENCY"):
        env.pop(k, None)
    env["COPILOT_HOME"] = COPILOT_HOME
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    cmd = [
        COPILOT_EXE, "-s",
        "--no-ask-user", "--available-tools=",
        "--no-custom-instructions",
        "--disable-builtin-mcps", "--disable-mcp-server", "garmin",
        "--no-color", "--sync-session", "off",
        "--reasoning-effort", COPILOT_REASONING_EFFORT, "--context", "default",
    ]
    for att in images:
        if att:
            cmd += ["--attachment", att]
    budget = COPILOT_TIMEOUT if timeout is None else min(COPILOT_TIMEOUT, timeout)
    deadline = time.monotonic() + budget
    models = list(dict.fromkeys((COPILOT_MODEL, *COPILOT_FALLBACK_MODELS)))
    for model_index, model in enumerate(models):
        if _unavailable_models.get(model, 0) > time.monotonic():
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _llm_last_error = "model_timeout"
            log.error("Copilot model attempts exhausted the %ss timeout", budget)
            return None
        command = cmd + (["--model", model] if model else [])
        try:
            res = subprocess.run(
                command, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=remaining / max(1, len(models) - model_index), env=env, cwd=BASE,
            )
        except subprocess.TimeoutExpired:
            _llm_last_error = "model_timeout"
            log.error("copilot model=%s timed out", model or "(CLI default)")
            _unavailable_models[model] = time.monotonic() + _MODEL_RECHECK_SECONDS
            continue
        except OSError:
            _llm_last_error = "cli_launch_error"
            log.exception("Could not launch the Copilot CLI")
            raise
        if res.returncode != 0:
            stderr = res.stderr or ""
            unavailable = _MODEL_UNAVAILABLE_RE.search(stderr)
            if unavailable and (not model or unavailable[1] == model):
                _unavailable_models[model] = time.monotonic() + _MODEL_RECHECK_SECONDS
                log.warning("Copilot model=%s unavailable; trying configured fallbacks",
                            model or "(CLI default)")
                continue
            _llm_last_outage = _is_llm_outage(stderr)
            _llm_last_error = "service_unavailable" if _llm_last_outage else "cli_error"
            log.error("copilot model=%s exit=%s category=%s", model, res.returncode, _llm_last_error)
            if re.search(r"\b503\b|No server is currently available|service unavailable|"
                         r"temporarily unavailable|Failed to fetch", stderr, re.I) and not re.search(
                             r"OAuth|authentication|token|unknown option|unrecognized", stderr, re.I):
                _unavailable_models[model] = time.monotonic() + _MODEL_RECHECK_SECONDS
                continue
            return None
        answer = _scrub(res.stdout or "")
        if not answer:
            _llm_last_error = "empty_response"
            log.error("copilot model=%s returned an empty answer", model)
            continue
        _unavailable_models.pop(model, None)
        _llm_active_model = model or "(CLI default)"
        _llm_last_error = None
        _llm_last_outage = False
        if model != COPILOT_MODEL:
            log.warning("stage=llm fallback configured=%s active=%s", COPILOT_MODEL, model)
        return answer
    if _llm_last_error not in ("model_timeout", "empty_response", "service_unavailable"):
        _llm_last_error = "model_unavailable"
    _llm_last_outage = True
    log.error("No configured Copilot model is currently available")
    return None


# --- Optional alternative backends -------------------------------------------
# Uncomment one (and `pip install` its SDK + set its API key), then set AGBOT_LLM
# to its name. Each takes (prompt: str, images: list[str]) and returns reply text.
#
# def _llm_openai(prompt, images):
#     from openai import OpenAI              # pip install openai ; set OPENAI_API_KEY
#     import base64, mimetypes
#     content = [{"type": "text", "text": prompt}]
#     for p in images:
#         mt = mimetypes.guess_type(p)[0] or "image/jpeg"
#         b64 = base64.b64encode(open(p, "rb").read()).decode()
#         content.append({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}})
#     r = OpenAI().chat.completions.create(
#         model=os.environ.get("AGBOT_OPENAI_MODEL", "gpt-4o"),
#         messages=[{"role": "user", "content": content}])
#     return r.choices[0].message.content
#
# def _llm_anthropic(prompt, images):
#     import anthropic, base64, mimetypes    # pip install anthropic ; set ANTHROPIC_API_KEY
#     blocks = [{"type": "text", "text": prompt}]
#     for p in images:
#         mt = mimetypes.guess_type(p)[0] or "image/jpeg"
#         b64 = base64.b64encode(open(p, "rb").read()).decode()
#         blocks.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}})
#     msg = anthropic.Anthropic().messages.create(
#         model=os.environ.get("AGBOT_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
#         max_tokens=1500, messages=[{"role": "user", "content": blocks}])
#     return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
#
# def _llm_ollama(prompt, images):
#     import base64, requests                # local Ollama server (ollama serve)
#     payload = {"model": os.environ.get("AGBOT_OLLAMA_MODEL", "llama3.1"),
#                "prompt": prompt, "stream": False}
#     if images:
#         payload["images"] = [base64.b64encode(open(p, "rb").read()).decode() for p in images]
#     return requests.post("http://localhost:11434/api/generate",
#                          json=payload, timeout=COPILOT_TIMEOUT).json().get("response")

_LLM_BACKENDS = {
    "copilot": _llm_copilot,
    # "openai": _llm_openai,
    # "anthropic": _llm_anthropic,
    # "ollama": _llm_ollama,
}


def today_human():
    return coach_clock.now().strftime("%a %d %b")


def _program_inputs():
    profile = read_file(PROFILE_FILE)
    records = []
    for kind in ("preference", "anchor", "health"):
        source_records = (memory_store().health_context() if kind == "health"
                          else memory_store().records(kind))
        for record in source_records:
            if kind == "health":
                if not record["context_constraint_active"]:
                    continue
                record = dict(record, stored_status=record["status"], status="active")
            if kind == "anchor" and not (
                    record.get("verified") and record.get("source_type") == "user"):
                continue
            if kind == "preference" and re.search(
                    r"nutrition|food|meal|protein|calorie|hydration", record["key"], re.IGNORECASE):
                continue
            records.append(record)
    facts = [{key: record.get(key) for key in (
        "id", "kind", "key", "text", "status", "source_type", "source_text",
        "verified", "revision", "observed_on")} for record in records]
    fingerprint = hashlib.sha256(json.dumps(
        {"profile": profile, "memory": facts, "training_policy": _training_policy()},
        sort_keys=True, ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    return profile, records, fingerprint


def _training_policy():
    return load_training_policy(os.path.join(os.path.dirname(STATE), "training_policy.json"))


def _rotation_context(reconcile=False):
    if not PROGRAM_ENABLED:
        return {"enabled": False}
    programme = program_store().peek_context().get("active")
    store = RotationStore(os.path.join(STATE, "coach.sqlite3"))
    if programme and reconcile:
        store.configure(programme)
    training = _program_training_context()
    if reconcile:
        store.reconcile(training, coach_clock.today())
    return store.context(training)


def _resolved_health_reports():
    return [{key: record.get(key) for key in (
        "key", "status", "resolved_at", "resolution_source_text")}
        for record in memory_store().health_context()
        if record["resolution_state"] == "source_supported_user_report"]


def _program_training_context():
    context = training_store().context(outcome_limit=None)
    for row in context["recent_outcomes"]:
        row["classification"] = garmin_coach.classify_activity(row["payload"])["classification"]
    return context


def _program_context(nutrition_focused=False):
    if not PROGRAM_ENABLED:
        return {"enabled": False}
    _, _, fingerprint = _program_inputs()
    context = program_store().context(fingerprint=fingerprint)
    active = context.get("active")
    # Evidence snapshots stay in the audit store; do not duplicate them in every reply.
    result = {key: context.get(key) for key in (
        "review_due", "due_reason", "next_review_date", "last_error", "retry_after")}
    result["enabled"] = True
    result["frequency_interpretation"] = (
        "weekly_training_days is a usual planning target, NOT a physiological ceiling. "
        "SCHEDULING_CONTEXT identifies any separately explicit hard limit. Older review "
        "wording about a budget/cap does not turn a target into an injury or recovery finding.")
    result["active"] = None
    if active:
        result["active"] = {key: active.get(key) for key in (
            "revision", "block_start", "block_end", "reviewed_on", "next_review_date", "review")}
        if nutrition_focused:
            result["active"]["review"] = {key: active["review"].get(key) for key in (
                "goal", "weekly_training_days", "recovery_rule")}
        else:
            result["active"]["review"] = dict(result["active"]["review"])
            result["active"]["review"]["decisions"] = [
                {k: v for k, v in decision.items() if k != "evidence_refs"}
                for decision in result["active"]["review"].get("decisions", [])]
    return result


def _program_evidence_prompt(evidence, repair=False):
    """Show each fact once; retain full provenance in the stored review audit."""
    def observation_view(observation):
        result = _routine_evidence({k: v for k, v in observation.items() if k != "logged_sets"})
        result["logged_sets"] = []
        for group in observation.get("logged_sets", []):
            reduced = {k: group[k] for k in (
                "exercise", "sets", "reps", "top_weight_kg", "user_corrections", "correction_warnings")
                       if k in group}
            freshness = group.get("data_freshness")
            if isinstance(freshness, dict):
                reduced["data_freshness"] = {k: freshness[k] for k in (
                    "status", "refresh_status", "weight_unit_policy_current", "weight_unit_policy")
                                            if k in freshness}
            result["logged_sets"].append(reduced)
        return result

    view = {key: value for key, value in evidence.items() if key not in ("memory", "profile")}
    view["observations"] = [observation_view(r) for r in evidence.get("observations", [])]
    view["refs"] = {key: dict(value) for key, value in evidence["refs"].items()}
    baseline = view["refs"].get("block_baseline")
    if baseline:
        baseline["observations"] = [observation_view(r) for r in baseline.get("observations", [])]
    view["detail_scope"] = ("Programme summary view: per-set chronology/group indices and repeated "
                            "refresh metadata omitted. Full evidence remains in the stored audit. "
                            "Rep ranges/top loads do not prove prescribed dose, form or effort.")
    if "profile" in view["refs"]:
        view["refs"]["profile"] = {"type": "profile", "text_reference": "PROFILE"}
    for record in evidence.get("memory", []):
        if record["ref"] in view["refs"]:
            view["refs"][record["ref"]].update(
                revision=record.get("revision"), updated_at=record.get("updated_at"))
    if repair:
        view.pop("observations", None)
        baseline = view["refs"].get("block_baseline")
        if baseline:
            baseline.pop("observations", None)
    return _prompt_json(view, "REVIEW_EVIDENCE")


def _queue_program_update(record):
    own = owner()
    if not own or not record:
        return
    rendered = md_to_html(_strip_control_markers(render_program(record)))
    for index, chunk in enumerate(html_chunks(rendered, 4000)):
        key = f"program-review:{record['revision']}:{record['reviewed_on']}:{index}"
        runtime_store().enqueue(key, own, {
            "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true",
            "_program_revision": record["revision"],
        })


def _program_review_failed(reason):
    log.error("stage=program_review failed reason=%s; prior programme unchanged", reason)
    program_store().record_failure(reason)
    own = owner()
    if own:
        runtime_store().enqueue(
            f"program-review:notice:{coach_clock.today().isoformat()}", own,
            {"text": "AgBot: I couldn't complete the programme review, so no programme "
                     "change was saved. I'll retry automatically. Your current symptoms "
                     "and movement exclusions still take priority over any older plan."})


def ensure_program_review(force=False):
    """Review only when due; a failed optional review must not disable ordinary replies."""
    if not PROGRAM_ENABLED:
        return None
    store = program_store()
    request_key = "program:" + _current_request if _current_request else None
    if request_key:
        prior = store.review_for_request(request_key)
        if prior:
            _queue_program_update(prior)
            return prior
    profile, records, fingerprint = _program_inputs()
    context = store.context(fingerprint=fingerprint)
    if (context.get("active") and
            context["active"].get("evidence", {}).get("policy_version") != PROGRAM_POLICY_VERSION):
        store.request_review("policy_changed")
        context = store.context(fingerprint=fingerprint)
    if not force and not context["review_due"]:
        return None
    snapshot = get_snapshot()
    if (not isinstance(snapshot, dict) or "__error__" in snapshot
            or snapshot.get("snapshot_cache", {}).get("stale")):
        _program_review_failed("garmin_unavailable")
        return None
    history = garmin_coach.program_history()
    training_store().record_activities(history["activities"])
    _rotation_context()
    training = _program_training_context()
    evidence = build_review_evidence(training, records, profile)
    evidence["training_policy"] = _training_policy()
    evidence["policy_version"] = PROGRAM_POLICY_VERSION
    evidence["refs"]["history_coverage"] = {"kind": "history_coverage", "data": history["coverage"]}
    evidence["refs"]["snapshot"] = {
        "kind": "device_snapshot",
        "data": {key: snapshot.get(key) for key in (
            "generated_at", "snapshot_cache", "last_sync_gmt", "last_sync_age_min",
            "last_night_sleep", "training_readiness", "morning_readiness", "hrv",
            "training_status", "training_rhythm", "wellness_today", "strain_yesterday",
            "latest_weigh_in_30d", "weight_trend_30d", "weekly_trends", "weekly_intensity",
            "recent_activities_7d_coverage")},
    }
    evidence["refs"]["fitness_metrics"] = {"kind": "device_metrics", "data": load_fitness_profile()}
    evidence["refs"]["resolved_health"] = {
        "kind": "user_reported_resolutions", "data": _resolved_health_reports(),
        "meaning": "Historical symptoms reported resolved, not active injuries or proof any exercise is safe.",
    }
    baseline = context.get("block_baseline")
    if baseline:
        baseline_evidence = baseline["evidence"]
        evidence["refs"]["block_baseline"] = {
            "kind": "historical_observations",
            "captured_on": baseline["reviewed_on"],
            "observations": baseline_evidence.get("observations", []),
            "metrics": {key: baseline_evidence.get("refs", {}).get(key)
                        for key in ("snapshot", "fitness_metrics")},
            "meaning": "Immutable block-start observations, not old programme proposals or recent progression proof.",
        }
    # These are proposals to honour, not evidence that their sets were completed.
    commitments = [dict(row, payload={k: v for k, v in row["payload"].items() if k != "exercises"},
                        detail_scope="Dated commitment; full dose stays in the plan store.")
                   for row in training["plans"] if row["date"] >= coach_clock.today().isoformat()]
    prompt = (
        read_file(PROGRAM_PROMPT_FILE)
        + "\n\nTODAY: " + coach_clock.today().isoformat()
        + "\n\nPROFILE:\n" + profile
        + "\n\nREVIEW_EVIDENCE:\n" + _program_evidence_prompt(evidence)
        + "\n\nEXISTING_DATED_PROPOSALS:\n" + json.dumps(commitments, ensure_ascii=False)
        + "\n\nCURRENT_PROGRAMME:\n" + json.dumps(
            _program_context(), ensure_ascii=False, default=str)
        + "\n\n" + read_file(os.path.join(PROMPTS, "coaching_policy.md")).split(
            "## Structured updates (hidden markers)")[0]
        + "\n\nEVIDENCE REFERENCES: JSON objects named prompt_reference point to identical "
          "data elsewhere in this prompt. Follow them; they are not missing evidence."
        + ("\n\nNUTRITION POLICY:\n" + NUTRITION_POLICY if NUTRITION_OPTIONAL else "")
        + "\n\nTASK BOUNDARY: this is only a programme review, not a daily prescription. "
          "Return exactly one TRAINING_PROGRAM marker; no SESSION_PLAN, MEMORY or LOG markers."
    )
    preferences = [record for record in records if record["kind"] == "preference"]
    previous = context.get("active")
    attempt_prompt = prompt
    deadline = time.monotonic() + PROGRAM_REVIEW_TIMEOUT
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _program_review_failed("review_timeout")
            return None
        try:
            answer = run_llm(attempt_prompt, cache_scope=f"program-review:{attempt}", timeout=remaining)
        except OSError:
            log.exception("stage=program_review model launch failed")
            _program_review_failed("model_launch_error")
            return None
        if not answer:
            _program_review_failed("review_timeout" if _llm_last_error == "model_timeout"
                                   else "model_unavailable")
            return None
        _, review, errors = parse_program_review(
            answer, evidence, preferences=preferences,
            previous=previous["review"] if previous else None)
        if not errors and review is not None:
            record = store.save_review(review, evidence, fingerprint, request_key=request_key)
            RotationStore(os.path.join(STATE, "coach.sqlite3")).configure(record)
            _queue_program_update(record)
            log.info("stage=program_review saved revision=%s next_review=%s",
                     record["revision"], record["next_review_date"])
            return record
        log.warning("stage=program_review validation attempt=%s errors=%s", attempt + 1, errors)
        attempt_prompt = (read_file(PROGRAM_PROMPT_FILE)
                          + "\n\nPROFILE:\n" + profile
                          + "\n\nREVIEW_EVIDENCE (reference index; keep supported draft measurements):\n"
                          + _program_evidence_prompt(evidence, repair=True)
                          + "\n\nDRAFT TO CORRECT:\n" + answer
                          + "\n\nREVIEW ERRORS:\n" + "\n".join(errors)
                          + "\nReturn one complete corrected TRAINING_PROGRAM marker. "
                          "Check EVERY exercise's conditions, not only the first reported error. "
                          "Preserve supported decisions and measurements unless a reported error "
                          "requires changing them. Do not invent new loads or capabilities. "
                          "Use keep/reduce/clarify when progression evidence is insufficient; "
                          "never invent references, tolerability or performance.")
    _program_review_failed("invalid_programme_review")
    return None


def maybe_program_review(now=None):
    if not PROGRAM_ENABLED or not owner():
        return
    now = now or coach_clock.now()
    _, _, fingerprint = _program_inputs()
    context = program_store().context(today=now.date(), fingerprint=fingerprint)
    if not AUTO_START <= (now.hour, now.minute) < AUTO_END:
        return
    # Recover a crash between saving a review and queuing its announcement.
    _queue_program_update(context.get("active"))
    ensure_program_review()


def _scheduling_context(plans=()):
    training = _program_training_context()
    profile, records, _ = _program_inputs()
    evidence = build_review_evidence(training, records, profile)
    active = program_store().peek_context().get("active") if PROGRAM_ENABLED else None
    review = active["review"] if active else {}
    target = review.get("weekly_training_days")
    availability = evidence["weekly_training_days"]
    prior = (active.get("evidence") or {}).get("weekly_training_days") if active else None
    if availability is not None and (target is None or target == prior or availability < target):
        if _training_policy().get("frequency_mode") != "propose" or target is None:
            target = availability
    intentional = {"strength", "cardio", "mixed"}
    observed = {r["date"] for r in training["recent_outcomes"]
                if r["classification"] == "intentional_training"}
    reported = {r["date"] for r in training["user_reported_day_status"]
                if r["status"] == "user_completed" and r.get("plan_kind_at_report") in intentional
                and r["date"] <= coach_clock.today().isoformat()}
    proposals = {r["date"]: r["payload"] for r in training["plans"]
                 if r["date"] >= coach_clock.today().isoformat()
                 and r["payload"].get("projection_status") != "needs_replanning"}
    proposals.update({p["date"]: p for p in plans})
    planned = {day for day, p in proposals.items() if p["kind"] in intentional
               and p.get("projection_status") != "needs_replanning"}
    all_days = observed | reported | planned
    proposed_days = {p["date"] for p in plans if p["kind"] in intentional
                     and p.get("projection_status") != "needs_replanning"}
    windows = []
    for day in sorted(planned):
        start = (date.fromisoformat(day) - timedelta(days=6)).isoformat()
        if plans and not any(start <= proposed <= day for proposed in proposed_days):
            continue
        counted = sorted(d for d in all_days if start <= d <= day)
        windows.append({"start": start, "end": day, "dates": counted, "count": len(counted)})
    return {
        "usual_target_days": target, "hard_limit_days": evidence["hard_weekly_limit"],
        "frequency_source_refs": evidence["weekly_training_days_refs"],
        "observed_training_dates": sorted(observed), "reported_training_dates": sorted(reported),
        "proposed_training_dates": sorted(planned), "rolling_windows": windows,
        "interpretation": (
            "A frequency target is flexible, not a recovery diagnosis or user consent to extra "
            "sessions. Discuss one-off changes using session duration/intensity, muscle overlap, "
            "symptoms, user-reported effort and current Garmin estimates. Preserve a planned "
            "recovery opportunity. Do not claim a rest day is medically required just to meet "
            "a number. A hard limit, if supplied, is an explicit user/configuration restriction."),
    }


def _plan_load_errors(plans):
    errors = []
    observed = training_store().context()["latest_observed_per_movement"]
    feedback = [r for r in memory_store().records("anchor")
                if r.get("verified") and r.get("source_type") == "user"]
    for plan in plans:
        for move in plan.get("exercises", []):
            name = canonical_exercise(move["name"])
            recorded = observed.get(name, {}).get("recorded_sets", {})
            own_load = (isinstance(recorded.get("top_weight_kg"), (int, float))
                        and recorded["top_weight_kg"] > 0
                        and recorded.get("weight_basis") == move.get("weight_basis")
                        and move.get("weight_basis") not in (None, "unspecified"))
            basis_pattern = {"per_hand": r"\b(?:per|each) hand\b",
                             "machine_stack": r"\b(?:machine|stack)\b"}.get(move.get("weight_basis"))
            if basis_pattern:
                own_load = own_load or any(
                    name in canonical_exercise(r["key"] + " " + r["text"])
                    and re.search(r"\b\d+(?:\.\d+)?\s*kg\b", r["text"], re.I)
                    and re.search(basis_pattern, r["text"], re.I) for r in feedback)
            if not own_load and any(
                    isinstance(item.get("weight_kg"), (int, float)) and item["weight_kg"] > 0
                    for item in move.get("sets", [])):
                errors.append("A new exercise without its own observed load needs "
                              "weight_kg=null and a calibration/effort instruction. Observations "
                              "must also establish the same apparatus/per-hand weight basis; "
                              "do not borrow a weight from a different curl or machine.")
    return errors


def _program_plan_errors(plans):
    errors = _plan_load_errors(plans) if plans else []
    if not plans or not PROGRAM_ENABLED:
        return errors
    active = program_store().peek_context().get("active")
    if not active:
        return errors
    review = active["review"]
    templates = {item["id"]: item for item in review["session_templates"]}
    errors.extend(rotation_plan_errors(plans, _rotation_context()))
    for plan in plans:
        if (plan["kind"] == "rest" or (plan.get("detail_level") == "outline"
                                      and plan["date"] > coach_clock.today().isoformat())):
            continue
        template_id = plan.get("program_template_id")
        if template_id is not None and not isinstance(template_id, str):
            errors.append("program_template_id must be a string.")
            template_id = None
        template = templates.get(template_id)
        adjustment = isinstance(plan.get("program_adjustment"), str) and plan["program_adjustment"].strip()
        if (not isinstance(plan.get("program_revision"), int)
                or isinstance(plan.get("program_revision"), bool)
                or plan.get("program_revision") != active["revision"]):
            errors.append("SESSION_PLAN must name the current program_revision.")
        if template is None:
            if not adjustment:
                errors.append("SESSION_PLAN needs a known program_template_id or an explicit "
                              "program_adjustment explaining the requested/safety exception.")
            continue
        if template["kind"] != plan["kind"] and not adjustment:
            errors.append("Changing the programme session kind needs a program_adjustment.")
        expected = {canonical_exercise(item["name"]) for item in template["exercises"]
                    if item.get("role") not in ("warmup", "cooldown")
                    and item.get("phase") not in ("warmup", "cooldown")}
        actual = {canonical_exercise(item["name"]) for item in plan.get("exercises", [])
                  if item.get("role") not in ("warmup", "cooldown")
                  and item.get("phase") not in ("warmup", "cooldown")}
        if actual != expected and not adjustment:
            errors.append("Changing programme exercises needs a program_adjustment: "
                          "explain the scope, recovery, equipment or tolerability reason.")
    schedule = _scheduling_context(plans)
    limit = schedule["hard_limit_days"]
    if limit is not None:
        for window in schedule["rolling_windows"]:
            if window["count"] > limit:
                errors.append(
                    f"The {window['start']} through {window['end']} window exceeds the user's "
                    f"explicit maximum of {limit} training days. Explain this as a user-set "
                    "scheduling restriction, NOT evidence of insufficient physical recovery. "
                    "Ask about changing the explicit limit rather than inventing medical reasons.")
                break
    return errors


def _enrich_activity_context(data, question="", summary=False):
    activities = data.get("recent_activities_7d") or data.get("activities") or []
    requested_day = None
    if summary or re.search(r"\byesterday\b", question, re.IGNORECASE):
        requested_day = (coach_clock.today() - timedelta(days=1)).isoformat()
    elif re.search(r"\btoday\b", question, re.IGNORECASE):
        requested_day = coach_clock.today().isoformat()
    explicit_day = re.search(r"\b\d{4}-\d{2}-\d{2}\b", question)
    if explicit_day:
        try:
            requested_day = date.fromisoformat(explicit_day.group()).isoformat()
        except ValueError:
            log.warning("Activity metric request contains an invalid calendar date.")
            data["activity_metric_coverage"] = {
                "status": "invalid_requested_date", "requested_day": explicit_day.group(),
                "instruction": "Clarify this invalid date rather than treating it as missing history.",
            }
            return
    candidates = [a for a in activities if a.get("activity_id") is not None
                  and (not requested_day or str(a.get("start", ""))[:10] == requested_day)]
    candidates.sort(key=lambda a: (
        garmin_coach.classify_activity(a)["classification"] == "intentional_training",
        str(a.get("start", ""))), reverse=True)
    selected = candidates[:3]
    for activity in selected:
        activity["detail_metrics"] = garmin_coach.activity_detail_metrics(activity["activity_id"])
    if selected:
        training_store().record_activities(selected)
    data["activity_metric_coverage"] = {
        "scope": "supplied activity history, with intentional training prioritized",
        "requested_day": requested_day, "returned": len(selected),
        "candidates": len(candidates), "omitted": len(candidates) - len(selected),
        "limit": 3, "availability": "per-activity status; missing fields are not zero",
    }


def _compact_training_context(context, data, data_key, keep_older_sequences=False):
    """Reference repeated workout payloads without removing their sole occurrence."""
    supplied, sequences, sequence_values, observed_sets = {}, {}, {}, {}
    for field in ("recent_activities_7d", "activities"):
        for index, activity in enumerate(data.get(field, []) or []):
            aid = str(activity.get("activity_id"))
            supplied[aid] = (activity, f"{data_key}.{field}[{index}]")
            sets = activity.get("logged_sets") or []
            for group_index, group in enumerate(sets):
                observed_sets[(aid, canonical_exercise(group.get("exercise", "")))] = (
                    group.get("observed_sets"), f"{supplied[aid][1]}.logged_sets[{group_index}].observed_sets")
            if sets and "set_sequence" in sets[0]:
                sequences[aid] = supplied[aid][1] + ".logged_sets[0].set_sequence"
                sequence_values[aid] = sets[0]["set_sequence"]
    for index, row in enumerate(context["recent_outcomes"]):
        aid = str(row["activity_id"])
        sets = row["payload"].get("logged_sets") or []
        if (sets and "set_sequence" in sets[0]
                and (aid not in sequences or sets[0]["set_sequence"] != sequence_values.get(aid))):
            sequences[aid] = (
                f"TRAINING_STATE.recent_outcomes[{index}].payload.logged_sets[0].set_sequence")
        if aid in supplied:
            activity, reference = supplied[aid]
            row["payload"] = {key: value for key, value in row["payload"].items()
                              if key not in activity or _stable_measurements(value) !=
                              _stable_measurements(activity[key])}
            row["activity_reference"] = reference
        for group_index, group in enumerate(row["payload"].get("logged_sets", [])):
            observed_sets[(aid, canonical_exercise(group.get("exercise", "")))] = (
                group.get("observed_sets"),
                f"TRAINING_STATE.recent_outcomes[{index}].payload.logged_sets[{group_index}].observed_sets")
    for movement in context["latest_observed_per_movement"].values():
        aid = str(movement["activity_id"])
        recorded = movement["recorded_sets"]
        shared = observed_sets.get((aid, canonical_exercise(recorded.get("exercise", ""))))
        if shared and recorded.get("observed_sets") is not None and recorded["observed_sets"] == shared[0]:
            recorded = movement["recorded_sets"] = dict(recorded)
            recorded.pop("observed_sets")
            recorded["observed_sets_reference"] = shared[1]
        if aid in sequences and "set_sequence" in movement["recorded_sets"]:
            movement["recorded_sets"] = dict(movement["recorded_sets"])
            movement["recorded_sets"].pop("set_sequence")
            movement["chronology_reference"] = sequences[aid]
        elif not keep_older_sequences and "set_sequence" in movement["recorded_sets"]:
            recorded = movement["recorded_sets"]
            indices = recorded.get("set_indices")
            if isinstance(indices, list) and indices:
                selected = [item for index, item in enumerate(recorded["set_sequence"])
                            if item.get("sequence_index", index) in indices]
                if selected:
                    movement["recorded_sets"] = dict(recorded, set_sequence=selected)
                    movement["chronology_scope"] = (
                        "This movement's sets only; the rest of this older session is omitted "
                        "from routine context. Do not infer preceding exercises from this subset.")
    context["reference_note"] = (
        "References point to complete data elsewhere in this same prompt. "
        "Unrepeated fields remain in payload; a reference is not missing evidence.")
    return context


def _stable_measurements(value):
    if isinstance(value, dict):
        return {key: _stable_measurements(item) for key, item in value.items()
                if key not in ("cache_age_hours", "age_seconds", "fetched_at")}
    if isinstance(value, list):
        return [_stable_measurements(item) for item in value]
    return value


def _prompt_json(value, label, seen=None):
    """Replace only exact repeated structures; every fact has a full first occurrence."""
    seen = {} if seen is None else seen

    def project(item, path):
        if not isinstance(item, (dict, list, str)):
            return item
        signature = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        if len(signature) >= (96 if isinstance(item, str) else 160):
            if signature in seen:
                return {"prompt_reference": seen[signature]}
            seen[signature] = path
        if isinstance(item, str):
            return item
        if (isinstance(item, list) and item and path.rsplit(".", 1)[-1] in (
                "set_sequence", "observed_sets")
                and all(isinstance(row, dict) for row in item)):
            columns = list(dict.fromkeys(k for row in item for k in row))
            item = {"columns": columns, "rows": [[row.get(k) for k in columns] for row in item],
                    "format": "Rows correspond to columns in order; null values are unknown."}
        if isinstance(item, dict):
            return {k: project(v, path + "." + str(k)) for k, v in item.items()}
        return [project(v, f"{path}[{i}]") for i, v in enumerate(item)]

    return json.dumps(project(value, label), ensure_ascii=False, default=str, separators=(",", ":"))


def _routine_evidence(value):
    """A bounded-detail view; originals and correction provenance remain in their stores."""
    if isinstance(value, list):
        return [_routine_evidence(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ("detail_descriptors", "source_descriptors", "raw_fields"):
            continue
        if key == "detail_metrics" and isinstance(item, dict):
            item = {k: item[k] for k in ("status", "fuel_estimate", "groups", "fetched_at")
                    if k in item}
        if key == "fuel_estimate" and isinstance(item, dict):
            item = {k: v for k, v in item.items() if k not in ("fields", "app_id")}
            item["detail_scope"] = "Raw developer fields retained in metric cache, not repeated here."
        if key == "observed_sets" and isinstance(item, list) and item and all(
                isinstance(row, dict) for row in item):
            columns = list(dict.fromkeys(k for row in item for k in row))
            item = {"columns": columns, "rows": [[row.get(k) for k in columns] for row in item],
                    "format": "Each row uses the listed columns; null/missing values are unknown."}
        if key == "recorded_sets" and isinstance(item, dict) and item.get("set_sequence"):
            indices = item.get("set_indices")
            if isinstance(indices, list):
                item = dict(item, set_sequence=[
                    row for i, row in enumerate(item["set_sequence"])
                    if row.get("sequence_index", i) in indices])
        result[key] = _routine_evidence(item)
    return result


def _nutrition_focused(prompt_file, question):
    if prompt_file == NUTRITION_PROMPT_FILE:
        return True
    question = question or ""
    return bool(
        re.search(r"\b(?:food|meal|breakfast|lunch|dinner|protein|calories|oats|scoop|hydration|ate|eaten|drank)\b|^\s*log:",
                  question, re.IGNORECASE)
        and not re.search(r"\b(?:exercise|workout|training|sets?|reps?|press|curl|row|squat|"
                          r"deadlift|rdl|run|running|bike|cycling|pain|injury|knee|back|shoulder)\b",
                          question, re.IGNORECASE))


def _trim_nutrition_activity(activity):
    out = {key: value for key, value in activity.items()
           if key not in ("logged_sets", "detail_metrics")}
    dm = activity.get("detail_metrics")
    if isinstance(dm, dict):
        lean = _routine_evidence({k: dm[k] for k in ("status", "fuel_estimate") if k in dm})
        if lean:
            out["detail_metrics"] = lean
    return out


def _nutrition_context(data, training):
    data = dict(data)
    for field in ("recent_activities_7d", "activities"):
        if field in data:
            data[field] = [_trim_nutrition_activity(activity)
                           for activity in data[field]]
    for outcome in training["recent_outcomes"]:
        outcome["payload"] = _trim_nutrition_activity(outcome["payload"])
    for row in training["plans"]:
        row["payload"] = {k: v for k, v in row["payload"].items() if k != "exercises"}
    training["latest_observed_per_movement"] = {}
    training["detail_selection"] = (
        "Nutrition-focused view: exact set timelines/capabilities are omitted, not absent. "
        "Activity totals, fuel estimates, plans, reported day status, active symptoms and preferences remain.")
    return data, training


def _qa_plan_scope(question, training=None):
    training = training if training is not None else training_store().context()
    completed = any(r["date"] == coach_clock.today().isoformat() and r["status"] == "user_completed"
                    for r in training["user_reported_day_status"])
    return plan_request_scope(question, completed_today=completed)


def _assemble(prompt_file, question=None, include_history=True,
              data=None, data_key="GARMIN_JSON", include_brief=True, routine_only=False):
    if question:
        memory_store().resolve_supported_health(question, prompt_context=_last_bot_message())
    correction = _prepare_workout_correction(question) if question else None
    if data is None:
        data = get_snapshot()
    data = _available_snapshot(data)
    nutrition_focused = _nutrition_focused(prompt_file, question) and not plan_request_scope(question)
    if (data.get("sensor_status") != "unavailable"
            and not data.get("snapshot_cache", {}).get("stale")) and (
            prompt_file in (SUMMARY_PROMPT_FILE, PERF_PROMPT_FILE, WEEKLY_PROMPT_FILE, NUTRITION_PROMPT_FILE)
            or re.search(r"\b(?:workout|exercise|training|stamina|running dynamics|"
                         r"fat burner|fat burned|fuel use|carbohydrate burned|"
                         r"ride|cycling|bike|run|running|hike|hiking|gym|session|"
                         r"calories|carbs|fat|recovery|rpe|feel|zone)\b",
                         question or "", re.IGNORECASE)):
        _enrich_activity_context(data, question or "", summary=prompt_file == SUMMARY_PROMPT_FILE)
    for field in ("recent_activities_7d", "activities"):
        if isinstance(data.get(field), list):
            data[field] = training_store().corrections.apply(data[field])
    training = training_store().context()
    if nutrition_focused:
        data, training = _nutrition_context(data, training)
    else:
        chronology = (not routine_only and (
            prompt_file == DEBRIEF_PROMPT_FILE or _is_workout_review(question)
            or bool(re.search(r"\b(?:order|sequence|superset)\b", question or "", re.I))))
        data = compact_workouts(data, full_chronology=chronology)
        training = compact_workouts(training, full_chronology=chronology)
        if not chronology:
            data, training = _routine_evidence(data), _routine_evidence(training)
            for row in training["plans"]:
                if row["date"] != coach_clock.today().isoformat():
                    row["payload"] = {k: v for k, v in row["payload"].items()
                                      if k != "exercises"}
                    if row["date"] < coach_clock.today().isoformat():
                        row["payload"] = {k: v for k, v in row["payload"].items() if k in (
                            "date", "kind", "objective", "rotation_slot", "projection_status",
                            "program_revision", "program_template_id")}
                    row["context_note"] = "Dated proposal; detailed prescription retained in the plan store."
    if NUTRITION_OPTIONAL and not nutrition_focused:
        data = {k: v for k, v in data.items() if k not in ("calorie_budget", "nutrition")}
    shared = {}
    js = _prompt_json(data, data_key, shared)
    parts = [
        read_file(prompt_file),
        "\n\n---\nTODAY: " + today_human() + " (" + coach_clock.today().isoformat() + ") "
        + coach_clock.now().strftime("%H:%M") + "\n",
    ]
    delayed = _delayed_message_note()
    if delayed:
        parts.append(delayed)
    if USER_NAME:
        parts.append("\nUSER: the person you are coaching is named " + USER_NAME
                     + " - you may address them by their first name.\n")
    flags = active_health_text()
    if flags:
        parts.append("\nACTIVE HEALTH REPORTS (read the actual statement and scope; "
                     "a positive exercise-tolerance report is NOT an active injury. "
                     "Respect actual symptoms, not a diagnosis inferred from this heading):\n"
                     + flags + "\n")
    if PROGRAM_ENABLED:
        resolved = _resolved_health_reports()
        if resolved:
            parts.append("\nRESOLVED SYMPTOM REPORTS (historical, not active flags; "
                         "do not repeatedly ask whether these remain active without new evidence. "
                         "Separate movement exclusions still apply):\n"
                         + json.dumps(resolved, ensure_ascii=False) + "\n")
    anchors = ("" if nutrition_focused else
               active_anchors_text(question or "training plan strength cycling running"))
    if anchors:
        parts.append("\nCAPABILITY RECORDS (check provenance and dates; unverified legacy "
                     "claims are NOT proof of effort, causes or safe execution):\n" + anchors + "\n")
    prefs = active_prefs_text()
    if prefs:
        parts.append("\nSTANDING PREFERENCES / RULES (durable do's & don'ts the user has stated "
                     "- exercise substitutions, movements to AVOID, equipment habits, scheduling "
                     "choices. HONOUR every one: never prescribe something they asked to avoid - "
                     "and do not assume an alternative is safe. Apply their equipment/scheduling "
                     "rules. Use dates and provenance, not list position; clarify genuine "
                     "conflicts between active records):\n" + prefs + "\n")
    if include_brief and not any(row["date"] == coach_clock.today().isoformat()
                                 for row in training["plans"]):
        brief = todays_brief_text()
        if brief:
            parts.append("\nTODAY'S PREVIOUS BRIEF (historical advice, not proof of execution; "
                         "a later dated TRAINING_STATE revision takes precedence):\n"
                         + brief + "\n")
    notes = recent_journal_text() if nutrition_focused else ""
    if notes:
        parts.append("\nNOTES YOU'VE SHARED (durable context the user logged):\n"
                     + notes + "\n")
    nutr_summary = rolling_nutrition_summary(
        days=7,
        today=coach_clock.today(),
        calorie_budget=data.get("calorie_budget") if isinstance(data, dict) else None,
    )
    if nutrition_focused and nutr_summary.get("logged_days_count", 0) > 0:
        parts.append("\nNUTRITION_SUMMARY_7D (aggregated from active food journal entries; "
                     "superseded corrections excluded):\n"
                     + json.dumps(nutr_summary, separators=(",", ":"), default=str) + "\n")
    hist = recent_history_text() if include_history else ""
    fit = (None if nutrition_focused or routine_only or _is_workout_review(question)
           or data.get("sensor_status") == "unavailable"
           or data.get("snapshot_cache", {}).get("stale") else load_fitness_profile())
    if isinstance(fit, dict):
        fit_view = fit
        if fit_view:
            parts.append("\nFITNESS_PROFILE (slow-changing performance metrics "
                         "Garmin computes; times are h:mm:ss):\n"
                         + json.dumps(fit_view, indent=2, default=str) + "\n")
    parts.append("\nPROFILE:\n" + read_file(PROFILE_FILE))
    parts.append("\n\n" + data_key + ":\n" + js)
    if hist:
        # Sits after the bulky profile/Garmin payload so recent turns stay near the
        # question, but BEFORE the directive - standing rules must remain the last
        # instruction the model reads.
        parts.append("\n\nRECENT CONVERSATION (context, oldest to newest):\n"
                     + hist + "\n")
    keep_older_sequences = (not routine_only and (
        prompt_file == DEBRIEF_PROMPT_FILE or _is_workout_review(question)
        or bool(re.search(r"\b(?:order|sequence|superset)\b", question or "", re.IGNORECASE))))
    training = _compact_training_context(training, data, data_key, keep_older_sequences)
    parts.append("\n\nTRAINING_STATE:\n" + _prompt_json(training, "TRAINING_STATE", shared))
    if correction:
        parts.append("\n\nWORKOUT_CORRECTION_RESULT:\n" + json.dumps(correction, default=str)
                     + "\nSaved corrections are user-reported overlays, not Garmin verification. "
                     "Use effective corrected sets and their source/freshness. If clarification "
                     "is needed, name the ambiguity and ask for activity/exercise/set; do not "
                     "claim the edit was applied. Handle other feedback in the message normally.")
    parts.append("\n\nPROGRAMME_STATE:\n" + _prompt_json(
        _program_context(nutrition_focused), "PROGRAMME_STATE", shared))
    if not nutrition_focused:
        parts.append("\n\nROTATION_STATE:\n" + _prompt_json(_rotation_context(), "ROTATION_STATE", shared))
        parts.append("\n\nSCHEDULING_CONTEXT:\n" + _prompt_json(
            _scheduling_context(), "SCHEDULING_CONTEXT", shared))
        parts.append("\n\n" + read_file(os.path.join(PROMPTS, "garmin_interpretation.md")))
    scope = _qa_plan_scope(question, training)
    if scope:
        parts.append("\n\nPLAN UPDATE CONTRACT: This request needs a dated SESSION_PLAN "
                     "or SESSION_PATCH, even when it is phrased as an exercise clarification. "
                     "Use a provisional outline for an unfamiliar future class; do not invent "
                     "its exercises or clear the user for an unknown intensity.")
    parts.append("\n\n" + read_file(os.path.join(PROMPTS, "coaching_policy.md")))
    if NUTRITION_OPTIONAL:
        parts.append("\n\nNUTRITION POLICY (overrides older profile/logging requests):\n" + NUTRITION_POLICY)
    parts.append("\n\nEVIDENCE REFERENCES: JSON objects named prompt_reference point to "
                 "identical structures elsewhere in this prompt. Follow those references; "
                 "they are not missing observations. Full originals remain in their stores.")
    if question is not None:
        parts.append("\n\nQUESTION:\n" + question)
    prompt = "".join(parts)
    log.info("stage=context prompt_chars=%d data_chars=%d training_chars=%d",
             len(prompt), len(js), len(json.dumps(training, default=str)))
    return prompt, data


def _food_reporting_errors(text, source_text):
    if not source_text:
        return []
    logs = _LOG_MARKER_RE.findall(text)
    replacements = _LOG_REPLACES_RE.findall(text)
    errors = []
    if replacements and (not logs or len(set(replacements)) != 1):
        errors.append("A meal correction needs one existing entry_id and a complete LOG note.")
    if replacements and replacements[0] not in {journal_entry_id(e) for e in journal_entries()}:
        errors.append("LOG_REPLACES must reference an entry_id from the supplied active journal.")
    for day, _ in logs:
        if day:
            try:
                if date.fromisoformat(day) > coach_clock.today():
                    errors.append("Do not log future food consumption.")
            except ValueError:
                errors.append("LOG must use a valid calendar date.")
    if logs:
        errors.append("Use structured MEAL items with a source_quote, not prose LOG accounting.")
    return errors


def _present_reply(text):
    parts = re.split(r"(```.*?```)", text.strip(), flags=re.DOTALL)
    text = "".join(part if i % 2 else re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", part)
                   for i, part in enumerate(parts))
    lines = text.split("\n")
    first = lines[0].strip().strip("*")
    if re.match(r"^(?:\U0001F916\s*)?AgBot\b", first, re.IGNORECASE):
        first = re.sub(r"^\U0001F916\s*", "", first).replace(" - ", " \u00b7 ")
        lines[0] = "**\U0001F916 " + first + "**"
    else:
        lines.insert(0, "**\U0001F916 AgBot \u00b7 " + today_human() + "**\n")
    return "\n".join(lines)


def _last_bot_message():
    history = load_history()
    for turn in reversed(history):
        if (turn.get("role") == "agbot" and turn.get("text")
                and _within_days(turn.get("ts"), 1)):
            return turn["text"]
    return ""


_TRAINING_REPORT_RE = re.compile(r"\[\[TRAINING_REPORT:\s*(.*?)\]\]", re.I | re.S)


def _training_reports(text, source):
    reports, errors = [], []
    for raw in _TRAINING_REPORT_RE.findall(text):
        try:
            item = json.loads(raw)
            quote = item["source_quote"]
            if not isinstance(quote, str) or not quote or quote not in source:
                raise ValueError("A training report requires the current user's exact quote.")
            if coach_clock.today() < date.fromisoformat(item["date"]):
                raise ValueError("A future workout cannot be marked complete or skipped.")
            clause = re.split(r"[.!?;\n]", source[:source.find(quote)])[-1] + quote
            if re.search(r"\b(?:if|would|might|could|should|tomorrow)\b|\?", clause, re.I):
                raise ValueError("Hypothetical training is not a completion/skip report.")
            if item["status"] == "user_completed":
                if (re.search(r"\b(?:not|never|didn't|haven't|isn't)\b", clause, re.I)
                        or not re.search(r"\b(?:done|finished|completed)\b", quote, re.I)
                        or not re.search(r"\b(?:exercise|workout|session|run|ride|training)\b", quote, re.I)):
                    raise ValueError("Completion requires affirmative exercise-specific evidence.")
            elif item["status"] == "user_skipped":
                if not re.search(r"\b(?:skip|skipping|rest day|not exercising|no workout)\b", quote, re.I):
                    raise ValueError("Skipping requires an explicit user choice.")
            else:
                raise ValueError("Unknown training report status.")
            reports.append(item)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    return _TRAINING_REPORT_RE.sub("", text), reports, errors


def _generate_checked(prompt, source_text="", images=None, require_plan=False, show_schedule=False):
    """Persist source-backed facts and validate proposals before returning an answer."""
    artifact_key = (_current_request + ":validated-answer:" +
                    hashlib.sha256(source_text.encode()).hexdigest()[:16]
                    if _current_request else None)
    if artifact_key:
        saved_answer = runtime_store().get_artifact(artifact_key)
        if saved_answer is not None:
            return saved_answer["text"]
    prev_bot = _last_bot_message() if source_text else ""
    auto_resolved = (memory_store().resolve_supported_health(source_text, prompt_context=prev_bot)
                     if source_text else [])
    text = run_llm(prompt, images=images)
    receipts = list(auto_resolved)
    pending_memory_errors = []
    for attempt in range(2):
        if not text:
            return None
        # User-sourced facts stand independently of whether the proposed workout is valid.
        # Apply them first so a newly stated exclusion guards this very response.
        candidate, changes, memory_errors = memory_store().process_markers(
            text, source_text=source_text, source_type="user", prompt_context=prev_bot)
        receipts.extend(changes)
        if memory_errors:
            pending_memory_errors = memory_errors
        elif changes:
            pending_memory_errors = []
        existing = training_store().context()["plans"]
        clean, plans, errors = parse_plans(candidate, memory_store().records("preference"),
                                          existing_plans=existing, require_phases=True)
        clean, meals, food_days, food_errors = parse_food(
            clean, source_text, reported_on=_MSG_SENT_AT.date() if _MSG_SENT_AT else None)
        errors.extend(food_errors)
        clean, reports, report_errors = _training_reports(clean, source_text)
        errors.extend(report_errors)
        if meals and plans and not require_plan:
            errors.append("Logging food must not create an unsolicited workout revision.")
        if NUTRITION_OPTIONAL:
            for plan in plans:
                reason = " ".join(str(plan.get(k, "")) for k in ("reason", "program_adjustment"))
                if re.search(r"\b(?:calorie|calories|food log|intake|protein shortfall|underfuel)", reason, re.I):
                    errors.append("Nutrition tracking must not determine training changes. "
                                  "Use independent symptoms, observed effort, availability and goals.")
        errors.extend(_program_plan_errors(plans))
        if (not source_text and plans and _rotation_context().get("completed_today")
                and any(p["date"] == coach_clock.today().isoformat() for p in plans)):
            errors.append("Today's rotation session is already completed; automatic advice must "
                          "not rewrite it or prescribe another session. Emit future dates only.")
        errors.extend(unsupported_claims(clean))
        if show_schedule or plans or require_plan:
            errors.extend(schedule_claim_errors(clean, plans, existing))
        errors.extend(_food_reporting_errors(clean, source_text))
        if require_plan and not plans:
            errors.append("a dated structured SESSION_PLAN is required")
        if require_plan in (True, "today") and not any(
                p["date"] == coach_clock.today().isoformat() for p in plans):
            errors.append("today's structured SESSION_PLAN is required")
        if require_plan in (True, "today") and any(
                p["date"] == coach_clock.today().isoformat() and p["kind"] != "rest"
                and (p.get("detail_level") == "outline" or not p.get("exercises"))
                and p.get("delivery") != "instructor_led"
                for p in plans):
            errors.append("Today's non-rest plan needs an actual exercise prescription, not an empty outline.")
        repair_reasons = errors + (pending_memory_errors if source_text else [])
        if not repair_reasons:
            break
        if attempt:
            if errors:
                raise ValueError("Coaching response rejected: " + "; ".join(errors))
            break
        log.warning("stage=output_guard repair_required=true issues=%s", repair_reasons)
        text = run_llm(prompt + "\n\nDRAFT TO CORRECT:\n" + text +
                       "\n\nOUTPUT ERRORS:\n" + "\n".join(repair_reasons) +
                       "\nReturn a corrected complete answer. Keep all visible food totals. "
                       "Copy MEMORY source_quote verbatim from the CURRENT USER MESSAGE, "
                       "never a paraphrase. Preserve the user's requested schedule where "
                       "possible. Correct false claims or metadata, NOT the recommendation "
                       "merely to avoid a repair. A frequency target is not a hard limit or "
                       "medical finding. Do not invent recovery reasons. Put the calendar "
                       "only in SESSION_PLAN; the application renders it.",
                       images=images)
    if plans:
        plans, projection_errors = project_rotation(plans, _rotation_context())
        if projection_errors:
            raise ValueError("Rotation changed before plan save: " + "; ".join(projection_errors))
        training_store().save(plans, source="model_proposal_in_response_to_user" if source_text
                              else "scheduled_coaching_proposal",
                              request_id=_current_request, source_text=source_text)
        RotationStore(os.path.join(STATE, "coach.sqlite3")).save_deferrals(plans)
        for plan in plans:
            if plan["date"] == coach_clock.today().isoformat():
                if _is_rest_or_recovery_plan(plan) or plan.get("rotation_slot"):
                    _set_coach_rest(_is_rest_or_recovery_plan(plan))
    for report in reports:
        training_store().set_status(report["date"], report["status"],
                                    source=report["source_quote"])
        if report["date"] == coach_clock.today().isoformat():
            _set_exercise_status("done" if report["status"] == "user_completed" else "skip")
        if report["status"] == "user_completed":
            _save_pending({"date": report["date"], "last_ts": time.time(),
                           "user_requested": True})
    if reports and PROGRAM_ENABLED:
        _rotation_context(reconcile=True)
    clean = _MODEL_LOG_RECEIPT_RE.sub("", clean)
    clean = _LOG_REPLACES_RE.sub("", clean)
    clean, _, _ = _extract_log_marker(clean)
    if source_text:
        _, intent = _extract_exercise_plan_marker(clean)
        if intent and intent in source_text and not _sent_backdate():
            clean = _harvest_exercise_plan(clean)
    if meals or food_days:
        source_id = _current_request or "food:" + hashlib.sha256(
            (coach_clock.today().isoformat() + source_text).encode()).hexdigest()
        nutrition_store().save(meals, food_days, source_id, source_text,
                               legacy_ids={journal_entry_id(e) for e in active_journal_entries()})
        if meals:
            # Replace model arithmetic with a receipt computed from the persisted ledger.
            clean = render_receipt(meals, rolling_nutrition_summary(days=35)["days"])
            notes = list(dict.fromkeys(meal["coaching_note"] for meal in meals if meal.get("coaching_note")))
            if notes:
                clean += "\n\n" + "\n\n".join(notes)
    clean = _strip_control_markers(clean)
    if plans or show_schedule:
        clean = strip_schedule_rendering(clean)
        visible = plans[:]
        mentioned = {p["date"] for p in visible}
        visible.extend(r["payload"] for r in training_store().context()["plans"]
                       if coach_clock.today().isoformat() < r["date"] <=
                       (coach_clock.today() + timedelta(days=7)).isoformat() and r["date"] not in mentioned)
        if visible:
            clean += "\n\n" + render_plans(sorted(visible, key=lambda p: p["date"]))
        if plans and PROGRAM_ENABLED:
            schedule = _scheduling_context(plans)
            target = schedule["usual_target_days"]
            above = [w for w in schedule["rolling_windows"]
                     if target is not None and w["count"] > target]
            if above:
                window = above[0]
                clean += (f"\n\nSchedule note: this proposal includes {window['count']} training "
                          f"days in {window['start']} to {window['end']}, against your usual "
                          f"target of {target}. This is a planning deviation, not evidence of "
                          "physical readiness; it does not change your ongoing target.")
    if source_text and correction_intent(source_text):
        corrections = [r for r in training_store().corrections.history()
                       if r["request_id"] == _correction_request_id(source_text)]
        if corrections:
            correction = corrections[-1]
            clean += (f"\n\nWorkout correction saved: {correction['target']['exercise']}, "
                      f"{correction['original_value']} → {correction['corrected_value']} reps "
                      "(user-reported; original Garmin data are preserved separately).")
        else:
            clean += ("\n\nNo workout correction was saved: the activity, exercise and set "
                      "need to be identified unambiguously.")
    saved = [r for r in receipts if r.get("saved")]
    if saved:
        if PROGRAM_ENABLED:
            _program_context()
        clean += "\n\nMemory updated:\n" + "\n".join(
            "- " + r.get("action", "saved") + ": " + r["text"] for r in saved)
    if pending_memory_errors:
        log.warning("stage=memory rejected_updates=%s", pending_memory_errors)
        clean += "\n\nA memory update was not saved; that change has not been made permanent."
    clean = _present_reply(clean)
    if plans and PROGRAM_ENABLED:
        rotation = _rotation_context()
        if rotation.get("enabled"):
            clean += ("\n\n**Rolling programme:** " + " → ".join(rotation["upcoming"])
                      + "\nRest does not skip a session. This is an order, not consecutive days.")
            deferred = rotation.get("deferral")
            if deferred:
                clean += ("\n**Reassess pending session:** " + deferred["reassess_on"]
                          + " — " + deferred["resume_when"])
            if rotation.get("unconfirmed_sessions"):
                clean += ("\nThe previous rotation session is unconfirmed. After completing "
                          "the prescribed session, use DWRE; report substitutions separately.")
    if plans and source_text and any(p["date"] == coach_clock.today().isoformat() for p in plans):
        save_todays_brief(clean)
    if artifact_key:
        runtime_store().save_artifact(artifact_key, {"text": clean})
    return clean


def generate_summary():
    # Optional review work has its own scheduler slot; it must not block a requested brief.
    prompt, snap = _assemble(SUMMARY_PROMPT_FILE, include_history=True, include_brief=False)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    completed_rotation = bool(_rotation_context(reconcile=True).get("completed_today"))
    if completed_rotation:
        prompt += ("\n\nTODAY'S ROTATION SESSION IS ALREADY COMPLETED. Do not emit or rewrite "
                   "a plan for today; summarise the recorded session and outline future dates.")
    text = _generate_checked(prompt, require_plan=not completed_rotation, show_schedule=completed_rotation)
    if text:
        append_history("user", "GMS (requested the morning brief)")
        append_history("agbot", text)
        save_todays_brief(text)
    return text, snap


def generate_qa(question):
    prompt, snap = _assemble(QA_PROMPT_FILE, question=question, include_history=True)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    require_plan = _qa_plan_scope(question)
    text = _generate_checked(prompt, source_text=question,
                             require_plan=require_plan, show_schedule=bool(require_plan))
    if text:
        append_history("user", question)
        append_history("agbot", text)
    return text, snap




def generate_images(image_paths, caption, extra=None, media_label="photo", spoken_text=""):
    n = len(image_paths)
    if n > 1:
        q = caption or ("(no caption - these items were sent together; analyze them AS "
                        "A SET and coach me on them)")
    else:
        q = caption or "(no caption - analyze this image and coach me on it)"
    source = "\n".join(s for s in (caption, spoken_text) if s)
    if spoken_text:
        q += "\nTranscribed user speech (confirm ambiguous transcription):\n" + spoken_text
    prompt, snap = _assemble(IMAGE_PROMPT_FILE, question=q, include_history=True)
    if n > 1:
        prompt += ("\n\nATTACHMENTS: " + str(n) + " images are attached and belong to a "
                   "SINGLE request. READ EVERY image first - they may be the same kind of "
                   "thing OR complementary parts (e.g. rounds 1-8 on one and 9-16 on the "
                   "next, or page 1 and page 2). Give ONE combined answer that ACCOUNTS FOR "
                   "THE CONTENT OF EVERY image - never analyse just one and never drop a part.")
    if extra:
        prompt += "\n\n" + extra
    text = _generate_checked(prompt, source_text=source, images=image_paths)
    if text:
        plural = "s" if n != 1 else ""
        label = caption or ("[shared " + str(n) + " " + media_label + plural + "]")
        append_history("user", label + " [" + media_label + plural + "]")
        append_history("agbot", text)
    return text, snap


def generate_image(image_path, caption):
    return generate_images([image_path], caption)


def generate_weekly():
    data = garmin_coach.build_weekly()
    if isinstance(data, dict) and "__error__" in data:
        return None, data
    prompt, _ = _assemble(WEEKLY_PROMPT_FILE, data=data, data_key="WEEKLY_JSON")
    text = _generate_checked(prompt)
    if text:
        append_history("user", "weekly review (requested)")
        append_history("agbot", text)
    return text, data


def generate_nutrition():
    prompt, snap = _assemble(NUTRITION_PROMPT_FILE)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = _generate_checked(prompt)
    if text:
        append_history("user", "nutrition targets (requested)")
        append_history("agbot", text)
    return text, snap


def generate_debrief(activities, source_text=""):
    """Collective post-workout debrief. `activities` is the just-finished session block - a
    list of trimmed activities the user logged back-to-back, often split across Garmin types
    (a cardio warm-up, the Strength block, a run, a Pilates/stretch cool-down). The WHOLE
    session is assessed against its dated plan, not necessarily today's.
    Accepts a single activity dict too, for back-compat."""
    if isinstance(activities, dict):
        activities = [activities]
    activities = [a for a in (activities or []) if isinstance(a, dict)]
    if not activities:
        return None
    snap = get_snapshot(force=True)
    base_prompt, _ = _assemble(DEBRIEF_PROMPT_FILE, data=snap, question=source_text or None,
                              routine_only=True)
    parts = [base_prompt]
    for a in activities:
        if "strength" in str(a.get("type") or ""):
            sets = garmin_coach.exercise_sets(a.get("activity_id"))
            if isinstance(sets, list) and sets:
                a["logged_sets"] = sets
    if len(activities) > 1:
        header = ("SESSION_ACTIVITIES - these " + str(len(activities)) + " activities are "
                  "nearby recordings on the reported day. Consider their dated plan collectively, "
                  "but proximity alone does not establish warmup/main/cooldown coverage. "
                  "Do not invent a completion percentage; "
                  "flag a genuinely-missing piece only once, and if they did something OFF the "
                  "plan note the pivot supportively - never ask 'is that all?' after one part")
    else:
        header = "SESSION_ACTIVITIES (one recorded activity)"
    training_store().record_activities(activities)
    effective = training_store().corrections.apply(activities)
    parts.append("\n\n" + header + ":\n" + _prompt_json(
        compact_workouts(effective, full_chronology=True), "SESSION_ACTIVITIES"))
    session_dates = {str(a.get("start", ""))[:10] for a in activities}
    dated_plans = [row for row in training_store().context()["plans"] if row["date"] in session_dates]
    parts.append("\n\nDATED_SESSION_PLANS (proposals, not execution evidence):\n"
                 + _prompt_json(dated_plans, "DATED_SESSION_PLANS"))
    extras_all = {}
    for a in activities:
        ex = garmin_coach.activity_extras(a.get("activity_id"))
        if isinstance(ex, dict) and ex and "__error__" not in ex:
            extras_all[str(a.get("name") or a.get("activity_id"))] = ex
    if extras_all:
        parts.append("\n\nACTIVITY_EXTRAS (per activity; time-in-HR-zone minutes; weather if "
                     "outdoor):\n" + json.dumps(extras_all, indent=2, default=str))
    parts.append("\nSESSION_DATES: " + json.dumps(sorted({
        str(a.get("start", ""))[:10] for a in activities}))
        + "\nUse these dates, not the processing day. Nearby activities are associated, "
        "not proof that prescribed warm-up, dose or cool-down was performed.")
    text = _generate_checked("".join(parts), source_text=source_text, show_schedule=True)
    if text:
        append_history("agbot", "[post-workout debrief] " + text,
                       activity_ids=[a["activity_id"] for a in activities if a.get("activity_id")])
    return text


def do_summary(chat_id, auto=False):
    log.info("Generating summary (auto=%s) for %s", auto, chat_id)
    if auto:
        send_message(chat_id, "\U0001F916 AgBot: pulling your overnight Garmin data for today's brief...")
    text, snap = generate_summary()
    if isinstance(snap, dict) and "__error__" in snap:
        raise RuntimeError("Garmin data unavailable: " + str(snap["__error__"])[:180])
    if not text:
        _llm_fail_notice(chat_id, "generate your brief")
        return
    send_message(chat_id, text, summary_date=coach_clock.today().isoformat())


def do_qa(chat_id, question):
    log.info("stage=qa input_chars=%d", len(question))
    text, snap = generate_qa(question)
    if isinstance(snap, dict) and "__error__" in snap:
        raise RuntimeError("Garmin data unavailable")
    if not text:
        _llm_fail_notice(chat_id, "generate an answer")
        return
    send_message(chat_id, text)


def do_weekly(chat_id):
    log.info("Generating weekly review for %s", chat_id)
    text, data = generate_weekly()
    if isinstance(data, dict) and "__error__" in data:
        raise RuntimeError("Weekly Garmin data unavailable")
    if not text:
        _llm_fail_notice(chat_id, "build your weekly review")
        return
    send_message(chat_id, text)


def do_nutrition(chat_id):
    log.info("Generating nutrition targets for %s", chat_id)
    text, snap = generate_nutrition()
    if isinstance(snap, dict) and "__error__" in snap:
        raise RuntimeError("Garmin nutrition data unavailable")
    if not text:
        _llm_fail_notice(chat_id, "work out your targets")
        return
    send_message(chat_id, text)


def generate_performance():
    prompt, snap = _assemble(PERF_PROMPT_FILE)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = _generate_checked(prompt)
    if text:
        append_history("user", "performance / fitness stats (requested)")
        append_history("agbot", text)
    return text, snap


def do_performance(chat_id):
    log.info("Generating performance card for %s", chat_id)
    text, snap = generate_performance()
    if isinstance(snap, dict) and "__error__" in snap:
        raise RuntimeError("Garmin performance data unavailable")
    if not text:
        _llm_fail_notice(chat_id, "build your performance card")
        return
    send_message(chat_id, text)


def _photo_file_id(msg):
    photos = msg.get("photo") or []
    if photos:
        return photos[-1].get("file_id")  # largest rendition
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        return doc.get("file_id")
    return None


def _video_file(msg):
    for key in ("video", "video_note", "animation"):
        v = msg.get(key)
        if v and v.get("file_id"):
            return v
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("video/"):
        return doc
    return None


def do_images(chat_id, file_ids, caption, extra=None, media_label="photo"):
    """Download 1+ images (an album is analyzed together) and coach on them."""
    file_ids = [f for f in (file_ids or []) if f]
    if not file_ids:
        send_message(chat_id, "\U0001F916 AgBot: send it as an image (photo) and I'll take a look.")
        return
    n = len(file_ids)
    if media_label == "photo":
        send_message(chat_id, "\U0001F916 AgBot: looking at your %d photos together..." % n
                     if n > 1 else "\U0001F916 AgBot: looking at your photo...")
    paths = []
    try:
        for fid in file_ids:
            p = download_file(fid)
            if p:
                paths.append(p)
        if not paths:
            send_message(chat_id, "\U0001F916 AgBot: I couldn't download that image. Please try again.")
            return
        text, _snap = generate_images(paths, caption, extra=extra, media_label=media_label)
        if not text:
            _llm_fail_notice(chat_id, "analyze " + ("those" if n > 1 else "that photo"))
            return
        send_message(chat_id, text)
    finally:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass


def do_image(chat_id, msg):
    do_images(chat_id, [_photo_file_id(msg)], (msg.get("caption") or "").strip())


def do_video(chat_id, msg):
    """Analyze a short video by sampling frames + transcribing any spoken audio."""
    v = _video_file(msg)
    if not v or not v.get("file_id"):
        send_message(chat_id, "\U0001F916 AgBot: I couldn't read that video.")
        return
    send_message(chat_id, "\U0001F3A5 AgBot: reviewing your video...")
    vpath, frames = None, []
    try:
        try:
            vpath = download_file(v.get("file_id"))
        except Exception as exc:  # noqa: BLE001
            log.error("video download failed: %s", exc)
            vpath = None
        if not vpath:
            send_message(chat_id, "\U0001F916 AgBot: I couldn't download that video - Telegram "
                                  "limits bots to ~20MB. Try a shorter or lower-res clip.")
            return
        frames = extract_video_frames(vpath)
        if not frames:
            send_message(chat_id, "\U0001F916 AgBot: I couldn't read any frames from that video. "
                                  "Try a shorter clip, or send a photo instead.")
            return
        transcript = transcribe_audio(vpath)  # spoken words over the clip, if any
        dur = v.get("duration")
        bits = ["VIDEO_CONTEXT: the user sent a short video. I sampled " + str(len(frames))
                + " still frames from it" + ((" over ~" + str(dur) + "s") if dur else "")
                + ", attached in time order - read them as a sequence (movement / form / a "
                  "pan across items over the clip), not as unrelated images."]
        if transcript:
            bits.append('Audio transcript of the video (what the user says): "' + transcript + '"')
        text, _snap = generate_images(frames, (msg.get("caption") or "").strip(),
                                      extra="\n".join(bits), media_label="video frame",
                                      spoken_text=transcript or "")
        if not text:
            _llm_fail_notice(chat_id, "analyze that video")
            return
        send_message(chat_id, text)
    finally:
        for p in frames:
            try:
                os.remove(p)
            except OSError:
                pass
        if vpath:
            try:
                os.remove(vpath)
            except OSError:
                pass


def do_voice(chat_id, msg):
    v = msg.get("voice") or msg.get("audio") or {}
    file_id = v.get("file_id")
    if not file_id:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't read that audio clip.")
        return
    send_message(chat_id, "\U0001F3A4 AgBot: transcribing your voice note...")
    path = None
    try:
        path = download_file(file_id)
        transcript = transcribe_audio(path) if path else None
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    if not path:
        send_message(chat_id, "\U0001F916 AgBot: couldn't download that audio. Please try again.")
        return
    if not transcript:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't make out any speech there. "
                              "Try again a bit closer to the mic, or just type it.")
        return
    caption = (msg.get("caption") or "").strip()
    text = (caption + " " + transcript).strip() if caption else transcript
    send_message(chat_id, "\U0001F3A4 Heard: \u201c" + transcript + "\u201d")
    _route_text(chat_id, text)


# --------------------------------------------------------------------- routing
HELP_TEXT = (
    "\U0001F916 **AgBot** - your personal Garmin coach.\n\n"
    "- **GMS** (or 'summary') - your morning brief: recovery read + today's workout (or a "
    "genuine rest day when you need to recover).\n"
    "- **DWRE** ('done with recommended exercise') - I pull your whole day's session "
    "together (warm-up + strength + cardio + stretch, however you split it in Garmin) and "
    "grade it against the plan.\n"
    "- **week** - a 7-day trend review.  **nutrition** - today's calorie/protein targets.\n"
    "- **performance** - your fitness stats: VO2max, fitness age, race predictions, "
    "endurance & hill score, cycling FTP, weekly intensity minutes.\n"
    "- **Send a photo** (meal, machine screen, exercise) and I'll analyze it - add a "
    "caption to ask something specific.\n"
    "- **Send several photos together** (an album) - e.g. all the breakfast options - and "
    "I'll weigh them up as ONE set and recommend what to pick.\n"
    "- \U0001F3A5 **Send a short video** (form check, a machine, a pan across the buffet) - "
    "I sample frames + listen to any narration and coach on the whole clip.\n"
    "- \U0001F3A4 **Send a voice note** and I'll transcribe it, then answer - speak naturally.\n"
    "- **Just tell me what you ate** ('had poha and a protein shake') and I'll log it "
    "automatically - no **log:** prefix needed (though 'log:' still works, incl. as a photo "
    "caption). I only confirm once it's actually saved, and I remember it across days.\n"
    "- \U0001FA79 **Tell me if you're hurt or unwell** ('my knee hurts', 'I feel sick') and "
    "I'll ask what's going on, remember it, and adapt your training (or call for rest) until "
    "you reply **recovered**.\n"
    "- Food logging is optional. Missing photos/meals never change your workout. "
    "Meal reminders are off unless explicitly enabled.\n"
    "- **starting workout** / **cooldown** shows your saved workout card immediately.\n"
    "- **pause reminders** / **resume reminders** controls optional check-ins.\n"
    "- \U0001F3CB\uFE0F I check in through the day (12 / 4pm / 9pm) on whether you did "
    "the recommended exercise - reply **DWRE** when done, or 'rest day' / 'skip today' to stop "
    "the check-ins for the day.\n"
    "- **Ask me anything**, e.g. 'how did I sleep?', 'what's my predicted 10K time?', "
    "'give me a 30-min rowing session'.\n"
    "- I auto-send your brief by ~9:30am, and ~90 min after your last logged activity I send "
    "ONE combined debrief of the whole session vs the plan (or reply DWRE to get it now).\n"
    "- **memory** shows saved facts; **plan** shows dated session proposals.\n"
    "- **programme** shows your multi-week framework; **review programme** requests a "
    "review now. When enabled, reviews also happen automatically without a command.\n"
    "- **/reset** clears recent chat, not durable preferences or health records.\n\n"
    "Advice uses available Garmin data and your private profile; missing data stays unknown."
)

def _report_day(report=""):
    day = _MSG_SENT_AT.date() if _MSG_SENT_AT else coach_clock.today()
    explicit = re.search(r"\b\d{4}-\d{2}-\d{2}\b", report)
    if explicit:
        day = date.fromisoformat(explicit.group())
    elif re.search(r"\byesterday\b", report, re.I):
        day -= timedelta(days=1)
    if day > coach_clock.today():
        raise ValueError("A future workout cannot be reported completed.")
    return day


def _deliver_debrief(chat_id, block, day, source_text=""):
    ids = sorted(str(a.get("activity_id")) for a in block)
    key = "debrief:" + day + ":" + hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:16]
    saved = runtime_store().get_artifact(key)
    text = saved["text"] if saved else generate_debrief(block, source_text=source_text)
    if not text:
        raise RuntimeError("Debrief generation failed; the dated job remains pending.")
    if not saved:
        runtime_store().save_artifact(key, {"text": text})
    send_message(chat_id, text, delivery_key=key)
    _clear_pending(day)


def do_dwre(chat_id, report=""):
    """Completion is a dated user report; syncing and debrief delivery are separate."""
    day = _report_day(report)
    if day == coach_clock.today():
        _set_exercise_status("done")
    training_store().set_status(day, "user_completed")
    if PROGRAM_ENABLED:
        _rotation_context(reconcile=True)
    _snapshot_cache.invalidate()
    _save_pending({"date": day.isoformat(), "last_ts": time.time() - DEBRIEF_QUIET_SECS,
                   "user_requested": True, "source_text": report})
    send_message(chat_id, "\U0001F916 AgBot: nice work \u2713 pulling your whole session "
                 "together...")
    block = garmin_coach.session_block(target_day=day)
    if isinstance(block, dict) and "__error__" in block:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't reach Garmin just now - your "
                     "completion is saved for " + day.isoformat() + ". The debrief will retry.")
        if re.match(r"^/?dwre\s*[-:]", report, re.I):
            do_qa(chat_id, report)
        return
    if not block:
        send_message(chat_id, "Completion saved for " + day.isoformat()
                     + ". No matching synced activity yet; the debrief remains pending. "
                     "You can also tell me what you did and how the main exercises felt.")
        if re.match(r"^/?dwre\s*[-:]", report, re.I):
            do_qa(chat_id, report)
        return
    _deliver_debrief(chat_id, block, day.isoformat(), source_text=report)


def do_skip_exercise(chat_id):
    """User is not exercising today - stop the check-ins for the rest of the day."""
    log.info("Exercise skipped for today (user opt-out)")
    day = _report_day()
    if day == coach_clock.today():
        _set_exercise_status("skip")
    training_store().set_status(day, "user_skipped")
    send_message(chat_id, "Rest/skip recorded for " + day.isoformat()
                 + (". Exercise check-ins are off for today." if day == coach_clock.today()
                    else ". Today's training status is unchanged."))


DWRE_EXACT = {"dwre", "/dwre",
              "done with exercise", "done with my exercise", "done with the exercise",
              "done with workout", "done with my workout", "done exercising",
              "finished exercising", "finished my workout", "finished my exercise",
              "completed my exercise", "done with today's exercise"}
DWRE_SUBSTR = ("done with recommended exercise", "done with the recommended exercise",
               "done with all the recommended", "done with my recommended",
               "finished the recommended exercise", "done with today's recommended",
               "done with all my exercise", "done with all exercise")
SKIP_EXACT = {"skip", "/skip", "noex", "rest day", "skip today", "skipping today",
              "skip exercise", "no exercise today", "not exercising today", "no workout today",
              "not working out today", "taking today off", "taking the day off",
              "resting today", "no exercise", "rest today"}
SKIP_SUBSTR = ("not exercising today", "no exercise today", "not going to exercise",
               "won't be exercising", "wont be exercising", "not doing any exercise",
               "no exercise for me today", "skipping exercise", "rest day today",
               "won't exercise today", "wont exercise today", "not doing exercise today")


SUMMARY_TRIGGERS = {"gms", "summary", "/summary", "/gms", "morning", "brief",
                    "report", "morning summary", "/report", "/brief"}


def classify(text):
    t = (text or "").strip()
    low = t.lower()
    for pre in ("agbot:", "agbot"):
        if low.startswith(pre):
            t = t[len(pre):].strip(" :")
            low = t.lower()
            break
    if low in ("/start", "start"):
        return "start", ""
    if low in ("/help", "help", "?"):
        return "help", ""
    if low in ("memory", "/memory", "what do you remember"):
        return "memory", ""
    if low in ("plan", "/plan", "current plan"):
        return "plan", ""
    if low in ("pause reminders", "resume reminders"):
        return "reminders", low
    if low in ("starting workout", "start workout", "warmup", "warm-up", "cooldown", "cool-down"):
        return "workout_card", ""
    if low in ("programme", "program", "/programme", "/program", "training programme",
               "training program"):
        return "program", ""
    if low in ("review programme", "review program", "/review_programme", "/review_program"):
        return "program_review", ""
    if low in ("/reset", "/clear", "reset", "forget", "new chat", "start over"):
        return "reset", ""
    if low in ("recovered", "/recovered", "recovered all", "fully recovered",
               "all injuries resolved"):
        return "recovered", ""
    if low.startswith("log ") or low.startswith("log:"):
        return "log", t[3:].lstrip(" :").strip()
    if low in ("week", "weekly", "/week", "/weekly", "week review",
               "weekly review", "review"):
        return "weekly", ""
    if low in ("nutrition", "/nutrition", "macros", "calories", "diet",
               "food targets", "targets"):
        return "nutrition", ""
    if low in ("performance", "/performance", "fitness", "stats", "/stats",
               "vo2", "vo2max", "race", "races", "race predictions",
               "fitness age", "endurance", "performance stats", "perf"):
        return "performance", ""
    if low in DWRE_EXACT or low in DWRE_SUBSTR:
        return "dwre", t
    if re.fullmatch(r"/?dwre\s+(?:yesterday|\d{4}-\d{2}-\d{2})", low):
        return "dwre", t
    if re.match(r"^/?dwre\s*[-:]\s*\S", low):
        return "dwre", t
    if low in SKIP_EXACT:
        return "skip_exercise", ""
    if low == "" or low in SUMMARY_TRIGGERS:
        return "summary", ""
    return "qa", t


def _owner_ok(chat_id):
    own = owner()
    if own is None:
        set_owner(chat_id)
        own = str(chat_id)
    if str(chat_id) != str(own):
        log.warning("Ignoring message from non-owner chat_id %s", chat_id)
        return False
    return True


def _route_text(chat_id, text):
    kind, payload = classify(text)
    log.info("stage=route kind=%s input_chars=%d", kind, len(text))
    if kind in ("start", "help"):
        send_message(chat_id, HELP_TEXT)
    elif kind == "memory":
        sections = []
        for record_kind in ("preference", "health", "anchor"):
            value = memory_store().render(record_kind)
            sections.append(record_kind.title() + ":\n" + (value or "(none recorded)"))
        send_message(chat_id, "AgBot - Saved memory\n\n" + "\n\n".join(sections))
    elif kind == "plan":
        plans = training_store().context()["plans"]
        lines = [f"{p['date']} - {p['payload']['kind']}: {p['payload']['objective']} "
                 f"(revision {p['revision']}, {p['status']})" for p in plans]
        send_message(chat_id, "AgBot - Dated plans\n" +
                     ("\n".join(lines) if lines else "No structured plan recorded yet."))
    elif kind == "workout_card":
        plans = [p["payload"] for p in training_store().context()["plans"]
                 if p["date"] == coach_clock.today().isoformat()]
        send_message(chat_id, render_plans(plans) if plans else
                     "No dated workout is saved for today. Ask for a plan before starting.")
    elif kind == "reminders":
        paused = payload.startswith("pause")
        _atomic_write(os.path.join(STATE, "reminder_settings.json"), json.dumps({"paused": paused}))
        send_message(chat_id, "Optional reminders paused." if paused else
                     "Optional reminders resumed. Meal logging remains optional.")
    elif kind in ("program", "program_review"):
        if not PROGRAM_ENABLED:
            send_message(chat_id, "AgBot: programme coaching is not enabled in this installation.")
        elif kind == "program_review":
            record = ensure_program_review(force=True)
            if record is None:
                send_message(chat_id, "AgBot: the programme review did not complete; no programme "
                             "change was saved. The automatic retry remains pending.")
        else:
            _, _, fingerprint = _program_inputs()
            context = program_store().context(fingerprint=fingerprint)
            active = context.get("active")
            if active:
                text = render_program(active)
                if context.get("next_review_date") != active.get("next_review_date"):
                    text += "\n\nFeedback review scheduled: " + str(context["next_review_date"])
                if context.get("last_error"):
                    text += "\n\nThe latest review attempt failed; a retry is pending."
                send_message(chat_id, text)
            else:
                send_message(chat_id, "AgBot: the initial programme review is pending. "
                             "It runs automatically during the daytime review window.")
    elif kind == "reset":
        clear_history()
        send_message(chat_id, "\U0001F916 AgBot: conversation memory cleared - fresh start.")
    elif kind == "recovered":
        cleared = resolve_health(text)
        if cleared and PROGRAM_ENABLED:
            _program_context()
        if cleared:
            send_message(chat_id, "\U0001F916 AgBot: great news \u2713 cleared your active "
                         "health flag(s): " + "; ".join(c[:60] for c in cleared)
                         + ". Separate movement preferences remain unchanged.")
        else:
            send_message(chat_id, "\U0001F916 AgBot: noted - you had no active injury/illness "
                         "flags on record. Glad you're feeling good!")
    elif kind == "log":
        if payload:
            do_qa(chat_id, "log: " + payload)
        else:
            send_message(chat_id, "\U0001F916 AgBot: tell me what to log, e.g. 'log: ate 3 eggs and oats'.")
    elif kind == "weekly":
        send_message(chat_id, "\U0001F916 AgBot: crunching your last 7 days...")
        do_weekly(chat_id)
    elif kind == "nutrition":
        send_message(chat_id, "\U0001F916 AgBot: working out today's targets...")
        do_nutrition(chat_id)
    elif kind == "performance":
        send_message(chat_id, "\U0001F916 AgBot: pulling your performance metrics...")
        do_performance(chat_id)
    elif kind == "dwre":
        do_dwre(chat_id, payload)
    elif kind == "skip_exercise":
        do_skip_exercise(chat_id)
    elif kind == "summary":
        send_message(chat_id, "\U0001F916 AgBot: on it - pulling your Garmin data...")
        do_summary(chat_id)
    else:
        send_message(chat_id, "\U0001F916 AgBot: let me check your data...")
        do_qa(chat_id, payload)


# Album (media-group) buffering: Telegram delivers each photo of an album as a
# SEPARATE update sharing one media_group_id, and only one carries the caption. We
# collect them briefly and analyze the whole set in a single reply.
ALBUM_DEBOUNCE_SEC = 3.0   # wait this long after the last album part before processing, so a
                           # slow-arriving 2nd/3rd photo isn't split off into its own reply
_album_buffer = {}  # media_group_id -> {chat_id, file_ids, caption, ts}


def _buffer_album(chat_id, gid, file_id, caption, sent_ts=None):
    grp = _album_buffer.get(gid)
    if grp is None:
        grp = {"chat_id": chat_id, "file_ids": [], "caption": "", "ts": 0.0,
               "sent_ts": sent_ts}
        _album_buffer[gid] = grp
    if file_id:
        grp["file_ids"].append(file_id)
    if caption and not grp["caption"]:
        grp["caption"] = caption
    if sent_ts and not grp.get("sent_ts"):
        grp["sent_ts"] = sent_ts
    grp["ts"] = time.time()


def flush_ready_albums():
    """Process any album whose photos have stopped arriving (idle > debounce)."""
    if not _album_buffer:
        return
    now = time.time()
    ready = [gid for gid, g in _album_buffer.items()
             if now - g["ts"] >= ALBUM_DEBOUNCE_SEC]
    for gid in ready:
        grp = _album_buffer.pop(gid, None)
        if not grp or not grp["file_ids"]:
            continue
        log.info("Album %s complete: %d photos", gid, len(grp["file_ids"]))
        try:
            _set_msg_sent_at(grp.get("sent_ts"))  # album may also have arrived late
            do_images(grp["chat_id"], grp["file_ids"], grp["caption"])
        except Exception as exc:  # noqa: BLE001
            log.exception("album handler error: %s", exc)


def dispatch(msg):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        return
    if not _owner_ok(chat_id):
        return
    _set_msg_sent_at(msg.get("date"))
    back = _sent_backdate()
    if back:
        log.info("Message delivered late - sent %s, treating its 'today' as %s",
                 _MSG_SENT_AT.strftime("%Y-%m-%d %H:%M"), back)
    gid = msg.get("media_group_id")
    photo_fid = _photo_file_id(msg)
    if gid and photo_fid:  # one item of a photo album - buffer, handle as a set
        _buffer_album(chat_id, gid, photo_fid, (msg.get("caption") or "").strip(),
                      sent_ts=msg.get("date"))
        return
    if _video_file(msg):
        log.info("Message kind=video")
        do_video(chat_id, msg)
        return
    if photo_fid:
        log.info("Message kind=image caption=%r", (msg.get("caption") or "")[:120])
        do_image(chat_id, msg)
        return
    if msg.get("voice") or msg.get("audio"):
        log.info("Message kind=voice")
        do_voice(chat_id, msg)
        return
    text = msg.get("text", "")
    _route_text(chat_id, text)


_last_sleep_sync_check = 0.0


def _auto_summary_sleep_ready(now):
    """Wait until AUTO_SLEEP_WAIT_UNTIL for overnight sleep to sync before auto-sending
    the morning brief; after that cutoff, or on any API error, proceed (fail-open)."""
    global _last_sleep_sync_check
    if (now.hour, now.minute) >= AUTO_SLEEP_WAIT_UNTIL:
        return True
    mono = time.monotonic()
    if _last_sleep_sync_check and mono - _last_sleep_sync_check < AUTO_SLEEP_POLL_SECS:
        return False
    _last_sleep_sync_check = mono
    try:
        logged = garmin_coach.sleep_logged_today(now.date())
    except Exception as exc:  # noqa: BLE001
        log.warning("sleep_logged_today check failed, proceeding with brief: %s", exc)
        return True
    if logged is False:
        log.info("Auto-summary waiting for overnight sleep sync at %s (cutoff %02d:%02d)",
                 now.strftime("%H:%M"), AUTO_SLEEP_WAIT_UNTIL[0], AUTO_SLEEP_WAIT_UNTIL[1])
        return False
    return True


def maybe_auto_summary():
    own = owner()
    if not own:
        return
    if (read_file(LAST_SUMMARY_FILE).strip() == coach_clock.today().isoformat()
            or runtime_store().summary_delivered(coach_clock.today().isoformat())):
        return
    if runtime_store().summary_pending(coach_clock.today().isoformat()):
        return
    now = coach_clock.now()
    cur = (now.hour, now.minute)
    if cur < AUTO_START or cur >= AUTO_END:
        return
    if not _auto_summary_sleep_ready(now):
        return
    log.info("Auto-summary trigger at %s", now.strftime("%H:%M"))
    do_summary(own, auto=True)


def _todays_food_log_times(now):
    """Local datetimes of today's journaled food entries. Used to skip a meal nudge
    for a meal that's already been logged (the journal is the bot's own food log)."""
    times = []
    today = now.date().isoformat()
    for e in active_journal_entries()[-JOURNAL_KEEP:]:
        if e.get("date") != today:
            continue
        try:
            times.append(datetime.fromisoformat(e.get("ts") or ""))
        except ValueError:
            continue
    return times


def _meal_slot_windows():
    """Each meal slot -> (lo_min, hi_min) span of the day it 'owns', with boundaries
    at the midpoints between the configured slot times (first slot opens at 00:00,
    last closes at 24:00). A food entry whose local time lands in a slot's span means
    that meal is already logged, so its reminder can be skipped."""
    slots = sorted(((slot, hh * 60 + mm) for slot, hh, mm, _ in MEAL_REMINDERS),
                   key=lambda x: x[1])
    windows = {}
    for i, (slot, mins) in enumerate(slots):
        lo = 0 if i == 0 else (slots[i - 1][1] + mins) // 2
        hi = 24 * 60 if i == len(slots) - 1 else (mins + slots[i + 1][1]) // 2
        windows[slot] = (lo, hi)
    return windows


_MACRO_NUM_RE = r"\d+(?:,\d{3})*(?:\.\d+)?"
_MACRO_SPAN_RE = rf"(?<!\d)(?<!\.)({_MACRO_NUM_RE})(?:\s*(?:[-–—]|to)\s*({_MACRO_NUM_RE}))?"
_PROTEIN_RE = re.compile(
    rf"{_MACRO_SPAN_RE}\s*g\s*(?:of\s+)?protein"
    r"(?!\s*(?:powder|isolate|concentrate|blend|scoop|bar|shake)\b)",
    re.IGNORECASE,
)
_CARBS_RE = re.compile(rf"{_MACRO_SPAN_RE}\s*g\s*(?:of\s+)?carb(?:ohydrate)?s?\b", re.IGNORECASE)
_FAT_RE = re.compile(
    rf"(?<!low[- ])(?<!full[- ]){_MACRO_SPAN_RE}\s*g\s*(?:of\s+)?fat\b",
    re.IGNORECASE,
)
_KCAL_RE = re.compile(rf"{_MACRO_SPAN_RE}\s*k?cal\b", re.IGNORECASE)


def _macro_midpoint(match):
    lo = float(match.group(1).replace(",", ""))
    hi = float(match.group(2).replace(",", "")) if match.group(2) else lo
    return (lo + hi) / 2.0


def _parse_entry_macros(txt):
    """Conservative legacy reader. Ambiguous aggregates and missing fields stay unknown."""
    if re.search(r"\b(?:total|combined)\b", txt, re.I) and len(_KCAL_RE.findall(txt)) > 1:
        return (None, None, None, None)
    parens = [p for p in re.findall(r"\([^()]*\)", txt) if _KCAL_RE.search(p)]
    target = " ".join(parens) if parens else txt
    result = []
    for pattern, name in ((_KCAL_RE, "kcal"), (_PROTEIN_RE, "protein"),
                          (_CARBS_RE, "carbs"), (_FAT_RE, "fat")):
        values = [_macro_midpoint(m) for m in pattern.finditer(target)]
        if not values and name != "kcal":
            reverse = re.compile(rf"\b{name}\s*[:=]?\s*~?{_MACRO_SPAN_RE}\s*g\b", re.I)
            values = [_macro_midpoint(m) for m in reverse.finditer(txt)]
        result.append(sum(values) if values else None)
    return tuple(result)


def _entry_macros(entry):
    if entry.get("entry_type") == "structured_meal":
        return tuple(midpoint(entry["macros"].get(k))
                     for k in ("kcal", "protein_g", "carbs_g", "fat_g"))
    return _parse_entry_macros(entry.get("text", ""))


def _protein_status(protein_g, calorie_budget):
    if not isinstance(calorie_budget, dict):
        return None
    pr = calorie_budget.get("protein_range_g")
    if isinstance(pr, dict) and isinstance(pr.get("min"), (int, float)) and isinstance(pr.get("max"), (int, float)):
        if protein_g < pr["min"]:
            return "below_range"
        if protein_g > pr["max"]:
            return "above_range"
        return "within_range"
    pt = calorie_budget.get("protein_target_g")
    if isinstance(pt, (int, float)) and pt > 0:
        return "met" if protein_g >= pt else "below_target"
    return None


def rolling_nutrition_summary(days=7, today=None, calorie_budget=None):
    """Reported intake only. Coverage and unknown macros are never inferred from calories."""
    d_today = today if isinstance(today, date) else date.fromisoformat(str(today or coach_clock.today())[:10])
    start_iso = (d_today - timedelta(days=max(1, days) - 1)).isoformat()
    today_iso = d_today.isoformat()
    by_day = {}
    for e in active_journal_entries():
        day = e.get("date")
        if not isinstance(day, str) or not (start_iso <= day <= today_iso):
            continue
        txt = e.get("text") or ""
        if txt.lstrip().startswith("[coach plan]"):
            continue
        values = _entry_macros(e)
        bucket = by_day.setdefault(day, {"entries_count": 0, "kcal": [], "protein_g": [],
                                         "carbs_g": [], "fat_g": []})
        bucket["entries_count"] += 1
        for key, value in zip(("kcal", "protein_g", "carbs_g", "fat_g"), values):
            bucket[key].append(value)
    coverage = nutrition_store().coverage()
    rows = []
    for day in sorted(by_day):
        b = by_day[day]
        row = {
            "date": day,
            "is_today": day == today_iso,
            "entries_count": b["entries_count"],
            "coverage": "user_reported_complete" if coverage.get(day) == "complete" else "partial_or_unconfirmed",
        }
        for key in ("kcal", "protein_g", "carbs_g", "fat_g"):
            row[key] = round(sum(b[key]), 1) if all(v is not None for v in b[key]) else None
        rows.append(row)
    completed = [r for r in rows if not r["is_today"] and r["coverage"] == "user_reported_complete"]
    avg_completed = None
    if completed:
        avg_completed = {key: (round(sum(r[key] for r in completed) / len(completed), 1)
                               if all(r[key] is not None for r in completed) else None)
                         for key in ("kcal", "protein_g", "carbs_g", "fat_g")}
    today_row = next((r for r in rows if r["is_today"]), None)
    return {
        "window_start": start_iso,
        "window_end": today_iso,
        "logged_days_count": len(rows),
        "completed_logged_days_count": len(completed),
        "protein_goal_hit_days": None,
        "completed_days_average": avg_completed,
        "today_so_far": today_row or {"date": today_iso, "is_today": True, "entries_count": 0,
                                      "coverage": "unlogged", "kcal": None, "protein_g": None,
                                      "carbs_g": None, "fat_g": None},
        "days": rows,
        "note": NUTRITION_POLICY,
    }


def _todays_food_summary(now):
    """(count, protein_g, kcal) from today's active journal food entries. protein_g / kcal are
    best-effort sums of the '~Xg protein' / '~Y kcal' estimates in the auto-log text (0 if
    none parseable); count excludes '[coach plan]' notes and superseded entries. Lets a meal
    nudge show real running totals without calling the model."""
    count = 0
    protein, kcal = [], []
    today = now.date().isoformat()
    for e in active_journal_entries()[-JOURNAL_KEEP:]:
        if e.get("date") != today:
            continue
        txt = e.get("text") or ""
        if txt.lstrip().startswith("[coach plan]"):
            continue
        count += 1
        entry_kcal, entry_prot, _c, _f = _entry_macros(e)
        protein.append(entry_prot)
        kcal.append(entry_kcal)
    return (count, round(sum(protein)) if all(v is not None for v in protein) else None,
            round(sum(kcal)) if all(v is not None for v in kcal) else None)


def _meal_nudge_text(slot, base_text, now):
    """Prepend a real-progress header (protein / kcal / items logged so far today) to the
    standard per-slot meal prompt, so the nudge reflects the whole day, not just the clock.
    Falls back to the plain prompt when nothing is logged yet."""
    count, protein, kcal = _todays_food_summary(now)
    if count <= 0:
        return base_text
    bits = []
    if protein is not None and protein > 0:
        bits.append("~%dg protein" % protein)
    if kcal is not None and kcal > 0:
        bits.append("~%d kcal" % kcal)
    if bits:
        head = "\U0001F4CA So far today: " + ", ".join(bits) + " logged.\n"
    else:
        head = "\U0001F4CA So far today: %d item%s logged.\n" % (count, "s" if count != 1 else "")
    return head + base_text


def maybe_meal_reminders():
    if not MEAL_REMINDERS_ENABLED or not _reminders_allowed():
        return
    own = owner()
    if not own:
        return
    now = coach_clock.now()
    today = coach_clock.today().isoformat()
    try:
        sent = json.loads(read_file(MEAL_STATE_FILE) or "{}")
    except Exception:  # noqa: BLE001
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    changed = False
    food_times = _todays_food_log_times(now)
    windows = _meal_slot_windows()
    for slot, hh, mm, text in MEAL_REMINDERS:
        if sent.get(slot) == today:
            continue
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < target:
            continue
        lo, hi = windows.get(slot, (0, 24 * 60))
        if any(lo <= (t.hour * 60 + t.minute) < hi for t in food_times):
            log.info("Meal reminder %s skipped - already logged", slot)
            sent[slot] = today
            changed = True
            continue
        late_min = (now - target).total_seconds() / 60
        if late_min <= MEAL_CATCHUP_MIN:
            log.info("Meal reminder: %s at %s", slot, now.strftime("%H:%M"))
            send_message(own, _meal_nudge_text(slot, text, now),
                         expires_at=target.timestamp() + MEAL_CATCHUP_MIN * 60)
        else:
            log.info("Meal reminder %s missed window (%.0f min late) - skipping",
                     slot, late_min)
        sent[slot] = today  # mark handled either way so it won't fire again today
        changed = True
    if changed:
        write_file(MEAL_STATE_FILE, json.dumps(sent))


def _reminders_allowed():
    settings = json.loads(read_file(os.path.join(STATE, "reminder_settings.json")) or "{}")
    if settings.get("paused"):
        return False
    start = int(os.environ.get("AGBOT_QUIET_START_HOUR", "22"))
    end = int(os.environ.get("AGBOT_QUIET_END_HOUR", "8"))
    if not 0 <= start <= 23 or not 0 <= end <= 23:
        raise ValueError("Quiet-hour settings must be hours from 0 to 23.")
    hour = coach_clock.now().hour
    return not (start <= hour < end if start < end else hour >= start or hour < end) if start != end else True


def hydration_slots_due(now, sent, today):
    """Pure schedule core (unit-testable): given the current datetime, the per-slot sent-map
    and today's date-string, return [(idx, slot, hour, should_send)] for hydration slots that
    have arrived today and aren't already sent; should_send is False past the catch-up window."""
    due = []
    slots = list(range(HYDRATION_START, HYDRATION_END + 1, HYDRATION_EVERY_H))
    for idx, hh in enumerate(slots):
        slot = "h%02d" % hh
        if sent.get(slot) == today:
            continue
        target = now.replace(hour=hh, minute=0, second=0, microsecond=0)
        if now < target:
            continue
        late_min = (now - target).total_seconds() / 60
        due.append((idx, slot, hh, late_min <= HYDRATION_CATCHUP_MIN))
    return due


def _hydration_progress():
    """(logged_ml, goal_ml) for today from Garmin, or (None, None) on any error.
    Fetched only when a hydration slot is actually due, so most poll loops cost nothing."""
    try:
        logged, goal = garmin_coach.hydration_today()
    except Exception as exc:  # noqa: BLE001
        log.error("hydration fetch failed: %s", exc)
        return None, None
    return logged, goal


def _hydration_on_pace(hour, logged_ml, goal_ml):
    """True only when Garmin CONFIRMS the user is at/ahead of the expected linear pace
    toward today's goal by this slot's hour - in which case the nudge is skipped. Any
    missing/unsynced data returns False so we nudge as before (fail-open)."""
    if not isinstance(logged_ml, (int, float)) or not isinstance(goal_ml, (int, float)) or goal_ml <= 0:
        return False
    span = max(1, HYDRATION_END - HYDRATION_START)
    frac = min(1.0, max(0.0, (hour - HYDRATION_START) / span))
    if logged_ml >= goal_ml:
        return True
    if frac <= 0:
        return logged_ml > 0
    return logged_ml >= goal_ml * frac


CUP_ML = 240  # approx ml per "cup" for a friendly count alongside the authoritative ml


def _hydration_nudge_text(hour, logged_ml, goal_ml, fallback):
    """A data-rich hydration nudge: real ml / goal, an approximate cup count, and how far
    behind linear pace the user is right now. Returns `fallback` (a canned line) when
    Garmin has no usable hydration data, so a nudge still goes out (fail-open)."""
    if (not isinstance(logged_ml, (int, float)) or not isinstance(goal_ml, (int, float))
            or goal_ml <= 0):
        return fallback
    span = max(1, HYDRATION_END - HYDRATION_START)
    frac = min(1.0, max(0.0, (hour - HYDRATION_START) / span))
    deficit = max(0.0, goal_ml * frac - logged_ml)
    head = ("\U0001F4A7 AgBot \u00B7 Hydration - you're at %d/%d ml (~%d/%d cups) today"
            % (round(logged_ml), round(goal_ml), round(logged_ml / CUP_ML),
               round(goal_ml / CUP_ML)))
    deficit_cups = round(deficit / CUP_ML)
    if deficit_cups >= 1:
        tail = (", about %d cup%s behind pace - grab a glass now."
                % (deficit_cups, "s" if deficit_cups != 1 else ""))
    elif deficit > 0:
        tail = ", a touch behind pace - a few sips keeps you on track."
    else:
        tail = " - nicely on track, a top-up keeps you ahead."
    return head + tail


def maybe_hydration_reminders():
    if not _reminders_allowed():
        return
    own = owner()
    if not own:
        return
    now = coach_clock.now()
    today = coach_clock.today().isoformat()
    try:
        sent = json.loads(read_file(HYDRATION_STATE_FILE) or "{}")
    except Exception:  # noqa: BLE001
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    due = hydration_slots_due(now, sent, today)
    if not due:
        return
    logged_ml, goal_ml = _hydration_progress()
    if logged_ml is None or goal_ml is None:
        log.info("stage=hydration unknown intake/goal; no deficit-based nudge")
        return
    changed = False
    for idx, slot, hh, should in due:
        if should and _hydration_on_pace(hh, logged_ml, goal_ml):
            log.info("Hydration %s skipped - on pace (%s/%s ml)", slot, logged_ml, goal_ml)
        elif should:
            log.info("Hydration reminder %s at %s (%s/%s ml)", slot,
                     now.strftime("%H:%M"), logged_ml, goal_ml)
            send_message(own, _hydration_nudge_text(
                hh, logged_ml, goal_ml, HYDRATION_MESSAGES[idx % len(HYDRATION_MESSAGES)]),
                expires_at=now.replace(hour=hh, minute=0, second=0).timestamp()
                + HYDRATION_CATCHUP_MIN * 60)
        else:
            log.info("Hydration %s missed window - skipping", slot)
        sent[slot] = today
        changed = True
    if changed:
        write_file(HYDRATION_STATE_FILE, json.dumps(sent))


# ---- Daily exercise-adherence check-ins -------------------------------------------------
# Ask a few times a day whether the recommended exercise got done. Fires ONLY while today is
# unresolved; once the user replies DWRE (done -> summary), says they're skipping/resting, or
# simply TELLS the coach in chat what today's plan is ('stated'), the check-ins stop for the
# day. Absence of any of those = 'pending'.
EXERCISE_CHECKIN_HOURS = (12, 16, 21)
EXERCISE_CHECKIN_CATCHUP_MIN = 55


def _load_exercise_state():
    try:
        with open(EXERCISE_STATE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(d, dict) or d.get("date") != coach_clock.today().isoformat():
        return {}   # stale day -> fresh 'pending' state
    return d


def _save_exercise_state(d):
    d["date"] = coach_clock.today().isoformat()
    _atomic_write(EXERCISE_STATE_FILE, json.dumps(d))


def _exercise_status():
    return _load_exercise_state().get("status") or "pending"


def _set_exercise_status(status):
    st = _load_exercise_state()
    st["status"] = status
    _save_exercise_state(st)


def _is_rest_or_recovery_plan(plan):
    if not isinstance(plan, dict):
        return False
    return plan.get("kind") in ("rest", "recovery") or bool(plan.get("rotation_defer"))


def _set_coach_rest(flag):
    """Record whether TODAY's plan prescribed rest/recovery (or deferred the pending session),
    so exercise-adherence check-ins stay quiet for the day. Cleared if a later plan the same
    day prescribes an active workout."""
    st = _load_exercise_state()
    if flag:
        st["coach_rest"] = True
    else:
        st.pop("coach_rest", None)
        st.pop("rest_note_sent", None)
    _save_exercise_state(st)


def _coach_prescribed_rest_today(st=None):
    st = _load_exercise_state() if st is None else st
    if st.get("coach_rest"):
        return True
    today_iso = coach_clock.today().isoformat()
    try:
        plans = training_store().context().get("plans") or []
    except Exception:  # noqa: BLE001
        plans = []
    for row in plans:
        if row.get("date") == today_iso:
            return _is_rest_or_recovery_plan(row.get("payload"))
    return False


def exercise_checkin_slots_due(now, asked, catchup_min=EXERCISE_CHECKIN_CATCHUP_MIN):
    """Pure core (testable): check-in slots that have arrived and aren't already asked; each
    is (slot, hour, should_send), should_send False past the catch-up window."""
    due = []
    for hh in EXERCISE_CHECKIN_HOURS:
        slot = "c%02d" % hh
        if slot in asked:
            continue
        target = now.replace(hour=hh, minute=0, second=0, microsecond=0)
        if now < target:
            continue
        late_min = (now - target).total_seconds() / 60
        due.append((slot, hh, late_min <= catchup_min))
    return due


def _exercise_checkin_message():
    logged = garmin_coach.trained_today()
    if logged:
        return ("\U0001F3CB\uFE0F AgBot check-in \u00B7 nice, I can see you've trained today. "
                "Reply DWRE when you're fully done and I'll pull your wrap-up - or tell me if "
                "you're calling it here.")
    return ("\U0001F3CB\uFE0F AgBot check-in \u00B7 have you done today's recommended exercise "
            "yet? Reply DWRE when you're done and I'll give you the summary, or say 'rest day' / "
            "'skip today' if you're not exercising and I'll stop asking.")


def _exercise_rest_message():
    """Sent ONCE on a day the coach itself prescribed rest - a gentle acknowledgement instead
    of the 'did you exercise?' nag."""
    return ("\U0001F6CC AgBot check-in \u00B7 today's a rest / recovery day per your brief - "
            "nothing to tick off, so I won't chase you. Rest up. If you did do the optional "
            "easy movement, reply DWRE and I'll wrap it up.")


def maybe_exercise_checkins():
    if not _reminders_allowed():
        return
    own = owner()
    if not own:
        return
    st = _load_exercise_state()
    if st.get("status") in ("done", "skip", "stated"):
        return  # resolved for today (finished, opted out, or they've told me their plan)
    # When the coach itself prescribed rest / recovery / rotation deferral today, stay
    # completely quiet - never nudge about logging or completing a workout.
    if _coach_prescribed_rest_today(st):
        return
    now = coach_clock.now()
    asked = list(st.get("asked") or [])
    changed = False
    for slot, hh, should in exercise_checkin_slots_due(now, asked):
        if should:
            # Redundant-nag guard: if Garmin already shows a real workout logged today (e.g.
            # a morning/evening commute ride or a gym session), don't ask 'did you exercise?'.
            # Leave status untouched so the post-workout auto-debrief still wraps up each
            # session. Fails open (asks) if the check errors.
            already_trained = None
            try:
                already_trained = garmin_coach.trained_today()
            except Exception as exc:  # noqa: BLE001
                log.warning("stage=exercise data unavailable; suppressing check-in (%s)", type(exc).__name__)
            if already_trained is None:
                log.info("stage=exercise unknown activity coverage; staying quiet")
            elif already_trained:
                log.info("Exercise check-in %s at %s - already trained today, staying quiet",
                         slot, now.strftime("%H:%M"))
            else:
                log.info("Exercise check-in %s at %s", slot, now.strftime("%H:%M"))
                send_message(own, _exercise_checkin_message(),
                             expires_at=time.time() + EXERCISE_CHECKIN_CATCHUP_MIN * 60)
        else:
            log.info("Exercise check-in %s missed window - skipping", slot)
        asked.append(slot)
        changed = True
    if changed:
        st["asked"] = asked
        _save_exercise_state(st)


_last_activity_check = 0.0


def maybe_post_workout():
    global _last_activity_check
    own = owner()
    if not own:
        return
    now = time.time()
    if now - _last_activity_check < ACTIVITY_CHECK_SECS:
        return
    _last_activity_check = now
    act = garmin_coach.latest_activity()
    if not isinstance(act, dict) or "__error__" in act:
        return
    aid = str(act.get("activity_id") or "")
    seen = read_file(LAST_ACTIVITY_FILE).strip()
    pend = _load_pending()
    # A new activity just appeared: (re)start the quiet timer and WAIT - don't debrief a
    # single part. A session is several back-to-back activities across different Garmin
    # types; we send ONE collective debrief once things have been quiet for a while.
    if aid and aid != seen:
        _snapshot_cache.invalidate()
        write_file(LAST_ACTIVITY_FILE, aid)
        if not seen:
            return  # first run - baseline only, don't debrief a historical activity
        if _exercise_status() in ("done", "skip") and not pend:
            # The user already got their DWRE wrap-up (or opted out) for today. An activity that
            # finishes syncing to Garmin just AFTER they pressed DWRE must NOT re-arm the auto-
            # debrief - that race produced a duplicate collective debrief ~90 min later. Just
            # re-baseline the seen id and stay quiet.
            log.info("New activity %s after exercise marked '%s' - not re-arming debrief",
                     aid, _exercise_status())
            return
        log.info("New activity %s (%s) - queuing collective debrief", aid, act.get("type"))
        _save_pending({"last_ts": now, "date": str(act.get("start", ""))[:10]})
        return
    # No new activity. If a session is pending and it's been quiet long enough, send the one
    # collective debrief for the whole just-finished block.
    if pend and (now - float(pend.get("last_ts", 0) or 0) >= DEBRIEF_QUIET_SECS):
        if (pend.get("date") == coach_clock.today().isoformat()
                and _exercise_status() == "skip" and not pend.get("user_requested")):
            _clear_pending(pend["date"])
            return
        day = pend.get("date") or coach_clock.today().isoformat()
        block = garmin_coach.session_block(target_day=date.fromisoformat(day))
        if isinstance(block, dict) and "__error__" in block:
            log.error("session_block error, will retry next poll: %s", block.get("__error__"))
            return  # keep pending; retry
        if block:
            log.info("Quiet for %dm - collective debrief of %d activities",
                     DEBRIEF_QUIET_SECS // 60, len(block))
            _deliver_debrief(own, block, day, source_text=pend.get("source_text", ""))
        else:
            _save_pending(dict(pend, last_ts=now - DEBRIEF_QUIET_SECS + ACTIVITY_CHECK_SECS))


def evaluate_red_flags(snap):
    flags = []
    r = snap.get("training_readiness") or {}
    if str(r.get("level", "")).upper() in ("LOW", "VERY_LOW"):
        flags.append("readiness LOW")
    sl = snap.get("last_night_sleep") or {}
    secs = sl.get("time_asleep_s")
    if isinstance(secs, (int, float)) and secs < 5.5 * 3600:
        flags.append("short sleep")
    hv = snap.get("hrv") or {}
    last = hv.get("last_night_avg")
    base_low = hv.get("baseline_balanced_low")
    if (isinstance(last, (int, float)) and isinstance(base_low, (int, float))
            and last < base_low):
        flags.append("HRV below baseline")
    return flags


def maybe_red_flags():
    own = owner()
    if not own:
        return
    today = coach_clock.today().isoformat()
    if read_file(RED_FLAGS_FILE).strip() == today:
        return
    if (read_file(LAST_SUMMARY_FILE).strip() == today
            or runtime_store().summary_delivered(today)
            or runtime_store().summary_pending(today)):
        write_file(RED_FLAGS_FILE, today)  # the brief already covers this ground
        return
    now = coach_clock.now()
    cur = (now.hour, now.minute)
    if cur < (5, 30) or cur >= AUTO_START:
        return
    snap = get_snapshot()
    if isinstance(snap, dict) and "__error__" in snap:
        return
    flags = evaluate_red_flags(snap)
    if not flags:
        write_file(RED_FLAGS_FILE, today)
        return
    log.info("Red flags this morning: %s", flags)
    prompt, _ = _assemble(SUMMARY_PROMPT_FILE, data=snap)
    prompt += ("\n\nURGENT_CONTEXT: It is early morning and these red flags fired: "
               + ", ".join(flags) + ". Instead of a full brief write a SHORT (< 90 "
               "words) early heads-up: name the concern(s) plainly and give ONE "
               "adjustment for today (dial back intensity / prioritise recovery). "
               "First line: \U0001F916 AgBot \u26A0\uFE0F Heads-up \u00B7 " + today_human() + ".")
    text = _generate_checked(prompt)
    if text:
        append_history("agbot", "[early red-flag alert] " + text)
        send_message(own, text)
        write_file(RED_FLAGS_FILE, today)


def acquire_singleton_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(1)
    except OSError:
        log.error("Another AgBot instance is already running. Exiting.")
        sys.exit(0)
    return s  # keep referenced for process lifetime


def _poll_messages():
    global _poll_progress
    delay = 5
    while not _stop.is_set():
        try:
            if _draining.is_set():
                _stop.wait(1)
                continue
            params = {"timeout": POLL_TIMEOUT}
            offset = runtime_store().offset(get_offset())
            if offset is not None:
                params["offset"] = offset
            response = tg("getUpdates", params, timeout=POLL_TIMEOUT + 15)
            if not response.get("ok"):
                raise RuntimeError(response.get("description", "Telegram poll rejected"))
            runtime_store().ingest(response.get("result", []))
            _poll_progress = time.time()
            delay = 5
        except (http.client.RemoteDisconnected, urllib.error.URLError, TimeoutError, ConnectionResetError) as exc:
            log.warning("stage=poll reconnecting error_type=%s; cursor retained", type(exc).__name__)
            _stop.wait(delay)
            delay = min(60, delay * 2)
        except Exception:
            log.exception("stage=poll failed; durable cursor retained")
            _stop.wait(delay)
            delay = min(60, delay * 2)


def _publish_messages():
    global _publisher_progress
    while not _stop.is_set():
        try:
            flush_outbox()
            _publisher_progress = time.time()
        except Exception:
            log.exception("stage=outbox failed; pending messages retained")
        _stop.wait(2)


def _health_report():
    now = time.time()
    busy = _worker_started is not None
    components = {
        "poller": _poll_progress is not None and (_draining.is_set() or now - _poll_progress < 180),
        "outbox": _publisher_progress is not None and now - _publisher_progress < 30,
        "worker": (now - (_worker_started or _last_progress)) < max(360, COPILOT_TIMEOUT * 2 + 60),
        "scheduler": (now - (_worker_started or _last_progress)) < max(360, COPILOT_TIMEOUT * 2 + 60),
    }
    report = {
        "status": ("degraded" if _llm_last_error else
                   "busy" if busy else "ready"),
        "ready": all(components.values()) and not _llm_last_error,
        "components": components,
        "worker_busy": busy,
        "scheduled_jobs_busy": _scheduled_job is not None,
        "draining": _draining.is_set(),
        "model": COPILOT_MODEL, "reasoning": COPILOT_REASONING_EFFORT,
        "active_model": _llm_active_model,
        "fallback_models": list(COPILOT_FALLBACK_MODELS),
        "last_model_error": _llm_last_error,
        "release": os.environ.get("AGBOT_RELEASE", "development"),
        "worker_busy_seconds": (round(time.time() - _worker_started)
                                if _worker_started else 0),
        "last_progress_age_seconds": round(time.time() - _last_progress),
        "queues": runtime_store().counts(),
        "queue_health": runtime_store().queue_health(),
    }
    if not report["ready"]:
        report["status"] = "degraded"
    if PROGRAM_ENABLED:
        context = program_store().peek_context()
        active = context.get("active") or {}
        report["programme"] = {
            "enabled": True, "revision": active.get("revision"),
            "next_review_date": context.get("next_review_date"),
            "review_due": context.get("review_due"), "last_error": context.get("last_error"),
        }
    return report


def _serve_health(listener):
    listener.settimeout(1)
    while not _stop.is_set():
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        with connection:
            connection.settimeout(2)
            try:
                request = connection.recv(4096)
                if request.startswith(b"POST /drain ") or request.startswith(b"POST /resume "):
                    if request.startswith(b"POST /drain "):
                        _draining.set()
                    else:
                        _draining.clear()
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
                    continue
                if not request.startswith(b"GET /health "):
                    connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    continue
                body = json.dumps(_health_report()).encode("utf-8")
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                   + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            except (OSError, ValueError):
                log.exception("stage=health request failed")


def _process_request(item):
    messages = item["payload"]["messages"]
    if item["payload"].get("album"):
        chat_id = messages[0]["chat"]["id"]
        if not _owner_ok(chat_id):
            return
        _set_msg_sent_at(messages[0].get("date"))
        caption = next((m["caption"] for m in messages if m.get("caption")), "")
        do_images(chat_id, [_photo_file_id(m) for m in messages], caption)
    else:
        dispatch(messages[0])


def main():
    global _current_request, _send_sequence, _generation_sequence, _worker_started, _last_progress
    global _poll_progress, _publisher_progress, _request_deadline, _scheduled_job
    listener = acquire_singleton_lock()
    if not os.path.isfile(PROFILE_FILE):
        raise FileNotFoundError("Create your private profile first: " + PROFILE_FILE)
    runtime_store().recover()
    memory_store()
    training_store()
    if PROGRAM_ENABLED:
        program_store()
        _rotation_context(reconcile=True)
    me = tg("getMe")
    if not me.get("ok"):
        raise RuntimeError("Telegram startup check failed.")
    _poll_progress = _publisher_progress = time.time()
    log.info("AgBot online as @%s", me.get("result", {}).get("username"))
    log.info("LLM model=%s reasoning-effort=%s release=%s",
             COPILOT_MODEL or "(CLI default)", COPILOT_REASONING_EFFORT,
             os.environ.get("AGBOT_RELEASE", "development"))
    for target, args in ((_poll_messages, ()), (_publish_messages, ()),
                         (_serve_health, (listener,))):
        threading.Thread(target=target, args=args, daemon=True).start()
    next_schedule = 0
    job_index = 0
    jobs = (maybe_program_review, maybe_auto_summary, maybe_red_flags, maybe_post_workout, maybe_meal_reminders,
            maybe_hydration_reminders, maybe_exercise_checkins)
    while not _stop.is_set():
        item = runtime_store().claim()
        if item:
            _current_request = item["id"]
            _send_sequence = _generation_sequence = 0
            _worker_started = time.time()
            _request_deadline = time.monotonic() + max(300, COPILOT_TIMEOUT * 2)
            try:
                _process_request(item)
                runtime_store().finish(item["id"])
            except Exception as exc:
                log.exception("stage=request failed id=%s", item["id"])
                state = runtime_store().fail(item["id"], str(exc),
                                             max_attempts=1 if isinstance(exc, ValueError) else 4)
                chat_id = item["payload"]["messages"][0]["chat"]["id"]
                notice = ("AgBot: I couldn't complete this request. "
                          "It is retained as failed; please resend when ready."
                          if state == "failed" else
                          "AgBot: I couldn't complete this yet. Your request is saved; "
                          "I'll retry automatically.")
                # Failure notices have their own stable identity, not the answer's slot.
                runtime_store().enqueue(f"{item['id']}:notice:{state}", chat_id,
                                        {"text": notice})
            finally:
                log.info("stage=request id=%s seconds=%.2f", item["id"],
                         time.time() - _worker_started)
                _current_request = None
                _worker_started = None
                _request_deadline = None
                _last_progress = time.time()
            continue
        if not _draining.is_set() and time.time() >= next_schedule:
            _set_msg_sent_at(None)
            job = jobs[job_index]
            _scheduled_job = job.__name__
            _current_request = f"scheduled:{job.__name__}:{int(time.time() // 60)}"
            _send_sequence = _generation_sequence = 0
            _worker_started = time.time()
            _request_deadline = time.monotonic() + max(300, COPILOT_TIMEOUT * 2)
            try:
                job()
            except Exception:
                log.exception("stage=scheduler job=%s failed; next tick will retry", job.__name__)
            finally:
                _scheduled_job = _current_request = _worker_started = _request_deadline = None
                job_index = (job_index + 1) % len(jobs)
                next_schedule = time.time() + (60 if job_index == 0 else 0)
                _last_progress = time.time()
        if _draining.is_set():
            _last_progress = time.time()
        _stop.wait(1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if "--selftest-summary" in sys.argv:
        text, _snap = generate_summary()
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-voice" in sys.argv:
        idx = sys.argv.index("--selftest-voice")
        audio = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        print("---- OUTPUT ----")
        print("transcript:", transcribe_audio(audio) if audio else "(pass an audio path)")
        sys.exit(0)
    if "--selftest-qa" in sys.argv:
        q = sys.argv[-1]
        text, _snap = generate_qa(q)
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-weekly" in sys.argv:
        text, _ = generate_weekly()
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-nutrition" in sys.argv:
        text, _ = generate_nutrition()
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-performance" in sys.argv:
        text, _ = generate_performance()
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-debrief" in sys.argv:
        block = garmin_coach.session_block()
        print("BLOCK:", json.dumps(block, default=str)[:500])
        text = (generate_debrief(block)
                if isinstance(block, list) and block else None)
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    if "--selftest-hydration" in sys.argv:
        _t = coach_clock.today().isoformat()

        def _hy(h, m, sent=None):
            return hydration_slots_due(coach_clock.now().replace(hour=h, minute=m, second=0,
                                                               microsecond=0), sent or {}, _t)
        assert _hy(7, 59) == [], "pre-8am must be empty"
        _d8 = _hy(8, 0)
        assert [x[1] for x in _d8] == ["h08"] and _d8[0][3], _d8
        assert _hy(8, 40)[0][3] is True, "40m late still sends"
        assert _hy(9, 10)[0][3] is False, "70m late suppressed"
        assert _hy(8, 30, {"h08": _t}) == [], "already-sent skipped"
        _all = _hy(22, 0)
        assert [x[1] for x in _all] == ["h08", "h10", "h12", "h14", "h16", "h18", "h20", "h22"], _all
        assert [x[1] for x in _all if x[3]] == ["h22"], "only h22 within catch-up"
        print("HYDRATION_SELFTEST_OK")
        sys.exit(0)
    if "--selftest-checkin" in sys.argv:
        def _ck(h, m, asked=None):
            return exercise_checkin_slots_due(coach_clock.now().replace(hour=h, minute=m, second=0,
                                                                     microsecond=0), asked or [])
        # Derive expected slots from EXERCISE_CHECKIN_HOURS so the test self-adjusts if the
        # schedule ever changes (the previous fixture hard-coded a since-removed 10am slot).
        _hours = EXERCISE_CHECKIN_HOURS
        _slots = ["c%02d" % h for h in _hours]
        _h0, _s0 = _hours[0], _slots[0]
        if _h0 >= 1:
            assert _ck(_h0 - 1, 59) == [], "before first check-in empty"
        _d0 = _ck(_h0, 0)
        assert [x[0] for x in _d0] == [_s0] and _d0[0][2], _d0
        assert _ck(_h0, 40)[0][2] is True, "40m late still asks"
        assert _ck(_h0 + 1, 10)[0][2] is False, "70m late suppressed"
        assert _ck(_h0, 30, [_s0]) == [], "already-asked skipped"
        _allc = _ck(_hours[-1], 0)
        assert [x[0] for x in _allc] == _slots, _allc
        assert [x[0] for x in _allc if x[2]] == [_slots[-1]], "only last slot within catch-up"
        print("CHECKIN_SELFTEST_OK")
        sys.exit(0)
    if "--selftest-image" in sys.argv:
        idx = sys.argv.index("--selftest-image")
        img = sys.argv[idx + 1]
        cap = sys.argv[idx + 2] if len(sys.argv) > idx + 2 else ""
        text, _ = generate_image(img, cap)
        print("---- OUTPUT ----")
        print(text)
        sys.exit(0)
    main()
