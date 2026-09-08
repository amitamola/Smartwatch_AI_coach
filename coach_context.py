"""Small, explicit views of coaching evidence and the authoritative schedule."""

import copy
import re
from datetime import date, timedelta


def plan_request_scope(question, completed_today=False):
    question = question or ""
    if (re.search(r"\b(?:recommended|prescribed|today's)\b", question, re.I)
            and re.search(r"\b(?:how exactly|what machine|which machine|what equipment|"
                          r"using dumbbells?|using cables?)\b", question, re.I)):
        if re.search(r"\btoday\b", question, re.I):
            return False if completed_today else "today"
        return "any"
    if (re.search(r"\b(?:class|rest day|schedule|tomorrow|monday|tuesday|wednesday|"
                  r"thursday|friday|saturday|sunday)\b", question, re.I)
            and re.search(r"\b(?:class|rest|recovery|workout|train|training|exercise|"
                          r"session|gym|cardio|strength|run|cycling|schedule)\b", question, re.I)
            and re.search(r"\b(?:can|could|should|join|attend|swap|move|keep|plan|want|"
                          r"let's|recommend|prescribe)\b", question, re.I)):
        return "any"
    if re.search(r"\b(?:give|recommend|prescribe|plan)\b.{0,60}\b(?:workout|session|routine)\b",
                 question, re.I | re.S):
        return "any"
    return False


def compact_workouts(value, full_chronology=False):
    """Preserve measured active sets; omit unrelated/rest chronology only when disclosed."""
    value = copy.deepcopy(value)

    def visit(item):
        if isinstance(item, dict):
            groups = item.get("logged_sets")
            if isinstance(groups, list) and groups:
                sequence = next((g.get("set_sequence") for g in groups
                                 if isinstance(g, dict) and g.get("set_sequence")), None)
                if sequence and not full_chronology:
                    for group in groups:
                        indices = group.get("set_indices")
                        if not isinstance(indices, list):
                            continue
                        group["observed_sets"] = [
                            thin(s) for i, s in enumerate(sequence)
                            if s.get("sequence_index", i) in indices]
                    # A missing grouping index must not cause loss of the sole measurement.
                    if all(isinstance(g.get("set_indices"), list) for g in groups):
                        for group in groups:
                            group.pop("set_sequence", None)
                        item["chronology_scope"] = (
                            "Active sets grouped by exercise; original indices retained. "
                            "Interleaved rests/other movements omitted in this view. "
                            "Do not infer preceding exercises or rest duration from it.")
            for key, child in item.items():
                if key == "set_sequence" and isinstance(child, list):
                    item[key] = [thin(s) for s in child]
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    def thin(item):
        if not isinstance(item, dict):
            return item
        # Unit/provenance policy remains on the activity. Unknown weights keep raw evidence.
        omitted = {"weight_unit_provenance", "source_index", "message_index"}
        result = {k: v for k, v in item.items() if k not in omitted}
        if len(result.get("exercises", [])) == 1 and result.get("exercise"):
            result.pop("exercises")
        return result

    visit(value)
    return value


def strip_schedule_rendering(text):
    """The application, not the model, owns the calendar card."""
    lines, skipping = [], False
    for line in text.splitlines():
        label = re.sub(r"[^a-z ]", "", line.lower()).strip()
        if label in {"coming up", "upcoming schedule", "saved schedule"}:
            skipping = True
            continue
        if skipping:
            if not line.strip() or re.match(r"\s*[-*]\s", line):
                continue
            skipping = False
        lines.append(line)
    return "\n".join(lines).strip()


def schedule_claim_errors(text, proposals, existing, today=None):
    """Catch definite near-term schedule promises contradicting the saved/returned plan."""
    today = today or date.today()
    current = {r["date"]: r.get("payload", r) for r in existing}
    current.update({p["date"]: p for p in proposals})
    days = {(today + timedelta(days=n)).strftime("%A").lower(): today + timedelta(days=n)
            for n in range(7)}
    days.update(today=today, tomorrow=today + timedelta(days=1))
    day_pattern = re.compile(r"\b(" + "|".join(days) + r")\b", re.I)
    errors = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if ("?" in sentence or re.search(
                r"\b(?:previous|earlier|original|showed|said|asked|last|was|had|if|could|consider|"
                r"depending|pending|possibly|skip|cancel|not)\b", sentence, re.I)):
            continue
        matches = list(day_pattern.finditer(sentence))
        for index, match in enumerate(matches):
            tail = sentence[match.end():matches[index + 1].start() if index + 1 < len(matches)
                            else len(sentence)][:100]
            rest = re.search(r"\b(?:rest|recovery)\b", tail, re.I)
            training = re.search(r"\b(?:strength|cardio|workout|class|run|cycling|"
                                 r"(?:upper|lower|full)[ -]body)\b", tail, re.I)
            if not rest and not training:
                continue
            is_rest = bool(rest and (not training or rest.start() < training.start()))
            day = days[match.group(1).lower()].isoformat()
            if day <= today.isoformat():
                continue
            plan = current.get(day)
            expected_rest = bool(plan and plan.get("kind") in {"rest", "recovery"})
            if not plan or expected_rest != is_rest:
                errors.append(
                    f"Definite {day} schedule wording does not match a saved or returned plan. "
                    "Return the intended dated SESSION_PLAN or remove the uncommitted promise; "
                    "calendar wording must come from the application's saved schedule.")
    return list(dict.fromkeys(errors))
