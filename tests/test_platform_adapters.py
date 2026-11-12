"""Synthetic, offline contracts for public YouTube/TikTok adapters."""

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platform_adapters as adapters


def response(status=200, text="", payload=None):
    value = Mock(status_code=status, text=text, headers={})
    value.json.return_value = payload if payload is not None else {}
    return value


class AdapterTests(unittest.TestCase):
    def test_youtube_channels_with_same_display_name_keep_distinct_ids(self):
        names = []
        for channel_id in ("UCaaaaaaaaaaaaaaaaaaaaaa", "UCbbbbbbbbbbbbbbbbbbbbbb"):
            data = {"channelMetadataRenderer": {"externalId": channel_id, "title": "Shared Display Name"}}
            profile = adapters._youtube_metadata(data, "https://www.youtube.com/channel/" + channel_id)
            names.append(profile["username"])
        self.assertNotEqual(names[0], names[1])

    def setUp(self):
        self.network_patch = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected live HTTP"))
        self.network = self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        adapters._YOUTUBE_METRICS_CACHE.clear()

    def fake_ytdlp(self, info=None, error=None, impersonation=False):
        instances = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.params = dict(options)
                instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def _impersonate_target_available(self, target):
                return impersonation

            def extract_info(self, url, **kwargs):
                self.call = (url, kwargs)
                if error:
                    raise error
                return info

            def urlopen(self, request):
                return "synthetic-response"

        return SimpleNamespace(YoutubeDL=FakeYoutubeDL), instances

    def tiktok_item(self, offset=0, **overrides):
        timestamp = int(datetime.now().timestamp()) - offset * 86400
        item = {
            "id": str((timestamp << 32) + offset), "timestamp": timestamp,
            "description": "Synthetic caption #example", "view_count": 0,
            "like_count": 0, "comment_count": None, "repost_count": 2,
        }
        item.update(overrides)
        return item

    def embed_response(self, items=None, user=None):
        state = {"source": {"data": {"/embed/@example": {
            "userInfo": user if user is not None else {"uniqueId": "example", "nickname": "Example", "followerCount": 0},
            "videoList": items if items is not None else [self.tiktok_item()],
        }}}}
        return response(text='<script id="__FRONTITY_CONNECT_STATE__">' + json.dumps(state) + '</script>')

    def test_rejects_malicious_or_media_identifiers_without_requests(self):
        cases = (
            (adapters.fetch_youtube, (
                "https://youtube.com.attacker.example/@example", "https://notyoutube.com/@example",
                "https://name:password@youtube.com/@example", "https://youtube.com:444/@example",
                "https://youtube.com/watch?v=VIDEO000001", "https://youtu.be/VIDEO000001",
                "https://youtube.com/shorts/SHORT000001", "@example?inject=1", "@example/../../watch",
                "https://youtube.com/@example?x=1", "https://youtube.com/@example#fragment", "file:///example",
            )),
            (adapters.fetch_tiktok, (
                "https://tiktok.com.attacker.example/@example", "https://nottiktok.com/@example",
                "https://name:password@tiktok.com/@example", "https://tiktok.com:444/@example",
                "https://www.tiktok.com/@example/video/1234567890123", "@example?inject=1",
                "@example/../../video", "https://tiktok.com/@example?x=1", "https://vm.tiktok.com/abc",
                "file:///example", "example.", "@@example",
            )),
        )
        for fetch, identifiers in cases:
            for identifier in identifiers:
                with self.subTest(fetch=fetch.__name__, identifier=identifier):
                    profile, posts, error, meta = fetch(identifier)
                    self.assertIsNone(profile)
                    self.assertIsNone(posts)
                    self.assertTrue(error)
                    self.assertEqual(meta["http_status"], 400)
                    self.assertEqual(meta["error_code"], "invalid_input")
        self.network.assert_not_called()

    def test_valid_channel_and_profile_identifiers_are_canonicalized(self):
        self.assertEqual(adapters._normalize_youtube_base("@example"), "https://www.youtube.com/@example")
        self.assertEqual(adapters._normalize_youtube_base("https://m.youtube.com/@example/shorts"), "https://www.youtube.com/@example")
        self.assertEqual(adapters._normalize_youtube_base("UC" + "a" * 22), "https://www.youtube.com/channel/UC" + "a" * 22)
        self.assertEqual(adapters._normalize_youtube_base("https://youtube.com/user/example"), "https://www.youtube.com/user/example")
        self.assertEqual(adapters._normalize_tiktok_username("https://www.tiktok.com/@example/"), "example")

    def test_numeric_metrics_keep_zero_and_unavailable_values_distinct(self):
        for value in (None, True, -1, float("inf"), "hidden", "-5"):
            self.assertIsNone(adapters._nullable_count(value))
        self.assertEqual(adapters._nullable_count(0), 0)
        self.assertEqual(adapters._nullable_count("0 views"), 0)
        self.assertEqual(adapters._nullable_count("1.2K views"), 1200)

    def test_tiktok_ytdlp_is_bounded_metadata_only_and_never_reads_credentials(self):
        consumed = []

        def entries():
            for index in range(100):
                consumed.append(index)
                yield self.tiktok_item(index)

        module, instances = self.fake_ytdlp({"entries": entries(), "follower_count": 0})
        with patch.object(adapters, "_load_ytdlp", return_value=module), \
                patch.object(adapters, "_fetch_tiktok_embed", side_effect=AssertionError("Unnecessary fallback")):
            profile, posts, error, meta = adapters.fetch_tiktok("@example", max_posts=3)
        self.assertIsNone(error)
        self.assertEqual(consumed, [0, 1, 2])
        self.assertEqual(len(posts), 3)
        self.assertEqual(posts[0]["views"], 0)
        self.assertIsNone(posts[0]["comments"])
        self.assertEqual(posts[0]["likes"], 0)
        self.assertIsNone(profile["post_count"])
        self.assertTrue(profile["content_count_is_sample"])
        self.assertEqual(profile["followers"], 0)
        self.assertEqual(profile["sampled_content_count"], 3)
        self.assertTrue(meta["source_limited"])
        self.assertTrue(meta["truncated"])
        self.assertTrue(meta["metrics_limited"])
        self.assertFalse(meta["content_count_available"])
        options = instances[0].params
        self.assertTrue(options["skip_download"])
        self.assertIsNone(options["cookiefile"])
        self.assertIsNone(options["cookiesfrombrowser"])
        self.assertFalse(options["usenetrc"])
        self.assertFalse(options["cachedir"])
        self.assertEqual(options["retries"], 0)
        self.assertEqual(instances[0].call[1], {"download": False, "process": False, "ie_key": "TikTokUser"})
        self.assertNotIn("formats", posts[0])
        self.network.assert_not_called()

    def test_tiktok_upper_bound_and_http_budget_prevent_unbounded_scanning(self):
        module, instances = self.fake_ytdlp({"entries": [self.tiktok_item()]})
        with patch.object(adapters, "_load_ytdlp", return_value=module):
            adapters.fetch_tiktok("example", max_posts=100000)
        instance = instances[0]
        self.assertEqual(instance.params["playlistend"], adapters.MAX_TIKTOK_POSTS)
        blocked = False
        for _ in range(41):
            try:
                instance.urlopen("https://www.tiktok.com/synthetic")
            except adapters._RequestBudgetExceeded:
                blocked = True
                break
        self.assertTrue(blocked, "Even empty-page scans must stop within 40 HTTP requests")

    def test_tiktok_uses_chrome_only_if_installed_handler_supports_it(self):
        module, instances = self.fake_ytdlp({"entries": [self.tiktok_item()]}, impersonation=True)
        real_import = importlib.import_module
        target = object()

        def import_optional(name):
            if name == "yt_dlp.networking.impersonate":
                return SimpleNamespace(ImpersonateTarget=lambda **kwargs: target)
            return real_import(name)

        with patch.object(adapters, "_load_ytdlp", return_value=module), \
                patch.object(adapters.importlib, "import_module", side_effect=import_optional):
            adapters.fetch_tiktok("example")
        self.assertIs(instances[0].params["impersonate"], target)

    def test_tiktok_filters_old_posts_and_deduplicates_without_synthesizing_metrics(self):
        item = self.tiktok_item(1, like_count=None, view_count=None)
        module, _ = self.fake_ytdlp({"entries": [self.tiktok_item(100), item, dict(item)]})
        with patch.object(adapters, "_load_ytdlp", return_value=module):
            profile, posts, error, meta = adapters.fetch_tiktok("example", cutoff_date=datetime.now() - timedelta(days=10))
        self.assertIsNone(error)
        self.assertEqual(len(posts), 1)
        self.assertIsNone(posts[0]["likes"])
        self.assertIsNone(posts[0]["views"])
        self.assertEqual(meta["sampled_count"], 1)
        self.assertEqual(meta["candidates_examined"], 3)

    def test_missing_ytdlp_uses_honest_embed_sample_and_preserves_zero(self):
        session = Mock()
        session.get.return_value = self.embed_response()
        with patch.object(adapters, "_load_ytdlp", side_effect=ImportError), \
                patch.object(adapters, "_public_session", return_value=session), \
                patch.object(adapters, "_tiktok_detail", return_value={}):
            profile, posts, error, meta = adapters.fetch_tiktok("example", max_posts=50)
        self.assertIsNone(error)
        self.assertEqual(meta["source"], "tiktok_public_embed")
        self.assertEqual(meta["fallback_reason"], "ytdlp_unavailable")
        self.assertEqual(profile["followers"], 0)
        self.assertIsNone(profile["post_count"])
        self.assertIsNone(profile["following"])
        self.assertEqual(profile["sampled_content_count"], 1)
        self.assertEqual(posts[0]["views"], 0)
        self.assertIsNone(posts[0]["comments"])
        self.assertTrue(meta["source_limited"])
        self.assertIn("tam profil geçmişi değildir", meta["note"])
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_failed_ytdlp_falls_back_without_tracebacks_or_exception_secrets(self):
        module, _ = self.fake_ytdlp(error=RuntimeError("secret-cookie-and-key"))
        session = Mock()
        session.get.return_value = self.embed_response()
        output = io.StringIO()
        with patch.object(adapters, "_load_ytdlp", return_value=module), \
                patch.object(adapters, "_public_session", return_value=session), \
                patch.object(adapters, "_tiktok_detail", return_value={}), \
                redirect_stdout(output), redirect_stderr(output):
            result = adapters.fetch_tiktok("example")
        self.assertIsNone(result[2])
        self.assertNotIn("secret-cookie-and-key", json.dumps(result))
        self.assertEqual(output.getvalue(), "")

    def test_empty_ytdlp_entries_do_not_raise_unbound_error(self):
        module, _ = self.fake_ytdlp({"entries": []})
        session = Mock()
        session.get.return_value = self.embed_response(items=[])
        with patch.object(adapters, "_load_ytdlp", return_value=module), \
                patch.object(adapters, "_public_session", return_value=session):
            profile, posts, error, meta = adapters.fetch_tiktok("example")
        self.assertIsNone(error)
        self.assertEqual(posts, [])
        self.assertEqual(profile["sampled_content_count"], 0)
        self.assertEqual(meta["sampled_count"], 0)

    def test_tiktok_embed_details_and_result_count_are_bounded(self):
        items = [self.tiktok_item(index) for index in range(25)]
        session = Mock()
        session.get.return_value = self.embed_response(items)
        with patch.object(adapters, "_load_ytdlp", side_effect=ImportError), \
                patch.object(adapters, "_public_session", return_value=session), \
                patch.object(adapters, "_tiktok_detail", return_value={}) as detail:
            profile, posts, error, meta = adapters.fetch_tiktok("example", max_posts=20)
        self.assertIsNone(error)
        self.assertEqual(len(posts), 20)
        self.assertEqual(detail.call_count, adapters.MAX_TIKTOK_EMBED_DETAILS)
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["embed_available_items"], 25)

    def test_tiktok_errors_are_stable_and_rate_limits_do_not_trigger_more_requests(self):
        error = requests.HTTPError("private details")
        error.status = 429
        module, _ = self.fake_ytdlp(error=error)
        with patch.object(adapters, "_load_ytdlp", return_value=module), \
                patch.object(adapters, "_fetch_tiktok_embed") as fallback:
            result = adapters.fetch_tiktok("example")
        self.assertEqual(result[3]["http_status"], 429)
        self.assertEqual(result[3]["error_code"], "rate_limited")
        self.assertEqual(result[3]["retry_after"], 60)
        fallback.assert_not_called()
        session = Mock()
        session.get.side_effect = requests.ConnectionError("private request details")
        with patch.object(adapters, "_load_ytdlp", side_effect=ImportError), \
                patch.object(adapters, "_public_session", return_value=session):
            result = adapters.fetch_tiktok("example")
        self.assertEqual(result[3]["http_status"], 502)
        self.assertNotIn("private request details", json.dumps(result))

    def test_tiktok_embed_cannot_return_a_different_profile(self):
        session = Mock()
        session.get.return_value = self.embed_response(user={"uniqueId": "someone_else"})
        with patch.object(adapters, "_load_ytdlp", side_effect=ImportError), \
                patch.object(adapters, "_public_session", return_value=session):
            result = adapters.fetch_tiktok("example")
        self.assertEqual(result[3]["http_status"], 404)

    def youtube_post(self, video_id="VIDEO000001", timestamp=None):
        return {
            "id": video_id, "timestamp": timestamp or int(datetime.now().timestamp()),
            "likes": None, "comments": None, "views": None,
        }

    def test_keyless_youtube_merges_videos_and_shorts_with_bounded_results(self):
        profile = {"username": "@example", "post_count": None}
        video = self.youtube_post()
        short = self.youtube_post("SHORT000001")
        tab = Mock(side_effect=[(profile, [video], {"pages": 1}), (profile, [short], {"pages": 1})])
        with patch.object(adapters, "_youtube_tab", tab), \
                patch.object(adapters, "_enrich_youtube_metrics", return_value={}):
            result = adapters.fetch_youtube("@example", max_posts=10000)
        self.assertIsNone(result[2])
        self.assertEqual({post["id"] for post in result[1]}, {"VIDEO000001", "SHORT000001"})
        self.assertEqual(result[3]["sampled_count"], 2)
        self.assertIsNone(result[0]["post_count"])
        self.assertTrue(all(call.args[2] == adapters.MAX_YOUTUBE_POSTS for call in tab.call_args_list))
        self.network.assert_not_called()

    def test_youtube_shorts_only_channel_is_supported(self):
        profile = {"username": "@example", "post_count": None}
        with patch.object(adapters, "_youtube_tab", side_effect=[
            (None, [], {"http_status": 404}), (profile, [self.youtube_post()], {"pages": 1}),
        ]), patch.object(adapters, "_enrich_youtube_metrics", return_value={}):
            result = adapters.fetch_youtube("example")
        self.assertIsNone(result[2])
        self.assertTrue(result[3]["source_limited"])

    def test_youtube_missing_short_dates_do_not_exclude_the_whole_shorts_tab(self):
        profile = {"username": "@example", "post_count": None}
        videos = [self.youtube_post(), self.youtube_post("VIDEO000002")]
        short = self.youtube_post("SHORT000001")
        short["timestamp"] = None
        with patch.object(adapters, "_youtube_tab", side_effect=[
            (profile, videos, {"pages": 1}), (profile, [short], {"pages": 1}),
        ]), patch.object(adapters, "_enrich_youtube_metrics", return_value={}) as enrich:
            result = adapters.fetch_youtube("example", max_posts=2)
        self.assertEqual({post["id"] for post in result[1]}, {"VIDEO000001", "SHORT000001"})
        self.assertEqual(len(enrich.call_args.args[0]), 2)
        self.assertTrue(result[3]["truncated"])

    def test_youtube_network_failure_and_rate_limit_have_stable_status(self):
        for upstream, expected in ((requests.ConnectionError("secret"), 502),):
            with patch.object(adapters, "_youtube_tab", side_effect=upstream):
                result = adapters.fetch_youtube("example")
            self.assertEqual(result[3]["http_status"], expected)
            self.assertNotIn("secret", json.dumps(result))
        with patch.object(adapters, "_youtube_tab", return_value=(None, [], {"http_status": 429})) as tab:
            result = adapters.fetch_youtube("example")
        self.assertEqual(result[3]["http_status"], 429)
        self.assertEqual(tab.call_count, 1)

    def test_youtube_watch_metrics_preserve_zero_and_missing_comments(self):
        player = {"videoDetails": {"viewCount": "0", "lengthSeconds": "0"},
                  "microformat": {"playerMicroformatRenderer": {"likeCount": "0", "publishDate": "2026-01-01"}}}
        session = Mock()
        session.get.return_value = response(text="var ytInitialPlayerResponse = " + json.dumps(player) + ";")
        with patch.object(adapters, "_youtube_thread_session", return_value=session):
            metrics = adapters._youtube_watch_metrics("VIDEO000001")
        self.assertEqual(metrics["views"], 0)
        self.assertEqual(metrics["likes"], 0)
        self.assertIsNone(metrics["comments"])
        self.assertEqual(metrics["duration"], 0)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_youtube_api_key_is_header_only_and_never_appears_in_error_metadata(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("secret-test-api-key")
        metrics, error = adapters._youtube_official_metrics([self.youtube_post()], "secret-test-api-key", session)
        self.assertEqual(metrics, {})
        self.assertEqual(error, "request_failed")
        self.assertEqual(session.get.call_args.kwargs["headers"]["X-Goog-Api-Key"], "secret-test-api-key")
        self.assertNotIn("secret-test-api-key", json.dumps(session.get.call_args.kwargs["params"]))

    def test_youtube_listing_supports_short_and_legacy_renderers_without_fake_dates(self):
        data = {"items": [
            {"videoRenderer": {"videoId": "VIDEO000001", "title": {"simpleText": "Example"}, "viewCountText": {"simpleText": "0 views"}}},
            {"shortsLockupViewModel": {
                "onTap": {"innertubeCommand": {"reelWatchEndpoint": {"videoId": "SHORT000001"}}},
                "overlayMetadata": {"primaryText": {"content": "Example Short"}, "secondaryText": {"content": "2K views"}},
            }},
        ]}
        posts = list(adapters._youtube_page_posts(data, "Shorts"))
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["views"], 0)
        self.assertEqual(posts[1]["views"], 2000)
        self.assertTrue(all(post["timestamp"] is None for post in posts))
        self.assertTrue(all(post["shares"] is None for post in posts))


if __name__ == "__main__":
    unittest.main()
