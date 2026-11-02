"""말투 시드 빌더 테스트."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["HOGI_BASE"] = tempfile.mkdtemp(prefix="hogi-seed-")

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build_style_seed as seed  # noqa: E402


class CollectTest(unittest.TestCase):
    def test_filters_other_users(self):
        rows = [("2026-01-01 00:00:00", "me", "나", "ㅋㅋ"),
                ("2026-01-01 00:01:00", "other", "남", "안녕하세요")]
        out = seed.collect(rows, user_id="me")
        self.assertEqual([r["text"] for r in out], ["ㅋㅋ"])

    def test_drops_noise(self):
        rows = [("t", "me", "나", "/명령"), ("t", "me", "나", "."), ("t", "me", "나", "ㅋ" * 10),
                ("t", "me", "나", "https://x.y"), ("t", "me", "나", "좋다")]
        out = seed.collect(rows, user_id="me")
        self.assertEqual([r["text"] for r in out], ["좋다"])

    def test_dedupes(self):
        rows = [("t", "me", "나", "좋다"), ("t", "me", "나", "좋다")]
        self.assertEqual(len(seed.collect(rows, user_id="me")), 1)

    def test_nick_filter_when_no_user_id(self):
        rows = [("t", "", "나", "좋다"), ("t", "", "남", "싫다")]
        out = seed.collect(rows, nick="나")
        self.assertEqual([r["text"] for r in out], ["좋다"])


if __name__ == "__main__":
    unittest.main()
