#!/usr/bin/env python3
"""Analyze Instagram, TikTok, and YouTube locally with machine-readable results."""
import argparse
from contextlib import redirect_stdout
import getpass
import json
from pathlib import Path
import sys


def bounded_integer(low, high):
    def parse(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number
    return parse


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Enter your Instagram session ID privately in this terminal")
    commands.add_parser("status", help="Read local Instagram login state without network requests")
    commands.add_parser("logout", help="Delete the local saved Instagram session")
    analyze = commands.add_parser("analyze", help="Fetch platform metrics; no LLM calls")
    analyze.add_argument("username", help="Platform username, @handle, profile/channel URL, or YouTube channel ID")
    analyze.add_argument("--platform", choices=("instagram", "tiktok", "youtube"), default="instagram")
    analyze.add_argument("--days", type=bounded_integer(1, 2555), help="Default: 90 for Instagram/TikTok, 365 for YouTube")
    analyze.add_argument("--max-posts", type=bounded_integer(1, 500), default=50,
                         help="Default: 50; maximum: Instagram/TikTok 500, YouTube 180")
    analyze.add_argument("--demo", action="store_true", help="Generate synthetic data without contacting platforms")
    analyze.add_argument("--format", choices=("json", "csv", "xlsx"), default="json")
    analyze.add_argument("--output", type=Path, help="Save the selected format to this local path")
    return parser


def get_client():
    # Import lazily so --help needs no dependencies or runtime directory.
    with redirect_stdout(sys.stderr):
        from app import app
    return app.test_client()


def emit(payload, error=False):
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False),
          file=sys.stderr if error else sys.stdout)


def check_response(response):
    if response.status_code >= 400:
        payload = response.get_json(silent=True) or {"error": "Local API request failed"}
        emit(payload, error=True)
        return False
    return True


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command == "analyze":
        args.days = args.days if args.days is not None else (365 if args.platform == "youtube" else 90)
        maximum = 180 if args.platform == "youtube" else 500
        if args.max_posts > maximum:
            parser.error(f"--max-posts must be between 1 and {maximum} for {args.platform}")
        if args.format != "json" and not args.output:
            parser.error("--output is required for csv and xlsx")
    if args.command == "login" and not sys.stdin.isatty():
        emit({"error": "Run 'python cli.py login' yourself in an interactive terminal, "
                       "or enter your session ID in the local web UI. Never send it to an AI chat.",
              "error_code": "interactive_login_required"}, error=True)
        return 1
    try:
        client = get_client()
        if args.command == "status":
            response = client.get("/api/login_status")
        elif args.command == "logout":
            response = client.post("/api/logout")
        elif args.command == "login":
            session_id = getpass.getpass("Instagram sessionid (hidden; saved only locally): ").strip()
            if not session_id:
                emit({"error": "Session ID cannot be empty"}, error=True)
                return 1
            with redirect_stdout(sys.stderr):
                response = client.post("/api/login_cookie", json={"sessionid": session_id})
            del session_id
        else:
            with redirect_stdout(sys.stderr):
                response = client.post("/api/fetch", json={
                    "input": args.username, "platform": args.platform, "days": args.days,
                    "max_posts": args.max_posts, "demo": args.demo,
                })
        if not check_response(response):
            return 1
        payload = response.get_json()
        if args.command != "analyze" or not args.output:
            emit(payload)
            return 0
        if args.format == "json":
            contents = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        else:
            route = "/api/export_excel" if args.format == "xlsx" else "/api/export_csv"
            with redirect_stdout(sys.stderr):
                exported = client.post(route, json=payload)
            if not check_response(exported):
                return 1
            contents = exported.data
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(contents)
        emit({"output": str(output), "format": args.format, "platform": args.platform,
              "posts": len(payload.get("posts", [])), "demo": args.demo})
        return 0
    except (KeyboardInterrupt, EOFError):
        emit({"error": "Cancelled"}, error=True)
        return 130
    except ImportError:
        emit({"error": "Install dependencies first: python -m pip install -r requirements.txt"}, error=True)
        return 1
    except OSError:
        emit({"error": "Could not access a local file. Check the runtime/output directory permissions."}, error=True)
        return 1
    except Exception:
        # Avoid echoing upstream exceptions that might contain cookie values.
        emit({"error": "Analysis failed. Check local setup and the selected platform's availability; "
                       "never include session IDs in a bug report."}, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
