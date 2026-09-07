# Smartwatch AI coach

A self-hosted personal fitness assistant using Garmin Connect and Telegram.
The repository is general-purpose code and synthetic examples. Your filled-in
profile, conversations, health records, credentials and measurements stay in a
separate private data directory.

This is general fitness guidance, **not medical advice**. Garmin access uses the
unofficial `garminconnect` library and can be affected by upstream API changes.
Device scores and model recommendations are estimates, not diagnoses.

## What it does

- Morning brief on `GMS`, with an automatic fallback from 09:30 when the worker is
  available. The brief covers recovery, relevant trends and a dated session/rest plan.
- Conversational coaching, food logging, photos/albums, and optional voice/video.
- Revisionable preferences, capability reports and health records with provenance.
  Supported updates receive a receipt only after persistence succeeds.
- Separate dated training proposals and observed workouts. A recommendation is not
  treated as evidence that you completed it.
- Garmin workout detail and performance metrics, with explicit availability,
  timestamps and coverage. See [metric coverage](docs/metrics.md).
- Exercise, movement, meal and hydration reminders with context-sensitive guards.
- Durable Telegram inbox/outbox, retry state and stage timings. Polling and message
  delivery continue while the serial coaching worker generates a response.

The model's weights are **not trained on your chats**. Personalization comes from
stored facts, relevant retrieval, recorded outcomes and revisions. Source-quoted
updates are more reliable than an unverified model narrative, but extraction and
coaching can still be wrong. You can inspect memory using `memory` and the current
plan using `plan`.

## Architecture and privacy boundary

```text
public source / versioned release
  garmin_coach.py + garmin_metrics.py    data collection and interpretation
  coach_memory.py                     private revisionable memory
  coach_plan.py                       plans, outcomes and constraint checks
  coach_runtime.py                    durable transport and snapshot cache
  telegram_bridge.py                  orchestration, Telegram, model adapter
  prompts/                            shared policy and task-specific templates

private AGBOT_DATA_DIR (outside the source checkout)
  .env                                local configuration
  profile.md                          your goals/equipment/preferences
  state/                              private SQLite, logs/journals, uploads, tokens
  logs/                               private operational logs
  backups/                            private recovery copies
  releases/<commit>/                  immutable copies of public source
```

No personal fixture is required in GitHub or CI. `.gitignore` is a safety net, not
a substitute for keeping real data outside the checkout. Do not upload raw health
payloads, screenshots, evaluation outputs, backups or tokens with bug reports.

The configured model provider receives the prompt and any supplied attachments.
Telegram and Garmin also process their respective data. "Private local storage"
does **not** mean model inference is offline. Protect the data directory with
appropriate OS permissions/disk encryption and protect its backups.

## Setup

Requires Python 3.12, a syncing Garmin account, a Telegram bot, and an authenticated
GitHub Copilot CLI supporting the configured model and command-line flags.

```powershell
git clone https://github.com/amitamola/Smartwatch_AI_coach.git
Set-Location Smartwatch_AI_coach
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock

# Choose a PRIVATE directory outside this repository.
$private = Join-Path $HOME ".smartwatch-coach"
New-Item -ItemType Directory -Path $private -Force
Copy-Item profile.example.md (Join-Path $private "profile.md")
Copy-Item .env.example (Join-Path $private ".env")
```

Edit the private profile and configuration. Set `AGBOT_TELEGRAM_TOKEN` from
BotFather and `AGBOT_OWNER_CHAT_ID` to your own chat. Alternatively use the private
files `state/telegram_token.txt` and `state/telegram_chat_id.txt`. Keep credentials
out of command history and do not share URLs containing your bot token.

Authenticate Garmin using the current `garminconnect`/Garmin MCP authentication
workflow. Existing OAuth credentials at `~/.garminconnect` are reused; override
with `GARMINTOKENS` if needed. This bot does not drive interactive MFA unattended.

Authenticate the Copilot CLI interactively, then choose a supported model in your
private `.env`. Example, **subject to account/CLI availability**:

```dotenv
AGBOT_LLM=copilot
AGBOT_MODEL=gemini-3.8-flash
AGBOT_REASONING_EFFORT=medium
AGBOT_LLM_TIMEOUT=600
AGBOT_SNAPSHOT_TTL=120
```

The shipped backend is Copilot CLI. The adapter has extension points for other
providers; those providers are not automatically enabled by changing a name.
Routine model calls have an empty tool allowlist and explicitly use default context.

Launch on Windows:

```powershell
.\scripts\run_bridge.ps1 -DataDir $private -PythonExe "$PWD\.venv\Scripts\python.exe"
```

On Linux/macOS, create the equivalent private files, export `AGBOT_DATA_DIR` and
optionally `AGBOT_PYTHON`, then run `scripts/run_bridge.sh`. A persistent service
manager is needed for unattended operation. An interactive terminal is not one.

## Telegram commands

