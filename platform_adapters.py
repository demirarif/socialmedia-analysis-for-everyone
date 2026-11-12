"""Keyless public-data adapters for YouTube and TikTok.

The adapters normalize both platforms to the post/profile contract consumed by app.py.
They deliberately use low-volume public pages and never download media files.
"""

from __future__ import annotations

import json
import importlib
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from itertools import islice
from urllib.parse import quote, unquote, urlparse

import requests


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

_YOUTUBE_METRICS_CACHE = {}
_YOUTUBE_METRICS_CACHE_LOCK = threading.Lock()
_YOUTUBE_METRICS_CACHE_TTL = 6 * 60 * 60
_YOUTUBE_THREAD_LOCAL = threading.local()
MAX_YOUTUBE_POSTS = 180
MAX_TIKTOK_POSTS = 500
MAX_YOUTUBE_PAGES_PER_TAB = 8
MAX_TIKTOK_EMBED_DETAILS = 12


def _bounded_limit(value, maximum):
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("İçerik sınırı geçerli bir pozitif sayı olmalı.") from None


def _failure(source, status, message, code=None):
    meta = {"source": source, "http_status": status, "error_code": code or "platform_unavailable"}
    if status in (429, 502, 503):
        meta["retryable"] = True
    if status == 429:
        meta["retry_after"] = 60
    return None, None, message, meta


def _profile_url_parts(raw, allowed_hosts, message):
    try:
        parsed = urlparse(raw)
        if (parsed.scheme.lower() not in ("http", "https")
                or (parsed.hostname or "").lower() not in allowed_hosts
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.params or parsed.query or parsed.fragment):
            raise ValueError(message)
        return parsed
    except (ValueError, TypeError):
        raise ValueError(message) from None


def _public_session(user_agent: str | None = None) -> requests.Session:
    session = requests.Session()
    # A stale machine-wide proxy must not silently break public platform requests.
    session.trust_env = False
    session.headers.update({
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    })
    return session


def _json_after(text: str, marker: str):
    index = text.find(marker)
    if index < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[index + len(marker):])
        return value
    except (ValueError, TypeError):
        return None


def _script_json(text: str, script_id: str):
    pattern = rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (ValueError, TypeError):
        return None


def _collect_key(data, key: str):
    found = []

    def walk(value):
        if isinstance(value, dict):
            if key in value:
                found.append(value[key])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return found


def _compact_number(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) and value >= 0 else 0
    text = str(value).replace("\xa0", " ").replace(",", "").strip().lower()
    match = re.search(r"([\d.]+)\s*([kmb])?", text)
    if not match:
        return 0
    try:
        number = float(match.group(1))
    except ValueError:
        return 0
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(match.group(2), 1)
    return int(number * multiplier)


def _relative_datetime(value: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    text = (value or "").lower().strip()
    text = re.sub(r"^(streamed|premiered)\s+", "", text)
    if "today" in text or "hour" in text or "minute" in text or "second" in text:
        if "hour" in text:
            count = int(re.search(r"\d+", text).group()) if re.search(r"\d+", text) else 1
            return now - timedelta(hours=count)
        if "minute" in text:
            count = int(re.search(r"\d+", text).group()) if re.search(r"\d+", text) else 1
            return now - timedelta(minutes=count)
        return now
    match = re.search(r"(\d+)\s+(day|week|month|year)s?\s+ago", text)
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2)
    days = {
        "day": count,
        "week": count * 7,
        "month": count * 30,
        "year": count * 365,
    }[unit]
    return now - timedelta(days=days)


def _duration_seconds(value: str | None) -> int:
    if not value:
        return 0
    parts = [int(part) for part in re.findall(r"\d+", value)]
    if not parts:
        return 0
    total = 0
    for part in parts:
        total = total * 60 + part
    return total


def _hashtags(text: str):
    return re.findall(r"#([\w.]+)", text or "", flags=re.UNICODE)


def _normalize_youtube_base(identifier: str) -> str:
    raw = (identifier or "").strip()
    if not raw:
        raise ValueError("YouTube kanal adı veya URL gerekli.")
    message = "Geçerli bir YouTube kanal URL'si, @kanal veya kanal kimliği girin; video bağlantısı kullanmayın."
    if "://" in raw:
        parsed = _profile_url_parts(raw, {"youtube.com", "www.youtube.com", "m.youtube.com"}, message)
        path = unquote(parsed.path).rstrip("/")
        path = re.sub(r"/(videos|shorts|featured|about|streams)$", "", path, flags=re.IGNORECASE)
        if re.fullmatch(r"/@[\w.-]{1,100}", path, flags=re.UNICODE):
            return "https://www.youtube.com/" + quote(path.lstrip("/"), safe="@._-")
        if re.fullmatch(r"/channel/UC[A-Za-z0-9_-]{22}", path):
            return "https://www.youtube.com" + path
        if re.fullmatch(r"/(?:c|user)/[\w.-]{1,100}", path, flags=re.UNICODE):
            return "https://www.youtube.com" + quote(path, safe="/._-")
        raise ValueError(message)
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", raw):
        return f"https://www.youtube.com/channel/{raw}"
    handle = raw[1:] if raw.startswith("@") else raw
    if not re.fullmatch(r"[\w.-]{1,100}", handle, flags=re.UNICODE) or handle in (".", ".."):
        raise ValueError(message)
    return "https://www.youtube.com/@" + quote(handle, safe="._-")


