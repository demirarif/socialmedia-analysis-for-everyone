"""Offline public-release contracts: run ``python -m unittest discover -s tests``.

All fixtures are synthetic. Runtime files live in temporary directories, and any
unmocked HTTP request fails the test instead of contacting Instagram or an AI API.
"""

import atexit
import csv
import io
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import Mock, patch
import uuid


_REPOSITORY = Path(__file__).resolve().parents[1]
_TEST_RUNTIME_ROOT = _REPOSITORY / ".runtime" / "test-runs"
_TEST_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)


class _RuntimeDirectory:
    """Use inherited directory permissions, including in restricted Windows runners."""

    def __init__(self, prefix):
        self.path = _TEST_RUNTIME_ROOT / (prefix + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self):
        if self.path.exists():
            if self.path.resolve().parent != _TEST_RUNTIME_ROOT.resolve():
                raise RuntimeError("Refusing cleanup outside the test runtime root")
            shutil.rmtree(self.path)


_IMPORT_RUNTIME = _RuntimeDirectory("import-")
atexit.register(_IMPORT_RUNTIME.cleanup)
os.environ["IG_ANALYSIS_DATA_DIR"] = _IMPORT_RUNTIME.name
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(_REPOSITORY))

import app as application  # noqa: E402


def _dispose_test_database():
    # SQLite file handles must close before TemporaryDirectory cleanup on Windows.
    with application.app.app_context():
        application.db.session.remove()
        application.db.engine.dispose()


atexit.register(_dispose_test_database)


