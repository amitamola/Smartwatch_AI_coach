# 🏃 Smartwatch AI coach

A personal, self-hosted AI fitness coach that lives in **Telegram** and runs on your
own smartwatch's (currently set up for Garmin Connect) data. Every morning it reads your recovery, sleep, training
load and workouts, then gives you a readiness-adapted brief and a specific workout
built around *your* equipment. You can chat with it all day — ask questions, send
photos of your meals or gym machines, log food, report an injury — and it keeps
context across the conversation.

It is **model-agnostic**: it ships working with the GitHub Copilot CLI, and you can
switch it to OpenAI, Anthropic, a local Ollama model, or anything else by editing one
function.

> ⚠️ **Not medical advice.** This is a hobby tool for general fitness guidance. It uses
> an **unofficial** Garmin Connect client (not an official Garmin API). Don't rely on it
> for medical, diagnostic or clinical decisions. See [Disclaimer](#-disclaimer).

---

## ✨ What it does

- **Morning brief ("GMS")** — recovery/readiness read (HRV, sleep + Garmin's own sleep
  verdict & sleep-need, naps, body battery, resting HR, training readiness) + a workout
  matched to your readiness and your gym kit.
- **Knows your plans** — mention an activity you've got coming up ("I've got a hike
  tomorrow") and the next morning's brief makes THAT the day's session: how to pace and fuel
  it given last night's recovery, plus a readiness-based fallback in case the plan changes.
- **Readiness-adapted programming** — backs off intensity when readiness is low, ACWR is
  high, recovery time is still counting down, or illness signals (skin-temp + RHR +
  respiration) line up — and calls a genuine **rest / recovery day** when readiness is RED
  or cumulative fatigue is high, instead of always prescribing a workout.
- **Progression coaching** — if your profile goal is to build (not just maintain), on
  genuinely good-recovery days (GREEN, or a train-ready AMBER when only sleep is a touch
  short) it *deliberately* programs vigorous aerobic / VO2max intervals aimed at whatever
  **Load Focus** is under target — the stimulus that moves your Garmin **Training Status
  toward Productive** instead of parking at Maintaining. Commit to a multi-day block in chat
  ("build intervals in over the next few days") and it **remembers the plan** and honours it
  in the coming briefs.
- **Calibrated to your real capacity** — prescriptions (watts, weights, paces) anchor to what
  you can *actually* do, recorded in your profile and learned from what you report you
  completed — not to stale device numbers like an old cycling FTP. It builds gradually
  (~5% steps, efforts kept a notch below all-out), so targets stay hard-but-doable.
- **Chat Q&A with a week of memory** — "how did I sleep?", "what's my predicted 10K time?",
  "give me a 30-min rowing session". It remembers the **last 7 days** of conversation — food,
  water, workouts, mood, and any multi-day plans you agreed — so its answers and nudges
  reflect everything you've told it, not just the last message.
- **Photo analysis** — send a meal, a machine screen, or an exercise; send **several photos
  at once** (an album) and it weighs them up as one set (e.g. "which of these breakfast
  options should I pick?").
- **Video understanding** *(optional)* — send a short clip; it samples frames and
  transcribes any narration, then coaches on the whole thing.
- **Voice notes** *(optional)* — transcribed locally, then answered.
- **Food logging & nutrition** — just tell it what you ate ("had poha and a protein shake")
  and it **auto-logs** a dated food journal that feeds calorie/protein coaching; a `log:`
  prefix (or `log:` photo caption) still works but isn't required. It only confirms a meal
  once it's actually saved. Optional meal reminders that **skip themselves once you've already
  logged that meal** — and when one does fire it leads with your **running totals so far today**
  (≈ protein / calories logged), not just a blank "time for dinner".
- **Injury/illness awareness** — tell it "my knee hurts" and it asks what's going on,
  remembers it, and adapts training (or calls for rest) until you say `recovered`.
- **Session-aware debrief** — a workout logged as several back-to-back Garmin activities
  (warm-up + strength + cardio + stretch) is treated as ONE session: ~90 min after your last
  activity the bot sends a single combined debrief graded against the morning plan, instead of
  nagging after each part. Reply **DWRE** ("done with recommended exercise") any time to get it
  immediately.
- **Exercise check-ins** — a few light nudges through the day (10am / 12 / 4pm / 9pm) asking if
  you did the recommended exercise; reply **DWRE** when done or `rest day` / `skip today` to
  stop them for the day. When the morning brief itself calls a **rest / recovery day**, the
  check-ins automatically switch to a single gentle rest-aware note — no "did you exercise?"
  nagging on a day the coach told you to rest.
- **Hydration reminders** — a "drink water" nudge every 2 hours from 8am–10pm (you log the
  actual water on your watch). They're **pace-aware and data-rich**: each nudge shows your real
  **ml / cups so far vs the day's goal** and how far behind pace you are, and skips entirely if
  Garmin shows you're already at or ahead of pace for that time (the 8am kick-off always sends).
  Toggleable like the other reminders.
- **Proactive nudges** — auto-sends the brief by ~9:30am if you didn't ask, warns you on a
  rough morning, and (optionally) reminds you to log meals.
- **Holistic context** — every reply is grounded in your *full* picture: recovery + load +
  what you've eaten/planned + active health flags. Food and training advice reference each
  other.

---

## 🧠 How it works

```
        Garmin Connect  ──(unofficial garminconnect lib)──►  garmin_coach.py
                                                                 (data layer:
                                                                  rich JSON dump)
                                                                      │
   Telegram  ◄──── telegram_bridge.py ────────────────────────────────┘
    (you)          • long-polls Telegram for your messages
                   • assembles prompt = profile + Garmin JSON + your logs/chat
                   • calls run_llm(prompt, images) ──►  your chosen model backend
                   • formats & sends the reply back to you
```

- **`garmin_coach.py`** — the data layer. Logs into Garmin Connect and produces a rich
  JSON snapshot (sleep + Garmin's sleep verdict / sub-scores / sleep-need, naps, HRV,
  readiness, training status & load focus, activities with per-session training effect,
  VO2max, body-battery feedback, hourly stress curve, weekly trends, and more).
- **`telegram_bridge.py`** — the brain + the Telegram front-end. It classifies your
  message, assembles a prompt from your `profile.md`, the Garmin snapshot, your food
  journal and recent chat, sends it to the model via **`run_llm()`**, and returns the
  answer. A localhost port lock ensures only one instance ever polls.
- **`prompts/`** — the coaching prompts (summary, Q&A, image, weekly, nutrition, debrief,
  performance) + a metrics reference. Edit these to change the coach's behavior.
- **`profile.md`** — *you*. Your goals, equipment and constraints. Injected into every
  prompt. (Ships as `profile.example.md`; copy it and edit.)

---

## ✅ Requirements

- **Python 3.9+**
- A **Garmin Connect account** (with a Garmin device syncing to it).
- A **Telegram account** (free).
- A **model backend**. Out of the box it uses the **GitHub Copilot CLI** (`copilot`). You
  can swap in OpenAI / Anthropic / Ollama / etc. — see [Choose your model](#4-choose-your-model-backend).

---

## 🚀 Quick start

### 1. Get the code & install dependencies

```bash
git clone https://github.com/amitamola/garmin-ai-coach.git
cd garmin-ai-coach

python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Create your Telegram bot

1. In Telegram, open a chat with **[@BotFather](https://t.me/BotFather)** and send `/newbot`.
2. Give it a **display name** and a **username** (must end in `bot`, e.g. `my_garmin_bot`).
3. BotFather replies with a **token** like `123456789:AAFD39kkdpWt3ywyRZergyOLMaJhac60qc`.
   Keep it secret.
4. **Find your chat id:** send any message to your new bot, then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `chat.id` from the JSON. (Alternatively, message [@userinfobot](https://t.me/userinfobot).)

Provide the token & chat id either via environment variables
(`AGBOT_TELEGRAM_TOKEN`, `AGBOT_OWNER_CHAT_ID`) or as files:

```bash
mkdir -p state
echo "123456789:AA...your-token..." > state/telegram_token.txt
echo "987654321"                    > state/telegram_chat_id.txt   # your numeric chat id
```

> The `state/` folder holds all runtime secrets and data and is git-ignored.

### 3. Connect your Garmin account

Garmin has **no public API key** for individuals — its official Health API is a B2B
partner program. This project uses the community **[`garminconnect`](https://github.com/cyberjunky/python-garminconnect)**
library, which logs in with your **Garmin Connect email + password** and then caches
OAuth tokens locally (default `~/.garminconnect`) so it doesn't log in every time.

For the **first login**, provide your credentials (they're only used to obtain the
token; you can remove them afterwards):

```bash
# Windows (PowerShell):
$env:EMAIL="you@example.com"; $env:PASSWORD="your-garmin-password"
# Linux/Mac:
export EMAIL="you@example.com" PASSWORD="your-garmin-password"

# Trigger a login + a data dump (this writes the token cache):
python garmin_coach.py dump
```

If your account has **two-factor auth**, you'll be prompted for the one-time code the
first time. After that, tokens auto-refresh and you won't need to log in again unless the
refresh token expires or is revoked.

> **Note:** because it's an unofficial client, Garmin could change their auth at any time
> and temporarily break logins. Don't hammer it — excessive requests can trigger rate
> limiting (HTTP 429).

### 4. Choose your model backend

The coach calls a single function, **`run_llm(prompt, images)`**, so the model is
pluggable. Set `AGBOT_LLM` to pick one.

- **`copilot` (default)** — uses the **[GitHub Copilot CLI](https://docs.github.com/copilot/github-copilot-in-the-cli)**.
  Install it and sign in once (`copilot`), make sure the `copilot` command is on your PATH
  (or set `COPILOT_EXE`), and you're done — no API keys to manage.
  Pin a specific model with **`AGBOT_MODEL`** (any id from `/model`, e.g. `gpt-5.6-luna`, or
  `auto`) and its reasoning depth with **`AGBOT_REASONING_EFFORT`** (`low` … `max`); leave both
  unset for the CLI's own defaults. High-reasoning models are slower per brief, so raise
  **`AGBOT_LLM_TIMEOUT`** (seconds) if generations start timing out.
- **OpenAI / Anthropic / Ollama / …** — open `telegram_bridge.py`, find the
  `# --- Optional alternative backends ---` section, **uncomment** the one you want (each is
  ~8 lines), register it in the `_LLM_BACKENDS` dict, `pip install` its SDK, set its API key,
  and set `AGBOT_LLM=openai` (or `anthropic`, `ollama`, …). Adding a brand-new backend is
  just writing one `def my_backend(prompt, images) -> str` and adding it to that dict.

### 5. Personalize your profile

```bash
# Windows:
Copy-Item profile.example.md profile.md
# Linux/Mac:
cp profile.example.md profile.md
```

Edit **`profile.md`** — your goals, your gym/home equipment, any constraints. The more
specific the equipment list, the better the workout programming. `profile.md` is
git-ignored, so your details stay local.

### 6. (Optional) Use a `.env` file

Instead of setting env vars each time, copy `.env.example` to `.env` and fill it in. The
run scripts in `scripts/` auto-load it.

```bash
cp .env.example .env      # then edit .env
```

### 7. Run it

```bash
# Windows:
./scripts/run_bridge.ps1
# Linux/Mac:
./scripts/run_bridge.sh
```

You should see `AgBot online as @your_bot`. Message your bot `AgBot: GMS` and you'll get
your first brief. 🎉

---

## 💬 Using the bot

Prefix a message with **`AgBot`** (or `AgBot:`) — or just send it plainly; the bot only
listens to your owner chat id.

| You send | It does |
|---|---|
| `AgBot: GMS` (or `summary`, `brief`, `report`) | Morning brief: recovery read + today's workout |
| `AgBot week` | 7-day trend review |
| `AgBot nutrition` (or `macros`, `calories`) | Today's calorie/protein targets |
| `AgBot performance` (or `vo2`, `race`, `fitness age`) | Fitness stats: VO2max, race predictions, endurance/hill, FTP |
| `DWRE` (or `done`, `finished`) | Marks today's exercise done + sends the combined session debrief vs the plan |
| `rest day` (or `skip today`) | Stops the day's exercise check-ins |
| `AgBot how did I sleep?` (any question) | Context-aware answer |
| `had 3 eggs, oats & coffee` (or `log: …`) | Auto-logs a dated food entry that feeds nutrition coaching — `log:` prefix optional |
| *send a photo* (optional caption) | Analyzes the meal / machine screen / exercise |
| *send several photos together* | Weighs them as one set and recommends |
| *send a short video* 🎥 | Samples frames + transcribes narration, then coaches *(needs optional extras)* |
| *send a voice note* 🎤 | Transcribes locally, then answers *(needs optional extra)* |
| `my knee hurts` / `I feel sick` | Asks what's wrong, remembers it, adapts training until you say `recovered` |
| `recovered` | Clears active injury/illness flags |
| `/reset` | Clears recent-chat memory |
| `/help` | Shows the built-in help |

The bot also **auto-sends** your brief by ~9:30am if you didn't ask, sends **one combined
debrief** ~90 min after your last logged activity (or immediately on `DWRE`), **checks in**
through the day on whether you did the recommended exercise, **warns** you on a rough morning,
and (if enabled) **reminds** you to log meals and to drink water.

---

## 🔌 Optional extras

These features degrade gracefully — if the dependency isn't installed, the bot just skips
that capability.

- **Voice-note transcription** (local, offline):
  ```bash
  pip install faster-whisper
  ```
  First use downloads a small Whisper model from Hugging Face and caches it. CPU-only is
  fine.

- **Video understanding** (sample frames from clips):
  ```bash
  pip install av Pillow
  ```
  PyAV bundles the decoders (no system ffmpeg needed); Pillow resizes the sampled frames.

---

## 🔁 Running it unattended

The bridge is a long-running poller; keep it alive so it can push proactive briefs and
respond any time.

**Windows (Task Scheduler)** — create a task that runs at logon:
- Program: `powershell.exe`
- Arguments: `-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\garmin-ai-coach\scripts\run_bridge.ps1"`
- "Run whether user is logged on or not" is optional; "Restart on failure" is nice to have.

**Linux (systemd user service)** — e.g. `~/.config/systemd/user/garmin-coach.service`:
```ini
[Unit]
Description=Garmin AI Coach
After=network-online.target

[Service]
ExecStart=%h/garmin-ai-coach/scripts/run_bridge.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```
```bash
systemctl --user enable --now garmin-coach
```

**Anywhere, quick & dirty:** `nohup ./scripts/run_bridge.sh >/dev/null 2>&1 &`

---

## 🧪 Testing & troubleshooting

Run a generation once and print it to the console (no Telegram needed):

```bash
python telegram_bridge.py --selftest-summary
python telegram_bridge.py --selftest-qa "how did I sleep last night?"
python telegram_bridge.py --selftest-weekly
python telegram_bridge.py --selftest-nutrition
python telegram_bridge.py --selftest-performance
python telegram_bridge.py --selftest-voice path\to\audio.ogg
```

Check the Garmin data layer on its own:

```bash
python garmin_coach.py dump         # prints the full JSON snapshot
```

Logs are written to `logs/telegram_bridge.log`.

Common issues:
- **"No valid Telegram bot token found"** — set `AGBOT_TELEGRAM_TOKEN` or create
  `state/telegram_token.txt`.
- **Bot ignores you** — it only replies to the owner chat id. Confirm
  `AGBOT_OWNER_CHAT_ID` / `state/telegram_chat_id.txt` matches your id.
- **Garmin login fails / 429** — re-check credentials; if rate-limited, wait a few minutes.
  On Windows set `PYTHONUTF8=1` so emoji/accents don't crash encoding (the run scripts do
  this for you).
- **`copilot` backend errors** — ensure the Copilot CLI is installed, signed in, and on
  PATH (or set `COPILOT_EXE`), or switch `AGBOT_LLM` to another backend.

---

## ⚙️ Configuration reference

Everything has a sensible default; override via environment (or `.env`).

| Variable | Purpose |
|---|---|
| `AGBOT_TELEGRAM_TOKEN` | Telegram bot token (else `state/telegram_token.txt`) |
| `AGBOT_OWNER_CHAT_ID` | Your numeric chat id (else `state/telegram_chat_id.txt`) |
| `AGBOT_LLM` | Model backend name (default `copilot`) |
| `AGBOT_USER_NAME` | Optional first name so the coach can greet you |
| `AGBOT_MODEL` | Pin the Copilot CLI model (e.g. `gpt-5.6-luna`, or `auto`); unset = CLI default |
| `AGBOT_REASONING_EFFORT` | Copilot reasoning depth: `none`\|`minimal`\|`low`\|`medium`\|`high`\|`xhigh`\|`max` (default `low`) |
| `AGBOT_LLM_TIMEOUT` | Seconds allowed per generation (default `180`; raise for `max` reasoning) |
| `EMAIL` / `PASSWORD` | Garmin Connect login (first-run only) |
| `GARMINTOKENS` | Where Garmin OAuth tokens are cached (default `~/.garminconnect`) |
| `COPILOT_EXE` / `COPILOT_HOME` | Only for the default `copilot` backend, if needed |

Tunables (meal & hydration reminder times, exercise check-in hours, the ~90-min session
quiet window before the collective debrief, auto-brief window, poll timeout, history budgets,
etc.) live as constants near the top of `telegram_bridge.py`.

---

## 🔒 Data, privacy & security

- **Everything runs on your machine.** Your Garmin data, food logs and conversation live
  in `state/` (git-ignored) and never leave your box except in the prompts you send to
  **your chosen model backend**.
- **Secrets stay local.** `profile.md`, `.env`, `state/`, `logs/`, and token files are all
  git-ignored. **Never commit them.** If you fork this, double-check `git status` before
  your first push.
- **Token files** are the keys to your bot and your Garmin account — treat them like
  passwords. Revoke a leaked Telegram token via BotFather (`/mybots` → API Token → Revoke).

---

## ⚠️ Disclaimer

This project is **not affiliated with, endorsed by, or supported by Garmin**. It relies on
an unofficial client that may break if Garmin changes their systems, and it may be subject
to their terms of service — use it at your own risk. The coaching output is generated by an
AI model and is **for general informational purposes only — not medical, diagnostic, or
professional fitness advice**. Consult a qualified professional before starting or changing
any exercise or nutrition program.

---

## 🙌 Credits

- [`cyberjunky/python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) —
  the Garmin Connect client.
- [`SYSTRAN/faster-whisper`](https://github.com/SYSTRAN/faster-whisper) — optional local
  voice transcription.
- Inspired by the idea behind [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp).

## 📄 License

[MIT](LICENSE) © Amit Amola
