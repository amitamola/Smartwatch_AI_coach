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
JOURNAL_FILE = os.path.join(STATE, "journal.jsonl")
HEALTH_FILE = os.path.join(STATE, "health.jsonl")
ANCHOR_FILE = os.path.join(STATE, "anchors.jsonl")
LAST_ACTIVITY_FILE = os.path.join(STATE, "last_activity_id.txt")
PENDING_DEBRIEF_FILE = os.path.join(STATE, "pending_debrief.json")
RED_FLAGS_FILE = os.path.join(STATE, "red_flags_date.txt")
MEAL_STATE_FILE = os.path.join(STATE, "meal_reminders.json")
HYDRATION_STATE_FILE = os.path.join(STATE, "hydration_reminders.json")
MOVEMENT_STATE_FILE = os.path.join(STATE, "movement_reminders.json")
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

# Auto-summary window (local time). If no summary has been sent yet today and the
# current time falls in this window, one is pushed automatically.
AUTO_START = (9, 30)
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

# Movement nudges: if Garmin shows a long uninterrupted SITTING stretch during the day, a light
# "get up and move" reminder - modelled on the hydration nudge but FAIL-CLOSED: it fires ONLY
# when recent inactivity is CONFIRMED (never on stale data, during a nap, or when the user has
# been moving), matching a "remind me only when I've actually been sedentary" request. Deduped
# per slot per day in MOVEMENT_STATE_FILE.
MOVEMENT_START = 9
MOVEMENT_END = 21
MOVEMENT_EVERY_H = 2
MOVEMENT_CATCHUP_MIN = 45
MOVEMENT_SEDENTARY_MIN = 50    # need at least this many trailing sedentary minutes to nudge
MOVEMENT_STALE_MAX_MIN = 60    # skip if the latest intraday bucket is older than this (unconfirmed)
MOVEMENT_POST_ACTIVITY_MIN = 60  # stay quiet this long after a logged workout/commute ends -
                                 # step-less efforts (e-bike, cycling, lifting) look 'sedentary'
                                 # in Garmin's step buckets, so never nudge straight after one