def _youtube_config(html: str):
    configs = []
    cursor = 0
    marker = "ytcfg.set("
    while True:
        index = html.find(marker, cursor)
        if index < 0:
            break
        try:
            value, consumed = json.JSONDecoder().raw_decode(html[index + len(marker):])
            if isinstance(value, dict):
                configs.append(value)
            cursor = index + len(marker) + consumed
        except ValueError:
            cursor = index + len(marker)
    merged = {}
    for config in configs:
        merged.update(config)
    return merged


def _youtube_text(value) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    for key in ("content", "simpleText"):
        if isinstance(value.get(key), str):
            return value[key]
    runs = value.get("runs")
    if isinstance(runs, list):
        return "".join(
            str(run.get("text") or run.get("content") or "")
            for run in runs
            if isinstance(run, dict)
        )
    return ""


def _nullable_count(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) and value >= 0 else None
    text = str(value).replace("\xa0", " ").strip()
    if not re.search(r"\d", text) or re.match(r"^-", text):
        return None
    try:
        return _compact_number(text)
    except (ValueError, OverflowError):
        return None


def _iso_duration_seconds(value: str | None) -> int:
    if not value:
        return 0
    match = re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
        value,
    )
    if not match:
        return 0
    days, hours, minutes, seconds = match.groups()
    return int(
        int(days or 0) * 86400
        + int(hours or 0) * 3600
        + int(minutes or 0) * 60
        + float(seconds or 0)
    )


def _youtube_thread_session(user_agent: str | None = None):
    resolved_agent = user_agent or DEFAULT_USER_AGENT
    session = getattr(_YOUTUBE_THREAD_LOCAL, "session", None)
    if session is None or getattr(_YOUTUBE_THREAD_LOCAL, "user_agent", None) != resolved_agent:
        session = _public_session(resolved_agent)
        _YOUTUBE_THREAD_LOCAL.session = session
        _YOUTUBE_THREAD_LOCAL.user_agent = resolved_agent
    return session


def _youtube_watch_metrics(video_id: str, user_agent: str | None = None):
    now = time.time()
    with _YOUTUBE_METRICS_CACHE_LOCK:
        cached = _YOUTUBE_METRICS_CACHE.get(video_id)
        if cached and now - cached[0] < _YOUTUBE_METRICS_CACHE_TTL:
            return dict(cached[1])

    metrics = {
        "likes": None,
        "comments": None,
        "views": None,
        "duration": None,
        "published_at": None,
        "ok": False,
    }
    try:
        session = _youtube_thread_session(user_agent)
        response = session.get(
            "https://www.youtube.com/watch",
            params={"v": video_id, "hl": "en", "gl": "US"},
            timeout=(8, 25),
            allow_redirects=False,
        )
        if response.status_code != 200:
            return metrics
        html = response.text
        initial = _json_after(html, "var ytInitialData = ") or _json_after(html, "ytInitialData = ")
        player = (
            _json_after(html, "var ytInitialPlayerResponse = ")
            or _json_after(html, "ytInitialPlayerResponse = ")
        )

        if isinstance(player, dict):
            details = player.get("videoDetails", {})
            metrics["views"] = _nullable_count(details.get("viewCount"))
            duration = _nullable_count(details.get("lengthSeconds"))
            metrics["duration"] = duration if duration is not None else None
            for renderer in _collect_key(player, "playerMicroformatRenderer"):
                if not isinstance(renderer, dict):
                    continue
                if metrics["likes"] is None:
                    metrics["likes"] = _nullable_count(renderer.get("likeCount"))
                metrics["published_at"] = renderer.get("publishDate") or renderer.get("uploadDate")
                if metrics["likes"] is not None:
                    break

        if isinstance(initial, dict):
            for header in _collect_key(initial, "engagementPanelTitleHeaderRenderer"):
                if not isinstance(header, dict):
                    continue
                if "comment" in _youtube_text(header.get("title")).lower():
                    metrics["comments"] = _nullable_count(_youtube_text(header.get("contextualInfo")))
                    break
            if metrics["comments"] is None:
                for header in _collect_key(initial, "commentsHeaderRenderer"):
                    if isinstance(header, dict):
                        metrics["comments"] = _nullable_count(_youtube_text(header.get("countText")))
                        if metrics["comments"] is not None:
                            break
            if metrics["likes"] is None:
                candidates = []
                for view_model in _collect_key(initial, "segmentedLikeDislikeButtonViewModel"):
                    if isinstance(view_model, dict):
                        candidates.extend(_collect_key(view_model.get("likeButtonViewModel", {}), "accessibilityText"))
                        candidates.extend(_collect_key(view_model.get("likeButtonViewModel", {}), "title"))
                for candidate in candidates:
                    text = _youtube_text(candidate) if isinstance(candidate, dict) else str(candidate)
                    if "like" in text.lower():
                        metrics["likes"] = _nullable_count(text)
                        if metrics["likes"] is not None:
                            break

        metrics["ok"] = isinstance(initial, dict) or isinstance(player, dict)
    except (requests.RequestException, ValueError, TypeError):
        pass

    with _YOUTUBE_METRICS_CACHE_LOCK:
        if len(_YOUTUBE_METRICS_CACHE) >= 512:
            _YOUTUBE_METRICS_CACHE.pop(next(iter(_YOUTUBE_METRICS_CACHE)))
        _YOUTUBE_METRICS_CACHE[video_id] = (now, dict(metrics))
    return metrics


