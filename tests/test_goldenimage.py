"""Golden-image metadata: record/get round-trip, staleness, status output."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker import commands, goldenimage  # noqa: E402

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


class GoldenImageModuleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "golden-image.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_empty_when_missing(self):
        self.assertEqual(goldenimage.load_golden_images(self.path), {})
        self.assertEqual(goldenimage.get_golden_image("digitalocean", path=self.path), {})

    def test_record_and_get_round_trip(self):
        goldenimage.record_golden_image(
            "do", image_id="999", image_name="clanker-toolbox-20260607",
            built_at="2026-06-05T00:00:00Z",
            agent_versions={"codex": "0.42.1", "claude-code": "1.2.3"}, path=self.path)
        entry = goldenimage.get_golden_image("digitalocean", path=self.path)  # alias normalizes
        self.assertEqual(entry["image_id"], "999")
        self.assertEqual(entry["image_name"], "clanker-toolbox-20260607")
        self.assertEqual(entry["agent_versions"]["codex"], "0.42.1")

    def test_record_two_providers_independent(self):
        goldenimage.record_golden_image("digitalocean", image_id="1", path=self.path)
        goldenimage.record_golden_image("gcp", image_id="2", path=self.path)
        data = goldenimage.load_golden_images(self.path)
        self.assertEqual(data["digitalocean"]["image_id"], "1")
        self.assertEqual(data["gcp"]["image_id"], "2")

    def test_age_and_staleness(self):
        self.assertAlmostEqual(goldenimage.age_days("2026-06-05T12:00:00Z", now=NOW), 2.0, places=3)
        self.assertFalse(goldenimage.is_stale("2026-06-05T12:00:00Z", now=NOW))
        self.assertTrue(goldenimage.is_stale("2026-04-01T12:00:00Z", now=NOW))
        self.assertIsNone(goldenimage.age_days(""))

    def test_human_age(self):
        self.assertEqual(goldenimage.human_age("2026-06-05T12:00:00Z", now=NOW), "2 days ago")
        self.assertEqual(goldenimage.human_age("2026-06-07T10:00:00Z", now=NOW), "2 hours ago")
        self.assertEqual(goldenimage.human_age("", now=NOW), "unknown")


class GoldenImageCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._orig = goldenimage.GOLDEN_IMAGE_PATH
        goldenimage.GOLDEN_IMAGE_PATH = Path(self._tmp.name) / "golden-image.json"

    def tearDown(self):
        goldenimage.GOLDEN_IMAGE_PATH = self._orig
        self._tmp.cleanup()

    def test_record_then_status(self):
        rc = commands.cmd_image_record("digitalocean", image_id="42", image_name="img-x",
                                       built_at="2026-06-06T12:00:00Z", codex_version="0.42.1")
        self.assertEqual(rc, 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.cmd_image_status(now=NOW)
        out = buf.getvalue()
        self.assertIn("img-x", out)
        self.assertIn("codex 0.42.1", out)
        self.assertIn("gcp", out)            # both providers listed
        self.assertIn("not built", out)      # gcp has no image yet

    def test_status_empty(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            commands.cmd_image_status(now=NOW)
        out = buf.getvalue()
        self.assertIn("clanker image bake --provider digitalocean", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
