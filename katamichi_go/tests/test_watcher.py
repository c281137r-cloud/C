import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import watcher  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixture.html").read_text(encoding="utf-8")


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.items = watcher.parse_listings(FIXTURE)

    def test_dedupes_start_and_return_tabs(self):
        self.assertEqual(len(self.items), 2)

    def test_fields(self):
        closed, hiace = self.items
        self.assertTrue(closed.closed)
        self.assertEqual(closed.start_shop, "トヨタレンタリース岩手 盛岡駅南口店（岩手県 盛岡市）")
        self.assertEqual(closed.return_shop, "トヨタレンタリース青森 返却可能店舗（下記参照）")
        self.assertEqual(closed.period, "2026年10月29日 ～ 11月5日")
        self.assertEqual(closed.car, "プロボックス 八戸400わ2917")
        self.assertEqual(closed.conditions, "禁煙 AT 4WD")
        self.assertEqual(closed.tel, "019-606-3076")
        self.assertEqual(closed.start_area_name, "東北")

        self.assertFalse(hiace.closed)
        self.assertEqual(hiace.car, "ハイエースバン 品川400わ1234")
        self.assertEqual(hiace.conditions, "禁煙 AT")
        self.assertEqual(hiace.reserve_shop, "新宿店")
        self.assertEqual((hiace.start_area_name, hiace.return_area_name), ("関東", "近畿"))


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.items = watcher.parse_listings(FIXTURE)

    def test_first_run_reports_open_items_only(self):
        new, state = watcher.diff_listings(self.items, None)
        self.assertEqual([x.car for x in new], ["ハイエースバン 品川400わ1234"])
        self.assertEqual(len(state["listings"]), 2)

    def test_seen_items_are_not_reported_again(self):
        _, state = watcher.diff_listings(self.items, None)
        new, _ = watcher.diff_listings(self.items, state)
        self.assertEqual(new, [])

    def test_reopened_item_is_reported(self):
        _, state = watcher.diff_listings(self.items, None)
        self.items[0].closed = False
        new, _ = watcher.diff_listings(self.items, state)
        self.assertEqual([x.car for x in new], ["プロボックス 八戸400わ2917"])

    def test_disappeared_items_are_pruned(self):
        _, state = watcher.diff_listings(self.items, None)
        _, state = watcher.diff_listings(self.items[1:], state)
        self.assertEqual(len(state["listings"]), 1)


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.hiace = watcher.parse_listings(FIXTURE)[1]

    def _filters(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return watcher.Filters.from_env()

    def test_no_filter_matches(self):
        self.assertTrue(self._filters().match(self.hiace))

    def test_area_filter(self):
        self.assertTrue(self._filters(FILTER_START_AREAS="関東,九州").match(self.hiace))
        self.assertFalse(self._filters(FILTER_START_AREAS="東北").match(self.hiace))
        self.assertFalse(self._filters(FILTER_RETURN_AREAS="関東").match(self.hiace))

    def test_keyword_filters(self):
        self.assertTrue(self._filters(FILTER_KEYWORDS="ハイエース、アルファード").match(self.hiace))
        self.assertFalse(self._filters(FILTER_KEYWORDS="アルファード").match(self.hiace))
        self.assertFalse(self._filters(FILTER_EXCLUDE_KEYWORDS="バン").match(self.hiace))


class MessageTest(unittest.TestCase):
    def test_messages_respect_limit(self):
        items = watcher.parse_listings(FIXTURE) * 30
        msgs = watcher.build_messages(items, 1900)
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(m) <= 1900 for m in msgs))
        self.assertTrue(msgs[0].startswith("【片道GO!】新着 60 件"))


class MainTest(unittest.TestCase):
    def test_first_run_is_silent_then_notifies_new(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(watcher, "send_notifications", return_value=["x"]) as send:
            state = Path(d) / "s.json"
            fixture = Path(__file__).parent / "fixture.html"
            self.assertEqual(watcher.main(["--html", str(fixture), "--state", str(state)]), 0)
            send.assert_not_called()
            self.assertTrue(state.exists())

            # 受付終了だった車両が受付中に戻ったケース
            reopened = Path(d) / "page.html"
            reopened.write_text(FIXTURE.replace("show-entry-end", ""), encoding="utf-8")
            self.assertEqual(watcher.main(["--html", str(reopened), "--state", str(state)]), 0)
            send.assert_called_once()
            self.assertEqual([x.car for x in send.call_args[0][0]], ["プロボックス 八戸400わ2917"])

    def test_empty_page_fails_without_touching_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            page = Path(d) / "empty.html"
            page.write_text("<html></html>", encoding="utf-8")
            state = Path(d) / "s.json"
            self.assertEqual(watcher.main(["--html", str(page), "--state", str(state)]), 2)
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