def _youtube_official_metrics(posts, api_key: str, session: requests.Session):
    metrics_by_id = {}
    if not api_key:
        return metrics_by_id, None
    video_ids = [str(post.get("id") or "") for post in posts if post.get("id")]
    try:
        for offset in range(0, len(video_ids), 50):
            response = session.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "part": "statistics,snippet,contentDetails",
                    "id": ",".join(video_ids[offset:offset + 50]),
                },
                headers={"X-Goog-Api-Key": api_key},
                timeout=(8, 30),
                allow_redirects=False,
            )
            if response.status_code != 200:
                return metrics_by_id, f"HTTP {response.status_code}"
            for item in response.json().get("items", []):
                statistics = item.get("statistics", {})
                snippet = item.get("snippet", {})
                content_details = item.get("contentDetails", {})
                metrics_by_id[str(item.get("id") or "")] = {
                    "likes": _nullable_count(statistics.get("likeCount")),
                    "comments": _nullable_count(statistics.get("commentCount")),
                    "views": _nullable_count(statistics.get("viewCount")),
                    "duration": _iso_duration_seconds(content_details["duration"]) if content_details.get("duration") else None,
                    "published_at": snippet.get("publishedAt"),
                    "ok": True,
                }
    except (requests.RequestException, ValueError):
        return metrics_by_id, "request_failed"
    return metrics_by_id, None


def _apply_youtube_metrics(post, metrics):
    if metrics.get("likes") is not None:
        post["likes"] = metrics["likes"]
    if metrics.get("comments") is not None:
        post["comments"] = metrics["comments"]
    if metrics.get("views") is not None:
        post["video_view_count"] = metrics["views"]
        post["video_play_count"] = metrics["views"]
        post["views"] = metrics["views"]
    if metrics.get("duration") is not None:
        post["video_duration"] = metrics["duration"]
    published_at = metrics.get("published_at")
    if published_at:
        try:
            parsed = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            post["date"] = parsed.strftime("%Y-%m-%d %H:%M:%S")
            post["timestamp"] = int(parsed.timestamp())
            post["date_is_estimate"] = False
        except ValueError:
            pass


def _enrich_youtube_metrics(posts, user_agent: str | None = None, api_key: str | None = None):
    if not posts:
        return {
            "metrics_source": "none",
            "metrics_limited": True,
            "metric_coverage": {"likes": {"available": 0, "total": 0}, "comments": {"available": 0, "total": 0}, "views": {"available": 0, "total": 0}},
        }

    by_id = {str(post.get("id")): post for post in posts if post.get("id")}
    source_parts = []
    api_error = None
    if api_key:
        official, api_error = _youtube_official_metrics(posts, api_key, _public_session(user_agent))
        if official:
            source_parts.append("youtube_data_api_v3")
            for video_id, metrics in official.items():
                if video_id in by_id:
                    _apply_youtube_metrics(by_id[video_id], metrics)

    targets = [
        post for post in posts
        if post.get("likes") is None
        or post.get("comments") is None
        or post.get("views") is None
    ]
    public_successes = 0
    if targets:
        worker_count = min(4, len(targets))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(_youtube_watch_metrics, str(post.get("id")), user_agent): post
                for post in targets
                if post.get("id")
            }
            for future in as_completed(futures):
                post = futures[future]
                try:
                    metrics = future.result()
                except Exception:
                    continue
                if metrics.get("ok"):
                    public_successes += 1
                _apply_youtube_metrics(post, metrics)
        if public_successes:
            source_parts.append("youtube_public_watch")

    total = len(posts)
    likes_available = sum(isinstance(post.get("likes"), (int, float)) for post in posts)
    comments_available = sum(isinstance(post.get("comments"), (int, float)) for post in posts)
    views_available = sum(isinstance(post.get("video_view_count"), (int, float)) for post in posts)
    limited = likes_available < total or comments_available < total or views_available < total
    note = (
        f"YouTube metrik kapsamı: beğeni {likes_available}/{total}, yorum {comments_available}/{total}, "
        f"izlenme {views_available}/{total}. Kapalı veya gizli metrikler boş bırakılır."
    )
    result = {
        "metrics_source": "+".join(source_parts) or "youtube_public_listing",
        "metrics_limited": limited,
        "metric_coverage": {
            "likes": {"available": likes_available, "total": total},
            "comments": {"available": comments_available, "total": total},
            "views": {"available": views_available, "total": total},
        },
        "note": note,
    }
    if api_error:
        result["youtube_api_error"] = api_error
    return result


def _youtube_continuation(data):
    candidates = []
    for renderer in _collect_key(data, "continuationItemRenderer"):
        endpoint = renderer.get("continuationEndpoint", {}) if isinstance(renderer, dict) else {}
        command = endpoint.get("continuationCommand", {})
        token = command.get("token")
        api_url = endpoint.get("commandMetadata", {}).get("webCommandMetadata", {}).get("apiUrl", "")
        if token and "browse" in api_url:
            candidates.append(token)
    return max(candidates, key=len) if candidates else None


