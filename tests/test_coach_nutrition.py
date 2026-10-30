import json
import tempfile
import unittest
from pathlib import Path
from datetime import date

from coach_nutrition import NutritionStore, parse_food, totals
from coach_trends import weight_trend


class NutritionTests(unittest.TestCase):
    def meal(self, quote="I ate yogurt.", **changes):
        value = {"date": "2026-02-03", "source_quote": quote,
                 "items": [{"name": "Yogurt", "kcal": [100, 150], "protein_g": 12}]}
        value.update(changes)
        return value

    def parse(self, meal, source=None):
        return parse_food("[[MEAL: " + json.dumps(meal) + "]]",
                          source or meal["source_quote"], today=date(2026, 2, 3))

    def test_consumption_provenance_is_not_just_a_substring(self):
        for source, quote in (("Tomorrow I will say I ate yogurt.", "I ate yogurt."),
                              ("I have not eaten yogurt.", "eaten yogurt."),
                              ("If I ate yogurt.", "I ate yogurt.")):
            self.assertTrue(self.parse(self.meal(quote), source)[3])
        self.assertFalse(self.parse(self.meal())[3])

    def test_item_arithmetic_and_unknown_values(self):
        meal = self.meal(items=[{"name": "Yogurt", "kcal": [100, 150], "protein_g": 12},
                               {"name": "Fruit", "kcal": 50}])
        _, meals, _, errors = self.parse(meal)
        self.assertEqual(errors, [])
        self.assertEqual(meals[0]["macros"]["kcal"], [150, 200])
        self.assertIsNone(meals[0]["macros"]["protein_g"])
        self.assertTrue(self.parse(self.meal(total={"kcal": 150}))[3])
        for value in (float("nan"), -1, True, [200, 100]):
            with self.assertRaises(ValueError):
                totals([{"kcal": value}])

    def test_two_dates_survive_and_replay_does_not_restore_old_coverage(self):
        first = self.meal("Yesterday I ate yogurt.", date="2026-02-02")
        second = self.meal("I ate fruit.", date="2026-02-03")
        text = "\n".join("[[MEAL: " + json.dumps(m) + "]]" for m in (first, second))
        _, meals, _, errors = parse_food(text, "Yesterday I ate yogurt. I ate fruit.", date(2026, 2, 3))
        self.assertEqual(errors, [])
        with tempfile.TemporaryDirectory() as tmp:
            store = NutritionStore(Path(tmp) / "coach.sqlite3")
            days = [{"date": "2026-02-03", "status": "complete",
                     "source_quote": "That's everything I ate."}]
            ids = store.save(meals, days, "request1", "source")
            self.assertEqual({e["date"] for e in store.entries()}, {"2026-02-02", "2026-02-03"})
            extra = dict(meals[1], items=[{"name": "Extra fruit", "kcal": 60}])
            extra["macros"] = totals(extra["items"])
            store.save([extra], [], "request2", "I ate extra fruit.")
            self.assertEqual(store.coverage()["2026-02-03"], "partial")
            self.assertEqual(store.save(meals, days, "request1", "source"), ids)
            self.assertEqual(store.coverage()["2026-02-03"], "partial")
            self.assertEqual(len(store.entries()), 3)

    def test_completeness_requires_a_full_day_statement(self):
        for quote in ("I ate breakfast, lunch and dinner.", "I meet my protein target.",
                      "That isn't everything I ate.", "If this were my complete food log."):
            marker = {"date": "2026-02-03", "status": "complete", "source_quote": quote}
            result = parse_food("[[FOOD_DAY: " + json.dumps(marker) + "]]", quote, date(2026, 2, 3))
            self.assertTrue(result[3])

    def test_explicit_and_delayed_food_dates_are_source_linked(self):
        wrong = self.meal("Yesterday I ate yogurt.")
        self.assertTrue(self.parse(wrong)[3])
        right = dict(wrong, date="2026-02-02")
        self.assertFalse(self.parse(right)[3])
        delayed = self.meal(date="2026-02-01")
        text = "[[MEAL: " + json.dumps(delayed) + "]]"
        self.assertFalse(parse_food(text, delayed["source_quote"], date(2026, 2, 3),
                                    reported_on=date(2026, 2, 1))[3])

    def test_correction_keeps_original_and_conflicting_replay_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = NutritionStore(Path(tmp) / "coach.sqlite3")
            meal = self.parse(self.meal())[1][0]
            old = store.save([meal], [], "one", "I ate yogurt.")[0]
            corrected = dict(meal, replaces_entry_id=old)
            store.save([corrected], [], "two", "It was two portions.")
            self.assertEqual(len(store.entries()), 2)
            self.assertEqual(store.entries()[1]["replaces_entry_id"], old)
            with self.assertRaises(ValueError):
                store.save([dict(corrected, date="2026-02-02")], [], "two", "different source")

    def test_daily_weight_means_do_not_overweight_multiple_weighins(self):
        rows = [{"calendarDate": "2026-02-01", "weight": 80000},
                {"calendarDate": "2026-02-01", "weight": 82000},
                {"calendarDate": "2026-02-02", "weight": 79000},
                {"calendarDate": "2026-01-25", "weight": 83000}]
        result = weight_trend(rows, date(2026, 2, 3))
        self.assertEqual(result["measured_days_7d"], 2)
        self.assertEqual(result["average_7d_kg"], 80)
        self.assertEqual(result["change_last_7d_kg"], -3)
        self.assertEqual(result["age_days"], 1)


if __name__ == "__main__":
    unittest.main()
