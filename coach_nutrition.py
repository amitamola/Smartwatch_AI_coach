"""Source-linked optional food logging; model prose is never the accounting ledger."""

import hashlib
import json
import math
import coach_clock
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

MEAL_MARKER = re.compile(r"\[\[MEAL:\s*(.*?)\]\]", re.I | re.S)
DAY_MARKER = re.compile(r"\[\[FOOD_DAY:\s*(.*?)\]\]", re.I | re.S)
MACROS = ("kcal", "protein_g", "carbs_g", "fat_g")


def _quantity(value):
    if value is None:
        return None
    bounds = value if isinstance(value, list) else [value, value]
    if (len(bounds) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                               or not math.isfinite(v) or v < 0 for v in bounds)
            or bounds[0] > bounds[1]):
        raise ValueError("Macro estimates must be nonnegative finite numbers, [low, high], or null.")
    return list(bounds)


def totals(items):
    result = {}
    for field in MACROS:
        values = [_quantity(item.get(field)) for item in items]
        result[field] = ([round(sum(v[i] for v in values), 1) for i in (0, 1)]
                         if values and all(v is not None for v in values) else None)
    return result


def midpoint(value):
    bounds = _quantity(value)
    return sum(bounds) / 2 if bounds is not None else None


def _source_quote(payload, source):
    quote = payload.get("source_quote")
    if not isinstance(quote, str) or not quote.strip() or quote not in source:
        raise ValueError("Food updates require an exact quote from the current user report.")
    return quote


def _consumed(quote, source, correction=False):
    position = source.find(quote)
    prefix = re.split(r"[.!?;\n]", source[:position])[-1]
    clause = prefix + quote
    if re.search(r"\b(?:not|never|didn't|did not|haven't|have not|won't|will|would|"
                 r"might|planning|plan to|if|tomorrow|should|could)\b|\?", clause, re.I):
        return False
    return bool(correction or re.search(
        r"\b(?:ate|eaten|had|having|drank|drunk|consumed|eating|drinking|finished)\b", quote, re.I)
        or re.match(r"\s*log\s*:", source, re.I))


def parse_food(text, source, today=None, reported_on=None):
    today = today or coach_clock.today()
    reported_on = reported_on or today
    meals, days, errors = [], [], []
    for marker, target in ((MEAL_MARKER, meals), (DAY_MARKER, days)):
        for raw in marker.findall(text or ""):
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("Food update must be an object.")
                quote = _source_quote(payload, source)
                day = date.fromisoformat(payload["date"])
                if day > today:
                    raise ValueError("Future food consumption cannot be logged.")
                prefix = re.split(r"[.!?;\n]", source[:source.find(quote)])[-1]
                clause = prefix + quote
                explicit_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", clause)
                if len(set(explicit_dates)) > 1:
                    raise ValueError("Use a separate source quote for each consumed-food date.")
                expected = (date.fromisoformat(explicit_dates[0]) if len(explicit_dates) == 1
                            else reported_on - timedelta(days=1) if re.search(r"\byesterday\b", clause, re.I)
                            else reported_on)
                if (not payload.get("replaces_entry_id") and len(explicit_dates) < 2
                        and day != expected):
                    raise ValueError("Food date must match its source report and original message day.")
                if marker is DAY_MARKER:
                    if payload.get("status") not in ("partial", "complete"):
                        raise ValueError("Food-day status must be partial or complete.")
                    full_day = re.search(
                        r"\b(?:(?:everything|all) (?:I(?:'ve| have)? )?(?:ate|had|eaten)|all (?:my )?meals|"
                        r"complete food log|nothing else|full day(?:'s)? (?:food|intake))\b", quote, re.I)
                    uncertain = re.search(
                        r"\b(?:not|never|isn't|is not|haven't|have not|won't|will|would|might|if|"
                        r"tomorrow|should|could)\b|\?", clause, re.I)
                    if payload["status"] == "complete" and (not full_day or uncertain):
                        raise ValueError("Complete intake requires an explicit full-day report.")
                else:
                    if not _consumed(quote, source, bool(payload.get("replaces_entry_id"))):
                        raise ValueError("Only reported consumption may be logged; clarify planned or ambiguous food.")
                    items = payload.get("items")
                    if not isinstance(items, list) or not 1 <= len(items) <= 40:
                        raise ValueError("Meal needs 1-40 food items, not an aggregate day total.")
                    for item in items:
                        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                            raise ValueError("Every food item needs a name.")
                        for field in MACROS:
                            _quantity(item.get(field))
                    payload["macros"] = totals(items)
                    if "total" in payload:
                        raise ValueError("Do not supply an extra meal/day total: the application sums items.")
                    note = payload.get("coaching_note", "")
                    if not isinstance(note, str) or re.search(
                            r"\d[\d,.]*\s*(?:kcal|calories|g\s*(?:protein|carbs?|fat))\b", note, re.I):
                        raise ValueError("coaching_note must be non-accounting advice, not competing food totals.")
                target.append(payload)
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc))
    clean = DAY_MARKER.sub("", MEAL_MARKER.sub("", text or "")).strip()
    return clean, meals, days, errors


class NutritionStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS food_events (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, payload TEXT NOT NULL,
                    request_id TEXT NOT NULL, source_text TEXT NOT NULL,
                    replaced_id TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS food_day_coverage (
                    day TEXT PRIMARY KEY, status TEXT NOT NULL, source_quote TEXT NOT NULL,
                    request_id TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS food_operations (
                    request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, entry_ids TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, meals, days, request_id, source_text, legacy_ids=()):
        if not request_id:
            raise ValueError("Food persistence requires a stable source request identity.")
        now = coach_clock.now().isoformat()
        entries = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            operation = json.dumps({"meals": meals, "days": days, "source": source_text}, sort_keys=True)
            prior = db.execute("SELECT payload,entry_ids FROM food_operations WHERE request_id=?",
                               (request_id,)).fetchone()
            if prior:
                if prior["payload"] != operation:
                    raise ValueError("A replay cannot change a recorded food operation.")
                return json.loads(prior["entry_ids"])
            for index, meal in enumerate(meals):
                event_id = "meal:" + hashlib.sha256(f"{request_id}:{index}".encode()).hexdigest()[:24]
                encoded = json.dumps(meal, sort_keys=True, ensure_ascii=False)
                old = db.execute("SELECT payload FROM food_events WHERE id=?", (event_id,)).fetchone()
                if old:
                    if old["payload"] != encoded:
                        raise ValueError("A replay cannot change an already recorded meal; use an explicit correction.")
                    entries.append(event_id)
                    continue
                replaced = meal.get("replaces_entry_id")
                if replaced:
                    target = db.execute("SELECT id,day FROM food_events WHERE id=?", (replaced,)).fetchone()
                    if target is None and replaced not in legacy_ids:
                        raise ValueError("The meal selected for correction was not found.")
                    if db.execute("SELECT 1 FROM food_events WHERE replaced_id=?", (replaced,)).fetchone():
                        raise ValueError("That meal already has a correction; select its latest version.")
                    if target:
                        db.execute("UPDATE food_day_coverage SET status='partial',updated_at=? WHERE day=?",
                                   (now, target["day"]))
                db.execute("INSERT INTO food_events VALUES (?,?,?,?,?,?,?)",
                           (event_id, meal["date"], encoded, request_id, source_text, replaced, now))
                # A new item after a complete-day declaration reopens its coverage.
                db.execute("UPDATE food_day_coverage SET status='partial',updated_at=? WHERE day=?",
                           (now, meal["date"]))
                entries.append(event_id)
            for day in days:
                db.execute("INSERT INTO food_day_coverage VALUES (?,?,?,?,?) ON CONFLICT(day) "
                           "DO UPDATE SET status=excluded.status,source_quote=excluded.source_quote,"
                           "request_id=excluded.request_id,updated_at=excluded.updated_at",
                           (day["date"], day["status"], day["source_quote"], request_id, now))
            db.execute("INSERT INTO food_operations VALUES (?,?,?)",
                       (request_id, operation, json.dumps(entries)))
        return entries

    def entries(self):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM food_events ORDER BY created_at,id").fetchall()
        return [{"entry_id": r["id"], "date": r["day"], "request_id": r["request_id"],
                 "ts": r["created_at"], "replaces_entry_id": r["replaced_id"],
                 "text": "; ".join(i["name"] for i in json.loads(r["payload"])["items"]),
                 "macros": json.loads(r["payload"])["macros"], "entry_type": "structured_meal"}
                for r in rows]

    def coverage(self):
        with self.connection() as db:
            return {r["day"]: r["status"] for r in db.execute("SELECT day,status FROM food_day_coverage")}


def render_receipt(meals, summaries):
    lines = []
    for meal in meals:
        labels = []
        for key, label in (("kcal", "kcal"), ("protein_g", "g protein"),
                           ("carbs_g", "g carbs"), ("fat_g", "g fat")):
            value = meal["macros"].get(key)
            amount = ("unknown" if value is None else
                      f"{value[0]:g}" if value[0] == value[1] else f"{value[0]:g}-{value[1]:g}")
            labels.append(f"{amount} {label}")
        breakdown = []
        for item in meal["items"]:
            estimates = []
            for key, label in (("kcal", "kcal"), ("protein_g", "g protein"),
                               ("carbs_g", "g carbs"), ("fat_g", "g fat")):
                bounds = _quantity(item.get(key))
                estimate = ("unknown" if bounds is None else f"{bounds[0]:g}"
                            if bounds[0] == bounds[1] else f"{bounds[0]:g}-{bounds[1]:g}")
                estimates.append(f"{estimate} {label}")
            breakdown.append("- " + item["name"] + ": " + ", ".join(estimates))
        lines.append(f"Meal saved for {meal['date']}:\n" + "\n".join(breakdown)
                     + "\nEstimated meal total: " + ", ".join(labels) + ".")
    for day in sorted({m["date"] for m in meals}):
        summary = next((r for r in summaries if r["date"] == day), None)
        if summary:
            amounts = [f"{summary[k]:g} {label}" if summary.get(k) is not None else f"{label} unknown"
                       for k, label in (("kcal", "kcal"), ("protein_g", "g protein"))]
            lines.append(f"Reported intake for {day}: " + ", ".join(amounts)
                         + ". Coverage: " + summary["coverage"] + ".")
    if lines:
        lines.append("Logging is optional. Unlogged food is unknown, not a shortfall or a reason to change training.")
    return "\n\n".join(lines)