def _youtube_thumbnail(lockup):
    sources = (
        lockup.get("contentImage", {})
        .get("thumbnailViewModel", {})
        .get("image", {})
        .get("sources", [])
    )
    return sources[-1].get("url", "") if sources else ""


def _youtube_lockup_post(lockup, category: str):
    if not isinstance(lockup, dict) or lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None
    video_id = lockup.get("contentId")
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None
    metadata = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = metadata.get("title", {}).get("content", "")
    rows = metadata.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
    parts = []
    for row in rows:
        for part in row.get("metadataParts", []):
            content = part.get("text", {}).get("content")
            if content:
                parts.append(content)
    view_text = next((part for part in parts if "view" in part.lower()), None)
    view_count = _nullable_count(view_text)
    published_text = next((part for part in parts if "ago" in part.lower()), "")
    published = _relative_datetime(published_text)

    duration_text = ""
    badges = _collect_key(lockup.get("contentImage", {}), "thumbnailBadgeViewModel")
    for badge in badges:
        text = badge.get("text", "") if isinstance(badge, dict) else ""
        if re.match(r"^\d+(?::\d+)+$", text):
            duration_text = text
            break
    duration = _duration_seconds(duration_text)
    normalized_category = "Shorts" if category == "Shorts" else "YouTube Video"
    content_type = "Shorts" if category == "Shorts" else "Video"
    return {
        "platform": "youtube",
        "id": video_id,
        "shortcode": video_id,
        "date": published.strftime("%Y-%m-%d %H:%M:%S") if published else "",
        "timestamp": int(published.timestamp()) if published else None,
        "date_is_estimate": bool(published),
        "typename": "YouTubeShort" if category == "Shorts" else "YouTubeVideo",
        "is_video": True,
        "category": normalized_category,
        "content_type": content_type,
        "likes": None,
        "comments": None,
        "shares": None,
        "video_view_count": view_count,
        "video_play_count": view_count,
        "views": view_count,
        "video_duration": duration if duration_text else None,
        "caption": title,
        "title": title,
        "hashtags": _hashtags(title),
        "media_count": 1,
        "thumbnail_url": _youtube_thumbnail(lockup),
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def _youtube_page_posts(data, category):
    """Support current lockups, Shorts shelves and older public channel renderers."""
    for lockup in _collect_key(data, "lockupViewModel"):
        post = _youtube_lockup_post(lockup, category)
        if post:
            yield post
    for key in ("videoRenderer", "gridVideoRenderer", "reelItemRenderer", "shortsLockupViewModel"):
        for renderer in _collect_key(data, key):
            if not isinstance(renderer, dict):
                continue
            video_id = renderer.get("videoId")
            if not video_id:
                candidates = _collect_key(renderer, "videoId")
                video_id = candidates[0] if candidates else None
            if not video_id:
                continue
            overlay = renderer.get("overlayMetadata") or {}
            title = _youtube_text(renderer.get("title") or renderer.get("headline") or overlay.get("primaryText"))
            parts = [
                _youtube_text(renderer.get("viewCountText") or overlay.get("secondaryText")),
                _youtube_text(renderer.get("publishedTimeText")),
            ]
            thumbnail = renderer.get("thumbnail") or {}
            sources = thumbnail.get("thumbnails") or thumbnail.get("sources") or []
            canonical = {
                "contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "contentId": video_id,
                "metadata": {"lockupMetadataViewModel": {
                    "title": {"content": title},
                    "metadata": {"contentMetadataViewModel": {"metadataRows": [{
                        "metadataParts": [{"text": {"content": part}} for part in parts if part],
                    }]}},
                }},
                "contentImage": {
                    "thumbnailViewModel": {"image": {"sources": sources}},
                    "thumbnailBadgeViewModel": {"text": _youtube_text(renderer.get("lengthText"))},
                },
            }
            post = _youtube_lockup_post(canonical, category)
            if post:
                yield post


def _youtube_metadata(initial_data, base_url: str):
    metadata_list = _collect_key(initial_data, "channelMetadataRenderer")
    metadata = metadata_list[0] if metadata_list else {}
    text_values = []

    def collect_text(value):
        if isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, str):
                text_values.append(content)
            for child in value.values():
                collect_text(child)
        elif isinstance(value, list):
            for child in value:
                collect_text(child)

    headers = _collect_key(initial_data, "pageHeaderViewModel")
    if headers:
        collect_text(headers[0].get("metadata", {}))
    for key in ("subscriberCountText", "videosCountText"):
        text_values.extend(_youtube_text(value) for value in _collect_key(initial_data, key))
    subscriber_text = next((text for text in text_values if "subscriber" in text.lower()), None)
    video_text = next((text for text in text_values if "video" in text.lower()), None)
    if not metadata.get("externalId") and not metadata.get("title"):
        return None
    avatars = metadata.get("avatar", {}).get("thumbnails", [])
    username = metadata.get("vanityChannelUrl", "").rstrip("/").split("/")[-1]
    if not username:
        # Channel display names are not unique; keep comparison identities stable.
        username = metadata.get("externalId") or base_url.rstrip("/").split("/")[-1]
    return {
        "platform": "youtube",
        "external_id": metadata.get("externalId", ""),
        "username": username,
        "full_name": metadata.get("title") or username,
        "biography": metadata.get("description", ""),
        "followers": _nullable_count(subscriber_text),
        "following": None,
        "post_count": _nullable_count(video_text),
        "is_private": False,
        "is_verified": False,
        "is_business": False,
        "profile_pic_url": avatars[-1].get("url", "") if avatars else "",
        "url": metadata.get("channelUrl") or base_url,
    }


