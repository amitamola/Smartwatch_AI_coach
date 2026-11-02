import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from coach_runtime import ArtifactConflictError, RuntimeStore, SnapshotCache


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.store = RuntimeStore(Path(self.temp.name) / "runtime.db")

    def update(self, number, album=None):
        message = {"chat": {"id": 123}, "text": "synthetic", "date": 100}
        if album:
            message.update(media_group_id=album, photo=[{"file_id": str(number)}])
        return {"update_id": number, "message": message}

    def test_cursor_and_inbox_survive_restart(self):
        self.store.ingest([self.update(10)], now=100)
        self.store.ingest([self.update(10)], now=101)
        self.assertEqual(self.store.offset(), 11)
        item = self.store.claim(now=101)
        self.assertEqual(item["attempts"], 1)
        self.assertIsNone(self.store.claim(now=101))
        self.store.recover()
        self.assertEqual(self.store.claim(now=102)["id"], item["id"])

    def test_album_parts_survive_restart_and_debounce(self):
        self.store.ingest([self.update(1, "group")], now=100)
        self.store.ingest([self.update(2, "group")], now=102)
        self.assertIsNone(self.store.claim(now=104))
        item = self.store.claim(now=105)
        self.assertEqual(len(item["payload"]["messages"]), 2)
        self.store.finish(item["id"])
        self.store.ingest([self.update(2, "group")], now=106)
        self.assertIsNone(self.store.claim(now=110))
        self.store.ingest([self.update(3, "group")], now=110)
        self.assertEqual(len(self.store.claim(now=114)["payload"]["messages"]), 1)

    def test_generation_failure_retries_then_surfaces_failure(self):
        self.store.ingest([self.update(1)], now=0)
        first = self.store.claim(now=0)
        self.assertEqual(self.store.fail(first["id"], "offline", max_attempts=2, now=0),
                         "pending")
        self.assertIsNone(self.store.claim(now=20))
        self.store.claim(now=31)
        self.assertEqual(self.store.fail(first["id"], "offline", max_attempts=2, now=31),
                         "failed")
        self.assertIsNone(self.store.claim(now=1000))

    def test_newer_requests_do_not_overtake_a_retry(self):
        self.store.ingest([self.update(1), self.update(2)], now=0)
        first = self.store.claim(now=0)
        self.store.fail(first["id"], "offline", now=0)
        self.assertIsNone(self.store.claim(now=1))
        self.assertEqual(self.store.claim(now=31)["id"], first["id"])

    def test_document_images_are_grouped_as_albums(self):
        updates = [self.update(1), self.update(2)]
        for update in updates:
            update["message"].update(media_group_id="documents",
                                     document={"mime_type": "image/jpeg", "file_id": "image"})
        self.store.ingest(updates, now=0)
        item = self.store.claim(now=4)
        self.assertTrue(item["payload"]["album"])
        self.assertEqual(len(item["payload"]["messages"]), 2)

    def test_summary_delivery_receipt_is_transactional(self):
        self.store.enqueue("summary", 1, {"text": "brief", "_summary_date": "2026-01-01",
                                         "_summary_complete": True}, now=0)
        self.store.delivered("summary", 99)
        self.assertTrue(self.store.summary_delivered("2026-01-01"))
        self.assertFalse(self.store.summary_pending("2026-01-01"))

    def test_delivery_failure_does_not_lose_answer(self):
        self.store.enqueue("reply", 1, {"text": "answer"}, now=0)
        self.store.enqueue("reply", 1, {"text": "different retry"}, now=0)
        self.store.delivery_failed("reply", "offline", retry_after=30, now=0)
        self.assertEqual(self.store.due_messages(now=20), [])
        reply = self.store.due_messages(now=31)[0]
        self.assertEqual(reply["payload"]["text"], "answer")
        self.store.delivered("reply", 99)
        self.assertEqual(self.store.due_messages(now=40), [])

    def test_backoff_does_not_let_later_chunks_overtake(self):
        self.store.enqueue("first", 1, {"text": "first part"}, now=0, group_id="response")
        self.store.enqueue("second", 1, {"text": "second part"}, now=1, group_id="response")
        self.store.enqueue("other", 1, {"text": "independent"}, now=2, group_id="other")
        self.store.delivery_failed("first", "rate limited", retry_after=30, now=1)
        self.assertEqual([r["id"] for r in self.store.due_messages(now=10)], ["other"])
        self.store.delivered("other", 3)
        self.assertEqual([r["id"] for r in self.store.due_messages(now=32)],
                         ["first", "second"])
        self.store.delivered("first", 1)
        self.assertEqual([r["id"] for r in self.store.due_messages(now=32)], ["second"])

    def test_terminal_failure_cancels_bundle_and_retains_records(self):
        for key in ("first", "second"):
            self.store.enqueue(key, 1, {"text": key}, now=0, group_id="bundle")
        self.assertEqual(self.store.delivery_failed("first", "blocked", permanent=True), "failed")
        self.store.enqueue("late", 1, {"text": "late"}, group_id="bundle")
        self.store.delivered("first", 99)
        self.assertEqual(self.store.counts()["outbox"], {"failed": 1, "cancelled": 2})
        self.assertEqual(self.store.due_messages(now=10000), [])

    def test_transient_failures_are_bounded_and_no_default_expiration(self):
        self.store.enqueue("request", 1, {"text": "user result"}, now=0)
        self.assertEqual(self.store.delivery_failed("request", "offline", max_attempts=2, now=0),
                         "pending")
        self.assertEqual(self.store.due_messages(now=10**9)[0]["id"], "request")
        self.assertEqual(self.store.delivery_failed("request", "offline", max_attempts=2, now=20),
                         "failed")

    def test_expiry_applies_to_entire_scheduled_bundle_only(self):
        self.store.enqueue("schedule1", 1, {}, now=0, group_id="scheduled", expires_at=20)
        self.store.enqueue("schedule2", 1, {}, now=0, group_id="scheduled")
        self.store.enqueue("request", 1, {}, now=0)
        self.assertEqual(self.store.queue_health(now=30)["outbox"]["pending"]["count"], 3)
        self.assertEqual([r["id"] for r in self.store.due_messages(now=30)], ["request"])
        self.assertEqual(self.store.counts()["outbox"]["expired"], 2)

    def test_artifacts_survive_restart_and_conflicting_replay_is_rejected(self):
        value = {"input": {"source": "synthetic"}, "events": [1, 2]}
        saved = self.store.save_artifact("request:input", value)
        saved["events"].append(3)
        reopened = RuntimeStore(self.store.path)
        self.assertEqual(reopened.get_artifact("request:input"), value)
        self.assertEqual(reopened.save_artifact("request:input", value), value)
        with self.assertRaises(ArtifactConflictError):
            reopened.save_artifact("request:input", {"events": [3]})
        self.assertEqual(reopened.get_artifact("request:input"), value)
        self.assertIsNone(reopened.get_artifact("missing"))
        with self.assertRaises(ValueError):
            reopened.save_artifact("nan", {"value": float("nan")})

    def test_requested_and_cached_summary_survive_restart_without_losing_other_days(self):
        day = "2026-02-01"
        record = {"state": {"plans": "revision-2"}, "evidence": {"sleep": "observed"},
                  "text": "A validated summary"}
        self.store.note_summary_request(day)
        self.store.save_daily_summary(day, record)
        reopened = RuntimeStore(self.store.path)
        self.assertTrue(reopened.summary_requested(day))
        self.assertFalse(reopened.summary_requested("2026-02-02"))
        self.assertEqual(reopened.daily_summary(day), record)
        returned = reopened.daily_summary(day)
        returned["state"]["plans"] = "modified copy"
        self.assertEqual(reopened.daily_summary(day), record)
        reopened.save_daily_summary("2026-02-02", dict(record, text="Next day"))
        self.assertEqual(reopened.daily_summary(day), record)

    def test_superseding_delivery_cancels_only_unsent_chunks_of_that_group(self):
        self.store.enqueue("first", 1, {"text": "old first"}, group_id="old")
        self.store.enqueue("second", 1, {"text": "old second"}, group_id="old")
        self.store.enqueue("new", 1, {"text": "new summary"}, group_id="new")
        self.store.delivered("first", 5)
        self.store.cancel_delivery("first", "Coaching context changed")
        self.assertEqual([r["id"] for r in self.store.due_messages()], ["new"])
        with self.store.connection() as db:
            row = db.execute("SELECT status,last_error,payload FROM outbox WHERE id='second'").fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["last_error"], "Coaching context changed")
        self.assertIn("old second", row["payload"])

    def test_sent_chunk_deadline_expires_remaining_scheduled_chunks(self):
        self.store.enqueue("first", 1, {}, now=0, group_id="scheduled", expires_at=20)
        self.store.enqueue("second", 1, {}, now=0, group_id="scheduled")
        self.store.delivered("first", 1)
        self.assertEqual(self.store.due_messages(now=30), [])
        self.assertEqual(self.store.counts()["outbox"], {"sent": 1, "expired": 1})

    def test_edited_update_keeps_source_identity_and_processing_order(self):
        original = self.update(1)
        original["message"]["message_id"] = 20
        edited = {"update_id": 2, "edited_message": {
            **original["message"], "edit_date": 150, "text": "corrected"}}
        self.store.ingest([original, edited], now=0)
        first = self.store.claim(now=0)
        self.assertIsNone(self.store.claim(now=0))
        self.store.finish(first["id"])
        second = self.store.claim(now=0)
        self.assertEqual(second["payload"]["updates"], [edited])
        self.assertEqual(second["payload"]["messages"][0]["message_id"], 20)

    def test_migration_of_old_outbox_is_additive(self):
        import sqlite3
        path = Path(self.temp.name) / "old.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE outbox (id TEXT PRIMARY KEY,chat_id TEXT,payload TEXT,"
                       "status TEXT,attempts INTEGER,available_at REAL,created_at REAL,"
                       "telegram_message_id TEXT,last_error TEXT)")
            db.execute("INSERT INTO outbox VALUES ('old','1','{}','pending',0,0,0,NULL,NULL)")
            db.commit()
        migrated = RuntimeStore(path)
        self.assertEqual(migrated.due_messages(now=1)[0]["id"], "old")
        self.assertIsNone(migrated.due_messages(now=1)[0]["expires_at"])

    def test_cache_does_not_hide_errors_or_mutate_shared_snapshot(self):
        calls = []
        def load():
            calls.append(1)
            return {"value": []}
        cache = SnapshotCache(load)
        first = cache.get()
        first["value"].append("changed")
        self.assertEqual(cache.get()["value"], [])
        self.assertEqual(len(calls), 1)
        cache.loader = lambda: {"__error__": "offline"}
        self.assertIn("__error__", cache.get(force=True))

    def test_stale_snapshot_requires_opt_in_and_keeps_original_provenance(self):
        cache = SnapshotCache(lambda: {"sensor": [1]}, ttl_seconds=1)
        with patch("coach_runtime.time.time", return_value=100):
            fresh = cache.get()
        cache.invalidate()
        cache.loader = lambda: {"__error__": "offline"}
        with patch("coach_runtime.time.time", return_value=200):
            self.assertIn("__error__", cache.get())
            stale = cache.get(allow_stale=True)
        self.assertEqual(stale["sensor"], [1])
        self.assertTrue(stale["snapshot_cache"]["stale"])
        self.assertEqual(stale["snapshot_cache"]["age_seconds"], 100)
        self.assertEqual(stale["snapshot_cache"]["loaded_at"], fresh["snapshot_cache"]["loaded_at"])
        self.assertEqual(stale["snapshot_cache"]["load_error"], "offline")
        cache.loader = lambda: {"sensor": [2]}
        self.assertFalse(cache.get()["snapshot_cache"]["stale"])

    def test_snapshot_io_errors_are_opt_in_but_programming_errors_propagate(self):
        cache = SnapshotCache(lambda: {"sensor": 1})
        cache.get()
        with patch.object(cache, "loader", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                cache.get(force=True)
            self.assertTrue(cache.get(force=True, allow_stale=True)["snapshot_cache"]["stale"])
        with patch.object(cache, "loader", side_effect=ValueError("broken parser")):
            with self.assertRaises(ValueError):
                cache.get(force=True, allow_stale=True)


if __name__ == "__main__":
    unittest.main()