class PublicReleaseTests(unittest.TestCase):
    def test_legacy_comparison_excludes_missing_view_counters(self):
        profile = {"username": "example", "followers": 1000}
        posts = [{"is_video": True, "views": None, "likes": None, "comments": None},
                 {"is_video": True, "views": 100, "likes": 10, "comments": 1}]
        with patch.object(application, "fetch_with_cookies", return_value=(profile, posts, None)), \
                patch("time.sleep"):
            response = self.client.post("/api/compare", json={"usernames": ["example", "example2"]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(account["avg_views"] == 100 for account in response.get_json()["accounts"]))

    def test_public_platforms_do_not_require_or_use_instagram_session(self):
        for platform, adapter_name in (("youtube", "fetch_youtube"), ("tiktok", "fetch_tiktok")):
            with self.subTest(platform=platform):
                profile = {"username": "same_name", "followers": None, "post_count": None}
                posts = [{"shortcode": "SYNTHETIC", "is_video": True, "views": 0, "likes": None, "comments": 0}]
                with patch.object(application, adapter_name, return_value=(profile, posts, None, {"source": "synthetic_fixture"})), \
                        patch.object(application, "_get_session", side_effect=AssertionError("Instagram session read")):
                    response = self.client.post("/api/fetch", json={"input": "@same_name", "platform": platform})
                self.assertEqual(response.status_code, 200)
                data = response.get_json()
                self.assertEqual(data["platform"], platform)
                self.assertIsNone(data["stats"]["avg_likes"])
                self.assertEqual(data["stats"]["avg_views"], 0)
                self.assertIsNone(data["stats"]["engagement_rate"])
                self.assertEqual(data["posts"][0]["views"], 0)
        with application.app.app_context():
            self.assertEqual(application.Brand.query.count(), 0)

    def test_youtube_uses_its_own_date_default_and_post_cap(self):
        with patch.object(application, "fetch_youtube", return_value=({"username": "example"}, [], None, {})) as fetch:
            response = self.client.post("/api/fetch", json={"input": "@example", "platform": "youtube", "max_posts": 900})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["meta"]["days"], 365)
        self.assertEqual(fetch.call_args.kwargs["max_posts"], 180)

    def test_public_adapter_errors_keep_status_and_never_suggest_instagram_login(self):
        for status, code in ((400, "invalid_input"), (429, "rate_limited")):
            with self.subTest(status=status), patch.object(application, "fetch_tiktok", return_value=(
                None, None, "Synthetic error", {"http_status": status, "error_code": code, "retry_after": 60}
            )):
                response = self.client.post("/api/fetch", json={"input": "@example", "platform": "tiktok"})
                self.assertEqual(response.status_code, status)
                data = response.get_json()
                self.assertEqual(data["error_code"], code)
                self.assertFalse(data["suggest_login"])
                self.assertNotIn("posts", data)

    def test_unexpected_adapter_exceptions_are_redacted(self):
        with patch.object(application, "fetch_youtube", side_effect=RuntimeError("synthetic-secret-do-not-echo")):
            response = self.client.post("/api/fetch", json={"input": "@example", "platform": "youtube"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("synthetic-secret-do-not-echo", response.get_data(as_text=True))

    def test_all_platform_demos_have_correct_links_and_explicit_synthetic_marker(self):
        for platform in ("instagram", "tiktok", "youtube"):
            with self.subTest(platform=platform), patch.object(application, "fetch_youtube", side_effect=AssertionError("Live fetch")), \
                    patch.object(application, "fetch_tiktok", side_effect=AssertionError("Live fetch")):
                response = self.client.post("/api/fetch", json={"input": "demo", "platform": platform, "demo": True, "max_posts": 3})
                self.assertEqual(response.status_code, 200)
                data = response.get_json()
                self.assertTrue(data["meta"]["synthetic"])
                self.assertEqual(len(data["posts"]), 3)
                self.assertTrue(all(f"{platform}.com/" in post["url"] for post in data["posts"]))
                self.assertTrue(all(post["platform"] == platform for post in data["posts"]))

    def test_tiktok_photo_views_and_youtube_short_types_are_preserved(self):
        photo = application._normalize_post_contract("tiktok", {"typename": "TikTokPhoto", "is_video": False, "views": 0})
        self.assertEqual(photo["content_type"], "Fotoğraf / Slayt")
        self.assertEqual(photo["views"], 0)
        short = application._normalize_post_contract("youtube", {"typename": "YouTubeShort", "is_video": True, "views": None})
        self.assertEqual(short["content_type"], "Shorts")
        self.assertIsNone(short["views"])

    def test_all_platform_excel_exports_label_platform_and_keep_missing_counts_unavailable(self):
        from openpyxl import load_workbook
        for platform, label in (("instagram", "Instagram"), ("youtube", "YouTube"), ("tiktok", "TikTok")):
            with self.subTest(platform=platform):
                data = self._export_fixture()
                data["platform"] = platform
                data["profile"]["followers"] = None
                response = self.client.post("/api/export_excel", json=data)
                self.assertEqual(response.status_code, 200)
                book = load_workbook(io.BytesIO(response.data))
                self.assertEqual(book["Özet"]["B4"].value, label)
                self.assertEqual(book["Özet"]["B7"].value, "Mevcut değil")
                book.close()

    def test_instagram_only_features_reject_other_platforms(self):
        for endpoint in ("content_dna", "sponsorship", "brand_analysis", "compare"):
            with self.subTest(endpoint=endpoint):
                response = self.client.post("/api/" + endpoint, json={"input": "example", "platform": "tiktok"})
                self.assertEqual(response.status_code, 400)

    def test_sponsorship_failure_never_substitutes_synthetic_data(self):
        failure = application.InstagramFetchError("Limited", "rate_limited", 429, retryable=True)
        with patch.object(application, "fetch_with_cookies", return_value=(None, None, failure)), \
                patch.object(application, "get_demo_data", side_effect=AssertionError("No implicit demo")):
            response = application.app.test_client().post("/api/sponsorship", json={"input": "example"})
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("demo", response.get_json())

    def test_content_dna_preserves_zero_and_uses_only_observed_views_for_ranking(self):
        model = Mock()
        model.models.generate_content.return_value.text = "{}"
        posts = [
            {"shortcode": "DEMOZERO", "is_video": True, "views": 0, "likes": 20, "comments": 2},
            {"shortcode": "DEMOFIVE", "is_video": True, "views": 5, "likes": 10, "comments": 1},
            {"shortcode": "DEMOMISSING", "is_video": True, "likes": 1000, "comments": 50},
        ]
        with patch.object(application, "gemini_client", model), \
                patch.object(application, "fetch_with_cookies", return_value=({"username": "example"}, posts, None)), \
                patch.object(application, "_fetch_comments_v1", return_value=[]), \
                patch("requests.get", return_value=Mock(status_code=404)):
            response = self.client.post("/api/content_dna", json={"input": "example"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual([post["views"] for post in data["best_posts"]], [5, 0])
        self.assertIn("ölçülen", data["ranking_note"])

    def test_pdf_template_handles_missing_metrics_and_only_sanitized_html(self):
        from flask import render_template
        safe = application._safe_ai_html('<p>Summary</p><script>alert(1)</script><a href="javascript:alert(1)">x</a>')
        with application.app.app_context():
            html = render_template("report_template.html", profile={"username": "example", "followers": None},
                                   stats={"avg_likes": None, "avg_comments": None, "engagement_rate": None},
                                   ai_analysis=safe)
        self.assertIn("Mevcut değil", html)
        self.assertIn("<p>Summary</p>", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("javascript:", html)

    def setUp(self):
        self.runtime = _RuntimeDirectory("session-")
        self.addCleanup(self.runtime.cleanup)
        self.session_file = Path(self.runtime.name) / ".ig_session.json"
        self.session_patch = patch.object(application, "SESSION_FILE", str(self.session_file))
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.network_patch = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Tests must never make real HTTP requests"),
        )
        self.network = self.network_patch.start()
        self.addCleanup(self.network_patch.stop)
        self.client = application.app.test_client()

    def test_runtime_is_isolated_from_source_and_original_project(self):
        runtime = Path(_IMPORT_RUNTIME.name).resolve()
        self.assertTrue(Path(application.app.instance_path).resolve().is_relative_to(runtime))
        self.assertEqual(application.GEMINI_API_KEY, "")

    def test_home_and_config_work_without_instagram_session_or_ai_key(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        config = response.get_json()
        self.assertEqual(config["platforms"], ["instagram", "tiktok", "youtube"])
        self.assertEqual(config["quick_brands"], [])
        self.assertEqual(config["our_brand"], "")
        self.assertFalse(config["ai_enabled"])
        self.network.assert_not_called()

    def test_missing_session_returns_401_instead_of_demo_data(self):
        response = self.client.post("/api/fetch", json={"input": "example_creator"})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.get_json()["suggest_login"])
        self.assertNotIn("posts", response.get_json())
        self.network.assert_not_called()

    def test_unknown_platforms_are_rejected_before_network_access(self):
        for platform in ("unknown", "facebook"):
            with self.subTest(platform=platform):
                response = self.client.post(
                    "/api/fetch", json={"input": "example_creator", "platform": platform}
                )
                self.assertEqual(response.status_code, 400)
        self.network.assert_not_called()

    def test_demo_is_explicit_and_does_not_create_history_or_session(self):
        with patch.object(application, "fetch_with_cookies", side_effect=AssertionError("Live fetch")):
            response = self.client.post(
                "/api/fetch", json={"input": "example_creator", "demo": True}
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["demo"])
        self.assertEqual(data["meta"]["source"], "demo")
        self.assertGreater(len(data["posts"]), 0)
        self.assertFalse(self.session_file.exists())
        with application.app.app_context():
            self.assertEqual(application.Brand.query.count(), 0)
            self.assertEqual(application.AnalysisHistory.query.count(), 0)
        self.network.assert_not_called()

    def test_blank_password_credentials_are_rejected_without_network_or_storage(self):
        response = self.client.post(
            "/api/login_password", json={"username": "example", "password": ""}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.session_file.exists())
        self.network.assert_not_called()

    def test_password_login_saves_session_but_never_password(self):
        import requests

        session = Mock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.cookies.set("csrftoken", "synthetic-csrf", domain=".instagram.com")
        session.cookies.set("sessionid", "synthetic-password-session", domain=".instagram.com")
        session.post.return_value.json.return_value = {"authenticated": True}
        with patch("requests.Session", return_value=session):
            response = self.client.post("/api/login_password", json={
                "username": "secondary_example", "password": "synthetic-password-never-save",
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        saved = application._get_session()
        self.assertEqual(saved["username"], "secondary_example")
        self.assertEqual(saved["cookies"]["sessionid"], "synthetic-password-session")
        self.assertNotIn("synthetic-password-never-save", self.session_file.read_text(encoding="utf-8"))
        self.assertNotIn("synthetic-password-never-save", response.get_data(as_text=True))

    def test_incorrect_password_does_not_create_saved_session(self):
        import requests

        session = Mock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.cookies.set("csrftoken", "synthetic-csrf", domain=".instagram.com")
        session.post.return_value.json.return_value = {
            "authenticated": False, "status": "fail", "message": "invalid credentials",
        }
        with patch("requests.Session", return_value=session):
            response = self.client.post("/api/login_password", json={
                "username": "secondary_example", "password": "incorrect-synthetic-password",
            })
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.session_file.exists())
        self.assertNotIn("incorrect-synthetic-password", response.get_data(as_text=True))

    def test_login_only_saves_username_verified_by_instagram(self):
        validation = {"state": "valid", "username": "verified_example", "http_status": 200}
        with patch.object(application, "_validate_instagram_session", return_value=validation), \
                patch.object(application, "_fetch_real_csrftoken", return_value=None):
            response = self.client.post(
                "/api/login_cookie",
                json={"sessionid": "synthetic-session-value", "username": "untrusted_name"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["username"], "verified_example")
        self.assertNotIn("synthetic-session-value", response.get_data(as_text=True))
        self.assertEqual(application._get_session()["username"], "verified_example")
        self.assertEqual(
            application._get_session()["cookies"]["sessionid"], "synthetic-session-value"
        )

    def test_invalid_or_rate_limited_login_preserves_existing_session(self):
        application._save_session(
            "existing_example", {"sessionid": "existing-synthetic-session"}, validation_status="valid"
        )
        before = self.session_file.read_bytes()
        for state, status in (("invalid", 401), ("rate_limited", 429), ("transient", 503)):
            with self.subTest(state=state), patch.object(
                application, "_validate_instagram_session",
                return_value={"state": state, "username": None, "http_status": status},
            ):
                response = self.client.post(
                    "/api/login_cookie", json={"sessionid": "rejected-synthetic-session"}
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(self.session_file.read_bytes(), before)

    def test_empty_session_is_rejected_before_validation(self):
        with patch.object(application, "_validate_instagram_session") as validate:
            response = self.client.post("/api/login_cookie", json={"sessionid": "   "})
        self.assertEqual(response.status_code, 400)
        validate.assert_not_called()
        self.assertFalse(self.session_file.exists())

    def test_status_is_disk_only_and_never_returns_session_secrets(self):
        application._save_session(
            "saved_example", {"sessionid": "private-synthetic-session", "csrftoken": "private-csrf"},
            validation_status="valid",
        )
        with patch.object(application, "_validate_instagram_session", side_effect=AssertionError("Network")):
            response = self.client.get("/api/login_status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["logged_in"])
        self.assertEqual(response.get_json()["username"], "saved_example")
        text = response.get_data(as_text=True)
        self.assertNotIn("private-synthetic-session", text)
        self.assertNotIn("private-csrf", text)
        self.assertNotIn("cookies", response.get_json())
        self.network.assert_not_called()

    def test_logout_deletes_local_session_and_is_idempotent(self):
        application._save_session("saved_example", {"sessionid": "synthetic-session"})
        for _ in range(2):
            response = self.client.post("/api/logout")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(self.session_file.exists())
        self.assertFalse(self.client.get("/api/login_status").get_json()["logged_in"])

    def test_foreign_hosts_cannot_access_even_read_only_session_routes(self):
        for host in ("https://attacker.example", "http://127.0.0.1.attacker.example"):
            with self.subTest(host=host):
                response = self.client.get("/api/login_status", base_url=host)
                self.assertIn(response.status_code, (400, 403))

    def test_foreign_or_null_origin_cannot_read_or_modify_local_session(self):
        for origin in ("https://attacker.example", "null", "http://localhost:9999"):
            with self.subTest(origin=origin):
                response = self.client.get("/api/login_status", headers={"Origin": origin})
                self.assertIn(response.status_code, (400, 403))
                response = self.client.post("/api/logout", headers={"Origin": origin})
                self.assertIn(response.status_code, (400, 403))

    def test_same_origin_loopback_requests_are_allowed(self):
        for origin in ("http://localhost", "http://127.0.0.1:3060", "http://[::1]:3060"):
            with self.subTest(origin=origin):
                response = self.client.get(
                    "/api/login_status", base_url=origin, headers={"Origin": origin}
                )
                self.assertEqual(response.status_code, 200)

    def test_image_proxy_rejects_untrusted_schemes_and_hosts(self):
        urls = (
            "http://scontent.cdninstagram.com/example.jpg",
            "https://cdninstagram.com.attacker.example/example.jpg",
            "https://attacker.example/example.jpg",
            "file:///etc/passwd",
            "https://127.0.0.1/example.jpg",
            "https://i.ytimg.com.attacker.example/example.jpg",
        )
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get("/api/proxy_pic", query_string={"url": url})
                self.assertIn(response.status_code, (400, 404))
        self.network.assert_not_called()

    def test_image_proxy_never_follows_allowed_host_redirects(self):
        upstream = Mock(status_code=302, headers={"Location": "http://127.0.0.1/private"})
        session = Mock()
        session.get.return_value = upstream
        with patch("requests.Session", return_value=session):
            response = self.client.get(
                "/api/proxy_pic", query_string={"url": "https://scontent.cdninstagram.com/example.jpg"}
            )
        self.assertEqual(response.status_code, 502)
        session.get.assert_called_once()
        self.assertIs(session.get.call_args.kwargs["allow_redirects"], False)

    def test_reels_use_observed_play_count_and_preserve_missing_views(self):
        self.assertEqual(application._resolve_post_views({
            "category": "Reels", "video_play_count": 120, "video_view_count": 40,
        }), 120)
        self.assertEqual(application._resolve_post_views({
            "category": "Reels", "video_play_count": 0, "video_view_count": 40,
        }), 0)
        self.assertIsNone(application._resolve_post_views({
            "category": "Reels", "video_view_count": 40,
        }))

    def _export_fixture(self):
        return {
            "platform": "instagram",
            "profile": {"username": "example_creator", "full_name": "Example Creator", "followers": 1000},
            "posts": [{
                "shortcode": "EXAMPLE1", "date": "2026-01-01 12:00:00", "typename": "Reels",
                "category": "Reels", "is_video": True, "likes": 25, "comments": 3,
                "video_play_count": 120, "video_view_count": 40,
                "url": "https://www.instagram.com/p/EXAMPLE1/", "caption": "Synthetic example",
            }],
            "stats": {"avg_likes": 25, "avg_comments": 3, "avg_views": 120, "engagement_rate": 2.8},
            "meta": {"days": 30, "source": "test_fixture"},
        }

    def test_csv_exports_observed_views_and_keeps_unavailable_views_empty(self):
        data = self._export_fixture()
        unavailable = dict(data["posts"][0], shortcode="EXAMPLE2")
        unavailable.pop("video_play_count")
        data["posts"].append(unavailable)
        response = self.client.post("/api/export_csv", json=data)
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip("\ufeff")), delimiter=";"))
        self.assertEqual(rows[1][7], "120")
        self.assertEqual(rows[2][7], "")

    def test_csv_neutralizes_formula_prefixes_in_untrusted_captions(self):
        formulas = ("=1+1", "+1+1", "-1+1", "@SUM(1,1)", "  =1+1", "\t=1+1")
        for caption in formulas:
            with self.subTest(caption=caption):
                data = self._export_fixture()
                data["posts"][0]["caption"] = caption
                response = self.client.post("/api/export_csv", json=data)
                self.assertEqual(response.status_code, 200)
                rows = list(csv.reader(
                    io.StringIO(response.get_data(as_text=True).lstrip("\ufeff")), delimiter=";"
                ))
                self.assertTrue(rows[1][-1].startswith("'"), repr(rows[1][-1]))

    def test_excel_exports_real_workbook_and_observed_reel_views(self):
        from openpyxl import load_workbook

        response = self.client.post("/api/export_excel", json=self._export_fixture())
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data), data_only=True)
        try:
            contents = workbook.worksheets[1]
            self.assertEqual(contents.cell(2, 2).value, "https://www.instagram.com/p/EXAMPLE1/")
            self.assertEqual(contents.cell(2, 4).value, 25)
            self.assertEqual(contents.cell(2, 5).value, 3)
            self.assertEqual(contents.cell(2, 6).value, 120)
        finally:
            workbook.close()


if __name__ == "__main__":
    unittest.main()
