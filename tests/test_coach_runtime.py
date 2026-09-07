import tempfile
import unittest
from pathlib import Path

from coach_runtime import RuntimeStore, SnapshotCache


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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
        self.store.enqueue("first", 1, {"text": "first part"}, now=0)
        self.store.enqueue("second", 1, {"text": "second part"}, now=1)
        self.store.delivery_failed("first", "rate limited", retry_after=30, now=1)
        self.assertEqual(self.store.due_messages(now=10), [])
        self.assertEqual([r["id"] for r in self.store.due_messages(now=32)],
                         ["first", "second"])

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


if __name__ == "__main__":
    unittest.main()
