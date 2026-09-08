"""Synthetic fixtures only; test artifacts are created under this project, not temp."""

import json
import shutil
import sqlite3
import unittest
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch

from coach_memory import (
    LegacyMigrationError, MemoryBudgetError, MemoryStore, MemoryStoreError, normalize_key,
)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent / ("memory-fixture-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.db_path = self.folder / "private.sqlite3"
        self.store = MemoryStore(self.db_path)

    def legacy(self, name, entries):
        path = self.folder / name
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        return path

    @staticmethod
    def marker(**values):
        return "[[MEMORY: " + json.dumps(values) + "]]"

    def test_full_long_preference_survives_roundtrip_and_render(self):
        text = ("Avoid the fictional crescent hinge. " +
                "Keep the complete setup restriction and substitution detail. " * 20 +
                "Do not silently omit this final condition.")
        receipt = self.store.upsert(
            "preference", " Avoid Exercise:Crescent-Hinge ", text,
            source_text=text, verified=True, observed_on="2026-01-01",
        )
        self.assertTrue(receipt["saved"])
        self.assertEqual(receipt["key"], "avoid_exercise:crescent_hinge")
        self.assertEqual(receipt["text"], text)
        self.assertEqual(MemoryStore(self.db_path).records("preference")[0]["text"], text)
        self.assertIn(text, self.store.render("preference"))
        with self.assertRaises(MemoryBudgetError) as error:
            self.store.render("preference", max_chars=240)
        self.assertGreater(error.exception.required_chars, 240)

    def test_refinement_supersedes_key_and_preserves_audit(self):
        first = self.store.upsert(
            "anchor", "moon press", "The moon press caused fatigue.",
            source_type="assistant", observed_on="2025-01-01",
        )
        correction = "I reduced the moon press load; I did not report fatigue."
        second = self.store.upsert(
            "anchor", "MOON-PRESS", correction, source_text=correction,
            observed_on="2026-01-01", verified=True,
        )
        self.assertEqual(second["action"], "updated")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(len(self.store.records("anchor")), 1)
        rendered = self.store.render("anchor")
        self.assertIn(correction, rendered)
        self.assertNotIn(first["text"], rendered)
        history = self.store.revisions("anchor", "Moon Press")
        self.assertEqual([item["text"] for item in history], [first["text"], correction])
        self.assertEqual([item["verified"] for item in history], [False, True])
        self.assertEqual(history[0]["source_type"], "assistant")

    def test_identical_upsert_is_not_an_extra_revision(self):
        arguments = dict(kind="preference", key="schedule", text="Use fictional day one.",
                         observed_on="2026-01-01")
        first = self.store.upsert(**arguments)
        repeated = self.store.upsert(**arguments)
        self.assertFalse(repeated["saved"])
        self.assertEqual(repeated["action"], "unchanged")
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(len(self.store.revisions("preference", "schedule")), 1)

    def test_normalized_collision_keeps_namespace_and_does_not_merge_kinds(self):
        self.assertEqual(normalize_key(" Avoid Exercise:Moon-Press "), "avoid_exercise:moon_press")
        self.assertEqual(normalize_key("ＭＯＯＮ_ＰＲＥＳＳ"), "moon_press")
        self.store.upsert("preference", "moon-press", "Avoid it.")
        self.store.upsert("anchor", "moon_press", "An old performance note.")
        self.assertEqual(len(self.store.records("preference")), 1)
        self.assertEqual(len(self.store.records("anchor")), 1)
        with self.assertRaises(ValueError):
            normalize_key(" : ")

    def test_health_does_not_expire_after_45_days_or_any_fixed_age(self):
        self.store.upsert("health", "thoracic", "Thoracic soreness persists.",
                          observed_on="2000-01-01")
        self.assertIn("2000-01-01", self.store.render("health"))
        with self.assertRaises(MemoryBudgetError):
            self.store.render("health", max_chars=20)

    def test_older_queried_anchor_not_evicted_by_many_unrelated_entities(self):
        self.store.upsert("anchor", "moon cycle", "Moon cycle output was 17 fictional units.",
                          observed_on="2001-01-01")
        for index in range(65):
            self.store.upsert("anchor", f"unrelated_{index}",
                              f"Unrelated movement {index}: an ordinary synthetic note.")
        result = self.store.render("anchor", query="moon cycle output", max_chars=550)
        self.assertIn("17 fictional units", result)
        self.assertIn("active anchor records omitted", result)
        self.assertLessEqual(len(result), 550)
        for record in self.store.records("anchor"):
            if "[" + record["key"] + " |" in result:
                self.assertIn(record["text"], result)
        self.assertEqual(len(self.store.records("anchor")), 66)

    def test_anchor_budget_reports_whole_record_omissions_and_tiny_budget_errors(self):
        self.store.upsert("anchor", "large", "Unverified synthetic record. " * 100)
        output = self.store.render("anchor", max_chars=250)
        self.assertIn("1 active anchor records omitted", output)
        self.assertNotIn("Unverified synthetic record.", output)
        with self.assertRaises(MemoryBudgetError):
            self.store.render("anchor", max_chars=1)

    def test_verified_only_anchors_hide_unverified_claims_without_changing_default(self):
        quote = "I used seven fictional units for the moon press."
        self.store.upsert("anchor", "moon_press", quote, source_text=quote, verified=True)
        for source_type in ("user", "assistant", "legacy"):
            self.store.upsert(
                "anchor", "invented_" + source_type,
                "Invented capability from " + source_type + ": effortless and pain-free.",
                source_type=source_type,
            )
        default = self.store.render("anchor")
        filtered = self.store.render("anchor", query="invented capability", verified_only=True)
        self.assertIn(quote, filtered)
        self.assertIn("3 active anchor records excluded by verified-only filter", filtered)
        self.assertIn("user-reported is not independently verified", filtered)
        self.assertNotIn("Invented capability", filtered)
        self.assertNotIn("invented_", filtered)
        self.assertNotIn("effortless", filtered)
        self.assertNotIn("verified-only filter", default)
        self.assertEqual(default, self.store.render("anchor", verified_only=False))
        self.assertEqual(len(self.store.records("anchor")), 4)
        for source_type in ("user", "assistant", "legacy"):
            self.assertIn("Invented capability from " + source_type, default)
            self.assertEqual(len(self.store.revisions("anchor", "invented_" + source_type)), 1)

    def test_verified_only_requires_user_provenance_as_well_as_verified_flag(self):
        quote = "I reported a synthetic movement observation."
        self.store.upsert("anchor", "moon_press", quote, source_text=quote, verified=True)
        records = self.store.records("anchor")
        records[0]["source_type"] = "assistant"
        with patch.object(self.store, "records", return_value=records):
            filtered = self.store.render("anchor", verified_only=True)
        self.assertIn("1 active anchor records excluded by verified-only filter", filtered)
        self.assertNotIn(quote, filtered)
        self.assertNotIn("moon_press", filtered)

    def test_all_unverified_anchors_return_only_disclosure_and_respect_exact_budget(self):
        self.assertEqual(self.store.render("anchor", verified_only=True), "")
        self.store.upsert("anchor", "invented", "Invented limitless moon press capability.")
        self.store.upsert("anchor", "retired", "Retired invented capability.", status="resolved")
        filtered = self.store.render("anchor", verified_only=True)
        self.assertIn("1 active anchor records excluded by verified-only filter", filtered)
        self.assertNotIn("capability", filtered)
        self.assertNotIn("omitted by character budget", filtered)
        self.assertEqual(
            filtered, self.store.render("anchor", max_chars=len(filtered), verified_only=True),
        )
        with self.assertRaises(MemoryBudgetError) as error:
            self.store.render("anchor", max_chars=len(filtered) - 1, verified_only=True)
        self.assertEqual(error.exception.required_chars, len(filtered))

    def test_verified_only_discloses_provenance_and_budget_exclusions_separately(self):
        quote = "A complete synthetic user report. " * 100
        self.store.upsert("anchor", "long_report", quote, source_text=quote, verified=True)
        for index in range(2):
            self.store.upsert("anchor", f"invented_{index}", "Invented moon press capability.")
        output = self.store.render("anchor", max_chars=400, verified_only=True)
        self.assertLessEqual(len(output), 400)
        self.assertIn("2 active anchor records excluded by verified-only filter", output)
        self.assertIn("1 active anchor records omitted by character budget", output)
        self.assertNotIn("Invented moon press", output)
        self.assertNotIn("A complete synthetic user report.", output)
        with self.assertRaises(MemoryBudgetError):
            self.store.render("anchor", max_chars=1, verified_only=True)

    def test_verified_only_filter_cannot_hide_standing_constraints(self):
        for kind in ("health", "preference"):
            self.store.upsert(kind, "synthetic", "Keep this synthetic standing constraint.")
            with self.assertRaisesRegex(ValueError, "anchor-only"):
                self.store.render(kind, verified_only=True)
            self.assertIn("standing constraint", self.store.render(kind))
        for value in ("true", 1, None):
            with self.assertRaisesRegex(ValueError, "boolean"):
                self.store.render("anchor", verified_only=value)

    def test_legacy_import_is_idempotent_unverified_and_originals_unchanged(self):
        texts = {
            "health.jsonl": [{"date": "2001-01-01", "text": "Thoracic discomfort.", "status": "active"},
                             {"text": "Old ankle discomfort.", "status": "resolved"}],
            "preferences.jsonl": [{"text": "Keep every word. " * 60, "custom": {"synthetic": True}}],
            "anchors.jsonl": [{"date": "2026-01-01", "text": "Moon press was easy and pain-free.",
                              "verified": True, "source_type": "user", "ts": "2026-01-01T10:00:00"}],
        }
        originals = {}
        for name, entries in texts.items():
            path = self.legacy(name, entries)
            originals[path] = path.read_bytes()
        first = self.store.migrate_legacy(self.folder)
        second = MemoryStore(self.db_path).migrate_legacy(self.folder)
        self.assertEqual(first["imported"], 4)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped"], 4)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        anchor = self.store.records("anchor")[0]
        self.assertFalse(anchor["verified"])
        self.assertEqual(anchor["source_type"], "legacy")
        self.assertEqual(anchor["source_metadata"]["legacy_entry"], texts["anchors.jsonl"][0])
        self.assertEqual(anchor["source_metadata"]["source_line"], 1)
        output = self.store.render("anchor")
        self.assertIn("UNVERIFIED legacy", output)
        self.assertIn("do not establish effort, pain-free status, safety or causes", output)
        self.assertEqual(len(self.store.records("health")), 1)
        self.assertEqual(len(self.store.records("health", status=None)), 2)
        self.assertEqual(self.store.records("preference")[0]["date"], "")

    def test_appending_legacy_lines_imports_only_new_line(self):
        path = self.legacy("anchors.jsonl", [{"text": "Original fictional motion."}])
        self.store.migrate_legacy(self.folder)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"text": "New fictional motion."}) + "\n")
        result = self.store.migrate_legacy(self.folder)
        self.assertEqual((result["imported"], result["skipped"]), (1, 1))
        self.assertEqual(len(self.store.records("anchor")), 2)

    def test_legacy_collision_cannot_overwrite_a_current_correction(self):
        self.store.upsert("anchor", "moon_press", "Corrected current fact.")
        self.legacy("anchors.jsonl", [{"key": "moon press", "text": "Old conflicting claim."}])
        self.store.migrate_legacy(self.folder)
        current = {record["key"]: record for record in self.store.records("anchor")}
        self.assertEqual(current["moon_press"]["text"], "Corrected current fact.")
        self.assertEqual(len(current), 2)
        self.assertEqual(len(self.store.revisions("anchor", "moon_press")), 1)

    def test_invalid_legacy_batch_is_atomic_and_error_is_surfaced(self):
        self.legacy("health.jsonl", [{"text": "A valid synthetic health note."}])
        path = self.folder / "preferences.jsonl"
        path.write_text('{"text":"Valid restriction."}\nnot json\n', encoding="utf-8")
        with self.assertLogs("coach_memory", level="ERROR"):
            with self.assertRaisesRegex(LegacyMigrationError, "line 2"):
                self.store.migrate_legacy(self.folder)
        self.assertEqual(self.store.records("health"), [])
        self.assertEqual(self.store.records("preference"), [])
        self.assertTrue(path.read_text(encoding="utf-8").endswith("not json\n"))
        self.legacy("preferences.jsonl", [{"text": "Fixed synthetic record."}])
        self.assertEqual(self.store.migrate_legacy(self.folder)["imported"], 2)

    def test_missing_legacy_files_are_normal(self):
        self.assertEqual(self.store.migrate_legacy(self.folder),
                         {"imported": 0, "skipped": 0, "files": []})

    def test_corrupt_database_is_logged_and_never_returned_as_empty(self):
        corrupt = self.folder / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a SQLite database")
        with self.assertLogs("coach_memory", level="ERROR"):
            with self.assertRaises(MemoryStoreError):
                MemoryStore(corrupt)
        self.assertEqual(corrupt.read_bytes(), b"this is not a SQLite database")

    def test_schema_damage_after_start_is_surfaced(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as db:
            db.execute("DROP TABLE memory_records")
        with self.assertLogs("coach_memory", level="ERROR"):
            with self.assertRaises(MemoryStoreError):
                self.store.records("health")

    def test_explicit_migrations_can_upgrade_version_one_and_reject_future_schema(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as db:
            db.execute("DELETE FROM memory_schema_migrations WHERE version=2")
            db.execute("DROP TABLE memory_legacy_imports")
        self.store = MemoryStore(self.db_path)
        self.assertEqual(self.store.migrate_legacy(self.folder)["imported"], 0)
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as db:
            db.execute("INSERT INTO memory_schema_migrations VALUES (3,'synthetic')")
        with self.assertLogs("coach_memory", level="ERROR"):
            with self.assertRaisesRegex(MemoryStoreError, "schema version"):
                MemoryStore(self.db_path)

    def test_schema_can_share_database_with_unrelated_stores(self):
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as db:
            db.execute("CREATE TABLE unrelated_state (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO unrelated_state VALUES (1,'synthetic')")
        MemoryStore(self.db_path).upsert("preference", "other", "Complete note.")
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT value FROM unrelated_state").fetchone()[0], "synthetic")
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_selective_health_clear_keeps_distinct_area_and_preferences(self):
        self.store.upsert("health", "thoracic", "Upper back soreness.")
        lower = self.store.upsert("health", "low_back", "Lower back soreness.")
        self.store.upsert("preference", "avoid_exercise:moon_hinge", "Avoid moon hinge.")
        source = "My upper back symptoms have resolved."
        changed = self.store.resolve_health(["upper back"], source_text=source)
        self.assertEqual([record["key"] for record in changed], ["thoracic"])
        self.assertEqual(self.store.records("health")[0]["id"], lower["id"])
        self.assertEqual(len(self.store.records("preference")), 1)
        resolved = self.store.records("health", status="resolved")[0]
        self.assertEqual(resolved["resolution_source_text"], source)
        history = self.store.revisions("health", "thoracic")
        self.assertEqual([r["status"] for r in history], ["active", "resolved"])
        self.assertEqual(history[-1]["action"], "resolved")
        self.assertEqual(self.store.resolve_health(["upper back"], source_text=source), [])

    def test_lower_back_clear_does_not_clear_thoracic(self):
        self.store.upsert("health", "thoracic", "Mid back symptom.")
        self.store.upsert("health", "low_back", "Lumbar symptom.")
        self.assertEqual([r["key"] for r in self.store.resolve_health(["lumbar"])], ["low_back"])
        self.assertEqual(self.store.records("health")[0]["key"], "thoracic")

    def test_health_matches_exact_entities_not_incidental_text_or_generic_back(self):
        self.store.upsert("health", "thoracic", "Thoracic soreness, not lower back soreness.")
        self.store.upsert("health", "left_knee", "Left knee symptom.")
        self.store.upsert("health", "right_knee", "Right knee symptom.")
        self.assertEqual(self.store.resolve_health(["low back"]), [])
        self.assertEqual(self.store.resolve_health(["back"]), [])
        self.assertEqual([r["key"] for r in self.store.resolve_health(["left knee"])], ["left_knee"])
        self.assertEqual({r["key"] for r in self.store.records("health")}, {"thoracic", "right_knee"})

    def test_combined_health_record_is_not_partially_cleared(self):
        self.store.upsert("health", "combined", "Lumbar and thoracic symptoms.")
        self.assertEqual(self.store.resolve_health(["lumbar"]), [])
        self.assertEqual(len(self.store.resolve_health(["lumbar", "thoracic"])), 1)

    def test_global_clear_requires_explicit_caller_authorization(self):
        self.store.upsert("health", "thoracic", "Synthetic symptom.")
        with self.assertRaises(ValueError):
            self.store.resolve_health(["all"], source_text="Recovered.")
        with self.assertRaises(ValueError):
            self.store.resolve_health(["all", "thoracic"], allow_all=True)
        self.assertEqual(self.store.resolve_health(["everything"]), [])
        self.assertEqual(len(self.store.resolve_health(["all"], allow_all=True)), 1)

    def test_upsert_reopens_resolved_fact_with_revision_history(self):
        self.store.upsert("health", "thoracic", "Thoracic discomfort.")
        self.store.resolve_health("thoracic", source_text="It resolved.")
        result = self.store.upsert("health", "thoracic", "Thoracic symptoms returned.")
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["status"], "active")
        self.assertIsNone(result["resolved_at"])
        self.assertEqual(result["resolution_source_text"], "")

    def test_tolerance_reclassification_preserves_provenance_without_clinical_resolution(self):
        quote = "My lower back felt all good during and after moon squats and star bridges."
        source = "An unrelated opening sentence. " + quote
        original = self.store.upsert(
            "health", "low_back", quote, source_text=source, verified=True, observed_on="2025-02-03",
        )
        other = self.store.upsert("health", "thoracic", "Synthetic thoracic symptom.")
        self.store.upsert("preference", "avoid_exercise:moon_hinge", "Avoid moon hinge.")
        result = self.store.reclassify_tolerance(original["id"], "Moon Squat")
        anchor, retired = result["anchor"], result["health"]
        self.assertEqual(anchor["action"], "created")
        self.assertEqual(anchor["key"], "moon_squat")
        self.assertTrue(anchor["verified"])
        self.assertEqual(anchor["source_type"], "user")
        self.assertEqual(anchor["observed_on"], original["observed_on"])
        self.assertEqual(anchor["text"], quote)
        self.assertEqual(anchor["source_text"], source)
        self.assertEqual(anchor["source_metadata"]["reclassified_from"]["record_id"], original["id"])
        self.assertEqual(retired["id"], original["id"])
        self.assertEqual(retired["action"], "reclassified")
        self.assertEqual(retired["status"], "resolved")
        self.assertEqual(retired["revision"], original["revision"] + 1)
        self.assertEqual(retired["resolution_source_text"], "")
        self.assertEqual(retired["text"], quote)
        self.assertEqual(retired["source_text"], source)
        self.assertFalse(retired["source_metadata"]["reclassification"]["clinical_resolution"])
        self.assertEqual(retired["source_metadata"]["reclassification"]["record_id"], anchor["id"])
        self.assertEqual([r["id"] for r in self.store.records("health")], [other["id"]])
        self.assertEqual(len(self.store.records("preference")), 1)
        self.assertEqual([r["action"] for r in self.store.revisions("health", "low_back")],
                         ["created", "reclassified"])
        self.assertIn(quote, MemoryStore(self.db_path).render("anchor", verified_only=True))

    def test_tolerance_reclassification_rejects_mixed_hypothetical_and_nonspecific_reports(self):
        for quote, source, key in (
            ("My lower back is fine.", "My lower back is fine.", "moon_squat"),
            ("The moon squat felt fine except for lower back pain.",
             "The moon squat felt fine except for lower back pain.", "moon_squat"),
            ("My lower back is not fine during moon squats.",
             "My lower back is not fine during moon squats.", "moon_squat"),
            ("My lower back might feel fine during moon squats.",
             "My lower back might feel fine during moon squats.", "moon_squat"),
            ("My lower back feels fine during moon squats.",
             "My lower back feels fine during moon squats. But it still hurts when bending.",
             "moon_squat"),
            ("My lower back feels fine during moon squats.",
             "My lower back feels fine during moon squats.", "star_bridge"),
            ("My lower back feels fine during moon squats.",
             "My lower back feels fine during moon squats.", "lower_back"),
        ):
            with self.subTest(quote=quote, key=key):
                record = self.store.upsert(
                    "health", "low_back", quote, source_text=source, verified=True,
                )
                with self.assertRaises(ValueError):
                    self.store.reclassify_tolerance(record["id"], key)
                self.assertEqual(self.store.records("health")[0]["revision"], record["revision"])
                self.assertEqual(self.store.records("anchor"), [])

    def test_tolerance_reclassification_requires_active_verified_user_record(self):
        quote = "My lower back felt fine during moon squats."
        for kind, source_type, verified, status in (
            ("health", "user", False, "active"),
            ("health", "assistant", False, "active"),
            ("health", "legacy", False, "active"),
            ("health", "user", True, "resolved"),
            ("anchor", "user", True, "active"),
        ):
            with self.subTest(kind=kind, source_type=source_type, status=status):
                record = self.store.upsert(
                    kind, "source", quote, source_text=quote,
                    source_type=source_type, verified=verified, status=status,
                )
                with self.assertRaises(ValueError):
                    self.store.reclassify_tolerance(record["id"], "moon_squat")
                self.assertEqual(len(self.store.revisions(kind, "source")), record["revision"])
        for record_id in (True, -1, "1", 999999):
            with self.assertRaises(ValueError):
                self.store.reclassify_tolerance(record_id, "moon_squat")

    def test_tolerance_reclassification_cannot_overwrite_an_existing_anchor(self):
        quote = "The moon squat felt fine."
        record = self.store.upsert("health", "low_back", quote, source_text=quote, verified=True)
        existing = self.store.upsert("anchor", "moon_squat", "Keep this separate synthetic report.")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.reclassify_tolerance(record["id"], "moon_squat")
        self.assertEqual(self.store.records("health")[0]["revision"], record["revision"])
        self.assertEqual(self.store.records("anchor")[0]["text"], existing["text"])
        self.assertEqual(self.store.records("anchor")[0]["revision"], existing["revision"])

    def test_tolerance_reclassification_never_retires_earlier_unresolved_symptoms(self):
        symptom = "My lower back hurts when bending."
        self.store.upsert("health", "low_back", symptom, source_text=symptom, verified=True)
        quote = "My lower back felt fine during moon squats."
        record = self.store.upsert("health", "low_back", quote, source_text=quote, verified=True)
        with self.assertRaisesRegex(ValueError, "Earlier active symptom history"):
            self.store.reclassify_tolerance(record["id"], "moon_squat")
        self.assertEqual(self.store.records("health")[0]["revision"], record["revision"])
        self.assertEqual(self.store.records("anchor"), [])

    def test_tolerance_reclassification_rolls_back_anchor_if_retirement_audit_fails(self):
        quote = "My lower back felt fine during moon squats."
        record = self.store.upsert("health", "low_back", quote, source_text=quote, verified=True)
        original_snapshot = self.store._snapshot

        def snapshot(db, current, action):
            if action == "reclassified":
                raise sqlite3.OperationalError("Synthetic audit failure.")
            return original_snapshot(db, current, action)

        with patch.object(self.store, "_snapshot", side_effect=snapshot):
            with self.assertLogs("coach_memory", level="ERROR"):
                with self.assertRaises(MemoryStoreError):
                    self.store.reclassify_tolerance(record["id"], "moon_squat")
        self.assertEqual(self.store.records("anchor"), [])
        self.assertEqual(self.store.records("health")[0]["revision"], record["revision"])
        self.assertEqual(len(self.store.revisions("health", "low_back")), 1)

    def test_failed_revision_write_rolls_back_current_record(self):
        self.store.upsert("anchor", "moon_press", "Original fact.")
        with patch.object(self.store, "_snapshot", side_effect=sqlite3.OperationalError("synthetic")):
            with self.assertLogs("coach_memory", level="ERROR"):
                with self.assertRaises(MemoryStoreError):
                    self.store.upsert("anchor", "moon_press", "Replacement fact.")
        self.assertEqual(self.store.records("anchor")[0]["text"], "Original fact.")
        self.assertEqual(len(self.store.revisions("anchor", "moon_press")), 1)

    def test_concurrent_updates_preserve_every_revision(self):
        self.store.upsert("anchor", "moon_press", "Initial synthetic fact.")

        def update(index):
            return self.store.upsert("anchor", "moon_press", f"Synthetic update {index}.")

        with ThreadPoolExecutor(max_workers=4) as workers:
            receipts = list(workers.map(update, range(12)))
        self.assertEqual({r["revision"] for r in receipts}, set(range(2, 14)))
        self.assertEqual(len(self.store.revisions("anchor", "moon_press")), 13)

    def test_sql_values_are_parameterized(self):
        text = "Synthetic quote '); DROP TABLE memory_records; --"
        self.store.upsert("anchor", "odd'); DROP TABLE memory_records; --", text)
        self.assertEqual(self.store.records("anchor")[0]["text"], text)

    def test_verification_cannot_use_assistant_narrative(self):
        for provenance in ("assistant", "model", "legacy"):
            with self.assertRaises(ValueError):
                self.store.upsert("anchor", "moon_press", "Easy.", source_text="Easy.",
                                  source_type=provenance, verified=True)
        with self.assertRaises(ValueError):
            self.store.upsert("anchor", "moon_press", "Invented.", source_text="Different.",
                              verified=True)
        self.assertEqual(self.store.records("anchor"), [])

    def test_marker_exact_current_quote_is_verified_and_has_full_receipt(self):
        quote = "Do not recommend the fictional moon hinge. " + "Keep the constraint. " * 30
        marker = self.marker(kind="preference", key="avoid exercise:moon hinge",
                             text=quote, source_quote=quote, action="upsert")
        cleaned, receipts, errors = self.store.process_markers(
            "Acknowledged.\n" + marker, source_text="My request: " + quote)
        self.assertEqual(cleaned, "Acknowledged.")
        self.assertEqual(errors, [])
        self.assertTrue(receipts[0]["verified"])
        self.assertEqual(receipts[0]["text"], "My request: " + quote)
        self.assertTrue(receipts[0]["saved"])
        self.assertEqual(receipts[0]["source_text"], "My request: " + quote)

    def test_marker_paraphrase_cannot_launder_unsupported_safety_or_effort(self):
        quote = "I used fewer fictional units for the moon press."
        marker = self.marker(kind="anchor", key="moon_press",
                             text="The moon press was easy, safe and pain-free.", source_quote=quote)
        _, receipts, errors = self.store.process_markers(marker, quote)
        self.assertFalse(errors)
        self.assertEqual(receipts[0]["text"], quote)
        self.assertNotIn("easy, safe", self.store.render("anchor"))

    def test_marker_quote_cannot_strip_negation_or_cut_a_word(self):
        for source, quote in (("The moon press was not easy.", "easy"),
                              ("The moon press was painful.", "pain")):
            marker = self.marker(kind="anchor", key="moon_press",
                                 text=quote, source_quote=quote)
            _, receipts, errors = self.store.process_markers(marker, source)
            self.assertFalse(errors)
            self.assertEqual(receipts[0]["text"], source)
            self.assertIn(source, self.store.render("anchor"))

    def test_marker_quote_does_not_pull_unrelated_surrounding_sentences(self):
        quote = "Moon press felt heavy."
        source = "First unrelated sentence. " + quote + " Last unrelated sentence."
        marker = self.marker(kind="anchor", key="moon_press", text=quote, source_quote=quote)
        _, receipts, errors = self.store.process_markers(marker, source)
        self.assertFalse(errors)
        self.assertEqual(receipts[0]["text"], quote)

    def test_standalone_also_report_does_not_absorb_rep_correction_or_later_class(self):
        for quote in (
            "Also, for single arm cable row, I was able to do 20kg but it was kind of difficult.",
            "I also tried the single arm cable row at 20kg, but it was difficult.",
        ):
            with self.subTest(quote=quote):
                source = (
                    "The fictional log says 21 reps, but I completed 15 reps.\n\n"
                    + quote
                    + "\n\nAlso, tomorrow we will have a class with an unknown routine."
                )
                marker = self.marker(kind="anchor", key="single_arm_cable_row",
                                     text="An invented capability.", source_quote=quote)
                _, receipts, errors = self.store.process_markers(marker, source)
                self.assertFalse(errors)
                self.assertEqual(receipts[0]["text"], quote)
                self.assertEqual(receipts[0]["source_text"], source)
                rendered = self.store.render("anchor", verified_only=True)
                self.assertIn(quote, rendered)
                self.assertNotIn("21 reps", rendered)
                self.assertNotIn("15 reps", rendered)
                self.assertNotIn("tomorrow", rendered)
                self.assertNotIn("class", rendered)

    def test_positive_tolerance_requires_anchor_repair_without_clearing_health(self):
        original = self.store.upsert("health", "low_back", "Synthetic lumbar symptom.")
        for quote in (
            "My lower back felt all good during and after moon squats and star bridges.",
            "My lower back felt fine during the moon squat.",
            "There was no pain or discomfort during the moon squat.",
            "I did not feel pain during the moon squat.",
            "I have no lower back pain during the moon squat.",
            "The moon squat was pain-free.",
            "My lower back is better.",
        ):
            with self.subTest(quote=quote):
                marker = self.marker(kind="health", key="low_back", text="Active injury.",
                                     source_quote=quote)
                with self.assertLogs("coach_memory", level="WARNING"):
                    clean, receipts, errors = self.store.process_markers("Visible. " + marker, quote)
                self.assertEqual(clean, "Visible.")
                self.assertEqual(receipts, [])
                self.assertIn("kind='anchor'", errors[0])
                self.assertIn("Do not resolve", errors[0])
                self.assertEqual(self.store.records("health")[0]["revision"], original["revision"])
                repaired = self.marker(kind="anchor", key="moon_squat", text="No spinal strain.",
                                       source_quote=quote)
                _, receipts, errors = self.store.process_markers(repaired, quote)
                self.assertFalse(errors)
                self.assertEqual(receipts[0]["text"], quote)
                self.assertIn(quote, self.store.render("anchor", query="moon squat"))
        self.assertEqual(len(self.store.records("health")), 1)
        self.assertNotIn("No spinal strain", self.store.render("anchor"))

    def test_mixed_conditional_and_negated_positive_health_reports_stay_faithful(self):
        for quote in (
            "My lower back is fine except pain on the moon hinge.",
            "My lower back feels fine unless I bend.",
            "My lower back only feels fine at rest.",
            "My lower back feels fine if I avoid bending.",
            "My lower back feels fine, but it hurts on the moon hinge.",
            "My lower back isn't feeling fine.",
            "My lower back doesn't feel fine.",
            "My lower back is anything but fine.",
            "My lower back is not all good.",
            "My lower back is fine but stiff.",
            "My lower back is not without discomfort.",
            "I have less lower back pain than last week, but it still hurts.",
            "The moon squat caused lower back pain when I bent.",
            "If I bend, my lower back hurts.",
            "I cannot say my lower back is pain-free.",
        ):
            with self.subTest(quote=quote):
                marker = self.marker(kind="health", key="low_back", text="Resolved.",
                                     source_quote=quote)
                _, receipts, errors = self.store.process_markers(marker, quote)
                self.assertFalse(errors)
                self.assertEqual(receipts[0]["text"], quote)
                self.assertEqual(receipts[0]["status"], "active")
                self.assertIn(quote, self.store.render("health"))

    def test_scoped_tolerance_and_mixed_recovery_never_resolve_health(self):
        self.store.upsert("health", "low_back", "Synthetic lumbar symptom.")
        for quote in (
            "My lower back is pain-free during the moon squat.",
            "My lower back is pain-free while doing the moon squat.",
            "My lower back is pain-free, but only at rest.",
            "My lower back has no pain after the moon squat.",
            "My lower back is pain-free at rest.",
            "My lower back is pain-free for moon squats.",
            "My lower back is pain-free most of the time.",
            "My lower back is pain-free except on the moon hinge.",
            "My lower back pain is gone but it still hurts when bending.",
            "My lower back is pain-free but not fully recovered.",
            "My lower back pain was gone last year.",
            "Previously my lower back was pain-free. Now it hurts when bending.",
            "My lower back pain is gone. But it still hurts when bending.",
            "My lower back pain is gone. My lower back hurts when bending.",
            "My lower back pain is gone. Pain with bending remains.",
        ):
            with self.subTest(quote=quote):
                marker = self.marker(kind="health", key="low_back",
                                     source_quote=quote.split(".")[0], action="resolve")
                with self.assertLogs("coach_memory", level="WARNING"):
                    _, receipts, errors = self.store.process_markers(marker, quote)
                self.assertFalse(receipts)
                self.assertTrue(errors)
        self.assertEqual(self.store.records("health")[0]["revision"], 1)

    def test_current_named_recovery_stays_scoped_despite_other_area_symptoms(self):
        self.store.upsert("health", "low_back", "Synthetic lumbar symptom.")
        self.store.upsert("health", "thoracic", "Synthetic thoracic symptom.")
        quote = "My lower back pain is gone but my thoracic area still hurts."
        marker = self.marker(kind="health", key="low_back", source_quote=quote, action="resolve")
        _, receipts, errors = self.store.process_markers(marker, quote)
        self.assertFalse(errors)
        self.assertEqual(receipts[0]["status"], "resolved")
        self.assertEqual([r["key"] for r in self.store.records("health")], ["thoracic"])

    def test_unqualified_named_recovery_and_historical_comparison_still_resolve(self):
        for source in (
            "My lower back no longer hurts.",
            "My lower back is no longer sore.",
            "Previously my lower back hurt. Now my lower back pain is gone.",
        ):
            with self.subTest(source=source):
                self.store.upsert("health", "low_back", "Synthetic lumbar symptom.")
                marker = self.marker(kind="health", key="low_back", source_quote=source,
                                     action="resolve")
                _, receipts, errors = self.store.process_markers(marker, source)
                self.assertFalse(errors)
                self.assertEqual(receipts[0]["status"], "resolved")

    def test_dependent_pronoun_keeps_contiguous_antecedents_and_not_model_paraphrase(self):
        antecedent = "I tried the cable moon hinge and a bench-contact variation."
        report = "But they usually cause back issues."
        context = antecedent + "\n\n" + report
        source = "An unrelated opening sentence.\n\n" + context + "\n\nAn unrelated closing sentence."
        for key in ("avoid_exercise:cable_moon_hinge", "avoid_exercise:bench_contact_moon_hinge"):
            marker = self.marker(kind="preference", key=key,
                                 text="All hinge variants are dangerous.", source_quote=report)
            _, receipts, errors = self.store.process_markers(marker, source)
            self.assertFalse(errors)
            self.assertTrue(receipts[0]["verified"])
            self.assertEqual(receipts[0]["text"], context)
            self.assertEqual(receipts[0]["source_text"], source)
            self.assertIn(receipts[0]["text"], receipts[0]["source_text"])
            self.assertIn(context, self.store.render("preference"))
        self.assertNotIn("All hinge variants", self.store.render("preference"))
        self.assertNotIn("unrelated", self.store.render("preference"))
        self.assertEqual(len(self.store.records("preference")), 2)

    def test_context_follows_pronoun_chain_and_right_hand_exception(self):
        source = ("I tried a moon squat.\nI also tried a star bridge.\n"
                  "Both felt fine.\nBut the second one caused lower back pain later.")
        for quote in ("Both felt fine.", "lower back pain"):
            with self.subTest(quote=quote):
                marker = self.marker(kind="health", key="low_back", text="Pain-free.",
                                     source_quote=quote)
                _, receipts, errors = self.store.process_markers(marker, source)
                self.assertFalse(errors)
                self.assertEqual(receipts[0]["text"], source)
                self.assertIn(source, self.store.render("health"))

    def test_repeated_quote_requires_unique_context_instead_of_choosing_old_claim(self):
        source = "Last year I was pain-free. Today I am not pain-free."
        marker = self.marker(kind="anchor", key="moon_squat", text="Recovered.",
                             source_quote="pain-free")
        with self.assertLogs("coach_memory", level="WARNING"):
            _, receipts, errors = self.store.process_markers(marker, source)
        self.assertFalse(receipts)
        self.assertIn("unique passage", errors[0])

    def test_availability_keeps_old_versus_current_scope_verbatim(self):
        for context, quote in (
            ("Previously five days; now three days per week.", "five days"),
            ("Previously five days. Now three days per week.", "five days"),
            ("Previously five days.\n\nNow three days per week.", "three days per week"),
        ):
            with self.subTest(context=context):
                source = "An unrelated opening sentence. " + context + " Unrelated closing."
                marker = self.marker(kind="preference", key="schedule",
                                     text="Five days per week.", source_quote=quote)
                _, receipts, errors = self.store.process_markers(marker, source)
                self.assertFalse(errors)
                self.assertEqual(receipts[0]["text"], context)
                self.assertIn(receipts[0]["text"], source)
                self.assertIn(context, self.store.render("preference"))
                self.assertNotIn("Five days per week.", self.store.render("preference"))

    def test_family_scope_requires_explicit_user_exclusion_not_named_variants(self):
        for source in (
            "The cable moon hinge and bench-contact variation cause back issues.",
            "Avoid the cable moon hinge and bench-contact moon hinge.",
            "Could I avoid all moon hinge variants?",
            "If I avoid all moon hinges, would that help?",
            "My trainer suggested avoiding all moon hinges.",
            "I do not want to avoid all moon hinges.",
            "Do not avoid all moon hinges.",
            "Avoid the star press. I enjoy all moon hinge variants.",
            "Avoid the star press because all moon hinge variants felt fine.",
            "Avoid all cable moon hinges.",
            "Avoid all moon hinges except cable moon hinges.",
        ):
            with self.subTest(source=source):
                marker = self.marker(kind="preference", key="avoid_family:moon_hinge",
                                     text="Avoid all moon hinges.", source_quote=source)
                with self.assertLogs("coach_memory", level="WARNING"):
                    _, receipts, errors = self.store.process_markers(marker, source)
                self.assertFalse(receipts)
                self.assertIn("avoid_family requires", errors[0])
                self.assertIn("avoid_exercise", errors[0])
        self.assertEqual(self.store.records("preference"), [])

    def test_explicit_family_avoidance_uses_existing_preference_namespace(self):
        for source in (
            "Please avoid all moon hinge variants.",
            "I want to avoid all moon hinges.",
            "Exclude the entire moon hinge family.",
            "No moon hinge variants.",
            "Avoid moon hinges and all their variations.",
        ):
            with self.subTest(source=source):
                marker = self.marker(kind="preference", key="Avoid Family:Moon Hinge",
                                     text="Broad invented interpretation.", source_quote=source)
                _, receipts, errors = self.store.process_markers(marker, source)
                self.assertFalse(errors)
                self.assertEqual(receipts[0]["key"], "avoid_family:moon_hinge")
                self.assertEqual(receipts[0]["text"], source)
                self.assertIn(source, self.store.render("preference"))

    def test_one_variant_revocation_cannot_clear_a_whole_family(self):
        self.store.upsert("preference", "avoid_family:moon_hinge", "Avoid all moon hinges.")
        for source in (
            "Reintroduce the cable moon hinge.",
            "Remove my cable moon hinge restriction.",
            "Remove my cable moon hinge restriction because all moon hinges feel fine.",
        ):
            with self.subTest(source=source):
                marker = self.marker(kind="preference", key="avoid_family:moon_hinge",
                                     source_quote=source, action="resolve")
                with self.assertLogs("coach_memory", level="WARNING"):
                    _, receipts, errors = self.store.process_markers(marker, source)
                self.assertFalse(receipts)
                self.assertTrue(errors)
        source = "Remove my restriction on all moon hinges."
        marker = self.marker(kind="preference", key="avoid_family:moon_hinge",
                             source_quote=source, action="resolve")
        _, receipts, errors = self.store.process_markers(marker, source)
        self.assertFalse(errors)
        self.assertEqual(receipts[0]["status"], "resolved")

    def test_hypothetical_or_reported_suggestion_does_not_revoke_avoidance(self):
        self.store.upsert("preference", "avoid_exercise:moon_hinge", "Avoid moon hinge.")
        for source in (
            "Could I reintroduce the moon hinge?",
            "If I reintroduce the moon hinge, how would that work?",
            "My trainer suggested I reintroduce the moon hinge.",
            "I am considering whether to reintroduce the moon hinge.",
        ):
            with self.subTest(source=source):
                marker = self.marker(kind="preference", key="avoid_exercise:moon_hinge",
                                     source_quote=source, action="resolve")
                with self.assertLogs("coach_memory", level="WARNING"):
                    _, receipts, errors = self.store.process_markers(marker, source)
                self.assertFalse(receipts)
                self.assertTrue(errors)
        self.assertEqual(self.store.records("preference")[0]["revision"], 1)

    def test_marker_missing_current_quote_or_assistant_source_saves_nothing(self):
        marker = self.marker(kind="anchor", key="moon_press", text="It was easy.",
                             source_quote="It was easy.")
        for source, source_type in (("", "user"), ("A different message.", "user"),
                                    ("It was easy.", "assistant")):
            with self.assertLogs("coach_memory", level="WARNING"):
                clean, receipts, errors = self.store.process_markers(marker, source, source_type)
            self.assertEqual(clean, "")
            self.assertEqual(receipts, [])
            self.assertTrue(errors)
        self.assertEqual(self.store.records("anchor"), [])

    def test_marker_rejects_unknown_fields_malformed_json_and_unclosed_tag(self):
        valid = dict(kind="preference", key="schedule", text="Use day one.", source_quote="Use day one.")
        markers = [
            self.marker(**dict(valid, verified=True)),
            '[[MEMORY: {"kind": ]]]',
            '[[MEMORY: {"kind":"preference"}',
            self.marker(kind="unexpected", key="name", text="Use day one.", source_quote="Use day one."),
        ]
        for marker in markers:
            with self.assertLogs("coach_memory", level="WARNING"):
                cleaned, receipts, errors = self.store.process_markers(
                    "Visible response. " + marker, "Use day one.")
            self.assertNotIn("[[MEMORY", cleaned)
            self.assertFalse(receipts)
            self.assertTrue(errors)

    def test_marker_parser_handles_delimiters_inside_json_strings(self):
        quote = "The fictional label contains ]] characters."
        marker = self.marker(kind="anchor", key="label", text=quote, source_quote=quote)
        cleaned, receipts, errors = self.store.process_markers("Before " + marker + " After", quote)
        self.assertEqual(cleaned, "Before  After")
        self.assertFalse(errors)
        self.assertEqual(receipts[0]["text"], quote)

    def test_marker_resolves_one_exact_key_and_preserves_original_source(self):
        symptom = "Thoracic discomfort today."
        self.store.upsert("health", "thoracic", symptom, source_text=symptom, verified=True)
        self.store.upsert("health", "low_back", "Lumbar discomfort.")
        self.store.upsert("preference", "avoid_exercise:moon_hinge", "Avoid moon hinge.")
        recovery = "My thoracic symptoms have resolved."
        marker = self.marker(kind="health", key="thoracic", source_quote=recovery, action="resolve")
        _, receipts, errors = self.store.process_markers(marker, recovery)
        self.assertFalse(errors)
        self.assertEqual(receipts[0]["action"], "resolved")
        self.assertEqual(receipts[0]["source_text"], symptom)
        self.assertEqual(receipts[0]["resolution_source_text"], recovery)
        self.assertEqual(self.store.records("health")[0]["key"], "low_back")
        self.assertEqual(len(self.store.records("preference")), 1)

    def test_recovery_marker_cannot_clear_different_area_or_global_all(self):
        self.store.upsert("health", "low_back", "Lumbar discomfort.")
        quote = "My thoracic symptoms resolved."
        for key in ("low_back", "all", "missing"):
            marker = self.marker(kind="health", key=key, source_quote=quote, action="resolve")
            with self.assertLogs("coach_memory", level="WARNING"):
                _, receipts, errors = self.store.process_markers(marker, quote)
            self.assertFalse(receipts)
            self.assertTrue(errors)
        self.assertEqual(len(self.store.records("health")), 1)

    def test_t4_pain_free_quote_clears_thoracic_but_never_lumbar(self):
        lumbar = self.store.upsert("health", "low_back", "Synthetic lumbar discomfort.")
        for area in ("T4", "T-4"):
            with self.subTest(area=area):
                self.store.upsert("health", "thoracic", "Synthetic thoracic discomfort.")
                quote = f"My {area} is pain-free now."
                marker = self.marker(kind="health", key="thoracic", source_quote=quote, action="resolve")
                _, receipts, errors = self.store.process_markers(marker, quote)
                self.assertFalse(errors)
                self.assertEqual([record["key"] for record in receipts], ["thoracic"])
                self.assertEqual(self.store.records("health")[0]["id"], lumbar["id"])
                wrong_area = self.marker(kind="health", key="low_back",
                                         source_quote=quote, action="resolve")
                with self.assertLogs("coach_memory", level="WARNING"):
                    _, receipts, errors = self.store.process_markers(wrong_area, quote)
                self.assertFalse(receipts)
                self.assertTrue(errors)
        self.assertEqual(self.store.records("health")[0]["revision"], 1)

    def test_t4_area_alias_is_token_bounded_and_thoracic_only(self):
        self.store.upsert("health", "thoracic", "Synthetic thoracic symptom.")
        self.store.upsert("health", "low_back", "Synthetic lumbar symptom.")
        for area in ("T40", "T-40", "T14", "T4x"):
            self.assertEqual(self.store.resolve_health([area]), [])
        changed = self.store.resolve_health(["T-4"], source_text="Synthetic authorized area clear.")
        self.assertEqual([record["key"] for record in changed], ["thoracic"])
        self.assertEqual(self.store.records("health")[0]["key"], "low_back")

    def test_named_fine_or_alright_now_is_explicit_health_clear(self):
        self.store.upsert("health", "low_back", "Synthetic lumbar symptom.")
        for area in ("T4", "T-4", "thoracic"):
            for state in ("fine", "alright", "all right"):
                with self.subTest(area=area, state=state):
                    self.store.upsert("health", "thoracic", "Synthetic thoracic symptom.")
                    quote = f"My {area} is {state} now."
                    marker = self.marker(kind="health", key="thoracic",
                                         source_quote=quote, action="resolve")
                    _, receipts, errors = self.store.process_markers(marker, quote)
                    self.assertFalse(errors)
                    self.assertEqual(receipts[0]["status"], "resolved")
                    self.assertEqual(self.store.records("health")[0]["key"], "low_back")

    def test_partial_negated_conditional_or_unrelated_fine_now_is_not_clear(self):
        self.store.upsert("health", "thoracic", "Synthetic thoracic symptom.")
        for quote in (
            "My T4 feels better now.",
            "My T4 is not fine now.",
            "My T-4 isn't alright now.",
            "If my T4 is fine now, I will try a walk.",
            "My T4 might be alright now.",
            "Is my T4 fine now?",
            "My T4 still hurts although my bike is fine now.",
            "My T4 is fine now at rest but still hurts with motion.",
        ):
            with self.subTest(quote=quote):
                marker = self.marker(kind="health", key="thoracic", source_quote=quote, action="resolve")
                with self.assertLogs("coach_memory", level="WARNING"):
                    _, receipts, errors = self.store.process_markers(marker, quote)
                self.assertFalse(receipts)
                self.assertTrue(errors)
        self.assertEqual(self.store.records("health")[0]["revision"], 1)

    def test_marker_cannot_resolve_current_pain_or_negated_recovery(self):
        self.store.upsert("health", "thoracic", "Thoracic discomfort.")
        for quote in ("My thoracic area still hurts.",
                      "My thoracic symptoms have not resolved.",
                      "Have my thoracic symptoms resolved?",
                      "My lumbar pain is gone but my thoracic area still hurts."):
            marker = self.marker(kind="health", key="thoracic", source_quote=quote, action="resolve")
            with self.assertLogs("coach_memory", level="WARNING"):
                _, receipts, errors = self.store.process_markers(marker, quote)
            self.assertFalse(receipts)
            self.assertTrue(errors)
        self.assertEqual(len(self.store.records("health")), 1)

    def test_resolution_cannot_cherry_pick_recovery_from_negated_or_conditional_source(self):
        self.store.upsert("health", "thoracic", "Thoracic discomfort.")
        for source, quote in (
            ("I cannot say my thoracic symptoms resolved.", "thoracic symptoms resolved"),
            ("If my thoracic symptoms resolved, I could resume.", "thoracic symptoms resolved"),
        ):
            marker = self.marker(kind="health", key="thoracic", source_quote=quote, action="resolve")
            with self.assertLogs("coach_memory", level="WARNING"):
                _, receipts, errors = self.store.process_markers(marker, source)
            self.assertFalse(receipts)
            self.assertTrue(errors)
        self.assertEqual(len(self.store.records("health")), 1)

    def test_marker_preference_revocation_requires_named_explicit_intent(self):
        self.store.upsert("preference", "avoid_exercise:moon_hinge", "Avoid moon hinge.")
        for quote in ("Keep avoiding the moon hinge.", "Remove my other equipment preference."):
            marker = self.marker(kind="preference", key="avoid_exercise:moon_hinge",
                                 source_quote=quote, action="resolve")
            with self.assertLogs("coach_memory", level="WARNING"):
                _, receipts, errors = self.store.process_markers(marker, quote)
            self.assertFalse(receipts)
            self.assertTrue(errors)
        quote = "Remove my moon hinge restriction."
        marker = self.marker(kind="preference", key="avoid_exercise:moon_hinge",
                             source_quote=quote, action="resolve")
        _, receipts, errors = self.store.process_markers(marker, quote)
        self.assertFalse(errors)
        self.assertEqual(receipts[0]["status"], "resolved")

    def test_symptom_improvement_never_revokes_a_movement_preference(self):
        self.store.upsert("preference", "avoid_exercise:moon_hinge", "Avoid moon hinge.")
        for quote in ("My back feels better.",
                      "My back feels better during the moon hinge.",
                      "My thoracic symptoms resolved after the moon hinge."):
            marker = self.marker(kind="preference", key="avoid_exercise:moon_hinge",
                                 source_quote=quote, action="resolve")
            with self.assertLogs("coach_memory", level="WARNING"):
                _, receipts, errors = self.store.process_markers(marker, quote)
            self.assertFalse(receipts)
            self.assertTrue(errors)
        self.assertEqual(self.store.records("preference")[0]["status"], "active")
        self.assertEqual(len(self.store.revisions("preference", "avoid_exercise:moon_hinge")), 1)

    def test_legacy_model_markers_are_not_evidence(self):
        response = ("Visible. [[ANCHOR: Moon press was easy.]]"
                    "[[PREF: Avoid moon hinge.]][[HEALTH_FLAG: thoracic | painful]]"
                    "[[HEALTH_CLEAR: all]]")
        with self.assertLogs("coach_memory", level="WARNING"):
            cleaned, receipts, errors = self.store.process_markers(response, "Unrelated caption.")
        self.assertEqual(cleaned, "Visible.")
        self.assertFalse(receipts)
        self.assertTrue(errors)
        for kind in ("anchor", "health", "preference"):
            self.assertEqual(self.store.records(kind), [])

    def test_marker_database_errors_propagate_instead_of_false_receipt(self):
        quote = "Use the fictional moon cycle."
        marker = self.marker(kind="preference", key="equipment", text=quote, source_quote=quote)
        with patch.object(self.store, "_snapshot", side_effect=sqlite3.OperationalError("synthetic")):
            with self.assertLogs("coach_memory", level="ERROR"):
                with self.assertRaises(MemoryStoreError):
                    self.store.process_markers(marker, quote)
        self.assertEqual(self.store.records("preference"), [])

    def test_date_input_and_invalid_fields(self):
        receipt = self.store.upsert("anchor", "date", "Synthetic note.", observed_on=date(2026, 1, 1))
        self.assertEqual(receipt["date"], "2026-01-01")
        for changes in ({"kind": "other"}, {"status": "expired"}, {"text": ""},
                        {"observed_on": "not-a-date"}, {"verified": "true"}):
            values = dict(kind="anchor", key="synthetic", text="Synthetic note.")
            values.update(changes)
            with self.assertRaises(ValueError):
                self.store.upsert(**values)


if __name__ == "__main__":
    unittest.main()
