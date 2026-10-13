import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core import config
from app.models import TrackSource
from app.routers import search as search_router
from app.schemas import SearchTrackOut, SearchTrackPartOut


class DurationTests(unittest.TestCase):
    def test_parses_numeric_and_clock_durations(self):
        self.assertEqual(search_router._parse_bili_duration_ms(12.5), 12_500)
        self.assertEqual(search_router._parse_bili_duration_ms("29:59.999"), 1_799_999)
        self.assertEqual(search_router._parse_bili_duration_ms("01:02:03"), 3_723_000)
        self.assertIsNone(search_router._parse_bili_duration_ms("unknown"))

    def test_duration_limit_is_strict_and_fail_closed(self):
        self.assertTrue(search_router._duration_allowed(1_799_999))
        self.assertFalse(search_router._duration_allowed(1_800_000))
        self.assertFalse(search_router._duration_allowed(0))
        self.assertFalse(search_router._duration_allowed(None))

    def test_multipart_filters_parts_and_preserves_real_part_number(self):
        track = search_router._bili_video_to_search_track(
            {
                "title": "collection",
                "duration": 9999,
                "pages": [
                    {"page": 2, "cid": 20, "part": "short", "duration": 100},
                    {"page": 7, "cid": 70, "part": "long", "duration": 1800},
                    {"page": 9, "cid": 90, "part": "unknown"},
                ],
            },
            bvid="BV1234567890",
        )
        self.assertIsNotNone(track)
        self.assertEqual([part.source_track_id for part in track.parts], ["BV1234567890#p=2&cid=20"])
        self.assertEqual(track.parts[0].part_number, 2)

    def test_collection_with_no_eligible_parts_is_removed(self):
        track = search_router._bili_video_to_search_track(
            {"pages": [{"duration": 1800}, {}]}, bvid="BV1234567890"
        )
        self.assertIsNone(track)


class RankingTests(unittest.TestCase):
    @staticmethod
    def track(source, sid, title, *, parts=None, artist=None):
        return SearchTrackOut(
            source=source,
            source_track_id=sid,
            title=title,
            artist=artist,
            duration_ms=100_000,
            parts=parts or [],
        )

    def test_exact_song_ranks_above_weak_collection_and_order_is_stable(self):
        weak = self.track(
            TrackSource.bilibili,
            "weak",
            "年度热门歌曲超长大合集以及许多完全无关的描述和关键词",
            parts=[SearchTrackPartOut(source=TrackSource.bilibili, source_track_id="weak#p=1", title="other", duration_ms=1)],
        )
        exact = self.track(TrackSource.netease, "exact", "周杰伦", artist="周杰伦")
        first = search_router._rank_results("周杰伦", [weak, exact])
        second = search_router._rank_results("周杰伦", [weak, exact])
        self.assertEqual([item.source_track_id for item in first], ["exact", "weak"])
        self.assertEqual([item.source_track_id for item in first], [item.source_track_id for item in second])

    def test_only_two_weak_collections_appear_before_available_alternatives(self):
        weak = [
            self.track(
                TrackSource.bilibili,
                f"b{index}",
                f"unrelated {index}",
                parts=[SearchTrackPartOut(source=TrackSource.bilibili, source_track_id=f"b{index}#p=1", title="other", duration_ms=1)],
            )
            for index in range(5)
        ]
        songs = [self.track(TrackSource.netease, f"n{index}", f"song {index}") for index in range(8)]
        ranked = search_router._rank_results("query", weak + songs)
        first_eight_weak = sum(bool(item.parts) for item in ranked[:8])
        self.assertLessEqual(first_eight_weak, 2)
        self.assertEqual(len(ranked), 13)

    def test_artist_match_does_not_exempt_weak_collection_titles(self):
        weak = [
            self.track(
                TrackSource.bilibili,
                f"artist-b{index}",
                f"unrelated collection {index}",
                artist="query",
                parts=[SearchTrackPartOut(source=TrackSource.bilibili, source_track_id=f"artist-b{index}#p=1", title="other", duration_ms=1)],
            )
            for index in range(5)
        ]
        songs = [self.track(TrackSource.netease, f"artist-n{index}", f"query song {index}") for index in range(8)]
        ranked = search_router._rank_results("query", weak + songs)
        self.assertLessEqual(sum(bool(item.parts) for item in ranked[:8]), 2)


class SearchSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_filters_before_source_limit(self):
        raw = [
            {"bvid": "BV0000000001", "title": "long", "duration": "30:00"},
            {"bvid": "BV0000000002", "title": "short", "duration": "03:00"},
        ]

        async def detail(bvid):
            duration = 1800 if bvid.endswith("1") else 180
            return {"title": bvid, "duration": duration}

        with patch.object(search_router.settings.search.bilibili, "result_limit", 1), patch.object(
            search_router, "_bili_search", AsyncMock(return_value=raw)
        ), patch.object(search_router, "_bili_video_by_bv", side_effect=detail):
            results = await search_router._run_bilibili("song")
        self.assertEqual([item.source_track_id for item in results], ["BV0000000002"])

    async def test_all_is_best_effort(self):
        result = SearchTrackOut(
            source=TrackSource.netease,
            source_track_id="1",
            title="song",
            duration_ms=1,
        )
        with patch.object(search_router, "_run_bilibili", AsyncMock(side_effect=RuntimeError("down"))), patch.object(
            search_router, "_run_netease", AsyncMock(return_value=[result])
        ), patch.object(search_router, "annotate_search_results", side_effect=lambda items: items):
            response = await search_router.search("song", source="all", user=object())
        self.assertEqual([item.source_track_id for item in response], ["1"])

    async def test_selected_source_failure_is_explicit(self):
        with patch.object(search_router, "_run_bilibili", AsyncMock(side_effect=RuntimeError("down"))):
            with self.assertRaisesRegex(Exception, "bilibili 搜索暂时不可用"):
                await search_router.search("song", source="bilibili", user=object())

    async def test_selected_source_does_not_request_the_other_upstream(self):
        netease = AsyncMock(return_value=[])
        bilibili = AsyncMock(side_effect=AssertionError("must not run"))
        with patch.object(search_router, "_run_bilibili", bilibili), patch.object(
            search_router, "_run_netease", netease
        ):
            self.assertEqual(await search_router.search("song", source="netease", user=object()), [])
        netease.assert_awaited_once_with("song")
        bilibili.assert_not_awaited()


class ConfigMergeTests(unittest.TestCase):
    def test_template_defaults_merge_without_overwriting_values_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "config.toml"
            template = root / "config_template.toml"
            current.write_text('[auth]\njwt_secret = "keep-me"\n\n[search]\naggregate_limit = 9\n', encoding="utf-8")
            current.chmod(0o640)
            template.write_text(
                '[auth]\n# secret\njwt_secret = "template-secret"\njwt_algorithm = "HS256"\n\n'
                '[search]\naggregate_limit = 40\n# duration\nmax_duration_s = 1800\n\n[new_section]\nenabled = true\n',
                encoding="utf-8",
            )
            real_replace = os.replace
            with patch.object(config, "CONFIG_PATH", current), patch.object(config, "CONFIG_TEMPLATE_PATH", template), patch.object(
                config.os, "replace", wraps=real_replace
            ) as replace:
                config.ensure_config_template_defaults()
                first = current.read_text(encoding="utf-8")
                config.ensure_config_template_defaults()
                second = current.read_text(encoding="utf-8")

            self.assertEqual(first, second)
            self.assertIn('jwt_secret = "keep-me"', first)
            self.assertNotIn("template-secret", first)
            self.assertIn('jwt_algorithm = "HS256"', first)
            self.assertIn("aggregate_limit = 9", first)
            self.assertIn("max_duration_s = 1800", first)
            self.assertIn("[new_section]\nenabled = true", first)
            self.assertEqual(os.stat(current).st_mode & 0o777, 0o640)
            replace.assert_called_once()

    def test_inline_table_conflict_leaves_existing_config_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "config.toml"
            template = root / "config_template.toml"
            original = (
                '[auth]\njwt_secret = "keep-this-secret"\n\n'
                '[search]\naggregate_limit = 9\nbilibili = { enabled = false }\n'
            )
            current.write_text(original, encoding="utf-8")
            template.write_text(
                '[auth]\njwt_secret = "template-secret"\n\n'
                '[search]\naggregate_limit = 40\nmax_duration_s = 1800\n\n'
                '[search.bilibili]\nenabled = true\nresult_limit = 10\n',
                encoding="utf-8",
            )
            with patch.object(config, "CONFIG_PATH", current), patch.object(config, "CONFIG_TEMPLATE_PATH", template), patch.object(
                config.os, "replace"
            ) as replace:
                config.ensure_config_template_defaults()

            self.assertEqual(current.read_text(encoding="utf-8"), original)
            self.assertIn('jwt_secret = "keep-this-secret"', current.read_text(encoding="utf-8"))
            replace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
