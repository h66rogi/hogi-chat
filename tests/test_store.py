"""승인 저장소 테스트. Discord·네트워크 없이 sender 콜백으로 검증한다."""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["HOGI_BASE"] = tempfile.mkdtemp(prefix="hogi-store-")

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import store  # noqa: E402


class VerdictTest(unittest.TestCase):
    def setUp(self):
        # 테스트마다 후보 파일을 비운다
        for name in ("candidates.jsonl", "training.jsonl"):
            open(os.path.join(store.BASE, name), "w").close()
        store.append_candidate({"message_id": "m1", "text": "ㅋㅋㅋ", "ts": int(time.time()),
                                "verdict": None, "context": []})
        store.append_candidate({"message_id": "m2", "text": "뭐고", "ts": int(time.time()),
                                "verdict": None, "context": []})

    def test_approve_sends_and_records(self):
        sent = []
        ok, res = store.set_verdict("m1", "approved", sender=lambda t: sent.append(t) or True)
        self.assertTrue(ok)
        self.assertEqual(res, "approved")
        self.assertEqual(sent, ["ㅋㅋㅋ"])
        row = [r for r in store.candidates() if r["message_id"] == "m1"][0]
        self.assertTrue(row["live_sent"])
        self.assertEqual(len(store.trainings()), 1)

    def test_repeat_approve_does_not_duplicate_training(self):
        store.set_verdict("m1", "approved", sender=lambda t: True)
        store.set_verdict("m1", "approved", sender=lambda t: True)
        self.assertEqual(len(store.trainings()), 1)

    def test_cannot_flip_decision(self):
        store.set_verdict("m1", "rejected")
        ok, res = store.set_verdict("m1", "approved", sender=lambda t: True)
        self.assertFalse(ok)
        self.assertIn("already decided", res)
        self.assertEqual(len(store.trainings()), 1)

    def test_unknown_candidate(self):
        ok, res = store.set_verdict("nope", "approved")
        self.assertFalse(ok)

    def test_send_failure_is_recorded(self):
        ok, _ = store.set_verdict("m2", "approved", sender=lambda t: False)
        self.assertTrue(ok)
        row = [r for r in store.candidates() if r["message_id"] == "m2"][0]
        self.assertFalse(row["live_sent"])

    def test_note_without_verdict(self):
        ok, _ = store.add_note("m2", "이건 좀 짧게")
        self.assertTrue(ok)
        row = [r for r in store.candidates() if r["message_id"] == "m2"][0]
        self.assertIsNone(row["verdict"])
        self.assertEqual(row["note"], "이건 좀 짧게")


if __name__ == "__main__":
    unittest.main()
