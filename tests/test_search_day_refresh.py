import unittest
from datetime import datetime
from unittest.mock import patch

import main


class SearchDayRefreshTests(unittest.TestCase):
    def test_successful_day_replaces_stale_cast_and_failed_day_is_kept(self):
        shows = [
            {
                "time": "2026-09-23 19:30",
                "city": "上海",
                "musical": "测试剧",
                "theatre": "测试剧场",
                "sourceType": "api-backfill",
                "sourceUrl": "https://example.invalid/old",
                "cast": [{"role": "A", "artist": "旧演员"}],
            },
            {
                "time": "2026-09-24 19:30",
                "city": "北京",
                "musical": "失败日保留剧",
                "theatre": "测试剧场",
                "sourceType": "api-backfill",
                "sourceUrl": "https://example.invalid/old",
                "cast": [{"role": "A", "artist": "保留演员"}],
            },
        ]

        def fake_fetch(date_key, timeout_seconds=20.0, retries=3):
            if date_key == "2026-09-23":
                return [
                    {
                        "time": "2026-09-23 19:30",
                        "city": "上海",
                        "musical": "测试剧",
                        "theatre": "测试剧场",
                        "sourceType": "search-day-live",
                        "sourceUrl": "https://example.invalid/live",
                        "cast": [{"role": "A", "artist": "新演员"}],
                    }
                ]
            raise TimeoutError("upstream timeout")

        with patch.object(main, "SEARCH_DAY_REFRESH_PAST_DAYS", 0), \
             patch.object(main, "SEARCH_DAY_REFRESH_FUTURE_DAYS", 1), \
             patch.object(main, "fetch_search_day_shows", side_effect=fake_fetch):
            stats = main.refresh_search_day_window(shows, datetime(2026, 9, 23, 12, 0))

        by_time = {show["time"]: show for show in shows}
        self.assertEqual(by_time["2026-09-23 19:30"]["cast"][0]["artist"], "新演员")
        self.assertEqual(by_time["2026-09-24 19:30"]["cast"][0]["artist"], "保留演员")
        self.assertEqual(stats["fetchedDays"], 1)
        self.assertEqual(len(stats["failedDays"]), 1)

    def test_pinned_manual_show_wins_even_when_theatre_label_differs(self):
        manual_show = {
            "time": "2026-09-23 19:30",
            "city": "上海",
            "musical": "测试剧",
            "theatre": "测试剧院中剧场",
            "sourceType": "manual-verified-backfill",
            "sourceUrl": "https://y.saoju.net/yyj/api/show/123/cast/",
            "cast": [{"role": "A", "artist": "人工确认演员"}],
        }
        shows = [manual_show]

        live_show = {
            "time": "2026-09-23 19:30",
            "city": "上海",
            "musical": "测试剧",
            "theatre": "测试剧院",
            "sourceType": "search-day-live",
            "sourceUrl": "https://example.invalid/live",
            "cast": [{"role": "A", "artist": "接口演员"}],
        }

        with patch.object(main, "SEARCH_DAY_REFRESH_PAST_DAYS", 0), \
             patch.object(main, "SEARCH_DAY_REFRESH_FUTURE_DAYS", 0), \
             patch.object(main, "fetch_search_day_shows", return_value=[live_show]):
            main.refresh_search_day_window(shows, datetime(2026, 9, 23, 12, 0))

        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0]["cast"][0]["artist"], "人工确认演员")

    def test_empty_success_response_preserves_existing_day(self):
        shows = [
            {
                "time": "2026-09-23 19:30",
                "city": "上海",
                "musical": "测试剧",
                "theatre": "测试剧场",
                "sourceType": "search-day-live",
                "sourceUrl": "https://example.invalid/old",
                "cast": [{"role": "A", "artist": "保留演员"}],
            }
        ]

        with patch.object(main, "SEARCH_DAY_REFRESH_PAST_DAYS", 0), \
             patch.object(main, "SEARCH_DAY_REFRESH_FUTURE_DAYS", 0), \
             patch.object(main, "fetch_search_day_shows", return_value=[]):
            stats = main.refresh_search_day_window(shows, datetime(2026, 9, 23, 12, 0))

        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0]["cast"][0]["artist"], "保留演员")
        self.assertEqual(stats["fetchedDays"], 0)
        self.assertEqual(len(stats["failedDays"]), 1)
        self.assertIn("suspicious empty", stats["failedDays"][0][1])

    def test_manual_verified_source_has_highest_duplicate_preference(self):
        manual = {
            "sourceType": "manual-verified-backfill",
            "sourceUrl": "https://y.saoju.net/yyj/api/show/123/cast/",
            "cast": [{"role": "A", "artist": "演员"}],
        }
        live = {
            "sourceType": "search-day-live",
            "sourceUrl": "https://y.saoju.net/yyj/api/search_day/",
            "cast": [{"role": "A", "artist": "演员"}],
        }
        self.assertGreater(main.duplicate_preference_score(manual), main.duplicate_preference_score(live))
        self.assertTrue(main.is_pinned_manual_show(manual))


if __name__ == "__main__":
    unittest.main()