| Command | Purpose |
|---|---|
| `GMS` / `AgBot: GMS` | Morning brief |
| Ordinary text | Coaching question, preference, correction or feedback |
| `memory` | Inspect active preferences, health and capability records |
| `plan` | Inspect saved dated session proposals and user-reported status |
| `week` | Weekly review and proposed upcoming schedule |
| `performance` / `stats` | Available performance metrics and their dates |
| `nutrition` | Targets and reported intake context |
| `log: ...` | Log a report through the same validated update pipeline |
| `DWRE` | Report completion and request the collective debrief |
| `rest day` / `skip today` | Opt out of exercise reminders today |
| `recovered` | Explicitly resolve all active health flags, not movement exclusions |
| `/reset` | Clear recent conversation only; durable memory remains |

For selective recovery or correction, use ordinary language naming the relevant
body area or fact. Prefer an exact movement variant and clear units for training
feedback. Proposed, completed and too-difficult loads are different facts.

## Persistence and limits

- First startup imports legacy preference/health/anchor JSONL idempotently without
  rewriting originals. Imported narratives are labelled unverified.
- Active preferences and health constraints are not silently trimmed or expired.
  Capability retrieval is entity-aware; recent chat remains a bounded selection,
  not a guarantee that every sentence from seven days is in every prompt.
- Food replies show per-item estimates, meal totals and daily calorie/protein
  progress. Corrections can supersede an existing journal entry rather than add
  another meal; originals remain in the append-only audit. Estimates are not
  measured intake, and unconfigured targets remain unknown.
- Replies use phone-friendly headings and lists. Markdown tables are converted to
  labeled cards, and long replies preserve their formatting across Telegram messages.
- Structured `SESSION_PLAN` proposals are checked against supported explicit
  exclusions and rest-plan consistency before saving. These checks are not a
  comprehensive medical or biomechanical safety assessment.
- Garmin set detail is preserved where available. Missing data/unsupported device
  metrics remain unknown. Sensor estimates cannot establish form or perceived effort.
- Morning briefs, performance requests and workout questions include up to three
  relevant activity-detail records, with explicit coverage and cache status.
  Collective debriefs request detail for each activity in that session.
  Repeated workout payloads are referenced within the prompt instead of duplicating
  entire set timelines. Nutrition-focused replies omit irrelevant set detail while
  keeping activity totals, symptoms, preferences and planned/reported training state.
- Transport acknowledges receipt after durable storage and retries generation
  failures with a finite limit. Failed requests are retained and a notice is queued.
- Outgoing messages are retained for retry; a summary is marked sent only after
  delivery acknowledgement. Telegram cannot guarantee exactly-once delivery:
  a crash after Telegram accepts a message but before local acknowledgement may
  produce a duplicate.
- A local `/health` endpoint on `127.0.0.1:49517` reports release/model, queue counts
  and worker progress. It is not exposed to the network and has no health records.

## Models and evaluation

Newer is not automatically better. `scripts/evaluate_models.py` uses synthetic
coaching cases and checks declared decision invariants without touching live state.
Compare availability, correctness, output format and latency across repeated runs;
small samples do not prove clinical safety or universal model superiority.
Keep evaluation outputs private. Run the script with `--help` for its interface.
Install `requirements-dev.txt` for synthetic image generation and the full suite.

## Development and deployment

Run the offline standard-library regression suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

CI runs synthetic offline cases on Windows and Linux. `--selftest-*` generation
modes create temporary state before importing runtime components; they must never
write your production memory. They can still call Garmin/the model and incur usage.

For an existing Windows scheduled task, commit the reviewed code and deploy:

```powershell
.\scripts\deploy.ps1 -DataDir $private `
  -PythonExe "$PWD\.venv\Scripts\python.exe" -TaskName "AgBot-TelegramBridge"
```

Deployment archives the selected commit into a versioned release, runs its offline
suite, refuses to interrupt an active model reply, stops only the bridge's confirmed
processes, backs up private state, updates the task and waits for the release health
endpoint. Startup failure restores the previous task action. Keep the old release
and private backup for recovery; schema/data rollback requires the matching backup.
An optional private `-BeforeStartScript` can perform an idempotent migration after
the stopped-state backup. It receives `DataDir`, `ReleaseDir` and `PythonExe`; keep
legacy data intact. Profile and `.env` changes from that hook are restored on failure.

Deployment runs the full suite, so install `requirements-dev.txt` (which includes
`requirements.lock`) in the selected interpreter first. Optional voice/video
dependencies beyond Pillow are installed separately.
For first-time scheduling, configure Task Scheduler to run the launcher at logon
with restart-on-failure and no execution time limit. A logon task cannot run while
the host is unavailable or the required user session is absent.

The repository does not upload personal backups. Choose and maintain your own
encrypted/off-host backup policy; retaining a local backup alone does not protect
against host loss.

## Licence

[MIT](LICENSE) - Copyright Amit Amola.