def _youtube_tab(session, url: str, max_items: int, cutoff_date: datetime | None, category: str):
    response = session.get(url, timeout=(8, 25), allow_redirects=False)
    if response.status_code != 200:
        return None, [], {"http_status": response.status_code, "pages": 0}
    marker = "var ytInitialData = "
    initial = _json_after(response.text, marker) or _json_after(response.text, "ytInitialData = ")
    if not isinstance(initial, dict):
        return None, [], {"http_status": response.status_code, "pages": 0}
    config = _youtube_config(response.text)
    profile = _youtube_metadata(initial, url.rsplit("/", 1)[0])
    if not profile:
        return None, [], {"http_status": 404, "pages": 0}
    posts = []
    seen = set()
    page_data = initial
    pages = 0

    partial = False
    token = None
    seen_tokens = set()
    while isinstance(page_data, dict) and len(posts) < max_items and pages < MAX_YOUTUBE_PAGES_PER_TAB:
        pages += 1
        page_posts = []
        for post in _youtube_page_posts(page_data, category):
            if post and post["id"] not in seen:
                seen.add(post["id"])
                page_posts.append(post)
        posts.extend(page_posts)
        token = _youtube_continuation(page_data)
        api_key = config.get("INNERTUBE_API_KEY")
        context = config.get("INNERTUBE_CONTEXT")
        if not token or len(posts) >= max_items:
            break
        if not api_key or not context or token in seen_tokens:
            partial = True
            break
        seen_tokens.add(token)
        try:
            continuation = session.post(
                "https://www.youtube.com/youtubei/v1/browse",
                params={"key": api_key},
                json={"context": context, "continuation": token},
                timeout=(8, 25),
                allow_redirects=False,
            )
        except requests.RequestException:
            partial = True
            break
        if continuation.status_code != 200:
            partial = True
            break
        try:
            page_data = continuation.json()
        except ValueError:
            partial = True
            break

    return profile, posts[:max_items], {
        "http_status": 200, "pages": pages,
        "truncated": len(posts) > max_items or bool(token),
        "source_limited": partial or (pages >= MAX_YOUTUBE_PAGES_PER_TAB and bool(token)),
    }


def fetch_youtube(
    identifier: str,
    max_posts: int = 120,
    cutoff_date: datetime | None = None,
    user_agent: str | None = None,
    api_key: str | None = None,
):
    source = "youtube_public_innertube"
    try:
        base_url = _normalize_youtube_base(identifier)
        limit = _bounded_limit(max_posts, MAX_YOUTUBE_POSTS)
    except ValueError as exc:
        return _failure(source, 400, str(exc), "invalid_input")

    session = _public_session(user_agent)
    tabs = []
    for suffix, category in (("videos", "Video"), ("shorts", "Shorts")):
        try:
            result = _youtube_tab(session, f"{base_url}/{suffix}", limit, cutoff_date, category)
        except requests.RequestException:
            result = (None, [], {"http_status": 502, "pages": 0})
        except (TypeError, ValueError, KeyError, AttributeError):
            result = (None, [], {"http_status": 503, "pages": 0})
        tabs.append(result)
        if result[2].get("http_status") == 429:
            return _failure(source, 429, "YouTube geçici istek sınırına ulaşıldı. Daha sonra tekrar deneyin.", "rate_limited")
    profile = next((entry[0] for entry in tabs if entry[0]), None)
    if not profile:
        statuses = [entry[2].get("http_status", 503) for entry in tabs]
        status = 404 if all(value == 404 for value in statuses) else (502 if 502 in statuses else 503)
        return _failure(source, status, "YouTube kanalına ulaşılamadı. Kanalın herkese açık olduğunu kontrol edin.",
                        "not_found" if status == 404 else "platform_unavailable")
    combined = {}
    for _, tab_posts, _ in tabs:
        for post in tab_posts:
            combined[post["id"]] = post
    listing_count = len(combined)
    # Shorts listings often omit publication dates. A global timestamp sort
    # before enrichment would silently exclude every Short on active channels.
    # Select a bounded sample from both tabs, then sort their exact dates below.
    posts, selected = [], set()
    groups = [entry[1] for entry in tabs]
    for position in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if position < len(group):
                post = group[position]
                if post["id"] not in selected:
                    posts.append(combined[post["id"]])
                    selected.add(post["id"])
            if len(posts) >= limit:
                break
        if len(posts) >= limit:
            break
    metric_meta = _enrich_youtube_metrics(posts, user_agent=user_agent, api_key=api_key)
    if cutoff_date:
        posts = [post for post in posts if not post.get("timestamp")
                 or datetime.fromtimestamp(post["timestamp"]) >= cutoff_date]
    posts.sort(key=lambda post: post.get("timestamp") or 0, reverse=True)
    # Recompute coverage after exact watch-page dates may have filtered the sample.
    coverage = _metric_coverage(posts, ("likes", "comments", "views"))
    metric_meta["metric_coverage"] = coverage
    metric_meta["metrics_limited"] = any(value["available"] < value["total"] for value in coverage.values())
    unknown_dates = sum(not post.get("timestamp") for post in posts)
    approximate_dates = sum(bool(post.get("date_is_estimate")) for post in posts)
    profile["sampled_content_count"] = len(posts)
    profile["content_count_is_sample"] = profile.get("post_count") is None
    meta = {
        "source": source, "pages": sum(entry[2].get("pages", 0) for entry in tabs),
        "sampled_count": len(posts), "available_recent_items": len(posts),
        "source_limited": any(not entry[0] or entry[2].get("source_limited") for entry in tabs),
        "truncated": listing_count > limit or any(entry[2].get("truncated") for entry in tabs),
        "content_count_available": profile.get("post_count") is not None,
        "dates_unavailable": unknown_dates, "dates_approximate": approximate_dates,
        **metric_meta,
    }
    meta["note"] = (
        f"YouTube video ve Shorts örneklemi: {len(posts)} içerik. "
        "Kamuya açık sayılarda yuvarlama olabilir; gizli metrikler boş kalır. "
        f"Tarihi eksik içerik: {unknown_dates}; yaklaşık tarih: {approximate_dates}. "
        "Sonuçlar kanalın tüm geçmişini kapsamayabilir."
    )
    return profile, posts, None, meta


