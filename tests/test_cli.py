"""CLI behavior with synthetic API responses; no application import or network."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import cli


@contextlib.contextmanager
def test_output_directory():
    """Use the writable checkout; some Windows sandboxes restrict system temp ACLs."""
    runs = (Path(__file__).resolve().parents[1] / ".runtime" / "test-runs").resolve()
    runs.mkdir(parents=True, exist_ok=True)
    folder = runs / ("cli-" + uuid4().hex)
    folder.mkdir()
    try:
        yield folder
    finally:
        # This UUID directory was created by this test. Never clean its parent.
        if folder.resolve().parent != runs or not folder.name.startswith("cli-"):
            raise RuntimeError("Refusing to clean an unexpected test directory")
        shutil.rmtree(folder)


class Response:
    def __init__(self, payload=None, status=200, data=b""):
        self.payload = payload
        self.status_code = status
        self.data = data

    def get_json(self, silent=False):
        return self.payload


class CliTests(unittest.TestCase):
    def invoke(self, args, client):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, "get_client", return_value=client), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_analyze_reuses_existing_api_and_emits_json(self):
        client = Mock()
        client.post.return_value = Response({"posts": [], "meta": {"source": "demo"}})
        code, stdout, stderr = self.invoke(["analyze", "demo", "--demo", "--max-posts", "8"], client)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["meta"]["source"], "demo")
        self.assertEqual(stderr, "")
        client.post.assert_called_once_with("/api/fetch", json={
            "input": "demo", "platform": "instagram", "days": 90,
            "max_posts": 8, "demo": True,
        })

    def test_rate_limit_returns_error_without_export_or_file(self):
        client = Mock()
        client.post.return_value = Response({"error": "Please wait", "error_code": "rate_limited"}, 429)
        with test_output_directory() as folder:
            output = folder / "result.xlsx"
            code, stdout, stderr = self.invoke(["analyze", "example", "--format", "xlsx", "--output", str(output)], client)
            self.assertFalse(output.exists())
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "rate_limited")
        self.assertEqual(client.post.call_count, 1)

    def test_public_platforms_need_no_instagram_login_or_session_prompt(self):
        for platform, identifier, days in (("tiktok", "@example", 90),
                                           ("youtube", "https://www.youtube.com/@example", 365)):
            with self.subTest(platform=platform):
                client = Mock()
                client.post.return_value = Response({"platform": platform, "posts": []})
                with patch.object(cli.getpass, "getpass") as prompt:
                    code, stdout, stderr = self.invoke(["analyze", identifier, "--platform", platform], client)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout)["platform"], platform)
                self.assertEqual(stderr, "")
                client.post.assert_called_once_with("/api/fetch", json={
                    "input": identifier, "platform": platform, "days": days,
                    "max_posts": 50, "demo": False,
                })
                client.get.assert_not_called()
                prompt.assert_not_called()

    def test_youtube_post_limit_is_checked_before_loading_the_client(self):
        with patch.object(cli, "get_client") as factory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                cli.main(["analyze", "@example", "--platform", "youtube", "--max-posts", "181"])
        self.assertEqual(error.exception.code, 2)
        factory.assert_not_called()

    def test_platform_limits_and_explicit_date_range_are_preserved(self):
        for platform, maximum in (("instagram", 500), ("tiktok", 500), ("youtube", 180)):
            with self.subTest(platform=platform):
                client = Mock()
                client.post.return_value = Response({"platform": platform, "posts": []})
                code, _, _ = self.invoke(["analyze", "@example", "--platform", platform,
                                         "--days", "30", "--max-posts", str(maximum)], client)
                self.assertEqual(code, 0)
                self.assertEqual(client.post.call_args.kwargs["json"]["days"], 30)
                self.assertEqual(client.post.call_args.kwargs["json"]["max_posts"], maximum)

    def test_public_platform_failure_does_not_turn_into_demo_success(self):
        client = Mock()
        client.post.return_value = Response({"error": "TikTok is unavailable", "platform": "tiktok"}, 503)
        code, stdout, stderr = self.invoke(["analyze", "@example", "--platform", "tiktok"], client)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["platform"], "tiktok")
        self.assertEqual(client.post.call_count, 1)

    def test_excel_saves_exported_bytes_from_existing_route(self):
        client = Mock()
        payload = {"platform": "youtube", "profile": {"username": "example"}, "posts": [{"likes": 3}]}
        client.post.side_effect = [Response(payload), Response(data=b"PK-test-workbook")]
        with test_output_directory() as folder:
            output = folder / "nested" / "youtube_example.xlsx"
            code, stdout, stderr = self.invoke(["analyze", "example", "--platform", "youtube",
                                               "--format", "xlsx", "--output", str(output)], client)
            self.assertEqual(output.read_bytes(), b"PK-test-workbook")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["posts"], 1)
        self.assertEqual(json.loads(stdout)["platform"], "youtube")
        self.assertEqual(stderr, "")
        self.assertEqual(client.post.call_args.args[0], "/api/export_excel")
        self.assertEqual(client.post.call_args.kwargs["json"], payload)

    def test_login_keeps_session_out_of_output(self):
        client = Mock()
        client.post.return_value = Response({"success": True, "username": "example"})
        with patch.object(cli.sys.stdin, "isatty", return_value=True), \
             patch.object(cli.getpass, "getpass", return_value="synthetic-secret"):
            code, stdout, stderr = self.invoke(["login"], client)
        self.assertEqual(code, 0)
        self.assertNotIn("synthetic-secret", stdout + stderr)
        client.post.assert_called_once_with("/api/login_cookie", json={"sessionid": "synthetic-secret"})

    def test_noninteractive_login_never_prompts_or_imports_app(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys.stdin, "isatty", return_value=False), \
             patch.object(cli, "get_client") as factory, \
             patch.object(cli.getpass, "getpass") as prompt, \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["login"])
        self.assertEqual(code, 1)
        factory.assert_not_called()
        prompt.assert_not_called()
        self.assertEqual(json.loads(stderr.getvalue())["error_code"], "interactive_login_required")

    def test_status_uses_disk_only_status_route(self):
        client = Mock()
        client.get.return_value = Response({"logged_in": False})
        code, stdout, _ = self.invoke(["status"], client)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(stdout)["logged_in"])
        client.get.assert_called_once_with("/api/login_status")
        client.post.assert_not_called()

    def test_out_of_bounds_request_is_rejected_before_client_load(self):
        with patch.object(cli, "get_client") as factory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                cli.main(["analyze", "example", "--max-posts", "501"])
        self.assertEqual(error.exception.code, 2)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
