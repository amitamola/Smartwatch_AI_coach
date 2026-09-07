"""Offline synthetic model evaluation for coach upgrade comparisons.

The script uses a single long prompt with fixed, fictional coaching cases and
scores only objective JSON labels. It does not touch Garmin, Telegram, or the
coach runtime state. Results are written to a private session directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_ROOT = Path(
    os.environ.get(
        "AGBOT_MODEL_EVAL_DIR",
        str(Path.home() / ".copilot" / "session-state" / "model-evaluation"),
    )
)
DEFAULT_MODELS = ("gemini-3.7-flash", "gemini-3.8-flash", "claude-sonnet-5")
DEFAULT_REASONING_EFFORT = os.environ.get("AGBOT_REASONING_EFFORT", "medium")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("AGBOT_MODEL_EVAL_TIMEOUT", "600"))
DEFAULT_PROBE_TIMEOUT_SECONDS = int(os.environ.get("AGBOT_MODEL_EVAL_PROBE_TIMEOUT", "90"))
DEFAULT_REPEATS = int(os.environ.get("AGBOT_MODEL_EVAL_REPEATS", "2"))


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    title: str
    scenario: str
    allowed_labels: tuple[str, str]
    expected_label: str


@dataclass
class RunRecord:
    model: str
    run_kind: str
    index: int
    ok: bool
    returncode: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    command: list[str]
    attachments: list[str] | None = None
    parsed: dict[str, Any] | None = None
    parse_error: str | None = None
    case_results: list[dict[str, Any]] | None = None
    score: float | None = None
    checks: dict[str, Any] | None = None


@dataclass(frozen=True)
class RealisticCaseSpec:
    case_id: str
    title: str
    prompt: str
    attachments: tuple[str, ...]
    validator: str
    metadata: dict[str, Any]


def synthetic_cases() -> list[EvalCase]:
    return [
        EvalCase(
            case_id="symptom_resolution",
            title="Resolved symptoms do not imply permanent exclusion",
            scenario=(
                "The athlete had shoulder pain and temporarily avoided overhead pressing. "
                "The pain is now fully resolved, pushups are pain-free, and the athlete "
                "asks whether pressing should be permanently excluded."
            ),
            allowed_labels=("temporary_guarded_reintroduction", "permanent_exercise_avoidance"),
            expected_label="temporary_guarded_reintroduction",
        ),
        EvalCase(
            case_id="reduced_working_load",
            title="Later unrelated notes do not erase a deliberate load reduction",
            scenario=(
                "A lift was working at 19 kg, then the athlete intentionally reduced it to "
                "17 kg because form was better. Many unrelated notes follow. The question is "
                "whether the later unrelated notes erase the fact that 17 kg became the "
                "working load."
            ),
            allowed_labels=("retain_reduced_working_load", "overwrite_with_unrelated_notes"),
            expected_label="retain_reduced_working_load",
        ),
        EvalCase(
            case_id="chronology_over_category",
            title="Actual set chronology beats category-first reading",
            scenario=(
                "Squat set A, row set B, and squat set C are interleaved by timestamp. "
                "The coach should not infer that the later squat set was caused by fatigue "
                "just because it appears after rows in a category-first summary."
            ),
            allowed_labels=("use_chronology_not_category_order", "infer_fatigue_from_category_order"),
            expected_label="use_chronology_not_category_order",
        ),
        EvalCase(
            case_id="fat_grams",
            title="Fat burned in grams is not body-fat loss twice",
            scenario=(
                "A nutrition log says fat burned = 12 g. The coach must decide whether to "
                "treat that as direct body-fat loss and subtract it from calories again."
            ),
            allowed_labels=("do_not_double_subtract", "double_subtract_energy"),
            expected_label="do_not_double_subtract",
        ),
        EvalCase(
            case_id="acwr_deload",
            title="Low ACWR after a planned deload is not automatic vigorous exercise",
            scenario=(
                "Acute:chronic workload ratio is low because the athlete just completed a "
                "planned deload week. The coach must decide whether this automatically "
                "means vigorous exercise should be prescribed immediately."
            ),
            allowed_labels=("respect_planned_recovery", "automatic_vigorous_prescription"),
            expected_label="respect_planned_recovery",
        ),
        EvalCase(
            case_id="missing_metrics",
            title="Missing metric values are unknown, not zero",
            scenario=(
                "A device export omits hill score and endurance score for one day, while other "
                "dated estimates exist for lactate threshold and related metrics. The coach "
                "must decide whether missing should be treated as zero."
            ),
            allowed_labels=("missing_is_unknown", "missing_means_zero"),
            expected_label="missing_is_unknown",
        ),
        EvalCase(
            case_id="source_quote_plan_actual",
            title="Factual memory, planned session, and actual completion stay separate",
            scenario=(
                "A source quote says the athlete found an easy run pleasant. A separate plan "
                "describes tomorrow's intervals. Another record says a workout was completed "
                "later. The coach must not collapse the quote, plan, and actual completion "
                "into one fact."
            ),
            allowed_labels=("keep_source_plan_actual_separate", "collapse_into_one_event"),
            expected_label="keep_source_plan_actual_separate",
        ),
    ]


def build_distractor_context() -> str:
    lines: list[str] = []
    contexts = [
        "sleep", "commute", "hydration", "steps", "calendar", "shopping", "mail",
        "desk work", "weather", "stretching", "music", "meal prep", "bike lock",
        "laundry", "errands", "travel", "screen time", "meeting notes",
    ]
    for day in range(1, 41):
        for slot, context in enumerate(contexts):
            hour = 5 + (slot % 12)
            minute = (day * 3 + slot * 7) % 60
            lines.append(
                f"2026-08-{day:02d} {hour:02d}:{minute:02d} fiction-log {context}: "
                f"Alex Park noted an ordinary day item, recorded no training decision, "
                f"and moved on without changing any coaching fact."
            )
    filler = "\n".join(lines)
    return textwrap.fill(
        filler,
        width=108,
        break_long_words=False,
        break_on_hyphens=False,
    )


def build_prompt(cases: list[EvalCase]) -> str:
    distractor = build_distractor_context()
    cases_block = "\n\n".join(
        f"CASE {idx + 1}:\n"
        f"id: {case.case_id}\n"
        f"title: {case.title}\n"
        f"allowed labels: {', '.join(case.allowed_labels)}\n"
        f"scenario: {case.scenario}\n"
        for idx, case in enumerate(cases)
    )
    schema = (
        '{\n'
        '  "cases": [\n'
        '    {"case_id": "...", "label": "...", "rationale": "..."}\n'
        "  ]\n"
        "}"
    )
    return textwrap.dedent(
        f"""
        You are evaluating a coaching assistant on fixed synthetic cases.

        Return JSON only, with this exact top-level shape:
        {schema}

        Rules:
        - Choose exactly one allowed label per case.
        - Keep each rationale brief, factual, and case-specific.
        - Do not mention scores, policy, or hidden reasoning.
        - Do not use markdown fences.
        - Preserve the case_id for each case.

        Synthetic distractor context follows. It is intentionally long and mostly
        irrelevant. Ignore it unless a case explicitly cites it.

        {distractor}

        Cases:
        {cases_block}
        """
    ).strip()


def build_command(
    model: str | None,
    effort: str,
    prompt: str,
    attachments: list[str] | None = None,
    strict_deny_wildcard: bool = False,
) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    for key in (
        "AGENCY_ENGINE",
        "AGENCY_SESSION_ID",
        "AGENCY_OPERATION_ID",
        "AGENCY_LOG_SESSION_DIR",
        "COPILOT_AGENT_SESSION_ID",
        "COPILOT_LOADER_PID",
        "COPILOT_CLI",
        "MSFT_AGENCY",
    ):
        env.pop(key, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env["COPILOT_HOME"] = os.environ.get("COPILOT_HOME", os.path.expanduser("~/.copilot"))

    command = [
        os.environ.get("COPILOT_EXE", "copilot"),
        "-s",
        "--no-ask-user",
        "--available-tools=",
    ]
    if strict_deny_wildcard:
        command.append("--deny-tool=*")
    command.extend([
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--disable-mcp-server",
        "garmin",
        "--no-color",
        "--no-remote",
        "--no-remote-export",
        "--reasoning-effort",
        effort,
        "--context",
        "default",
    ])
    if model:
        command.extend(["--model", model])
    for att in attachments or []:
        if att:
            command.extend(["--attachment", att])
    return command, env


def run_copilot(
    model: str | None,
    effort: str,
    prompt: str,
    cwd: Path,
    timeout: int,
    attachments: list[str] | None = None,
    strict_deny_wildcard: bool = False,
) -> RunRecord:
    command, env = build_command(model, effort, prompt, attachments, strict_deny_wildcard)
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
            env=env,
        )
        duration = time.perf_counter() - start
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        return RunRecord(
            model=model or "auto",
            run_kind="probe",
            index=0,
            ok=completed.returncode == 0,
            returncode=completed.returncode,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            command=command,
            attachments=attachments or [],
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.perf_counter() - start
        return RunRecord(
            model=model or "auto",
            run_kind="probe",
            index=0,
            ok=False,
            returncode=None,
            duration_seconds=duration,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\nTIMEOUT after {timeout}s",
            command=command,
            attachments=attachments or [],
        )


def probe_model(model: str, effort: str, cwd: Path, timeout: int) -> RunRecord:
    probe_prompt = (
        'Return JSON only: {"cases":[{"case_id":"probe","label":"ok","rationale":"probe"}]}'
    )
    record = run_copilot(model, effort, probe_prompt, cwd, timeout)
    record.run_kind = "probe"
    return record


def parse_markers(text: str, marker_name: str) -> list[dict[str, Any]]:
    pattern = re.compile(rf"\[\[{marker_name}:\s*(.*?)\]\]", re.IGNORECASE | re.DOTALL)
    markers: list[dict[str, Any]] = []
    for raw in pattern.findall(text or ""):
        markers.append(json.loads(raw))
    return markers


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _extract_set_weights(plan: dict[str, Any]) -> list[Any]:
    weights: list[Any] = []
    for exercise in plan.get("exercises") or []:
        if not isinstance(exercise, dict):
            continue
        sets = exercise.get("sets") or []
        if not isinstance(sets, list):
            weights.append({"__invalid_sets__": sets})
            continue
        for item in sets:
            if isinstance(item, dict):
                weights.append(item.get("weight_kg"))
            else:
                weights.append({"__invalid_set_item__": item})
    return weights


def validate_case_a_output(text: str, *, today_iso: str, source_quote: str,
                           allowed_weight_kg: float, banned_variant: str) -> dict[str, Any]:
    try:
        memory_markers = parse_markers(text, "MEMORY")
        plan_markers = parse_markers(text, "SESSION_PLAN")
    except Exception as exc:  # noqa: BLE001 - malformed markers are a valid failure mode
        return {
            "memory_ok": False,
            "memory_match": None,
            "plan_ok": False,
            "plan_match": None,
            "bad_weights": [],
            "no_saved_promise": "saved" not in _lower(text),
            "parse_error": str(exc),
            "overall": False,
        }
    strict_quote = source_quote.strip()
    memory_ok = False
    memory_match: dict[str, Any] | None = None
    for marker in memory_markers:
        if _lower(marker.get("action")) != "upsert":
            continue
        if marker.get("source_quote") != strict_quote:
            continue
        if marker.get("text") != strict_quote:
            continue
        memory_ok = True
        memory_match = marker
        break

    plan_ok = False
    plan_match: dict[str, Any] | None = None
    weight_mismatch: list[Any] = []
    for marker in plan_markers:
        if marker.get("date") != today_iso:
            continue
        if _lower(marker.get("kind")) != "strength":
            continue
        exercises = marker.get("exercises")
        if not isinstance(exercises, list) or not exercises:
            continue
        names = [_lower(ex.get("name")) for ex in exercises if isinstance(ex, dict)]
        if any(banned_variant in name for name in names):
            continue
        if not any("goblet" in name for name in names):
            continue
        weights = _extract_set_weights(marker)
        if not weights:
            continue
        bad_weights = []
        for weight in weights:
            if weight is None:
                continue
            try:
                value = float(weight)
            except (TypeError, ValueError):
                bad_weights.append(weight)
                continue
            if value != float(allowed_weight_kg):
                bad_weights.append(weight)
        if bad_weights:
            weight_mismatch = bad_weights
            continue
        plan_ok = True
        plan_match = marker
        break

    no_saved_promise = "saved" not in _lower(text)
    return {
        "memory_ok": memory_ok,
        "memory_match": memory_match,
        "plan_ok": plan_ok,
        "plan_match": plan_match,
        "bad_weights": weight_mismatch,
        "no_saved_promise": no_saved_promise,
        "overall": memory_ok and plan_ok and no_saved_promise,
    }


def validate_case_b_output(text: str) -> dict[str, Any]:
    lower = _lower(text)
    number_checks = {
        "fat_12g": bool(re.search(r"\b12\s*g\b", lower)),
        "elapsed_23min": bool(re.search(r"\b23\s*min\b", lower)),
        "calories_240": bool(re.search(r"\b240\s*(?:cal|kcal|calories)\b", lower)),
    }
    negative_body_fat = any(
        phrase in lower
        for phrase in (
            "not body",
            "not body-fat",
            "not body fat",
            "not a measure of body-fat loss",
            "not a measure of body fat loss",
            "not actual body-fat loss",
            "not actual body fat loss",
            "rather than direct body-fat loss",
            "rather than direct body fat loss",
            "not a body-fat loss",
            "not a body fat loss",
        )
    )
    interpretation_ok = (
        "fuel" in lower
        and negative_body_fat
    )
    return {
        "number_checks": number_checks,
        "interpretation_ok": interpretation_ok,
        "overall": all(number_checks.values()) and interpretation_ok,
    }


def load_font(size: int):
    try:
        from PIL import ImageFont  # type: ignore
    except ImportError:  # pragma: no cover - environment already has Pillow
        return None
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for candidate in (
        windir / "Fonts" / "segoeui.ttf",
        windir / "Fonts" / "arial.ttf",
    ):
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except Exception:  # noqa: BLE001 - fallback to default font
                pass
    return ImageFont.load_default()


def write_png_text_image(path: Path, title: str, lines: list[str]) -> None:
    from PIL import Image, ImageDraw  # type: ignore

    width, height = 1280, 720
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    title_font = load_font(54)
    body_font = load_font(88)
    caption_font = load_font(28)

    draw.rectangle((20, 20, width - 20, height - 20), outline="black", width=4)
    draw.text((48, 42), title, fill="black", font=title_font)
    if caption_font:
        draw.text((48, height - 76), "synthetic evaluation image", fill="gray", font=caption_font)

    y = 180
    for line in lines:
        draw.text((60, y), line, fill="black", font=body_font)
        y += 140

    img.save(path)


def build_realistic_cases(run_dir: Path, today_iso: str) -> list[RealisticCaseSpec]:
    case_a_quote = "standard barbell back squats still irritate my knee, but goblet squats are okay again."
    case_a_prompt = textwrap.dedent(f"""
        You are replying to a current coaching correction.

        Current user message:
        "{case_a_quote}"
        "My last comfortable goblet squat was 18 kg for 3x8, and I can train 45 minutes today."

        Write a brief coaching reply that:
        - starts with `AgBot - {today_iso}`
        - includes exactly one [[MEMORY: ...]] marker
        - sets both `text` and `source_quote` in that MEMORY marker to the exact quote:
          "{case_a_quote}"
        - uses `action: "upsert"` and a stable avoidance/capability key for the standard barbell back squat
        - includes exactly one dated [[SESSION_PLAN: ...]] marker for {today_iso}
        - the marker values themselves must be valid compact JSON objects on a single line
        - the SESSION_PLAN JSON must prescribe a goblet-squat-based strength session, not the standard barbell back squat
        - use 18 kg for the working sets or a smaller conservative re-entry, but no other invented weight
        - keeps the answer concise and does not say anything was saved

        Use this exact marker style:
        [[MEMORY: {{"kind":"preference","key":"avoid_exercise:standard barbell back squat","text":"{case_a_quote}","source_quote":"{case_a_quote}","action":"upsert"}}]]
        [[SESSION_PLAN: {{"date":"{today_iso}","kind":"strength","objective":"Goblet squat re-entry","reason":"Keep the standard back squat out while reintroducing a tolerable variant.","exercises":[{{"name":"Goblet squat","sets":[{{"reps":"8","weight_kg":18,"rest_seconds":90}}],"effort":"Stop with two reps in reserve"}}]}}]]
    """).strip()

    image_dir = run_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    image1 = image_dir / "case_b_image1.png"
    image2 = image_dir / "case_b_image2.png"
    write_png_text_image(image1, "IMAGE 1 - FAT BURNER", ["FAT BURNER", "12 G"])
    write_png_text_image(image2, "IMAGE 2 - WORKOUT SUMMARY", ["ELAPSED 23 MIN", "240 CAL"])
    case_b_prompt = textwrap.dedent("""
        You have two attached synthetic images. Read both and combine the numbers.
        Answer in one short paragraph only.

        Preserve the units exactly as shown. Explain that the measurements are exercise
        fuel use, not body-fat loss, so do not subtract them twice from calories.
    """).strip()

    return [
        RealisticCaseSpec(
            case_id="case_a",
            title="Current avoidance correction with dated plan",
            prompt=case_a_prompt,
            attachments=(),
            validator="case_a",
            metadata={
                "today": today_iso,
                "source_quote": case_a_quote,
                "allowed_weight_kg": 18.0,
                "banned_variant": "standard barbell back squat",
            },
        ),
        RealisticCaseSpec(
            case_id="case_b",
            title="Two complementary synthetic images",
            prompt=case_b_prompt,
            attachments=(str(image1), str(image2)),
            validator="case_b",
            metadata={
                "expected_numbers": {
                    "fat_12g": True,
                    "elapsed_23min": True,
                    "calories_240": True,
                }
            },
        ),
    ]


def probe_exact_deny_tool_flag(cwd: Path, effort: str = "medium") -> dict[str, Any]:
    prompt = 'Return JSON only: {"cases":[{"case_id":"probe","label":"ok","rationale":"probe"}]}'
    start = time.perf_counter()
    command, env = build_command("gemini-3.7-flash", effort, prompt, strict_deny_wildcard=True)
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(cwd),
            env=env,
        )
        return {
            "command": command,
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "duration_seconds": round(time.perf_counter() - start, 3),
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "ok": False,
            "returncode": None,
            "duration_seconds": round(time.perf_counter() - start, 3),
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\nTIMEOUT after 30s",
        }


def save_run_artifacts(base_dir: Path, model: str, case_id: str, index: int,
                       run: RunRecord, checks: dict[str, Any] | None = None) -> dict[str, str]:
    run_dir = base_dir / model / case_id / f"run-{index}"
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    meta_path = run_dir / "meta.json"
    check_path = run_dir / "checks.json"
    stdout_path.write_text(run.stdout or "", encoding="utf-8")
    stderr_path.write_text(run.stderr or "", encoding="utf-8")
    write_json(meta_path, {
        "model": model,
        "case_id": case_id,
        "index": index,
        "ok": run.ok,
        "returncode": run.returncode,
        "duration_seconds": run.duration_seconds,
        "command": run.command,
        "attachments": run.attachments or [],
    })
    write_json(check_path, checks or {})
    return {
        "run_dir": str(run_dir),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "meta_path": str(meta_path),
        "check_path": str(check_path),
    }


def run_realistic_case(model: str, case: RealisticCaseSpec, workdir: Path, effort: str,
                       repeats: int, timeout: int, artifacts_root: Path) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for index in range(1, repeats + 1):
        run = run_copilot(
            model,
            effort,
            case.prompt,
            workdir,
            timeout,
            attachments=list(case.attachments),
        )
        run.run_kind = case.case_id
        run.index = index
        if case.validator == "case_a":
            checks = validate_case_a_output(
                run.stdout,
                today_iso=case.metadata["today"],
                source_quote=case.metadata["source_quote"],
                allowed_weight_kg=case.metadata["allowed_weight_kg"],
                banned_variant=case.metadata["banned_variant"],
            )
        else:
            checks = validate_case_b_output(run.stdout)
        run.checks = checks
        artifacts = save_run_artifacts(artifacts_root, model, case.case_id, index, run, checks)
        run_dict = run.__dict__
        run_dict["artifacts"] = artifacts
        runs.append(run_dict)
    latencies = [run["duration_seconds"] for run in runs if run["ok"]]
    pass_rate = sum(1 for run in runs if run.get("checks", {}).get("overall")) / len(runs) if runs else 0.0
    return {
        "case_id": case.case_id,
        "title": case.title,
        "attachments": list(case.attachments),
        "metadata": case.metadata,
        "runs": runs,
        "pass_rate": pass_rate,
        "median_latency_seconds": statistics.median(latencies) if latencies else None,
    }


def evaluate_realistic_models(models: list[str], workdir: Path, effort: str, repeats: int,
                              timeout: int) -> dict[str, Any]:
    today_iso = datetime.now().date().isoformat()
    cases = build_realistic_cases(workdir, today_iso)
    for case in cases:
        (workdir / f"{case.case_id}.prompt.txt").write_text(case.prompt, encoding="utf-8")
    protocol_probe = probe_exact_deny_tool_flag(workdir, effort=effort)
    results: list[dict[str, Any]] = []
    artifacts_root = workdir / "artifacts"
    for model in models:
        probe = probe_model(model, effort, workdir, timeout=30)
        if not probe.ok:
            results.append({
                "model": model,
                "supported": False,
                "probe": probe.__dict__,
                "cases": [],
                "overall_pass_rate": 0.0,
                "median_latency_seconds": None,
                "notes": probe.stderr or f"probe failed with code {probe.returncode}",
            })
            continue
        case_results = [
            run_realistic_case(model, case, workdir, effort, repeats, timeout, artifacts_root)
            for case in cases
        ]
        all_runs = [run for case in case_results for run in case["runs"] if run.get("ok")]
        results.append({
            "model": model,
            "supported": True,
            "probe": probe.__dict__,
            "cases": case_results,
            "overall_pass_rate": (
                sum(1 for case in case_results for run in case["runs"] if run.get("checks", {}).get("overall"))
                / sum(len(case["runs"]) for case in case_results)
                if case_results else 0.0
            ),
            "median_latency_seconds": statistics.median([r["duration_seconds"] for r in all_runs]) if all_runs else None,
            "notes": None,
        })
    recommendation = recommend_realistic(results)
    return {
        "mode": "realistic",
        "today": today_iso,
        "output_root": str(workdir),
        "artifacts_root": str(artifacts_root),
        "protocol_probe": protocol_probe,
        "cases": [
            {
                "case_id": case.case_id,
                "title": case.title,
                "prompt_path": str(workdir / f"{case.case_id}.prompt.txt"),
                "attachments": list(case.attachments),
                "metadata": case.metadata,
            }
            for case in cases
        ],
        "models": results,
        "recommendation": recommendation,
    }


def recommend_realistic(results: list[dict[str, Any]]) -> dict[str, Any]:
    lookup = {item["model"]: item for item in results}
    current = lookup.get("gemini-3.7-flash")
    upgraded = lookup.get("gemini-3.8-flash")
    sonnet = lookup.get("claude-sonnet-5")
    recommendation = {
        "switch": False,
        "message": "No clear upgrade from gemini-3.7-flash yet.",
        "basis": {},
    }
    if current:
        recommendation["basis"]["gemini-3.7-flash"] = {
            "pass_rate": current.get("overall_pass_rate"),
            "median_latency_seconds": current.get("median_latency_seconds"),
            "supported": current.get("supported"),
        }
    if upgraded:
        recommendation["basis"]["gemini-3.8-flash"] = {
            "pass_rate": upgraded.get("overall_pass_rate"),
            "median_latency_seconds": upgraded.get("median_latency_seconds"),
            "supported": upgraded.get("supported"),
        }
    if sonnet:
        recommendation["basis"]["claude-sonnet-5"] = {
            "pass_rate": sonnet.get("overall_pass_rate"),
            "median_latency_seconds": sonnet.get("median_latency_seconds"),
            "supported": sonnet.get("supported"),
        }
    if not current or not upgraded or not current.get("supported") or not upgraded.get("supported"):
        recommendation["message"] = "Could not compare Gemini models because one probe failed."
        return recommendation

    current_pass = current.get("overall_pass_rate") or 0.0
    upgraded_pass = upgraded.get("overall_pass_rate") or 0.0
    current_latency = current.get("median_latency_seconds") or float("inf")
    upgraded_latency = upgraded.get("median_latency_seconds") or float("inf")
    if upgraded_pass >= current_pass and upgraded_latency <= current_latency * 1.1 and upgraded_pass > 0.0:
        recommendation["switch"] = True
        recommendation["message"] = "Gemini 3.8 Flash looks equivalent or slightly better in this sample."
    else:
        recommendation["message"] = "Gemini 3.8 Flash did not clear the conservative switch threshold."
    return recommendation


def render_realistic_summary(result: dict[str, Any]) -> str:
    lines = [
        "# Realistic model evaluation summary",
        "",
        f"- Date: `{result['today']}`",
        f"- Output root: `{result['output_root']}`",
        f"- Protocol probe (strict `--deny-tool=*`): {result['protocol_probe']['ok']} "
        f"(code={result['protocol_probe']['returncode']})",
        f"- Protocol probe stderr: {result['protocol_probe']['stderr'][:200].strip() or 'n/a'}",
        "",
        "## Candidate overview",
    ]
    rows = []
    for model in result["models"]:
        rows.append([
            model["model"],
            "yes" if model["supported"] else "no",
            f"{model['overall_pass_rate']:.0%}",
            f"{model['median_latency_seconds']:.1f}" if model["median_latency_seconds"] is not None else "n/a",
        ])
    lines.append(format_table(["model", "supported", "overall_pass_rate", "median_latency_s"], rows))
    lines.append("")
    lines.append("## Case outputs")
    for model in result["models"]:
        lines.append(f"### {model['model']}")
        if not model["supported"]:
            lines.append(f"- unsupported: {model['notes']}")
            continue
        for case in model["cases"]:
            lines.append(f"#### {case['case_id']} — {case['title']}")
            lines.append(f"- pass rate: {case['pass_rate']:.0%}")
            lines.append(f"- median latency: {case['median_latency_seconds']:.1f}s" if case['median_latency_seconds'] is not None else "- median latency: n/a")
            for run in case["runs"]:
                excerpt = (run["stdout"] or "").strip().replace("\r", "")
                excerpt = excerpt[:300] + ("..." if len(excerpt) > 300 else "")
                lines.append(f"  - run {run['index']}: ok={run['ok']} code={run['returncode']} "
                             f"latency={run['duration_seconds']:.1f}s check={run.get('checks', {}).get('overall')} "
                             f"stdout={excerpt!r}")
                lines.append(f"    stdout_path: {run['artifacts']['stdout_path']}")
                lines.append(f"    stderr_path: {run['artifacts']['stderr_path']}")
                lines.append(f"    check_path: {run['artifacts']['check_path']}")
    lines.append("")
    lines.append("## Recommendation")
    lines.append(json.dumps(result["recommendation"], indent=2, sort_keys=True))
    lines.append("")
    lines.append("## Implementation limits")
    lines.append("- The strict `--deny-tool=*` syntax is rejected by this Copilot CLI, so working runs use the no-tool equivalent (`--available-tools=` plus disabled built-ins/MCPs).")
    lines.append("- Image quality is synthetic and file-based; the benchmark checks whether the model reads the intended numbers and units, not broader vision robustness.")
    lines.append("- Two repeats per case per model are directional only; no statistical significance is claimed.")
    return "\n".join(lines)


def extract_json_value(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty response")

    decoder = json.JSONDecoder()
    starts = [idx for idx in (stripped.find("{"), stripped.find("[")) if idx >= 0]
    for start in starts:
        try:
            value, _ = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
        raise ValueError("top-level JSON value must be an object")
    raise ValueError("no JSON object found in response")


def normalize_label(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def score_response(text: str, cases: list[EvalCase]) -> tuple[dict[str, Any] | None, list[dict[str, Any]], float | None, str | None]:
    try:
        parsed = extract_json_value(text)
    except Exception as exc:  # noqa: BLE001 - scoring helper should preserve the reason
        return None, [], None, str(exc)

    received = parsed.get("cases")
    if not isinstance(received, list):
        return parsed, [], 0.0, "missing cases list"

    by_id = {}
    duplicates = []
    for item in received:
        if not isinstance(item, dict):
            continue
        case_id = item.get("case_id")
        if case_id in by_id:
            duplicates.append(case_id)
            continue
        by_id[case_id] = item

    results: list[dict[str, Any]] = []
    correct = 0
    for case in cases:
        item = by_id.get(case.case_id)
        label = normalize_label(item.get("label")) if isinstance(item, dict) else ""
        rationale = ""
        if isinstance(item, dict):
            rationale = str(item.get("rationale", "")).strip()
        matched = bool(item) and label == normalize_label(case.expected_label) and bool(rationale)
        if matched:
            correct += 1
        results.append(
            {
                "case_id": case.case_id,
                "expected_label": case.expected_label,
                "received_label": label or None,
                "rationale": rationale,
                "matched": matched,
            }
        )

    score = correct / len(cases) if cases else 0.0
    if duplicates:
        return parsed, results, score, f"duplicate case ids: {', '.join(map(str, duplicates))}"
    return parsed, results, score, None


def evaluate_model(model: str, cases: list[EvalCase], prompt: str, workdir: Path,
                   effort: str, repeats: int, probe_timeout: int, run_timeout: int) -> dict[str, Any]:
    probe = probe_model(model, effort, workdir, probe_timeout)
    runs: list[RunRecord] = [probe]
    if not probe.ok:
        return {
            "model": model,
            "supported": False,
            "probe": probe.__dict__,
            "runs": [],
            "scores": [],
            "median_score": None,
            "median_latency_seconds": None,
            "parse_success_rate": 0.0,
            "case_summary": case_summary(cases, []),
            "error": probe.stderr or f"probe failed with code {probe.returncode}",
        }

    scores: list[float] = []
    evaluated_runs: list[dict[str, Any]] = []
    for index in range(1, repeats + 1):
        run = run_copilot(model, effort, prompt, workdir, run_timeout)
        run.run_kind = "evaluation"
        run.index = index
        parsed, case_results, score, parse_error = score_response(run.stdout, cases)
        run.parsed = parsed
        run.case_results = case_results
        run.score = score
        run.parse_error = parse_error
        runs.append(run)
        evaluated_runs.append(run.__dict__)
        if score is not None:
            scores.append(score)

    latencies = [run["duration_seconds"] for run in evaluated_runs if run["ok"]]
    valid_json_runs = [run for run in evaluated_runs if run["parsed"] is not None and run["score"] is not None]
    return {
        "model": model,
        "supported": True,
        "probe": probe.__dict__,
        "runs": evaluated_runs,
        "scores": scores,
        "median_score": statistics.median(scores) if scores else None,
        "median_latency_seconds": statistics.median(latencies) if latencies else None,
        "parse_success_rate": (len(valid_json_runs) / len(evaluated_runs)) if evaluated_runs else 0.0,
        "case_summary": case_summary(cases, evaluated_runs),
        "error": None,
    }


def case_summary(cases: list[EvalCase], runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for case in cases:
        labels = []
        matched = 0
        for run in runs:
            for item in run.get("case_results") or []:
                if item.get("case_id") == case.case_id:
                    labels.append(item.get("received_label"))
                    matched += int(bool(item.get("matched")))
                    break
        summary.append(
            {
                "case_id": case.case_id,
                "expected_label": case.expected_label,
                "labels": labels,
                "correct_runs": matched,
                "runs": len(runs),
            }
        )
    return summary


def recommend(results: list[dict[str, Any]]) -> dict[str, Any]:
    lookup = {item["model"]: item for item in results}
    current = lookup.get("gemini-3.7-flash")
    upgraded = lookup.get("gemini-3.8-flash")
    claude = lookup.get("claude-sonnet-5")
    recommendation = {
        "switch": False,
        "message": "No clear evidence to switch from gemini-3.7-flash yet.",
        "basis": {},
    }
    if not current or not upgraded or not current.get("supported") or not upgraded.get("supported"):
        recommendation["message"] = "Could not compare the Gemini models because one probe failed."
        return recommendation

    current_score = current.get("median_score") or 0.0
    upgraded_score = upgraded.get("median_score") or 0.0
    current_latency = current.get("median_latency_seconds") or float("inf")
    upgraded_latency = upgraded.get("median_latency_seconds") or float("inf")

    recommendation["basis"] = {
        "current_model": current["model"],
        "current_median_score": current_score,
        "current_median_latency_seconds": current_latency,
        "upgraded_model": upgraded["model"],
        "upgraded_median_score": upgraded_score,
        "upgraded_median_latency_seconds": upgraded_latency,
    }
    if upgraded_score - current_score >= 0.15 and upgraded_latency <= current_latency * 1.2:
        recommendation["switch"] = True
        recommendation["message"] = (
            "Gemini 3.8 Flash is the clearest upgrade in this sample and is not materially slower."
        )
    else:
        recommendation["message"] = (
            "Gemini 3.8 Flash did not clear the conservative switch threshold over Gemini 3.7 Flash."
        )
    if claude and claude.get("supported"):
        recommendation["basis"]["claude_median_score"] = claude.get("median_score")
        recommendation["basis"]["claude_median_latency_seconds"] = claude.get("median_latency_seconds")
    return recommendation


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    sep = "| " + " | ".join("-" * width for width in widths) + " |"
    head = "| " + " | ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)) + " |"
    body = [
        "| " + " | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)) + " |"
        for row in rows
    ]
    return "\n".join([head, sep, *body])


def render_summary(results: list[dict[str, Any]], prompt_path: Path, manifest: dict[str, Any]) -> str:
    overview_rows = []
    for item in results:
        overview_rows.append(
            [
                item["model"],
                "yes" if item["supported"] else "no",
                f"{item['median_score']:.3f}" if item["median_score"] is not None else "n/a",
                f"{item['median_latency_seconds']:.1f}" if item["median_latency_seconds"] is not None else "n/a",
                f"{item['parse_success_rate']:.0%}",
            ]
        )
    case_rows = []
    for item in results:
        for case in item["case_summary"]:
            labels = ", ".join(label for label in case["labels"] if label is not None) or "n/a"
            case_rows.append(
                [
                    item["model"],
                    case["case_id"],
                    case["expected_label"],
                    labels,
                    str(case["correct_runs"]),
                    str(case["runs"]),
                ]
            )

    rec = recommend(results)
    parts = [
        "# Model evaluation summary",
        "",
        f"- Prompt: `{prompt_path}`",
        f"- Output root: `{manifest['output_root']}`",
        f"- Repeats per supported model: {manifest['repeats']}",
        f"- Reasoning effort: `{manifest['reasoning_effort']}`",
        "",
        "## Candidate overview",
        format_table(
            ["model", "supported", "median_score", "median_latency_s", "parse_success"],
            overview_rows,
        ),
        "",
        "## Per-case outcomes",
        format_table(
            ["model", "case_id", "expected", "labels_seen", "correct_runs", "runs"],
            case_rows,
        ),
        "",
        "## Recommendation",
        json.dumps(rec, indent=2, sort_keys=True),
        "",
        "## Implementation limits",
        "- Objective scoring uses parsed JSON labels only; rationale text is human-readable context.",
        "- The sample is small and synthetic, so the result is directional rather than statistically strong.",
        "- Unsupported models or probe failures are reported but not scored as completed evaluations.",
        "- The script never touches Garmin, Telegram, deployed state, or repository files outside the two allowed edits.",
    ]
    return "\n".join(parts)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate Copilot CLI models on synthetic coaching cases.")
    parser.add_argument("--suite", choices=("standard", "realistic"), default="standard",
                        help="Which synthetic benchmark suite to run.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_ROOT), help="Private results root.")
    parser.add_argument("--models", nargs="*", default=list(DEFAULT_MODELS),
                        help="Models to probe; defaults to current, upgrade, and optional Claude.")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help="Evaluation runs per supported model.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Evaluation timeout in seconds.")
    parser.add_argument("--probe-timeout", type=int, default=DEFAULT_PROBE_TIMEOUT_SECONDS,
                        help="Probe timeout in seconds.")
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    args = parser.parse_args(argv)

    output_root = Path(args.output_dir)
    run_dir = output_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.suite == "standard":
        cases = synthetic_cases()
        prompt = build_prompt(cases)
        prompt_path = run_dir / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        manifest = {
            "suite": "standard",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "output_root": str(output_root),
            "run_dir": str(run_dir),
            "models": list(args.models),
            "repeats": args.repeats,
            "reasoning_effort": args.reasoning_effort,
            "timeout_seconds": args.timeout,
            "probe_timeout_seconds": args.probe_timeout,
            "command": " ".join(sys.argv if argv is None else [Path(sys.argv[0]).name, *argv]),
            "cases": [case.__dict__ for case in cases],
        }
        write_json(run_dir / "manifest.json", manifest)

        results = [
            evaluate_model(
                model=model,
                cases=cases,
                prompt=prompt,
                workdir=run_dir,
                effort=args.reasoning_effort,
                repeats=args.repeats,
                probe_timeout=args.probe_timeout,
                run_timeout=args.timeout,
            )
            for model in args.models
        ]
        summary = {
            "manifest": manifest,
            "results": results,
            "recommendation": recommend(results),
        }
        write_json(run_dir / "summary.json", summary)
        md = render_summary(results, prompt_path, manifest)
        (run_dir / "summary.md").write_text(md, encoding="utf-8")
        print(md)
        print()
        print(f"Results written to: {run_dir}")
        any_supported = any(item["supported"] for item in results)
        return 0 if any_supported else 1

    realistic = evaluate_realistic_models(list(args.models), run_dir, args.reasoning_effort,
                                          args.repeats, args.timeout)
    manifest = {
        "suite": "realistic",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "output_root": str(output_root),
        "run_dir": str(run_dir),
        "models": list(args.models),
        "repeats": args.repeats,
        "reasoning_effort": args.reasoning_effort,
        "timeout_seconds": args.timeout,
        "probe_timeout_seconds": args.probe_timeout,
        "command": " ".join(sys.argv if argv is None else [Path(sys.argv[0]).name, *argv]),
        "cases": realistic["cases"],
        "protocol_probe": realistic["protocol_probe"],
    }
    write_json(run_dir / "manifest.json", manifest)
    write_json(run_dir / "summary.json", realistic)
    md = render_realistic_summary(realistic)
    (run_dir / "summary.md").write_text(md, encoding="utf-8")
    print(md)
    print()
    print(f"Results written to: {run_dir}")
    any_supported = any(item["supported"] for item in realistic["models"])
    return 0 if any_supported else 1


if __name__ == "__main__":
    raise SystemExit(main())