def _normalize_tiktok_username(identifier: str) -> str:
    raw = (identifier or "").strip()
    message = "Geçerli bir TikTok profil URL'si veya @kullanıcı adı girin; video bağlantısı kullanmayın."
    if not raw:
        raise ValueError(message)
    if "://" in raw:
        parsed = _profile_url_parts(raw, {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}, message)
        match = re.fullmatch(r"/@([A-Za-z0-9_.]{1,24})/?", unquote(parsed.path))
        if not match:
            raise ValueError(message)
        username = match.group(1)
    else:
        username = raw[1:] if raw.startswith("@") else raw
    if (not re.fullmatch(r"[A-Za-z0-9_.]{1,24}", username)
            or username.endswith(".") or username in (".", "..")):
        raise ValueError(message)
    return username


def _tiktok_state_node(state, route_fragment: str):
    source_data = (state or {}).get("source", {}).get("data", {})
    if not isinstance(source_data, dict):
        return {}
    for route, node in source_data.items():
        # Match the complete route, rather than accepting @example_other for @example.
        if str(route).split("?")[0].rstrip("/") == route_fragment.rstrip("/") and isinstance(node, dict):
            return node
    return {}


def _tiktok_timestamp(video_id: str) -> int | None:
    try:
        timestamp = int(video_id) >> 32
        # Treat malformed IDs as unknown dates, never as Jan 1970 or far-future posts.
        return timestamp if 1_420_070_400 <= timestamp <= int(time.time()) + 86400 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _tiktok_detail(session, video_id: str):
    if not re.fullmatch(r"[0-9]{10,25}", str(video_id)):
        return None
    response = session.get(f"https://www.tiktok.com/embed/v2/{video_id}", timeout=(8, 20), allow_redirects=False)
    if response.status_code != 200:
        return None
    state = _script_json(response.text, "__FRONTITY_CONNECT_STATE__")
    node = _tiktok_state_node(state, f"/embed/v2/{video_id}")
    video_data = node.get("videoData", {})
    return video_data.get("itemInfos") if isinstance(video_data, dict) else None


def _metric_coverage(posts, keys=("likes", "comments", "views", "shares")):
    return {
        key: {"available": sum(_nullable_count(post.get(key)) is not None for post in posts), "total": len(posts)}
        for key in keys
    }


def _first_count(*values):
    for value in values:
        normalized = _nullable_count(value)
        if normalized is not None:
            return normalized
    return None


def _thumbnail(value):
    thumbnails = value.get("thumbnails") or []
    if isinstance(thumbnails, list):
        for entry in reversed(thumbnails):
            if isinstance(entry, dict) and entry.get("url"):
                return str(entry["url"])
    return str(value.get("thumbnail") or value.get("coverUrl") or "")


def _tiktok_post(item, username, source):
    if not isinstance(item, dict):
        return None
    video_id = str(item.get("id") or item.get("aweme_id") or "")
    if not re.fullmatch(r"[0-9]{10,25}", video_id):
        return None
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    timestamp = _first_count(item.get("timestamp"), item.get("createTime"), item.get("create_time"))
    if timestamp is None:
        timestamp = _tiktok_timestamp(video_id)
    try:
        date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""
    except (OSError, OverflowError, ValueError):
        timestamp, date = None, ""
    caption = str(item.get("description") or item.get("desc") or item.get("text") or item.get("title") or "")
    images = item.get("imagePost") or item.get("image_post_info") or {}
    image_list = images.get("images", []) if isinstance(images, dict) else []
    is_photo = bool(image_list) or item.get("ext") in ("m4a", "mp3")
    views = _first_count(item.get("view_count"), item.get("playCount"), stats.get("playCount"))
    duration = _first_count(item.get("duration"))
    video = item.get("video") if isinstance(item.get("video"), dict) else {}
    if duration is None:
        duration = _nullable_count(video.get("duration"))
    return {
        "platform": "tiktok", "id": video_id, "shortcode": video_id,
        "date": date, "timestamp": timestamp,
        "typename": "TikTokPhoto" if is_photo else "TikTokVideo", "is_video": not is_photo,
        "category": "TikTok Fotoğraf" if is_photo else "TikTok Video",
        "content_type": "Fotoğraf / Slayt" if is_photo else "Video",
        "likes": _first_count(item.get("like_count"), item.get("diggCount"), stats.get("diggCount")),
        "comments": _first_count(item.get("comment_count"), item.get("commentCount"), stats.get("commentCount")),
        # yt-dlp maps TikTok shareCount to repost_count; no missing values become zero.
        "shares": _first_count(item.get("repost_count"), item.get("share_count"), item.get("shareCount"), stats.get("shareCount")),
        "views": views, "video_view_count": views, "video_play_count": views,
        "video_duration": duration, "caption": caption, "title": caption,
        "hashtags": _hashtags(caption), "media_count": len(image_list) if image_list else 1,
        "thumbnail_url": _thumbnail(item),
        "url": f"https://www.tiktok.com/@{username}/{'photo' if is_photo else 'video'}/{video_id}",
        "metrics_source": source,
    }


