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
import html
import time
import socket
import logging
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
LOGS = os.path.join(BASE, "logs")
os.makedirs(STATE, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)

TOKEN_FILE = os.path.join(STATE, "telegram_token.txt")
OWNER_FILE = os.path.join(STATE, "telegram_chat_id.txt")
OFFSET_FILE = os.path.join(STATE, "tg_offset.txt")
LAST_SUMMARY_FILE = os.path.join(STATE, "last_summary_date.txt")
HISTORY_FILE = os.path.join(STATE, "conversation.json")
TODAYS_BRIEF_FILE = os.path.join(STATE, "todays_brief.json")
PROMPTS = os.path.join(BASE, "prompts")
PROFILE_FILE = os.path.join(BASE, "profile.md")
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
COPILOT_REASONING_EFFORT = (os.environ.get("AGBOT_REASONING_EFFORT", "").strip() or "low")

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
MOVEMENT_MESSAGES = (
    "\U0001FA91 AgBot \u00B7 Move break - you've been sitting {mins}. Stand up, roll the "
    "shoulders and take a 2-3 min walk (kettle, stairs, a lap). Your back and energy will "
    "thank you.",
    "\U0001F9CD AgBot \u00B7 You've been seated {mins}. Switch to the standing desk for a "
    "while, or grab some water and walk it back - frequent little breaks beat one big one.",
    "\U0001FA91 AgBot \u00B7 Long sit ({mins}). Quick reset: stand, 10 easy squats or a "
    "short walk, loosen the hips, then back to it. Keeps posture and focus sharp.",
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
HEALTH_ACTIVE_DAYS = 45      # active injury/illness flags injected for up to this long
HEALTH_PROMPT_CHARS = 800    # char budget for injected health flags
ANCHOR_KEEP = 24             # capability anchors kept on disk
ANCHOR_PROMPT_CHARS = 1200   # char budget for injected capability anchors
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


# --------------------------------------------------------------------- helpers
def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def write_file(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def append_history(role, text):
    hist = load_history()
    hist.append({"role": role, "text": text,
                 "ts": datetime.now().isoformat(timespec="seconds")})
    hist = [t for t in hist if _within_days(t.get("ts"), HISTORY_KEEP_DAYS)]
    hist = hist[-MAX_STORED_TURNS:]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
            json.dump(hist, fh, ensure_ascii=False, indent=1)
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
        with open(TODAYS_BRIEF_FILE, "w", encoding="utf-8") as fh:
            json.dump({"date": date.today().isoformat(), "text": text},
                      fh, ensure_ascii=False)
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
        with open(PENDING_DEBRIEF_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
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
    lines = []
    for turn in hist:
        who = "You" if turn.get("role") == "user" else "AgBot"
        raw = turn.get("ts") or ""
        short = raw[:16].replace("T", " ")
        tag = _day_tag(raw)
        stamp = ("[" + short + ((" " + tag) if tag else "") + "] ") if raw else ""
        lines.append(stamp + who + ": " + (turn.get("text") or "").strip())
    text = "\n".join(lines)
    if len(text) > HISTORY_PROMPT_CHARS:
        text = "..." + text[-HISTORY_PROMPT_CHARS:]
    return text


def append_journal(text, on_date=None):
    """Journal a durable note. `on_date` ('YYYY-MM-DD') back-dates the entry - used when the
    user reports a meal from an earlier day ('last night's dinner', shared the morning after).
    Defaults to today. The stored `ts` always stays the real capture time."""
    entry = {"date": on_date or date.today().isoformat(), "text": text.strip(),
             "ts": datetime.now().isoformat(timespec="seconds")}
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("journal save failed: %s", exc)


_LOG_MARKER_RE = re.compile(r"\[\[LOG(?:\s+(\d{4}-\d{2}-\d{2}))?:\s*(.*?)\]\]",
                            re.IGNORECASE | re.DOTALL)


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
    if on_date and on_date > date.today().isoformat():
        on_date = None  # never accept a future date
    return clean, (note or None), on_date


def _logged_confirmation(on_date=None):
    """The 'logged' line appended after a meal is saved. Names the day whenever the entry
    was back-dated, so it's obvious it did NOT land on today's tally."""
    base = "\n\n\U0001F37D\uFE0F logged \u2713"
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
# The poll loop is single-threaded, so one module-level value is safe.
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


def _extract_plan_marker(text):
    """Pull a model-emitted [[PLAN: ...]] marker off a reply. The prompts append it when
    AgBot commits to a MULTI-DAY training intent (e.g. 'I'll program vigorous VO2max
    intervals over the next few days'), so the plan survives beyond a single chat and can
    be re-surfaced each morning. Returns (clean_text, plan_or_None)."""
    if not text:
        return text, None
    plans = _PLAN_MARKER_RE.findall(text)
    if not plans:
        return text, None
    clean = _PLAN_MARKER_RE.sub("", text).rstrip()
    plan = " ".join(" ".join(plans).split()).strip()
    return clean, (plan or None)


def _harvest_plan(text):
    """Extract any [[PLAN: ...]] marker and journal it as durable coach context so it
    persists into future briefs/answers. Returns the cleaned reply text."""
    text, plan = _extract_plan_marker(text)
    if plan:
        append_journal("[coach plan] " + plan)
    return text


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
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().strip().split("\n") if ln]
    except FileNotFoundError:
        return ""
    entries = []
    for ln in lines[-JOURNAL_KEEP:]:
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        d = e.get("date", "")
        tag = _day_tag(d)
        label = d + ((" (" + tag + ")") if tag else "")
        entries.append((label + ": " + (e.get("text") or "")).strip())
    text = "\n".join(entries)
    if len(text) > JOURNAL_PROMPT_CHARS:
        text = "..." + text[-JOURNAL_PROMPT_CHARS:]
    return text


HEALTH_RE = re.compile(
    r"\b("
    r"injur\w*|hurts?|hurting|"
    r"pain\w*|sore(?:ness)?|aching|aches?|"
    r"sprain\w*|strained|straining|pulled|tweak\w*|twisted|"
    r"sick|ill|unwell|fever\w*|the flu|a cold|coughing|sore throat|"
    r"nause\w*|dizzy|migraine|headache|cramp\w*|"
    r"swollen|swelling|stiff\w*|niggl\w*|"
    r"food poisoning|throwing up|vomit\w*|"
    r"can'?t (?:walk|run|lift|train|move)"
    r")\b", re.IGNORECASE)


def append_health(text, status="active"):
    text = (text or "").strip()[:240]
    if not text:
        return
    entry = {"date": date.today().isoformat(), "text": text, "status": status,
             "ts": datetime.now().isoformat(timespec="seconds")}
    try:
        with open(HEALTH_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("health save failed: %s", exc)


def _load_health():
    try:
        with open(HEALTH_FILE, "r", encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []


def active_health_text():
    """Injuries/illness the user reported and hasn't marked recovered - injected into EVERY
    prompt so no recommendation ignores them, and they don't scroll out of chat."""
    items = _load_health()
    if not items:
        return ""
    cutoff = (date.today() - timedelta(days=HEALTH_ACTIVE_DAYS)).isoformat()
    active = [e for e in items if e.get("status", "active") == "active"
              and str(e.get("date", "")) >= cutoff]
    if not active:
        return ""
    lines = [(str(e.get("date", "")) + ": " + (e.get("text") or "")).strip() for e in active]
    text = "\n".join(lines)
    if len(text) > HEALTH_PROMPT_CHARS:
        text = text[-HEALTH_PROMPT_CHARS:]
    return text


def resolve_health():
    items = _load_health()
    cleared = [e for e in items if e.get("status", "active") == "active"]
    if not cleared:
        return []
    for e in items:
        if e.get("status", "active") == "active":
            e["status"] = "resolved"
    try:
        with open(HEALTH_FILE, "w", encoding="utf-8") as fh:
            for e in items:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("health resolve failed: %s", exc)
    return [(e.get("text") or "").strip() for e in cleared]


_HEALTH_NEG_RE = re.compile(
    r"\b(?:no|not|n'?t|never|without|zero)\s+\w*\s*"
    r"(?:pain|sore\w*|hurt\w*|ach\w*|injur\w*|niggl\w*|stiff\w*|cramp\w*|tight\w*|sprain\w*)"
    r"|\b(?:pain|ache)[- ]?free\b"
    r"|\b(?:all (?:good|fine|better|clear|healed)|absolutely (?:fine|good)|"
    r"feeling (?:good|fine|great|better)|no longer (?:hurt\w*|sore)|fully recovered|"
    r"back to normal|0\s*/\s*10|0 pain|zero pain)\b",
    re.IGNORECASE)

_HEALTH_PAST_RE = re.compile(
    r"\b(?:previous\w*|used to|in the past|usually|history of|tend to|tends to|"
    r"prone to|old injur\w*|past injur\w*)\b", re.IGNORECASE)


def _health_snippet(probe):
    """Keep only the clause(s) that genuinely report a CURRENT symptom, so a long multi-topic
    message isn't stored verbatim as a 'flag'. Drops fragments that are negated ('no pain',
    'knee's fine'), a question ('is that fine or hurts my nutrition?'), or about a PAST/general
    tendency ('my previous injuries') - these were the false positives that piled up. Returns ''
    when nothing genuine remains, which tells the caller not to capture anything."""
    frags = re.split(r"(?<=[.!?])\s+|\n+", probe)
    hits = []
    for f in frags:
        f = f.strip(" -\u2022\t")
        if not f or not HEALTH_RE.search(f):
            continue
        if f.rstrip().endswith("?"):
            continue  # a question about symptoms is not a report of one
        if _HEALTH_NEG_RE.search(f):
            continue  # negated or an all-clear ("no pain", "feeling fine")
        if _HEALTH_PAST_RE.search(f):
            continue  # a past/general tendency, not a current injury
        hits.append(f)
    snip = re.sub(r"\s+", " ", "; ".join(hits)).strip()
    return snip[:200]


def _maybe_capture_health(kind, text, payload):
    """Durably record a self-reported injury/illness so it's injected into every future
    prompt (until the user says 'recovered'), not just the ephemeral chat window."""
    if kind not in ("qa", "log"):
        return
    probe = ((payload if kind == "log" else text) or "").strip()
    if not probe or not HEALTH_RE.search(probe):
        return
    if not re.search(r"\b(i|i'?m|ive|i'?ve|my|me)\b", probe.lower()):
        return  # only first-person reports about the user's own state
    snippet = _health_snippet(probe)
    if not snippet:
        return  # nothing genuinely reported (negated, a question, or a past tendency)
    for e in _load_health():
        if e.get("status", "active") == "active" and (e.get("text") or "").strip() == snippet:
            return  # already on record
    append_health(snippet)
    log.info("Captured health flag: %r", snippet[:80])


# ---- Model-driven health-flag clearing --------------------------------------------------
# Regex capture (above) and the exact-keyword "recovered" route are coarse: a natural all-clear
# like "knees, shoulder and elbow are all fine now" matches neither, so old flags never clear
# and keep surfacing in every brief. This lets the model resolve flags from ordinary phrasing,
# and to clear ONE area while leaving others active.
_HEALTH_CLEAR_RE = re.compile(r"\[\[HEALTH_CLEAR:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)

# body-part -> substrings that count as the same area in a stored flag's text, so clearing
# "elbow" also resolves a flag phrased "triceps tendonitis", etc.
_PART_SYNONYMS = {
    "elbow": ("elbow", "tricep", "forearm"),
    "shoulder": ("shoulder", "delt", "rotator"),
    "knee": ("knee", "patell"),
    "back": ("back", "lumbar", "spine"),
    "hamstring": ("hamstring",),
    "glute": ("glute",),
    "quad": ("quad",),
    "calf": ("calf", "calves"),
    "hip": ("hip",),
    "wrist": ("wrist",),
    "ankle": ("ankle", "achilles"),
    "neck": ("neck",),
    "groin": ("groin", "adductor"),
    "chest": ("chest", "pec"),
    "bicep": ("bicep",),
}


def _extract_health_clear_marker(text):
    if not text:
        return text, None
    found = _HEALTH_CLEAR_RE.findall(text)
    if not found:
        return text, None
    clean = _HEALTH_CLEAR_RE.sub("", text).rstrip()
    what = " ".join(" ".join(found).split()).strip().lower()
    return clean, (what or None)


def _harvest_health_clear(text):
    """Resolve active health flags the model says the user reported better. `all` (or similar)
    clears everything; otherwise only flags matching a named body area are cleared."""
    text, what = _extract_health_clear_marker(text)
    if not what:
        return text
    items = _load_health()
    active = [e for e in items if e.get("status", "active") == "active"]
    if not active:
        return text
    clear_all = any(w in what for w in ("all", "everything", "no injur", "nothing left",
                                        "no more", "fully recovered", "back to normal"))
    named = []
    if not clear_all:
        for part, syns in _PART_SYNONYMS.items():
            if part in what:
                named.extend(syns)
    cleared = []
    for e in active:
        etext = (e.get("text") or "").lower()
        if clear_all or (named and any(s in etext for s in named)):
            e["status"] = "resolved"
            cleared.append(e)
    if cleared:
        try:
            with open(HEALTH_FILE, "w", encoding="utf-8") as fh:
                for e in items:
                    fh.write(json.dumps(e, ensure_ascii=False) + "\n")
            log.info("Health cleared via marker (%r): %d flag(s)", what[:40], len(cleared))
        except Exception as exc:  # noqa: BLE001
            log.error("health clear (marker) failed: %s", exc)
    return text


# ---- Durable capability anchors (what the user ACTUALLY performed) -----------------------
# Garmin indoor rides carry HR only (no power), and a load the user verbally reduced to lives
# only in the 7-day chat window before scrolling away. So the weights/watts/durations they
# actually managed are re-injected here, in full, until superseded - the model calibrates to
# THESE.
def append_anchor(text):
    text = (text or "").strip()[:240]
    if not text:
        return
    entry = {"date": date.today().isoformat(), "text": text,
             "ts": datetime.now().isoformat(timespec="seconds")}
    try:
        with open(ANCHOR_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("anchor save failed: %s", exc)


def _load_anchors():
    try:
        with open(ANCHOR_FILE, "r", encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []


def active_anchors_text():
    """The most recent capability anchors, oldest-last so a newer line supersedes an older one
    for the same movement. Injected into every prompt."""
    items = _load_anchors()[-ANCHOR_KEEP:]
    if not items:
        return ""
    lines = []
    for e in items:
        d = e.get("date", "")
        tag = _day_tag(d)
        label = d + ((" (" + tag + ")") if tag else "")
        lines.append((label + ": " + (e.get("text") or "")).strip())
    text = "\n".join(lines)
    if len(text) > ANCHOR_PROMPT_CHARS:
        text = "..." + text[-ANCHOR_PROMPT_CHARS:]
    return text


_ANCHOR_MARKER_RE = re.compile(r"\[\[ANCHOR:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)


def _extract_anchor_marker(text):
    if not text:
        return text, None
    found = _ANCHOR_MARKER_RE.findall(text)
    if not found:
        return text, None
    clean = _ANCHOR_MARKER_RE.sub("", text).rstrip()
    note = " ".join(" ".join(found).split()).strip()
    return clean, (note or None)


def _harvest_anchor(text):
    """Persist a capability anchor the model captured (a load/watt/duration the user actually
    did or reduced to), skipping an exact repeat of the latest one. Returns the cleaned text."""
    text, note = _extract_anchor_marker(text)
    if note:
        existing = _load_anchors()
        if not existing or (existing[-1].get("text") or "").strip() != note:
            append_anchor(note)
            log.info("Captured capability anchor: %r", note[:80])
    return text


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
    if isinstance(cached, dict):
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
            with open(FITNESS_FILE, "w", encoding="utf-8") as fh:
                json.dump(prof, fh, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            log.error("fitness cache save failed: %s", exc)
        return prof
    return cached  # stale cache (or None) if the refresh failed


def load_token():
    raw = os.environ.get("AGBOT_TELEGRAM_TOKEN", "").strip() or read_file(TOKEN_FILE)
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", raw)
    if not m:
        raise SystemExit(
            "No valid Telegram bot token found. Set AGBOT_TELEGRAM_TOKEN or put the "
            "token in " + TOKEN_FILE)
    return m.group(0)


TOKEN = load_token()
API = "https://api.telegram.org/bot" + TOKEN


def tg(method, params=None, timeout=60):
    url = API + "/" + method
    data = urllib.parse.urlencode(params).encode("utf-8") if params else None
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(method, params, timeout=30):
    """POST that returns Telegram's JSON even on HTTP 4xx (never raises)."""
    url = API + "/" + method
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


def _chunks(text, n):
    if not text:
        return [""]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            if cur:
                out.append(cur)
            while len(line) > n:
                out.append(line[:n])
                line = line[n:]
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out


# ---- lightweight Markdown -> Telegram-HTML renderer -----------------------
# The model writes natural Markdown; Telegram renders a small HTML subset
# (<b> <i> <u> <s> <code> <pre> <a>). We convert the constructs the coach
# actually uses and, if Telegram ever rejects the markup, fall back to plain
# text so a message is never lost. Underscore italics are deliberately NOT
# supported so identifiers like strain_yesterday survive intact.
_SENT_O, _SENT_C = "\ue000", "\ue001"
_RE_FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*\n(.*?)```", re.DOTALL)
_RE_ICODE = re.compile(r"`([^`\n]+)`")
_RE_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_RE_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_RE_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])")
_RE_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_RE_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_RE_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_RE_RESTORE = re.compile(_SENT_O + r"(\d+)" + _SENT_C)


def md_to_html(text):
    """Convert the model's Markdown-ish text to Telegram-safe HTML."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Unwrap a single code fence that wraps the WHOLE message (model slip-up).
    s = text.strip()
    if s.startswith("```") and s.endswith("```") and s.count("```") == 2:
        s = re.sub(r"^```[ \t]*[A-Za-z0-9_+-]*\n?", "", s)
        text = s[:-3].strip("\n")

    store = []

    def _stash(frag):
        store.append(frag)
        return _SENT_O + str(len(store) - 1) + _SENT_C

    # Protect code first (its contents must not be re-formatted).
    text = _RE_FENCE.sub(
        lambda m: _stash("<pre>" + html.escape(m.group(1).rstrip("\n"), quote=False) + "</pre>"),
        text)
    text = _RE_ICODE.sub(
        lambda m: _stash("<code>" + html.escape(m.group(1), quote=False) + "</code>"),
        text)

    # Escape everything else, then inject the known-good tags.
    text = html.escape(text, quote=False)
    text = _RE_LINK.sub(
        lambda m: _stash('<a href="' + html.escape(m.group(2), quote=True) + '">'
                         + m.group(1) + "</a>"),
        text)
    text = _RE_BOLD.sub(lambda m: "<b>" + m.group(1) + "</b>", text)
    text = _RE_STRIKE.sub(lambda m: "<s>" + m.group(1) + "</s>", text)
    text = _RE_ITALIC.sub(lambda m: "<i>" + m.group(1) + "</i>", text)

    out = []
    for line in text.split("\n"):
        if _RE_HR.match(line):
            continue
        h = _RE_HEAD.match(line)
        if h:
            out.append("<b>" + h.group(1) + "</b>")
            continue
        b = _RE_BULLET.match(line)
        if b:
            out.append(("  " if b.group(1) else "") + "\u2022 " + b.group(2))
            continue
        out.append(line)
    text = "\n".join(out)

    return _RE_RESTORE.sub(lambda m: store[int(m.group(1))], text)


def _html_to_plain(html_text):
    """Strip tags / unescape entities for the plain-text fallback path."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def send_message(chat_id, text):
    if text:  # safety net: never let a raw marker ([[LOG: ...]] / [[REST_DAY]] / [[PLAN: ...]]) leak into a message
        text = _PLAN_MARKER_RE.sub(
            "", _REST_MARKER_RE.sub("", _LOG_MARKER_RE.sub("", text))).rstrip()
    rendered = md_to_html(text)
    for chunk in _chunks(rendered, 4000):
        if not chunk.strip():
            continue
        resp = _post("sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
        if resp.get("ok"):
            continue
        # Telegram rejected the markup - resend as plain text so nothing is lost.
        log.warning("HTML send failed (%s); retrying as plain text",
                    resp.get("description"))
        resp = _post("sendMessage", {
            "chat_id": chat_id,
            "text": _html_to_plain(chunk),
            "disable_web_page_preview": "true",
        })
        if not resp.get("ok"):
            log.error("sendMessage failed (plain too): %s", resp.get("description"))


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
    url = "https://api.telegram.org/file/bot" + TOKEN + "/" + fp
    ext = os.path.splitext(fp)[1] or ".jpg"
    dest = os.path.join(INCOMING, "img_" + str(int(time.time())) + ext)
    urllib.request.urlretrieve(url, dest)
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


def run_llm(prompt, image=None, images=None):
    """Generate a reply from the configured model/agent backend.

    *** THIS IS THE ONE PLACE TO CHANGE TO USE A DIFFERENT MODEL. ***
    Select a backend with the AGBOT_LLM env var (default "copilot"). To add your
    own, write a _llm_<name>(prompt, images) -> str and register it in _LLM_BACKENDS
    below. `images` is a list of local image file paths; only vision-capable
    backends use them (text-only backends simply ignore them).
    """
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
    try:
        return backend(prompt, imgs)
    except Exception as exc:  # noqa: BLE001
        log.error("LLM backend %r failed: %s", LLM_BACKEND, exc)
        return None


def _llm_copilot(prompt, images):
    """Default backend: the GitHub Copilot CLI (`copilot`). The prompt is piped via
    stdin (so the OS command-line length limit never applies); images are passed as
    --attachment (Copilot is vision-capable)."""
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
        "--no-ask-user", "--allow-all-tools",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--no-color", "--no-remote", "--no-remote-export",
        "--reasoning-effort", COPILOT_REASONING_EFFORT,
    ]
    if COPILOT_MODEL:
        cmd += ["--model", COPILOT_MODEL]
    for att in images:
        if att:
            cmd += ["--attachment", att]
    try:
        res = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=COPILOT_TIMEOUT, env=env, cwd=BASE,
        )
    except subprocess.TimeoutExpired:
        log.error("copilot timed out after %ss", COPILOT_TIMEOUT)
        return None
    if res.returncode != 0:
        log.error("copilot exit %s: %s", res.returncode, (res.stderr or "")[:600])
    return _scrub(res.stdout or "") or None


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


# Injected into every generation path (summary, Q&A, image, weekly, nutrition,
# performance). Forces the model to reason over the WHOLE data picture, not a
# single metric, then report tightly. The bot's standing coaching directive.
DATA_USE_DIRECTIVE = (
    "COACHING DIRECTIVE - read before you answer:\n"
    "Ground every recommendation in the user's FULL data picture above, never one metric in "
    "isolation. Weigh together, and only use fields that are actually present:\n"
    "- Recovery: for a morning brief prefer morning_readiness (wake-time, WELL_RECOVERED etc.); "
    "otherwise training-readiness score + sub-factors, recovery-time hours, HRV vs baseline, "
    "body_battery_current (Garmin's most-recent value; matches the Connect app/web), sleep "
    "score/duration/stages plus Garmin's own verdict on the night (last_night_sleep.score_feedback_garmin "
    "and score_personalized_insight) and its per-dimension sub_scores - name the weak dimension (e.g. REM "
    "or deep below its optimal band) instead of only the overall number, naps_today (daytime naps add "
    "recovery and alertness - factor them in, don't discuss overnight sleep alone), stress.\n"
    "- Sleep need & timing: last_night_sleep.sleep_need_tonight_h is Garmin's PERSONALISED sleep "
    "target for tonight (compare with sleep_need_baseline_h; sleep_need_tonight_feedback and "
    "sleep_need_tonight_adjustments explain why it moved - sleep debt, HRV, or today's nap). Turn it "
    "into a concrete bedtime, and read bed_time_local / wake_time_local against "
    "profile_config.habitual_sleep_window (the user's usual bed/wake schedule) to flag late or irregular "
    "timing rather than judging total hours alone.\n"
    "- Illness / overreaching watch: last_night_sleep.skin_temp_deviation_c is the deviation from "
    "the user's ~19-day baseline. A notable swing (roughly >=+/-0.5C) - ESPECIALLY together with a "
    "raised resting HR (wellness_today.rhr_today vs rhr_7d_avg), elevated respiration "
    "(wellness_today.respiration_sleep_avg / waking), low HRV, or breathing_disruptions - suggests "
    "the body is fighting illness, alcohol, heat or under-recovery; when several line up, back off "
    "intensity and say why. A small deviation with everything else normal is noise - don't alarm.\n"
    "- Day strain: wellness_today sedentary_h vs active_h/highly_active_h (long sedentary days are "
    "their own load), the stress-duration split (rest/low/medium/high_stress_min + stress_qualifier) "
    "plus stress_curve_hourly (WHEN stress peaked through the day, e.g. a stressful evening), "
    "and the resting-HR trend (rhr_today vs rhr_7d_avg - a multi-day rise is an early fatigue flag).\n"
    "- Load & direction: ACWR + acute/chronic load, training status/trend, and the last 7 days of "
    "sessions - including logged_sets (exercises, reps, top weight) so you know which muscle groups "
    "were just trained. Use training_status.load_focus (aerobic-low / aerobic-high / anaerobic load "
    "vs Garmin's target ranges + its feedback phrase, e.g. AEROBIC_LOW_FOCUS) to steer WHICH kind of "
    "session to add so the 4-week balance moves toward target. Each recent session also carries "
    "Garmin's training_effect_label (e.g. RECOVERY / TEMPO / VO2MAX), its bb_cost (body-battery drain) "
    "and per-session intensity minutes - use these to judge how taxing each workout actually was. When "
    "scheduling, respect profile_config.available_training_days / preferred_long_training_days, and read "
    "weekly_trends (last 8 weeks of avg daily steps and avg stress) for overall direction.\n"
    "- Fitness: VO2max, fitness age, endurance score, hill score, race predictions, lactate-threshold "
    "HR, cycling FTP, weekly_intensity (total_toward_goal vs weekly_goal - the WHO 150-min target, "
    "vigorous counts double), and personal_records (nudge when a session is within reach of a PR).\n"
    "- Body & context: weight trend (plus latest_weigh_in_30d body_water_pct / bone_mass_g when a "
    "composition weigh-in exists), sweat_loss_ml (post-sweaty-session "
    "rehydration nudge vs hydration_goal_ml), and the NOTES the user logged.\n"
    "- Calories: calorie_budget is the user's deterministic daily plan, computed from their OWN "
    "body stats (weight, height, age, BMI) - calorie_budget.target_kcal is their fat-loss intake "
    "TARGET (maintenance_kcal minus a BMI-scaled deficit_kcal). Present calories as THREE numbers "
    "when they ask or when you nudge food: TARGET (calorie_budget.target_kcal) - EATEN (tally what "
    "they have actually LOGGED today) - REMAINING (target minus eaten). NEVER use Garmin's "
    "remaining_kcal / net_calorie_goal (they log food in the bot, not Garmin, so wellness_today."
    "calorie_intake_tracked_in_garmin is false and those fields are bogus - they ignore what the "
    "user ate and grow as they train). energy_burned_so_far_kcal / today_so_far.total_kcal are "
    "burn SO FAR, not a budget. If nothing is logged yet, just state the target; keep it "
    "encouraging, not restrictive, and if calorie_budget is null fall back to protein + portion "
    "guidance.\n"
    "- Protein: use calorie_budget.protein_target_g as THE daily protein target and "
    "protein_floor_g as the do-not-drop-below line. These are computed from their CURRENT "
    "weight (2.2 g/kg, i.e. ~1 g per pound, floor 1.8 g/kg) - quote those numbers, never "
    "invent or carry over a different figure. Protein is the single biggest lever on whether "
    "the weight being lost comes off as fat or muscle, so treat the target as the priority "
    "macro: if something has to be missed on a given day, miss carbs or fat, not protein. "
    "Track it the same way as calories - TARGET, EATEN so far, REMAINING - and when they're "
    "short late in the day name a concrete high-protein option they actually have. Roughly "
    "4 kcal per gram, so protein_kcal of target_kcal is already spoken for; build the rest of "
    "the day's food around it.\n"
    "- Hydration: the Garmin hydration goal is a BASELINE FLOOR for the day, NOT a quota to "
    "finish, a cap, or a task with a deadline. Treat it accordingly: (a) work out whether they "
    "are actually behind by comparing intake against the time of day, and if they are ON or "
    "AHEAD of pace SAY SO - never manufacture a deficit or tell them to 'catch up' when they "
    "aren't behind; (b) once the goal is met, note it ONCE and switch to thirst-led language "
    "('you're covered - drink to thirst from here'), don't keep counting up or imply they have "
    "finished drinking for the day; (c) NEVER suggest downing several glasses at once to close a "
    "gap - surplus is simply excreted, and steady sipping beats catch-up chugging; (d) don't "
    "append a hydration line to every reply - raise it when they're genuinely behind, after a "
    "high sweat_loss_ml session, or when they ask; (e) late in the evening prefer sips to volume, "
    "since a big pre-bed load costs them sleep.\n"
    "- Post-workout only: ACTIVITY_EXTRAS gives time-in-HR-zone (minutes per zone -> was it truly "
    "easy/hard, and did it match the plan) and, for outdoor sessions, the weather (heat/humidity "
    "context for pace and HR).\n"
    "- Progression: when you prescribe sets/reps/weights (or watts/duration for cardio), START "
    "from what the user ACTUALLY did last time - read CAPABILITY ANCHORS first, then recent "
    "logged_sets - never from a number they could not complete. Move exactly ONE rung on the reps "
    "-> sets -> weight ladder (profile has the rule): add reps within range; once the top of the "
    "range is hit on all sets with 1-2 in reserve, add a SET at the same load; only after they "
    "handle that higher volume for ~2 sessions, raise load ~5% and drop to the bottom of the "
    "range. A single good session is NOT a reason to jump weight, and never leap two rungs. If "
    "they say they reduced a prescribed load to finish, treat the lower number as the new anchor. "
    "State the one-line reasoning ('last time 12kg x16-22 clean, so today hold 12kg and add a 4th "
    "set'). Same on the bike: hold watts and extend the interval/add a rep before raising wattage.\n"
    "Cross-check these against each other: don't program a hard or heavy session when readiness is "
    "LOW / ACWR is high / recovery-time is still counting down / skin temp + RHR + respiration point "
    "to illness / the same muscles were hit in the last ~48h; match intensity to recovery and pick "
    "novelty that avoids what was just trained; tie any nutrition advice to today's load and calorie "
    "budget. After reasoning over everything, report tightly and name the 2-3 signals that actually "
    "drove the advice. For body battery always use body_battery_current (Garmin's canonical value, "
    "same as the Connect app/web). If body_battery_current_age_min is above ~15, add its as-of time "
    "(body_battery_current_as_of) and explain the lag HONESTLY: this is Garmin's most recently "
    "PUBLISHED body-battery sample, and Garmin's body-battery feed trails real time by ~15-25 min "
    "even right after a fresh sync - so never imply the watch is unsynced when it isn't. Cross-check "
    "last_sync_age_min: if it's small (recently synced), say the watch IS synced and this is just "
    "Garmin's body-battery data catching up; only if last_sync_age_min is also large (say >30) should "
    "you attribute the staleness to the watch not having uploaded. Otherwise just state the number. "
    "When present, wellness_today.body_battery_feedback is Garmin's own plain-English read of the day's "
    "battery (e.g. DAY_BALANCED_AND_INACTIVE) and wellness_today.body_battery_events lists daytime "
    "recovery/nap periods (e.g. RESTFUL_PERIOD) with their battery impact - use them to explain WHY the "
    "battery sits where it does. "
    "TIMEZONE: treat profile_config.timezone as the user's real local timezone for any time-of-day or "
    "'as of' reasoning - it is authoritative when they are travelling and the server clock may differ. "
    "NOTES and RECENT CONVERSATION lines are DATE-STAMPED with a relative tag (today / "
    "yesterday / N days ago): attribute food, workouts and events to the day they happened, "
    "count only TODAY's entries (and anything the user shared today) as today's, and never "
    "present an earlier day's log as if it were today. "
    "PLANNED FOOD RULE: a meal counts as planned for today ONLY if the user, in a message from "
    "today, says they still intend to eat it (today or on a named upcoming day). Food they "
    "mentioned on an earlier day was eaten that day - treat it as already consumed, never "
    "as an upcoming meal, unless they restate it today. Read what they have eaten or plan to "
    "eat from the USER's own messages and logs, not from your own earlier replies; if you are "
    "unsure whether a meal is still coming, ask instead of assuming. "
    "HOLISTIC CONTEXT: you are given the user's FULL picture every time - recovery/vitals, "
    "training load, their logged and PLANNED food, and any ACTIVE HEALTH FLAGS. Use all of "
    "it together and connect the dots; don't answer as if you only know the last message. "
    "FOOD <-> TRAINING: tie them together - shape refuel/nutrition advice around what they "
    "have already eaten today and any meal they have genuinely planned for later today (don't "
    "re-recommend protein/calories they have already had; when they have a later meal planned "
    "today, say whether it fits or should change), and factor fuelling into session "
    "recommendations. "
    "HEALTH: if ACTIVE HEALTH FLAGS lists an injury or illness, RESPECT it - do not program "
    "through it: adapt the modality to avoid the affected area, cut intensity, or recommend "
    "REST/recovery outright when they're unwell, and briefly check how it's doing. If the user "
    "REPORTS a NEW injury or illness in their message, lead with ONE caring, specific "
    "clarifying question (what exactly, where, how bad, since when, and are they up for gentle "
    "movement or need rest) and clearly restate what you've noted, BEFORE any training push. "
    "Never invent numbers - if a field is missing say so rather than guessing."
)


def _assemble(prompt_file, question=None, include_history=True,
              data=None, data_key="GARMIN_JSON", include_brief=True):
    if data is None:
        data = garmin_coach.build_snapshot()
    js = json.dumps(data, indent=2, default=str)
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
    anchors = active_anchors_text()
    if anchors:
        parts.append("\nCURRENT CAPABILITY ANCHORS (what the user ACTUALLY performed or "
                     "adjusted to recently - calibrate weights, watts, cadence and durations "
                     "to THESE. A later line supersedes an earlier one for the same movement, "
                     "and these OVERRIDE stale profile/Garmin numbers):\n" + anchors + "\n")
    if include_brief:
        brief = todays_brief_text()
        if brief:
            parts.append("\nTODAY'S BRIEF YOU ALREADY SENT (the recovery read + workout and "
                         "recommendations you gave the user in this morning's brief - THIS is what "
                         "they mean by 'the plan' / 'the recommendations' / 'what you told me to "
                         "do'. Use it to judge whether they followed it and to stay consistent):\n"
                         + brief + "\n")
    notes = recent_journal_text()
    if notes:
        parts.append("\nNOTES YOU'VE SHARED (durable context the user logged):\n"
                     + notes + "\n")
    hist = recent_history_text() if include_history else ""
    fit = load_fitness_profile()
    if isinstance(fit, dict):
        fit_view = {k: v for k, v in fit.items() if k != "generated_at"}
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
    parts.append("\n\n" + DATA_USE_DIRECTIVE)
    if question is not None:
        parts.append("\n\nQUESTION:\n" + question)
    return "".join(parts), data


def generate_summary():
    prompt, snap = _assemble(SUMMARY_PROMPT_FILE, include_history=True, include_brief=False)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = run_llm(prompt)
    if text:
        text, is_rest = _extract_rest_marker(text)
        _set_coach_rest(is_rest)  # tell the day's check-ins whether the brief prescribed rest
        text = _harvest_plan(text)  # persist any multi-day training plan the brief commits to
        append_history("user", "GMS (requested the morning brief)")
        append_history("agbot", text)
        save_todays_brief(text)
    return text, snap


def generate_qa(question):
    prompt, snap = _assemble(QA_PROMPT_FILE, question=question, include_history=True)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = run_llm(prompt)
    if text:
        text, logged, log_date = _extract_log_marker(text)
        if logged:
            log_date = log_date or _sent_backdate()  # late-delivered message -> its own day
            append_journal(logged, on_date=log_date)  # auto-log any meal the user reported, not just 'log:'-prefixed
            text = text + _logged_confirmation(log_date)
        text = _harvest_plan(text)  # persist any multi-day training plan this answer commits to
        text = _harvest_exercise_plan(text)  # they told me today's plan -> stop the check-ins
        text = _harvest_health_clear(text)  # they said an injury is better -> clear that flag
        text = _harvest_anchor(text)  # they reported what they actually did -> save the anchor
        append_history("user", question)
        append_history("agbot", text)
    if _is_workout_review(question):
        _clear_pending()  # asked how they did -> don't also auto-debrief this session later
    return text, snap


def _journal_shared_media(caption, analysis, n, media_label, on_date=None):
    """Record an EXPLICITLY LOGGED meal/note in the DATED journal. Only fires when
    the caption starts with 'log:' / 'log ' - i.e. the user is telling me they ATE (or did)
    something, not just asking about it. Photos they only ask about ('should I eat
    this?', 'what do you suggest?') are NOT journaled, so we never over-count food they
    only browsed. Non-logged shares still live in the recent-chat window for context."""
    cap = (caption or "").strip()
    low = cap.lower()
    if not (low.startswith("log:") or low.startswith("log ")):
        return  # not an explicit log - don't record as consumed
    note = cap[3:].lstrip(" :").strip()  # caption minus the 'log' prefix
    body = (analysis or "").strip()
    kept = [ln for ln in body.split("\n") if ln.strip()]
    if kept and kept[0].lstrip().startswith("\U0001F916"):  # drop the AgBot signature line
        kept = kept[1:]
    summary = " ".join(kept).strip()
    if len(summary) > 300:
        summary = summary[:300].rstrip() + "..."
    media = "video" if "video" in media_label else ("photos" if n > 1 else "photo")
    parts = ["logged"]
    if note:
        parts.append(note)
    parts.append("(via " + media + "):")
    if summary:
        parts.append(summary)
    append_journal(" ".join(parts).strip(), on_date=on_date)


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
    text = run_llm(prompt, images=image_paths)
    if text:
        text, marker_note, log_date = _extract_log_marker(text)
        log_date = log_date or _sent_backdate()  # late-delivered message -> its own day
        cap_low = (caption or "").strip().lower()
        explicit_log = cap_low.startswith("log:") or cap_low.startswith("log ")
        logged = False
        if explicit_log:
            _journal_shared_media(caption, text, n, media_label, on_date=log_date)  # keep the richer log: summary
            logged = True
        elif marker_note:
            append_journal(marker_note, on_date=log_date)  # model spotted a meal the user reported without a 'log:' prefix
            logged = True
        if logged:
            text = text + _logged_confirmation(log_date)
        text = _harvest_plan(text)  # persist any multi-day training plan this answer commits to
        text = _harvest_exercise_plan(text)  # they told me today's plan -> stop the check-ins
        text = _harvest_health_clear(text)  # they said an injury is better -> clear that flag
        text = _harvest_anchor(text)  # they reported what they actually did -> save the anchor
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
    text = run_llm(prompt)
    if text:
        append_history("user", "weekly review (requested)")
        append_history("agbot", text)
    return text, data


def generate_nutrition():
    prompt, snap = _assemble(NUTRITION_PROMPT_FILE)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = run_llm(prompt)
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
    snap = garmin_coach.build_snapshot()
    parts = [
        read_file(DEBRIEF_PROMPT_FILE),
        "\n\n---\nTODAY: " + today_human() + "\n",
    ]
    flags = active_health_text()
    if flags:
        parts.append("\nACTIVE HEALTH FLAGS (injuries/illness to respect - adapt or rest, "
                     "don't train through them):\n" + flags + "\n")
    anchors = active_anchors_text()
    if anchors:
        parts.append("\nCURRENT CAPABILITY ANCHORS (what the user ACTUALLY performed recently "
                     "- judge this session against THESE loads/watts/durations, and if they "
                     "did more or less than last time, capture the new level):\n" + anchors + "\n")
    brief = todays_brief_text()
    if brief:
        parts.append("\nTODAY'S BRIEF YOU ALREADY SENT (the workout and recommendations you "
                     "gave the user this morning - THIS is 'the plan' they were asked to follow; "
                     "judge the WHOLE session below against it, collectively):\n" + brief + "\n")
    notes = recent_journal_text()
    if notes:
        parts.append("\nNOTES YOU'VE SHARED (durable, date-stamped - includes the meals "
                     "the user logged TODAY = their food intake so far):\n" + notes + "\n")
    hist = recent_history_text()
    if hist:
        parts.append("\nRECENT CONVERSATION (date-stamped today / yesterday / N days ago - "
                     "watch for a meal they said TODAY they still plan to eat later today, and "
                     "how they fuelled/felt around the session; a meal they mentioned on an "
                     "earlier day was already eaten, don't treat it as upcoming; oldest to "
                     "newest):\n" + hist + "\n")
    parts.append("\nPROFILE:\n" + read_file(PROFILE_FILE))
    for a in activities:
        if "strength" in str(a.get("type") or ""):
            sets = garmin_coach.exercise_sets(a.get("activity_id"))
            if sets:
                parts.append("\n\nLOGGED_SETS for " + json.dumps(a.get("name") or a.get("type"))
                             + " (per exercise: #sets, rep range, top weight kg):\n"
                             + json.dumps(sets, indent=2, default=str))
    if len(activities) > 1:
        header = ("SESSION_JUST_FINISHED - these " + str(len(activities)) + " activities were "
                  "logged back-to-back and TOGETHER form ONE workout session (the user records "
                  "each part under a different Garmin type - e.g. a cardio warm-up, the Strength "
                  "block, a run, a Pilates/stretch cool-down). Judge this morning's plan as "
                  "COLLECTIVELY covered by ALL of them together: estimate rough %-completion, "
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
    parts.append("\n\nGARMIN_JSON:\n" + json.dumps(snap, indent=2, default=str))
    parts.append("\n\n" + DATA_USE_DIRECTIVE)
    text = run_llm("".join(parts))
    if text:
        text = _harvest_anchor(text)  # capture what they actually lifted/rode as the new anchor
        append_history("agbot", "[post-workout debrief] " + text)
    return text


def do_summary(chat_id, auto=False):
    log.info("Generating summary (auto=%s) for %s", auto, chat_id)
    if auto:
        send_message(chat_id, "\U0001F916 AgBot: pulling your overnight Garmin data for today's brief...")
    text, snap = generate_summary()
    if isinstance(snap, dict) and "__error__" in snap:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't read your Garmin data right now ("
                     + str(snap["__error__"])[:180] + "). I'll try again later.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: I hit a problem generating your brief. Try again in a minute.")
        return
    send_message(chat_id, text)
    write_file(LAST_SUMMARY_FILE, date.today().isoformat())


def do_qa(chat_id, question):
    log.info("Q&A: %s", question[:120])
    text, snap = generate_qa(question)
    if isinstance(snap, dict) and "__error__" in snap:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't read your Garmin data right now. Try again shortly.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: Sorry, I couldn't generate an answer just now. Try again in a minute.")
        return
    send_message(chat_id, text)


def do_weekly(chat_id):
    log.info("Generating weekly review for %s", chat_id)
    text, data = generate_weekly()
    if isinstance(data, dict) and "__error__" in data:
        send_message(chat_id, "\U0001F916 AgBot: couldn't pull your weekly data right now. Try again shortly.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't build your weekly review just now. Try again in a minute.")
        return
    send_message(chat_id, text)


def do_nutrition(chat_id):
    log.info("Generating nutrition targets for %s", chat_id)
    text, snap = generate_nutrition()
    if isinstance(snap, dict) and "__error__" in snap:
        send_message(chat_id, "\U0001F916 AgBot: couldn't read your Garmin data for nutrition targets. Try again shortly.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't work out your targets just now. Try again in a minute.")
        return
    send_message(chat_id, text)


def generate_performance():
    prompt, snap = _assemble(PERF_PROMPT_FILE)
    if isinstance(snap, dict) and "__error__" in snap:
        return None, snap
    text = run_llm(prompt)
    if text:
        append_history("user", "performance / fitness stats (requested)")
        append_history("agbot", text)
    return text, snap


def do_performance(chat_id):
    log.info("Generating performance card for %s", chat_id)
    text, snap = generate_performance()
    if isinstance(snap, dict) and "__error__" in snap:
        send_message(chat_id, "\U0001F916 AgBot: couldn't read your Garmin data right now. Try again shortly.")
        return
    if not text:
        send_message(chat_id, "\U0001F916 AgBot: I couldn't build your performance card just now. Try again in a minute.")
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
            send_message(chat_id, "\U0001F916 AgBot: I couldn't analyze %s just now. Try again in a minute."
                         % ("those" if n > 1 else "that photo"))
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
            send_message(chat_id, "\U0001F916 AgBot: I couldn't analyze that video just now. Try again in a minute.")
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
    "- \U0001F3CB\uFE0F I check in through the day (10am / 12 / 4pm / 9pm) on whether you did "
    "the recommended exercise - reply **DWRE** when done, or 'rest day' / 'skip today' to stop "
    "the check-ins for the day.\n"
    "- **Ask me anything**, e.g. 'how did I sleep?', 'what's my predicted 10K time?', "
    "'give me a 30-min rowing session'.\n"
    "- I auto-send your brief by ~9:30am, and ~90 min after your last logged activity I send "
    "ONE combined debrief of the whole session vs the plan (or reply DWRE to get it now).\n"
    "- **/reset** clears our recent-chat memory.\n\n"
    "Everything is based on your live Garmin data and your gym equipment."
)

def do_dwre(chat_id):
    """User signalled they're done with today's recommended exercise (DWRE). Mark today done
    so the check-ins stop, cancel any pending auto-debrief, and send the collective wrap-up."""
    log.info("DWRE - marking today's exercise done, sending collective summary")
    _set_exercise_status("done")
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
    if low in ("/reset", "/clear", "reset", "forget", "new chat", "start over"):
        return "reset", ""
    if low in ("recovered", "resolved", "all better", "i'm fine now", "im fine now",
               "healed", "back to normal", "all good now", "feeling better now",
               "i'm better now", "im better now", "fully recovered"):
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
    _maybe_capture_health(kind, text, payload)
    if kind in ("start", "help"):
        send_message(chat_id, HELP_TEXT)
    elif kind == "reset":
        clear_history()
        send_message(chat_id, "\U0001F916 AgBot: conversation memory cleared - fresh start.")
    elif kind == "recovered":
        cleared = resolve_health()
        if cleared:
            send_message(chat_id, "\U0001F916 AgBot: great news \u2713 cleared your active "
                         "health flag(s): " + "; ".join(c[:60] for c in cleared)
                         + ". Back to normal programming - shout if anything still bothers you.")
        else:
            send_message(chat_id, "\U0001F916 AgBot: noted - you had no active injury/illness "
                         "flags on record. Glad you're feeling good!")
    elif kind == "log":
        if payload:
            append_journal(payload)
            send_message(chat_id, "\U0001F916 AgBot: logged \u2713 - I'll factor that into your coaching.")
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
    if read_file(LAST_SUMMARY_FILE).strip() == date.today().isoformat():
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
    try:
        with open(EXERCISE_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    except Exception as exc:  # noqa: BLE001
        log.error("exercise state save failed: %s", exc)


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
    logged = False
    try:
        act = garmin_coach.latest_activity()
        if isinstance(act, dict) and "__error__" not in act:
            logged = str(act.get("start") or "")[:10] == date.today().isoformat()
    except Exception:  # noqa: BLE001
        logged = False
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
        _clear_pending()
        if block:
            log.info("Quiet for %dm - collective debrief of %d activities",
                     DEBRIEF_QUIET_SECS // 60, len(block))
            text = generate_debrief(block)
            if text:
                send_message(own, text)


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
    if read_file(LAST_SUMMARY_FILE).strip() == today:
        write_file(RED_FLAGS_FILE, today)  # the brief already covers this ground
        return
    now = datetime.now()
    cur = (now.hour, now.minute)
    if cur < (5, 30) or cur >= AUTO_START:
        return
    snap = garmin_coach.build_snapshot()
    if isinstance(snap, dict) and "__error__" in snap:
        return
    flags = evaluate_red_flags(snap)
    write_file(RED_FLAGS_FILE, today)
    if not flags:
        return
    log.info("Red flags this morning: %s", flags)
    prompt, _ = _assemble(SUMMARY_PROMPT_FILE, data=snap)
    prompt += ("\n\nURGENT_CONTEXT: It is early morning and these red flags fired: "
               + ", ".join(flags) + ". Instead of a full brief write a SHORT (< 90 "
               "words) early heads-up: name the concern(s) plainly and give ONE "
               "adjustment for today (dial back intensity / prioritise recovery). "
               "First line: \U0001F916 AgBot \u26A0\uFE0F Heads-up \u00B7 " + today_human() + ".")
    text = run_llm(prompt)
    if text:
        append_history("agbot", "[early red-flag alert] " + text)
        send_message(own, text)


def acquire_singleton_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(1)
    except OSError:
        log.error("Another AgBot instance is already running. Exiting.")
        sys.exit(0)
    return s  # keep referenced for process lifetime


def main():
    _lock = acquire_singleton_lock()  # noqa: F841
    me = tg("getMe")
    log.info("AgBot online as @%s", me.get("result", {}).get("username"))
    log.info("LLM model=%s reasoning-effort=%s",
             COPILOT_MODEL or "(CLI default)", COPILOT_REASONING_EFFORT)
    offset = get_offset()
    while True:
        try:
            pt = 1 if _album_buffer else POLL_TIMEOUT
            params = {"timeout": pt}
            if offset is not None:
                params["offset"] = offset
            resp = tg("getUpdates", params, timeout=pt + 15)
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                set_offset(offset)
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    try:
                        dispatch(msg)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("dispatch error: %s", exc)
            flush_ready_albums()
            maybe_auto_summary()
            maybe_red_flags()
            maybe_post_workout()
            maybe_meal_reminders()
            maybe_hydration_reminders()
            maybe_movement_reminders()
            maybe_exercise_checkins()
        except Exception as exc:  # noqa: BLE001
            log.error("poll loop error: %s", exc)
            time.sleep(5)


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
        assert _ck(9, 59) == [], "pre-10am empty"
        _d10 = _ck(10, 0)
        assert [x[0] for x in _d10] == ["c10"] and _d10[0][2], _d10
        assert _ck(10, 40)[0][2] is True, "40m late still asks"
        assert _ck(11, 10)[0][2] is False, "70m late suppressed"
        assert _ck(10, 30, ["c10"]) == [], "already-asked skipped"
        _allc = _ck(21, 0)
        assert [x[0] for x in _allc] == ["c10", "c12", "c16", "c21"], _allc
        assert [x[0] for x in _allc if x[2]] == ["c21"], "only c21 within catch-up"
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