# ...and never nudge DURING a step-less workout that hasn't saved yet (indoor bike, rowing):
# a fresh, elevated heart rate means the user is exercising right now even with ~zero steps.
MOVEMENT_HR_ACTIVE_DELTA = 30    # bpm over resting that flags "currently exercising"
MOVEMENT_HR_ACTIVE_FLOOR = 100   # ...and an absolute bpm floor
MOVEMENT_HR_MAX_AGE = 20         # only trust an HR reading this fresh (minutes)
MOVEMENT_MESSAGES = (
    "\U0001FA91 AgBot \u00B7 Move break - Garmin suggests little recent movement "
    "({mins}). If you aren't already active and it feels comfortable, take a short break.",
    "\U0001F9CD AgBot \u00B7 Movement check ({mins} with little recorded movement). "
    "A comfortable change of position or brief walk may be useful; skip anything painful.",
    "\U0001FA91 AgBot \u00B7 Break reminder - limited movement recorded for {mins}. "
    "If appropriate, pause work and move comfortably. The watch cannot confirm your posture.",
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
        logging.FileHandler(os.path.join(LOGS, "telegram_bridge.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("agbot")

sys.path.insert(0, BASE)
import garmin_coach  # noqa: E402
from coach_memory import MemoryStore
from coach_plan import TrainingStore, parse_plans, render_plans, unsupported_claims
from coach_runtime import RuntimeStore, SnapshotCache, message_key
from telegram_formatting import md_to_html, html_chunks, html_to_plain as _html_to_plain

_memory_store = None
_training_store = None
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


def runtime_store():
    global _runtime_store
    if _runtime_store is None:
        _runtime_store = RuntimeStore(os.path.join(STATE, "transport.sqlite3"))
    return _runtime_store


def get_snapshot(force=False):
    started = time.monotonic()
    result = _snapshot_cache.get(force=force)
    if isinstance(result, dict) and "__error__" not in result:
        training_store().record_activities(result.get("recent_activities_7d"))
    log.info("stage=snapshot seconds=%.2f cached_age=%s", time.monotonic() - started,
             (result.get("snapshot_cache") or {}).get("age_seconds")
             if isinstance(result, dict) else None)
    return result


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


def append_history(role, text):
    if role == "user":
        text = _defang_markers(text)
    hist = load_history()
    if _current_request and any(t.get("request_id") == _current_request
                               and t.get("role") == role and t.get("text") == text for t in hist):
        return
    hist.append({"role": role, "text": text,
                 "request_id": _current_request,
                 "ts": ((_MSG_SENT_AT if role == "user" and _MSG_SENT_AT else datetime.now())
                        .isoformat(timespec="seconds"))})
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
                      json.dumps({"date": date.today().isoformat(), "text": text},
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
    if isinstance(d, dict) and d.get("date") == date.today().isoformat():
        return (d.get("text") or "").strip()
    return ""


def _load_pending():
    """Pending post-workout debrief marker: {'last_ts': epoch of the last new activity}."""
    try:
        with open(PENDING_DEBRIEF_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pending(d):
    try:
        _atomic_write(PENDING_DEBRIEF_FILE, json.dumps(d))
    except Exception as exc:  # noqa: BLE001
        log.error("pending save failed: %s", exc)


def _clear_pending():
    try:
        os.remove(PENDING_DEBRIEF_FILE)
    except FileNotFoundError:
        pass


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
    delta = (date.today() - d).days
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
    return 0 <= (date.today() - d).days <= days


def recent_history_text():
    hist = load_history()
    hist = [t for t in hist if _within_days(t.get("ts"), HISTORY_PROMPT_DAYS)]
    hist = hist[-HISTORY_PROMPT_TURNS:]
    # Older user statements are more useful than repeatedly injecting long bot answers.
    last_assistants = {id(t) for t in [t for t in hist if t.get("role") != "user"][-4:]}
    hist = [t for t in hist if t.get("role") == "user" or id(t) in last_assistants]
    lines, used, omitted = [], 0, 0
    for turn in reversed(hist):
        who = "You" if turn.get("role") == "user" else "AgBot"
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
    prefix = f"[Bounded context: {omitted} older/oversized turns omitted.]\n" if omitted else ""
    return prefix + "\n".join(reversed(lines))


def journal_entry_id(entry):
    identity = [entry.get("request_id") or entry.get("ts"), entry.get("date"), entry.get("text")]
    return entry.get("entry_id") or "note:" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def journal_entries():
    return [json.loads(line) for line in read_file(JOURNAL_FILE).splitlines() if line.strip()]


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
    entry = {"date": on_date or (target["date"] if target else date.today().isoformat()),
             "text": text.strip(),
             "ts": datetime.now().isoformat(timespec="seconds"), "request_id": _current_request}
    if date.fromisoformat(entry["date"]) > date.today():
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
    today = date.today().isoformat()
    if not on_date or on_date == today:
        return base
    try:
        d = date.fromisoformat(on_date)
    except ValueError:
        return base
    label = "yesterday" if (date.today() - d).days == 1 else d.strftime("%a %d %b")
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
        _MSG_SENT_AT = datetime.fromtimestamp(ts) if ts else None
    except (TypeError, ValueError, OSError):
        _MSG_SENT_AT = None


def _sent_backdate():
    """'YYYY-MM-DD' when the current message was SENT on an earlier day than today - i.e. it
    was delivered late - else None. Used to file its food on the day it was actually eaten."""
    if not _MSG_SENT_AT:
        return None
    d = _MSG_SENT_AT.date()
    return d.isoformat() if d < date.today() else None


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
        if day != date.today().isoformat() and size + len(line) > JOURNAL_PROMPT_CHARS:
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
    return memory_store().render("anchor", query=query, max_chars=8000)


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
            age_h = ((datetime.now()
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


def send_message(chat_id, text, summary_date=None):
    global _send_sequence
    text = _strip_control_markers(text)
    rendered = md_to_html(text)
    keys = []
    chunks = [chunk for chunk in html_chunks(rendered, 4000) if _html_to_plain(chunk).strip()]
    for index, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        _send_sequence += 1
        key = message_key(_current_request, _send_sequence, chat_id, chunk)
        payload = {"text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if summary_date:
            payload.update(_summary_date=summary_date, _summary_complete=index == len(chunks) - 1)
        runtime_store().enqueue(key, chat_id, payload)
        keys.append(key)
    return keys


def flush_outbox():
    if not _delivery_lock.acquire(blocking=False):
        return
    try:
        for item in runtime_store().due_messages():
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
                runtime_store().delivery_failed(item["id"], description, retry)
                log.error("stage=delivery queued_for_retry=true error=%s", description)
                break  # Preserve chunk order while the endpoint is unavailable.
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


def run_llm(prompt, image=None, images=None):
    """Generate a reply from the configured model/agent backend.

    *** THIS IS THE ONE PLACE TO CHANGE TO USE A DIFFERENT MODEL. ***
    Select a backend with the AGBOT_LLM env var (default "copilot"). To add your
    own, write a _llm_<name>(prompt, images) -> str and register it in _LLM_BACKENDS
    below. `images` is a list of local image file paths; only vision-capable
    backends use them (text-only backends simply ignore them).
    """
    global _llm_last_outage, _generation_sequence
    _llm_last_outage = False
    _generation_sequence += 1
    cache_key = (f"{_current_request}:generation:{_generation_sequence}"
                 if _current_request else None)
    cached = runtime_store().saved_generation(cache_key)
    if cached is not None:
        return cached
    if len(prompt) > 120000:
        log.warning("prompt is very large (%d chars, ~%dK tokens)", len(prompt), len(prompt) // 4000)
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
        result = backend(prompt, imgs)
        if result:
            runtime_store().save_generation(cache_key, result)
        return result
    finally:
        log.info("stage=llm configured=%s active=%s seconds=%.2f prompt_chars=%d images=%d",
                 COPILOT_MODEL, _llm_active_model,
                 time.monotonic() - started, len(prompt), len(imgs))


def _llm_copilot(prompt, images):
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
        "--no-color", "--no-remote", "--no-remote-export",
        "--reasoning-effort", COPILOT_REASONING_EFFORT, "--context", "default",
    ]
    for att in images:
        if att:
            cmd += ["--attachment", att]
    deadline = time.monotonic() + COPILOT_TIMEOUT
    for model in dict.fromkeys((COPILOT_MODEL, *COPILOT_FALLBACK_MODELS)):
        if _unavailable_models.get(model, 0) > time.monotonic():
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _llm_last_error = "model_timeout"
            log.error("Copilot model attempts exhausted the %ss timeout", COPILOT_TIMEOUT)
            return None
        command = cmd + (["--model", model] if model else [])
        try:
            res = subprocess.run(
                command, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=remaining, env=env, cwd=BASE,
            )
        except subprocess.TimeoutExpired:
            _llm_last_error = "model_timeout"
            log.error("copilot model=%s timed out", model or "(CLI default)")
            return None
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
            log.error("copilot model=%s exit %s: %s", model, res.returncode, stderr[:600])
            return None
        answer = _scrub(res.stdout or "")
        if not answer:
            _llm_last_error = "empty_response"
            log.error("copilot model=%s returned an empty answer", model)
            return None
        _unavailable_models.pop(model, None)
        _llm_active_model = model or "(CLI default)"
        _llm_last_error = None
        _llm_last_outage = False
        if model != COPILOT_MODEL:
            log.warning("stage=llm fallback configured=%s active=%s", COPILOT_MODEL, model)
        return answer
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
    return datetime.now().strftime("%a %d %b")


def _enrich_activity_context(data, question="", summary=False):
    activities = data.get("recent_activities_7d") or data.get("activities") or []
    requested_day = None
    if summary or re.search(r"\byesterday\b", question, re.IGNORECASE):
        requested_day = (date.today() - timedelta(days=1)).isoformat()
    elif re.search(r"\btoday\b", question, re.IGNORECASE):
        requested_day = date.today().isoformat()
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


def _compact_training_context(context, data, data_key):
    """Reference repeated workout payloads without removing their sole occurrence."""
    supplied, sequences, sequence_values = {}, {}, {}
    for field in ("recent_activities_7d", "activities"):
        for index, activity in enumerate(data.get(field, []) or []):
            aid = str(activity.get("activity_id"))
            supplied[aid] = (activity, f"{data_key}.{field}[{index}]")
            sets = activity.get("logged_sets") or []
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
                              if key not in activity or value != activity[key]}
            row["activity_reference"] = reference
    for movement in context["latest_observed_per_movement"].values():
        aid = str(movement["activity_id"])
        if aid in sequences and "set_sequence" in movement["recorded_sets"]:
            movement["recorded_sets"] = dict(movement["recorded_sets"])
            movement["recorded_sets"].pop("set_sequence")
            movement["chronology_reference"] = sequences[aid]
    context["reference_note"] = (
        "References point to complete data elsewhere in this same prompt. "
        "Unrepeated fields remain in payload; a reference is not missing evidence.")
    return context


def _nutrition_focused(prompt_file, question):
    if prompt_file == NUTRITION_PROMPT_FILE:
        return True
    question = question or ""
    return bool(
        re.search(r"\b(?:food|meal|breakfast|lunch|dinner|protein|calories|oats|scoop|hydration)\b",
                  question, re.IGNORECASE)
        and not re.search(r"\b(?:exercise|workout|training|sets?|reps?|press|curl|row|squat|"
                          r"deadlift|rdl|run|running|bike|cycling|pain|injury|knee|back|shoulder)\b",
                          question, re.IGNORECASE))


def _nutrition_context(data, training):
    data = dict(data)
    for field in ("recent_activities_7d", "activities"):
        if field in data:
            data[field] = [{key: value for key, value in activity.items()
                            if key not in ("logged_sets", "detail_metrics")}
                           for activity in data[field]]
    for outcome in training["recent_outcomes"]:
        outcome["payload"] = {key: value for key, value in outcome["payload"].items()
                              if key not in ("logged_sets", "detail_metrics")}
    training["latest_observed_per_movement"] = {}
    training["detail_selection"] = (
        "Nutrition-focused view: exact set timelines/capabilities are omitted, not absent. "
        "Activity totals, plans, reported day status, active symptoms and preferences remain.")
    return data, training


def _assemble(prompt_file, question=None, include_history=True,
              data=None, data_key="GARMIN_JSON", include_brief=True):
    if data is None:
        data = get_snapshot()
    nutrition_focused = _nutrition_focused(prompt_file, question)
    if (prompt_file in (SUMMARY_PROMPT_FILE, PERF_PROMPT_FILE)
            or re.search(r"\b(?:workout|exercise|training|stamina|running dynamics|"
                         r"fat burner|fat burned|fuel use|carbohydrate burned)\b",
                         question or "", re.IGNORECASE)):
        _enrich_activity_context(data, question or "", summary=prompt_file == SUMMARY_PROMPT_FILE)
    training = training_store().context()
    if nutrition_focused:
        data, training = _nutrition_context(data, training)
    js = json.dumps(data, separators=(",", ":"), default=str)
    parts = [
        read_file(prompt_file),
        "\n\n---\nTODAY: " + today_human() + " (" + date.today().isoformat() + ")\n",
    ]
    delayed = _delayed_message_note()
    if delayed:
        parts.append(delayed)
    if USER_NAME:
        parts.append("\nUSER: the person you are coaching is named " + USER_NAME
                     + " - you may address them by their first name.\n")
    flags = active_health_text()
    if flags:
        parts.append("\nACTIVE HEALTH FLAGS (injuries/illness the user reported and has NOT "
                     "marked recovered - respect these: adapt or rest, don't train through "
                     "them, and check how they're doing):\n" + flags + "\n")
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
    if include_brief:
        brief = todays_brief_text()
        if brief:
            parts.append("\nTODAY'S PREVIOUS BRIEF (historical advice, not proof of execution; "
                         "a later dated TRAINING_STATE revision takes precedence):\n"
                         + brief + "\n")
    notes = recent_journal_text()
    if notes:
        parts.append("\nNOTES YOU'VE SHARED (durable context the user logged):\n"
                     + notes + "\n")
    hist = recent_history_text() if include_history else ""
    fit = None if nutrition_focused else load_fitness_profile()
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
    training = _compact_training_context(training, data, data_key)
    parts.append("\n\nTRAINING_STATE:\n" + json.dumps(training,
                 separators=(",", ":"), default=str))
    parts.append("\n\n" + read_file(os.path.join(PROMPTS, "coaching_policy.md")))
    if question is not None:
        parts.append("\n\nQUESTION:\n" + question)
    return "".join(parts), data


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
                if date.fromisoformat(day) > date.today():
                    errors.append("Do not log future food consumption.")
            except ValueError:
                errors.append("LOG must use a valid calendar date.")
    if not logs:
        return errors
    visible = _LOG_MARKER_RE.sub("", _LOG_REPLACES_RE.sub("", text))
    progress_pattern = r"\b(?:daily|today['\u2019]s)\s+(?:progress|totals?|intake|tally)\b"
    sections = {
        "Meal estimate": r"\b(?:meal estimate|meal breakdown|food breakdown|rough estimates?)\b",
        "Meal total": (r"\b(?:meal|snack)\s+(?:total|summary)\b|"
                       r"^\s*(?:[-*]\s+)?(?:\*\*)?Total\s*:"),
        "Daily progress": progress_pattern,
    }
    for section, pattern in sections.items():
        if not re.search(pattern, visible, re.IGNORECASE | re.MULTILINE):
            errors.append("Food reply needs a visible " + section + " section.")
    progress = re.search(progress_pattern, visible, re.IGNORECASE)
    if progress:
        tail = visible[progress.end():]
        if not re.search(r"\b(?:calories|kcal)\b", tail, re.IGNORECASE):
            errors.append("Daily progress must include calories eaten, target and remaining.")
        if not re.search(r"\bprotein\b", tail, re.IGNORECASE):
            errors.append("Daily progress must include protein eaten, target and remaining.")
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


def _generate_checked(prompt, source_text="", images=None, require_plan=False):
    """Persist source-backed facts and validate proposals before returning an answer."""
    text = run_llm(prompt, images=images)
    receipts = []
    pending_memory_errors = []
    for attempt in range(2):
        if not text:
            return None
        # User-sourced facts stand independently of whether the proposed workout is valid.
        # Apply them first so a newly stated exclusion guards this very response.
        candidate, changes, memory_errors = memory_store().process_markers(
            text, source_text=source_text, source_type="user")
        receipts.extend(changes)
        if memory_errors:
            pending_memory_errors = memory_errors
        elif changes:
            pending_memory_errors = []
        clean, plans, errors = parse_plans(candidate, memory_store().records("preference"))
        errors.extend(unsupported_claims(clean))
        errors.extend(_food_reporting_errors(clean, source_text))
        if require_plan and not plans:
            errors.append("a dated structured SESSION_PLAN is required")
        if require_plan in (True, "today") and not any(
                p["date"] == date.today().isoformat() for p in plans):
            errors.append("today's structured SESSION_PLAN is required")
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
                       "never a paraphrase. Do not relax user constraints.",
                       images=images)
    if plans:
        training_store().save(plans, source="user_requested_revision" if source_text
                              else "scheduled_coaching_proposal")
        for plan in plans:
            if plan["date"] == date.today().isoformat():
                _set_coach_rest(plan["kind"] == "rest")
    clean = _MODEL_LOG_RECEIPT_RE.sub("", clean)
    replacements = _LOG_REPLACES_RE.findall(clean)
    clean = _LOG_REPLACES_RE.sub("", clean)
    clean, logged, log_date = _extract_log_marker(clean)
    if logged and source_text:
        log_date = log_date or (None if replacements else _sent_backdate())
        entry = append_journal(logged, on_date=log_date,
                               replaces_entry_id=replacements[0] if replacements else None)
        clean = re.sub(r"(?m)^Logged\.\s*", "", clean)
        clean += _logged_confirmation(entry["date"], updated=bool(replacements))
    if source_text:
        clean = _harvest_exercise_plan(clean)
    clean = _strip_control_markers(clean)
    if plans:
        clean += "\n\n" + render_plans(plans)
    saved = [r for r in receipts if r.get("saved")]
    if saved:
        clean += "\n\nMemory updated:\n" + "\n".join(
            "- " + r.get("action", "saved") + ": " + r["text"] for r in saved)
    if pending_memory_errors:
        log.warning("stage=memory rejected_updates=%s", pending_memory_errors)
        clean += "\n\nA memory update was not saved; that change has not been made permanent."
    clean = _present_reply(clean)
    if plans and source_text and any(p["date"] == date.today().isoformat() for p in plans):
        save_todays_brief(clean)
    return clean


def generate_summary():
    prompt, snap = _assemble(SUMMARY_PROMPT_FILE, include_history=True, include_brief=False)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = _generate_checked(prompt, require_plan=True)
    if text:
        append_history("user", "GMS (requested the morning brief)")
        append_history("agbot", text)
        save_todays_brief(text)
    return text, snap


def generate_qa(question):
    prompt, snap = _assemble(QA_PROMPT_FILE, question=question, include_history=True)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    require_plan = bool(re.search(
        r"\b(?:give|recommend|prescribe|plan)\b.{0,60}\b(?:workout|session|routine)\b",
        question, re.IGNORECASE | re.DOTALL))
    text = _generate_checked(prompt, source_text=question,
                             require_plan="any" if require_plan else False)
    if text:
        append_history("user", question)
        append_history("agbot", text)
    if text and _is_workout_review(question):
        _clear_pending()  # asked how they did -> don't also auto-debrief this session later
    return text, snap




def generate_images(image_paths, caption, extra=None, media_label="photo"):
    n = len(image_paths)
    if n > 1:
        q = caption or ("(no caption - these items were sent together; analyze them AS "
                        "A SET and coach me on them)")
    else:
        q = caption or "(no caption - analyze this image and coach me on it)"
    prompt, snap = _assemble(IMAGE_PROMPT_FILE, question=q, include_history=True)
    if n > 1:
        prompt += ("\n\nATTACHMENTS: " + str(n) + " images are attached and belong to a "
                   "SINGLE request. READ EVERY image first - they may be the same kind of "
                   "thing OR complementary parts (e.g. rounds 1-8 on one and 9-16 on the "
                   "next, or page 1 and page 2). Give ONE combined answer that ACCOUNTS FOR "
                   "THE CONTENT OF EVERY image - never analyse just one and never drop a part.")
    if extra:
        prompt += "\n\n" + extra
    text = _generate_checked(prompt, source_text=caption or "", images=image_paths)
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


def generate_debrief(activities):
    """Collective post-workout debrief. `activities` is the just-finished session block - a
    list of trimmed activities the user logged back-to-back, often split across Garmin types
    (a cardio warm-up, the Strength block, a run, a Pilates/stretch cool-down). The WHOLE
    session is judged against this morning's plan at once, so we never nag after a single
    part. Accepts a single activity dict too, for back-compat."""
    if isinstance(activities, dict):
        activities = [activities]
    activities = [a for a in (activities or []) if isinstance(a, dict)]
    if not activities:
        return None
    snap = get_snapshot(force=True)
    base_prompt, _ = _assemble(DEBRIEF_PROMPT_FILE, data=snap)
    parts = [base_prompt]
    for a in activities:
        if "strength" in str(a.get("type") or ""):
            sets = garmin_coach.exercise_sets(a.get("activity_id"))
            if isinstance(sets, list) and sets:
                a["logged_sets"] = sets
    if len(activities) > 1:
        header = ("SESSION_JUST_FINISHED - these " + str(len(activities)) + " activities were "
                  "logged back-to-back and TOGETHER form ONE workout session (the user records "
                  "each part under a different Garmin type - e.g. a cardio warm-up, the Strength "
                  "block, a run, a Pilates/stretch cool-down). Judge this morning's plan as "
                  "potentially covered collectively; do not invent a completion percentage; "
                  "flag a genuinely-missing piece only once, and if they did something OFF the "
                  "plan note the pivot supportively - never ask 'is that all?' after one part")
    else:
        header = "JUST_FINISHED_ACTIVITY"
    parts.append("\n\n" + header + ":\n" + json.dumps(activities, indent=2, default=str))
    extras_all = {}
    for a in activities:
        ex = garmin_coach.activity_extras(a.get("activity_id"))
        if isinstance(ex, dict) and ex and "__error__" not in ex:
            extras_all[str(a.get("name") or a.get("activity_id"))] = ex
    if extras_all:
        parts.append("\n\nACTIVITY_EXTRAS (per activity; time-in-HR-zone minutes; weather if "
                     "outdoor):\n" + json.dumps(extras_all, indent=2, default=str))
    training_store().record_activities(activities)
    text = _generate_checked("".join(parts))
    if text:
        append_history("agbot", "[post-workout debrief] " + text)
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
    send_message(chat_id, text, summary_date=date.today().isoformat())


def do_qa(chat_id, question):
    log.info("Q&A: %s", question[:120])
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
                                      extra="\n".join(bits), media_label="video frame")
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
    "- \U0001F37D\uFE0F I nudge you to log meals at 8:30am, 12:15pm & 9pm, and \U0001F4A7 "
    "remind you to drink water every 2h from 8am-10pm (log the water on your watch).\n"
    "- \U0001F3CB\uFE0F I check in through the day (12 / 4pm / 9pm) on whether you did "
    "the recommended exercise - reply **DWRE** when done, or 'rest day' / 'skip today' to stop "
    "the check-ins for the day.\n"
    "- **Ask me anything**, e.g. 'how did I sleep?', 'what's my predicted 10K time?', "
    "'give me a 30-min rowing session'.\n"
    "- I auto-send your brief by ~9:30am, and ~90 min after your last logged activity I send "
    "ONE combined debrief of the whole session vs the plan (or reply DWRE to get it now).\n"
    "- **memory** shows saved facts; **plan** shows dated session proposals.\n"
    "- **/reset** clears recent chat, not durable preferences or health records.\n\n"
    "Advice uses available Garmin data and your private profile; missing data stays unknown."
)

def do_dwre(chat_id):
    """User signalled they're done with today's recommended exercise (DWRE). Mark today done
    so the check-ins stop, cancel any pending auto-debrief, and send the collective wrap-up."""
    log.info("DWRE - marking today's exercise done, sending collective summary")
    _set_exercise_status("done")
    training_store().set_status(date.today(), "user_completed")
    _snapshot_cache.invalidate()
    _clear_pending()
    send_message(chat_id, "\U0001F916 AgBot: nice work \u2713 pulling your whole session "
                 "together...")
    block = garmin_coach.session_block()
    if isinstance(block, dict) and "__error__" in block:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't reach Garmin just now - you're "
                     "marked done for the day; ask me later for the full breakdown.")
        return
    if not block:
        send_message(chat_id, "\U0001F916 AgBot: marked you done for today \u2713 - but I don't "
                     "see any activity synced from your watch yet. Sync it and I'll give you the "
                     "full wrap-up, or tell me what you did.")
        return
    text = generate_debrief(block)
    if text:
        send_message(chat_id, text)


def do_skip_exercise(chat_id):
    """User is not exercising today - stop the check-ins for the rest of the day."""
    log.info("Exercise skipped for today (user opt-out)")
    _set_exercise_status("skip")
    training_store().set_status(date.today(), "user_skipped")
    _clear_pending()
    send_message(chat_id, "\U0001F916 AgBot: all good \u2713 no exercise pencilled in for today "
                 "- I'll stop the check-ins and pick it up again tomorrow. Rest up and keep the "
                 "water going. \U0001F4A7")


DWRE_EXACT = {"dwre", "/dwre", "done", "all done", "done for the day", "finished",
              "done with exercise", "done with my exercise", "done with the exercise",
              "done with workout", "done with my workout", "done exercising",
              "finished exercising", "finished my workout", "finished my exercise",
              "completed my exercise", "done with today's exercise", "i'm done", "im done"}
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
    if low in DWRE_EXACT or any(p in low for p in DWRE_SUBSTR):
        return "dwre", ""
    if low in SKIP_EXACT or any(p in low for p in SKIP_SUBSTR):
        return "skip_exercise", ""
    if (low == "" or low in SUMMARY_TRIGGERS or low.startswith("gms")
            or ("morning" in low and "summ" in low)):
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
    log.info("Message kind=%s text=%r", kind, text[:120])
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
    elif kind == "reset":
        clear_history()
        send_message(chat_id, "\U0001F916 AgBot: conversation memory cleared - fresh start.")
    elif kind == "recovered":
        cleared = resolve_health(text)
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
        do_dwre(chat_id)
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


def maybe_auto_summary():
    own = owner()
    if not own:
        return
    if (read_file(LAST_SUMMARY_FILE).strip() == date.today().isoformat()
            or runtime_store().summary_delivered(date.today().isoformat())):
        return
    if runtime_store().summary_pending(date.today().isoformat()):
        return
    now = datetime.now()
    cur = (now.hour, now.minute)
    if cur < AUTO_START or cur >= AUTO_END:
        return
    log.info("Auto-summary trigger at %s", now.strftime("%H:%M"))
    do_summary(own, auto=True)


def _todays_food_log_times(now):
    """Local datetimes of today's journaled food entries. Used to skip a meal nudge
    for a meal that's already been logged (the journal is the bot's own food log)."""
    times = []
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().strip().split("\n") if ln]
    except FileNotFoundError:
        return times
    today = now.date().isoformat()
    for ln in lines[-JOURNAL_KEEP:]:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
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


_PROTEIN_RE = re.compile(r"(\d+)\s*g\s*(?:of\s+)?protein", re.IGNORECASE)
_KCAL_RE = re.compile(r"(\d+)\s*k?cal\b", re.IGNORECASE)


def _todays_food_summary(now):
    """(count, protein_g, kcal) from today's journal food entries. protein_g / kcal are
    best-effort sums of the '~Xg protein' / '~Y kcal' estimates in the auto-log text (0 if
    none parseable); count excludes '[coach plan]' notes. Lets a meal nudge show real
    running totals without calling the model."""
    count = protein = kcal = 0
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().strip().split("\n") if ln]
    except FileNotFoundError:
        return (0, 0, 0)
    today = now.date().isoformat()
    for ln in lines[-JOURNAL_KEEP:]:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("date") != today:
            continue
        txt = e.get("text") or ""
        if txt.lstrip().startswith("[coach plan]"):
            continue
        count += 1
        pm = _PROTEIN_RE.search(txt)
        if pm:
            protein += int(pm.group(1))
        km = _KCAL_RE.search(txt)
        if km:
            kcal += int(km.group(1))
    return (count, protein, kcal)


def _meal_nudge_text(slot, base_text, now):
    """Prepend a real-progress header (protein / kcal / items logged so far today) to the
    standard per-slot meal prompt, so the nudge reflects the whole day, not just the clock.
    Falls back to the plain prompt when nothing is logged yet."""
    count, protein, kcal = _todays_food_summary(now)
    if count <= 0:
        return base_text
    bits = []
    if protein > 0:
        bits.append("~%dg protein" % protein)
    if kcal > 0:
        bits.append("~%d kcal" % kcal)
    if bits:
        head = "\U0001F4CA So far today: " + ", ".join(bits) + " logged.\n"
    else:
        head = "\U0001F4CA So far today: %d item%s logged.\n" % (count, "s" if count != 1 else "")
    return head + base_text


def maybe_meal_reminders():
    own = owner()
    if not own:
        return
    now = datetime.now()
    today = date.today().isoformat()
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
            send_message(own, _meal_nudge_text(slot, text, now))
        else:
            log.info("Meal reminder %s missed window (%.0f min late) - skipping",
                     slot, late_min)
        sent[slot] = today  # mark handled either way so it won't fire again today
        changed = True
    if changed:
        write_file(MEAL_STATE_FILE, json.dumps(sent))


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
    if frac <= 0:
        return False  # first slot of the day: always nudge to kick hydration off
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
    own = owner()
    if not own:
        return
    now = datetime.now()
    today = date.today().isoformat()
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
    changed = False
    for idx, slot, hh, should in due:
        if should and _hydration_on_pace(hh, logged_ml, goal_ml):
            log.info("Hydration %s skipped - on pace (%s/%s ml)", slot, logged_ml, goal_ml)
        elif should:
            log.info("Hydration reminder %s at %s (%s/%s ml)", slot,
                     now.strftime("%H:%M"), logged_ml, goal_ml)
            send_message(own, _hydration_nudge_text(
                hh, logged_ml, goal_ml, HYDRATION_MESSAGES[idx % len(HYDRATION_MESSAGES)]))
        else:
            log.info("Hydration %s missed window - skipping", slot)
        sent[slot] = today
        changed = True
    if changed:
        write_file(HYDRATION_STATE_FILE, json.dumps(sent))


def movement_slots_due(now, sent, today):
    """Hourly-grid movement slots that have arrived today and aren't already handled;
    should_send is False past the catch-up window (mirror of hydration_slots_due)."""
    due = []
    for hh in range(MOVEMENT_START, MOVEMENT_END + 1, MOVEMENT_EVERY_H):
        slot = "m%02d" % hh
        if sent.get(slot) == today:
            continue
        target = now.replace(hour=hh, minute=0, second=0, microsecond=0)
        if now < target:
            continue
        late_min = (now - target).total_seconds() / 60
        due.append((slot, hh, late_min <= MOVEMENT_CATCHUP_MIN))
    return due


def _movement_mins_phrase(mins):
    if mins >= 120:
        return "over 2 hours"
    if mins >= 90:
        return "~1.5 hours"
    if mins >= 60:
        return "over an hour"
    return "about %d min" % mins


def _movement_hr_active(info):
    """True when a fresh, elevated intraday HR shows the user is exercising right now - covers
    step-less workouts (indoor bike, rowing, lifting) that read 'sedentary' on step buckets."""
    hr = info.get("last_hr")
    age = info.get("last_hr_age_min")
    if hr is None or age is None or age > MOVEMENT_HR_MAX_AGE:
        return False
    rest = info.get("resting_hr") or 60
    return hr >= max(rest + MOVEMENT_HR_ACTIVE_DELTA, MOVEMENT_HR_ACTIVE_FLOOR)


def maybe_movement_reminders():
    """Gentle 'get up and move' nudge - fires ONLY when Garmin confirms a long recent sedentary
    stretch (fail-closed). Never on stale data, during a nap, or when the user has been moving.
    One evaluation per slot per day, deduped in MOVEMENT_STATE_FILE."""
    own = owner()
    if not own:
        return
    now = datetime.now()
    today = date.today().isoformat()
    try:
        sent = json.loads(read_file(MOVEMENT_STATE_FILE) or "{}")
    except Exception:  # noqa: BLE001
        sent = {}
    if not isinstance(sent, dict):
        sent = {}
    due = movement_slots_due(now, sent, today)
    if not due:
        return
    info = None
    try:
        info = garmin_coach.recent_inactivity()
    except Exception as exc:  # noqa: BLE001
        log.error("recent_inactivity fetch failed: %s", exc)
    changed = False
    for i, (slot, hh, should) in enumerate(due):
        if not should:
            log.info("Movement %s missed window - skipping", slot)
        elif not isinstance(info, dict):
            log.info("Movement %s skipped - no intraday data (fail-closed)", slot)
        elif info.get("data_age_min", 999) > MOVEMENT_STALE_MAX_MIN:
            log.info("Movement %s skipped - data stale (%s min old)", slot, info.get("data_age_min"))
        elif info.get("last_level") == "sleeping":
            log.info("Movement %s skipped - resting/napping", slot)
        elif (info.get("since_activity_min") is not None
              and info["since_activity_min"] < MOVEMENT_POST_ACTIVITY_MIN):
            log.info("Movement %s skipped - workout/commute ended %s min ago", slot,
                     info.get("since_activity_min"))
        elif _movement_hr_active(info):
            log.info("Movement %s skipped - HR %s bpm shows an active workout now "
                     "(resting %s, %s min old)", slot, info.get("last_hr"),
                     info.get("resting_hr"), info.get("last_hr_age_min"))
        elif (info.get("sedentary_run_min") or 0) < MOVEMENT_SEDENTARY_MIN:
            log.info("Movement %s skipped - not sedentary (run=%s min)", slot,
                     info.get("sedentary_run_min"))
        else:
            mins = info["sedentary_run_min"]
            log.info("Movement reminder %s at %s (sedentary %s min, %s steps last hour)",
                     slot, now.strftime("%H:%M"), mins, info.get("last_hour_steps"))
            send_message(own, MOVEMENT_MESSAGES[i % len(MOVEMENT_MESSAGES)].format(
                mins=_movement_mins_phrase(mins)))
        sent[slot] = today
        changed = True
    if changed:
        write_file(MOVEMENT_STATE_FILE, json.dumps(sent))


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
    if not isinstance(d, dict) or d.get("date") != date.today().isoformat():
        return {}   # stale day -> fresh 'pending' state
    return d


def _save_exercise_state(d):
    d["date"] = date.today().isoformat()
    _atomic_write(EXERCISE_STATE_FILE, json.dumps(d))


def _exercise_status():
    return _load_exercise_state().get("status") or "pending"


def _set_exercise_status(status):
    st = _load_exercise_state()
    st["status"] = status
    _save_exercise_state(st)


def _set_coach_rest(flag):
    """Record whether TODAY's morning brief prescribed a genuine rest/recovery day, so the
    adherence check-ins go rest-aware instead of nagging. Cleared if a later brief the same
    day prescribes a workout."""
    st = _load_exercise_state()
    if flag:
        st["coach_rest"] = True
    else:
        st.pop("coach_rest", None)
        st.pop("rest_note_sent", None)
    _save_exercise_state(st)


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
    own = owner()
    if not own:
        return
    st = _load_exercise_state()
    if st.get("status") in ("done", "skip", "stated"):
        return  # resolved for today (finished, opted out, or they've told me their plan)
    now = datetime.now()
    asked = list(st.get("asked") or [])
    # The coach itself prescribed rest today -> send ONE gentle rest-aware note (not the
    # 'did you exercise?' nag), then stay quiet for the rest of the day.
    if st.get("coach_rest"):
        if st.get("rest_note_sent"):
            return
        due = exercise_checkin_slots_due(now, asked)
        if not due:
            return  # first check-in hour not reached yet
        if any(should for _s, _h, should in due):
            log.info("Exercise rest-day note at %s", now.strftime("%H:%M"))
            send_message(own, _exercise_rest_message())
        st["rest_note_sent"] = True
        _save_exercise_state(st)
        return
    changed = False
    for slot, hh, should in exercise_checkin_slots_due(now, asked):
        if should:
            # Redundant-nag guard: if Garmin already shows a real workout logged today (e.g.
            # a morning/evening commute ride or a gym session), don't ask 'did you exercise?'.
            # Leave status untouched so the post-workout auto-debrief still wraps up each
            # session. Fails open (asks) if the check errors.
            already_trained = False
            try:
                already_trained = garmin_coach.trained_today()
            except Exception as exc:  # noqa: BLE001
                log.error("trained_today check failed, asking anyway: %s", exc)
            if already_trained:
                log.info("Exercise check-in %s at %s - already trained today, staying quiet",
                         slot, now.strftime("%H:%M"))
            else:
                log.info("Exercise check-in %s at %s", slot, now.strftime("%H:%M"))
                send_message(own, _exercise_checkin_message())
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
        if _exercise_status() in ("done", "skip"):
            # The user already got their DWRE wrap-up (or opted out) for today. An activity that
            # finishes syncing to Garmin just AFTER they pressed DWRE must NOT re-arm the auto-
            # debrief - that race produced a duplicate collective debrief ~90 min later. Just
            # re-baseline the seen id and stay quiet.
            log.info("New activity %s after exercise marked '%s' - not re-arming debrief",
                     aid, _exercise_status())
            return
        log.info("New activity %s (%s) - queuing collective debrief", aid, act.get("type"))
        pend["last_ts"] = now
        _save_pending(pend)
        return
    # No new activity. If a session is pending and it's been quiet long enough, send the one
    # collective debrief for the whole just-finished block.
    if pend and (now - float(pend.get("last_ts", 0) or 0) >= DEBRIEF_QUIET_SECS):
        if _exercise_status() in ("done", "skip"):
            _clear_pending()  # already debriefed via DWRE / opted out today - never duplicate
            return
        block = garmin_coach.session_block()
        if isinstance(block, dict) and "__error__" in block:
            log.error("session_block error, will retry next poll: %s", block.get("__error__"))
            return  # keep pending; retry
        if block:
            log.info("Quiet for %dm - collective debrief of %d activities",
                     DEBRIEF_QUIET_SECS // 60, len(block))
            text = generate_debrief(block)
            if text:
                send_message(own, text)
                _clear_pending()


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
    today = date.today().isoformat()
    if read_file(RED_FLAGS_FILE).strip() == today:
        return
    if (read_file(LAST_SUMMARY_FILE).strip() == today
            or runtime_store().summary_delivered(today)
            or runtime_store().summary_pending(today)):
        write_file(RED_FLAGS_FILE, today)  # the brief already covers this ground
        return
    now = datetime.now()
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
    delay = 5
    while not _stop.is_set():
        try:
            params = {"timeout": POLL_TIMEOUT}
            offset = runtime_store().offset(get_offset())
            if offset is not None:
                params["offset"] = offset
            response = tg("getUpdates", params, timeout=POLL_TIMEOUT + 15)
            if not response.get("ok"):
                raise RuntimeError(response.get("description", "Telegram poll rejected"))
            runtime_store().ingest(response.get("result", []))
            delay = 5
        except Exception:
            log.exception("stage=poll failed; durable cursor retained")
            _stop.wait(delay)
            delay = min(60, delay * 2)


def _publish_messages():
    while not _stop.is_set():
        try:
            flush_outbox()
        except Exception:
            log.exception("stage=outbox failed; pending messages retained")
        _stop.wait(2)


def _health_report():
    return {
        "status": ("degraded" if _llm_last_error else
                   "busy" if _current_request else "ready"),
        "model": COPILOT_MODEL, "reasoning": COPILOT_REASONING_EFFORT,
        "active_model": _llm_active_model,
        "fallback_models": list(COPILOT_FALLBACK_MODELS),
        "last_model_error": _llm_last_error,
        "release": os.environ.get("AGBOT_RELEASE", "development"),
        "worker_busy_seconds": (round(time.time() - _worker_started)
                                if _worker_started else 0),
        "last_progress_age_seconds": round(time.time() - _last_progress),
        "queues": runtime_store().counts(),
    }


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
    listener = acquire_singleton_lock()
    if not os.path.isfile(PROFILE_FILE):
        raise FileNotFoundError("Create your private profile first: " + PROFILE_FILE)
    runtime_store().recover()
    memory_store()
    training_store()
    me = tg("getMe")
    log.info("AgBot online as @%s", me.get("result", {}).get("username"))
    log.info("LLM model=%s reasoning-effort=%s release=%s",
             COPILOT_MODEL or "(CLI default)", COPILOT_REASONING_EFFORT,
             os.environ.get("AGBOT_RELEASE", "development"))
    for target, args in ((_poll_messages, ()), (_publish_messages, ()),
                         (_serve_health, (listener,))):
        threading.Thread(target=target, args=args, daemon=True).start()
    next_schedule = 0
    jobs = (maybe_auto_summary, maybe_red_flags, maybe_post_workout, maybe_meal_reminders,
            maybe_hydration_reminders, maybe_movement_reminders, maybe_exercise_checkins)
    while not _stop.is_set():
        item = runtime_store().claim()
        if item:
            _current_request = item["id"]
            _send_sequence = _generation_sequence = 0
            _worker_started = time.time()
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
                _last_progress = time.time()
            continue
        if time.time() >= next_schedule:
            _set_msg_sent_at(None)
            for job in jobs:
                try:
                    job()
                except Exception:
                    log.exception("stage=scheduler job=%s failed; next tick will retry", job.__name__)
            next_schedule = time.time() + 60
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
        _t = date.today().isoformat()

        def _hy(h, m, sent=None):
            return hydration_slots_due(datetime.now().replace(hour=h, minute=m, second=0,
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
            return exercise_checkin_slots_due(datetime.now().replace(hour=h, minute=m, second=0,
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