def _tiktok_profile(username, info=None, embed=False):
    info = info or {}
    return {
        "platform": "tiktok", "external_id": str(info.get("id") or ""), "username": username,
        "full_name": info.get("nickname") or info.get("uploader") or username,
        "biography": info.get("signature") or info.get("description") or "",
        "followers": _first_count(info.get("followerCount"), info.get("follower_count")),
        "following": _nullable_count(info.get("followingCount")),
        # The public list is a sample; playlist_count may only count retrieved items.
        "post_count": None, "sampled_content_count": 0, "content_count_is_sample": True,
        "total_likes": _nullable_count(info.get("heartCount")) if embed else None,
        "is_private": bool(info.get("privateAccount")), "is_verified": bool(info.get("verified")),
        "is_business": False,
        "profile_pic_url": str(info.get("avatarThumbUrl") or "") if embed else "",
        "url": f"https://www.tiktok.com/@{username}",
    }


def _tiktok_meta(source, posts, *, truncated=False, candidates=0, reason=None):
    coverage = _metric_coverage(posts)
    meta = {
        "source": source, "source_limited": True, "content_count_available": False,
        "sampled_count": len(posts), "available_recent_items": len(posts),
        "candidates_examined": candidates, "truncated": bool(truncated),
        "metric_coverage": coverage,
        "metrics_limited": any(value["available"] < value["total"] for value in coverage.values()),
        "dates_unavailable": sum(not post.get("timestamp") for post in posts),
        "note": (
            f"TikTok: {len(posts)} herkese açık içerikten oluşan örneklem. "
            "Toplam gönderi sayısı ve tüm geçmiş doğrulanamaz; gizli metrikler boş kalır. "
            + ("Public embed yalnız seçili içerikleri sunar; tam profil geçmişi değildir."
               if source == "tiktok_public_embed" else "yt-dlp ile sınırlı, indirmesiz liste okundu.")
        ),
    }
    if reason:
        meta["fallback_reason"] = reason
    return meta


def _load_ytdlp():
    return importlib.import_module("yt_dlp")


class _QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


class _RequestBudgetExceeded(Exception):
    pass


def _upstream_status(error):
    for current in (error, getattr(error, "cause", None), getattr(error, "__cause__", None)):
        status = getattr(current, "status", None) or getattr(current, "status_code", None)
        if status in (403, 404, 429, 502, 503):
            return status
    return 503


def _fetch_tiktok_ytdlp(username, limit, cutoff_date, user_agent):
    try:
        module = _load_ytdlp()
    except ImportError:
        return None, [], {"status": 503, "reason": "ytdlp_unavailable"}
    request_budget = min(40, 4 + math.ceil(limit / 30) * 2)

    class MetadataYoutubeDL(module.YoutubeDL):
        # TikTok can iterate empty historical pages without yielding a post. Cap
        # actual HTTP requests as well as playlist entries to bound that case.
        def urlopen(self, request):
            self._analysis_requests = getattr(self, "_analysis_requests", 0) + 1
            if self._analysis_requests > request_budget:
                raise _RequestBudgetExceeded()
            return super().urlopen(request)

    options = {
        "quiet": True, "no_warnings": True, "logger": _QuietLogger(),
        "extract_flat": True, "skip_download": True, "simulate": True,
        "playlistend": limit, "lazy_playlist": True,
        "socket_timeout": 20, "retries": 0, "extractor_retries": 0,
        "fragment_retries": 0, "file_access_retries": 0, "cachedir": False,
        "cookiefile": None, "cookiesfrombrowser": None, "usenetrc": False,
        "proxy": "", "http_headers": {"User-Agent": user_agent or DEFAULT_USER_AGENT},
    }
    profile, posts, candidates, incomplete, status, reason = None, [], 0, False, 200, None
    try:
        with MetadataYoutubeDL(options) as extractor:
            try:
                target = importlib.import_module("yt_dlp.networking.impersonate").ImpersonateTarget(client="chrome")
                available = getattr(extractor, "_impersonate_target_available", None)
                if callable(available) and available(target):
                    extractor.params["impersonate"] = target
            except (ImportError, AttributeError, ValueError):
                pass
            # process=False prevents per-video extraction or media resolution.
            info = extractor.extract_info(
                f"https://www.tiktok.com/@{username}", download=False, process=False, ie_key="TikTokUser"
            )
            if not isinstance(info, dict) or "entries" not in info:
                return None, [], {"status": 404, "reason": "no_public_entries"}
            profile = _tiktok_profile(username, info)
            seen = set()
            entries = info.get("entries") or ()
            for item in islice(entries, limit):
                candidates += 1
                post = _tiktok_post(item, username, "tiktok_ytdlp")
                if not post or post["id"] in seen:
                    continue
                seen.add(post["id"])
                if cutoff_date and post["timestamp"] and datetime.fromtimestamp(post["timestamp"]) < cutoff_date:
                    continue
                posts.append(post)
    except Exception as error:
        incomplete, status = True, _upstream_status(error)
        reason = "request_budget" if isinstance(error, _RequestBudgetExceeded) else "ytdlp_failed"
    posts.sort(key=lambda post: post.get("timestamp") or 0, reverse=True)
    if profile:
        profile["sampled_content_count"] = len(posts)
    return profile, posts, {
        "status": status, "reason": reason, "candidates": candidates,
        "truncated": incomplete or candidates >= limit,
    }


def _fetch_tiktok_embed(username, limit, cutoff_date, user_agent, reason):
    source = "tiktok_public_embed"
    session = _public_session(user_agent)
    try:
        response = session.get(f"https://www.tiktok.com/embed/@{username}", timeout=(8, 20), allow_redirects=False)
        if response.status_code != 200:
            status = response.status_code if response.status_code in (404, 429) else 503
            return _failure(source, status, "TikTok herkese açık profil verisi alınamadı.",
                            "rate_limited" if status == 429 else "not_found" if status == 404 else "platform_unavailable")
        state = _script_json(response.text, "__FRONTITY_CONNECT_STATE__")
        node = _tiktok_state_node(state, f"/embed/@{username}")
        user = node.get("userInfo") or {}
        if not isinstance(user, dict) or str(user.get("uniqueId") or "").lower() != username.lower():
            return _failure(source, 404, "TikTok profili bulunamadı veya herkese açık değil.", "not_found")
        profile = _tiktok_profile(username, user, embed=True)
        raw_items = node.get("videoList") or []
        if not isinstance(raw_items, list):
            raw_items = []
        posts, seen = [], set()
        candidates = 0
        for item in islice(raw_items, limit):
            candidates += 1
            post = _tiktok_post(item, username, source)
            if not post or post["id"] in seen:
                continue
            seen.add(post["id"])
            if cutoff_date and post["timestamp"] and datetime.fromtimestamp(post["timestamp"]) < cutoff_date:
                continue
            # Embed cards often omit likes/comments/shares. Fetch only a small,
            # bounded number of public detail JSON pages, never the media itself.
            if len(posts) < MAX_TIKTOK_EMBED_DETAILS and any(post[key] is None for key in ("likes", "comments", "shares")):
                try:
                    detail = _tiktok_detail(session, post["id"])
                    enriched = _tiktok_post({**item, **detail}, username, source) if isinstance(detail, dict) else None
                    if enriched:
                        for key in ("likes", "comments", "shares", "views", "video_view_count", "video_play_count", "video_duration"):
                            if enriched[key] is not None:
                                post[key] = enriched[key]
                except (requests.RequestException, ValueError, TypeError):
                    pass
            posts.append(post)
        posts.sort(key=lambda post: post.get("timestamp") or 0, reverse=True)
        profile["sampled_content_count"] = len(posts)
        meta = _tiktok_meta(source, posts, truncated=len(raw_items) > limit, candidates=candidates, reason=reason)
        meta["embed_available_items"] = len(raw_items)
        return profile, posts, None, meta
    except requests.RequestException:
        return _failure(source, 502, "TikTok bağlantısı kurulamadı. Daha sonra tekrar deneyin.")
    except (ValueError, TypeError, KeyError, AttributeError):
        return _failure(source, 503, "TikTok herkese açık veri biçimi okunamadı.")


def fetch_tiktok(identifier: str, max_posts: int = 30, cutoff_date: datetime | None = None, user_agent: str | None = None):
    try:
        username = _normalize_tiktok_username(identifier)
        limit = _bounded_limit(max_posts, MAX_TIKTOK_POSTS)
    except ValueError as error:
        return _failure("tiktok_public", 400, str(error), "invalid_input")
    profile, posts, extraction = _fetch_tiktok_ytdlp(username, limit, cutoff_date, user_agent)
    if posts:
        return profile, posts, None, _tiktok_meta(
            "tiktok_ytdlp", posts, truncated=extraction.get("truncated"), candidates=extraction.get("candidates", 0)
        )
    if extraction.get("status") == 429:
        return _failure("tiktok_ytdlp", 429, "TikTok geçici istek sınırına ulaşıldı. Daha sonra tekrar deneyin.", "rate_limited")
    fallback = _fetch_tiktok_embed(username, limit, cutoff_date, user_agent, extraction.get("reason") or "empty_ytdlp_sample")
    if not fallback[2]:
        return fallback
    # A successfully retrieved playlist may legitimately contain no content in the
    # chosen period. Keep that result instead of turning it into an extractor bug.
    if profile and extraction.get("status") == 200:
        return profile, [], None, _tiktok_meta(
            "tiktok_ytdlp", [], truncated=extraction.get("truncated"), candidates=extraction.get("candidates", 0)
        )
    return fallback
