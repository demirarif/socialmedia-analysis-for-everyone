#!/usr/bin/env python3
"""
Social Media Analysis for Everyone — local profile metrics and reports.
Uses the user's own Instagram session; never ships credentials or datasets.
"""

import csv
import io
import json
import re
import sys
import os
import math
import tempfile
import bleach
from datetime import datetime, timedelta
from itertools import islice
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, render_template, request, jsonify, Response

from dotenv import load_dotenv
from google import genai
from google.genai import types
from models import db, Brand, AnalysisHistory
from platform_adapters import fetch_youtube, fetch_tiktok
import instaloader

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("IG_ANALYSIS_DATA_DIR", str(BASE_DIR / ".runtime"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

# Instagram oturumları User-Agent bilgisine duyarlı olabilir. Chrome'un azaltılmış
# User-Agent biçimi major sürümü taşır; gerektiğinde .env üzerinden değiştirilebilir.
INSTAGRAM_BROWSER_MAJOR = re.sub(r"\D", "", os.getenv("INSTAGRAM_BROWSER_MAJOR", "150")) or "150"
INSTAGRAM_USER_AGENT = os.getenv("INSTAGRAM_USER_AGENT", (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{INSTAGRAM_BROWSER_MAJOR}.0.0.0 Safari/537.36"
)).strip()

def get_etki_oranlari():
    try:
        with open(BASE_DIR / "weights.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "izlenme": 0.35,
            "begeni": 0.25,
            "yorum": 0.25,
            "takipci": 0.05,
            "etkilesim": 0.10
        }
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

app = Flask(__name__, template_folder="templates", static_folder="static", instance_path=str(DATA_DIR))
app.secret_key = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

# Veritabanı Konfigürasyonu
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

with app.app_context():
    db.create_all()

SESSION_FILE = str(DATA_DIR / "ig_session.json")


@app.before_request
def local_requests_only():
    """Protect the single-user session against remote hosts and cross-site requests."""
    hostname = urlsplit("http://" + request.host).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        return jsonify({"error": "Bu uygulama yalnız kendi bilgisayarınızda kullanılabilir."}), 403
    origin = request.headers.get("Origin")
    if origin and origin != request.host_url.rstrip("/"):
        return jsonify({"error": "Farklı bir siteden gelen isteğe izin verilmiyor."}), 403
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify({"error": "Farklı bir siteden gelen isteğe izin verilmiyor."}), 403
    if request.method == "POST" and request.path in {
        "/api/content_dna", "/api/sponsorship", "/api/brand_analysis", "/api/compare", "/api/benchmarking", "/api/analyze_comments"
    }:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict) and payload.get("platform", "instagram") != "instagram":
            return jsonify({"error": "Bu özellik Instagram hesapları içindir."}), 400


@app.after_request
def private_response_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _safe_ai_html(value):
    return bleach.clean(
        str(value or ""),
        tags={"h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "b", "em", "br", "a"},
        attributes={"a": ["href", "title"]},
        protocols={"https", "http"},
        strip=True,
    )


def _spreadsheet_text(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value

# ============ SPONSORLUK SABİTLERİ ============
# Instagram sponsorlu içerik benchmark'ı: bin gösterim başı yaklaşık maliyet
DEFAULT_CPM_TL = 30.0

TIME_RANGE_OPTIONS = [
    {"value": 7, "label": "Son 1 Hafta"},
    {"value": 30, "label": "Son 1 Ay"},
    {"value": 90, "label": "Son 3 Ay"},
    {"value": 180, "label": "Son 6 Ay"},
    {"value": 365, "label": "Son 1 Yıl"},
    {"value": 1095, "label": "Son 3 Yıl"},
]

PLATFORM_TIME_RANGES = {
    "instagram": TIME_RANGE_OPTIONS,
    "tiktok": TIME_RANGE_OPTIONS,
    "youtube": [
        {"value": 180, "label": "Son 6 Ay"},
        {"value": 365, "label": "Son 1 Yıl"},
        {"value": 1095, "label": "Son 3 Yıl"},
        {"value": 2555, "label": "Son 7 Yıl"},
    ],
}
SUPPORTED_PLATFORMS = {"instagram", "tiktok", "youtube"}
PLATFORM_LABELS = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube"}

# Kendi markamız
OUR_BRAND_USERNAME = ""

# Hızlı analiz markaları
QUICK_BRANDS = []

PAGE_TYPE_PROFILES = {
    "influencer": {
        "label": "Influencer",
        "description": "Kişisel güven ve ikna etkisi yüksek creator hesabı.",
        "awareness_mult": 1.10,
        "trust_mult": 1.18,
        "conversion_mult": 1.16,
        "engagement_mult": 1.08,
        "follower_confidence_mult": 1.02,
        "format_bias": {"image": 1.00, "carousel": 1.02, "reels": 1.10, "story": 0.95, "ugc_reel": 1.12},
        "integration_bias": {"product_placement": 0.96, "soft_mention": 0.93, "dedicated": 1.12, "review": 1.14, "launch": 1.06},
        "pricing_note": "Bu tip hesaplarda güven ve satın alma niyeti güçlü olduğu için dedicated ve review işlerinde değer artar.",
    },
    "meme": {
        "label": "Meme Sayfası",
        "description": "Erişim ve viral dağılımı yüksek, güven etkisi daha düşük eğlence odaklı sayfa.",
        "awareness_mult": 1.22,
        "trust_mult": 0.82,
        "conversion_mult": 0.80,
        "engagement_mult": 1.05,
        "follower_confidence_mult": 0.98,
        "format_bias": {"image": 1.00, "carousel": 0.96, "reels": 1.14, "story": 1.02, "ugc_reel": 0.92},
        "integration_bias": {"product_placement": 1.01, "soft_mention": 1.04, "dedicated": 0.86, "review": 0.82, "launch": 0.96},
        "pricing_note": "Meme sayfaları hızlı görünürlük verir; ancak doğrudan satış ve güven katkısı influencer kadar yüksek fiyatlanmaz.",
    },
    "community": {
        "label": "Topluluk Sayfası",
        "description": "Sadık takipçi tabanı olan, yorum kalitesi ve aidiyet hissi güçlü sayfa.",
        "awareness_mult": 1.05,
        "trust_mult": 1.10,
        "conversion_mult": 1.08,
        "engagement_mult": 1.08,
        "follower_confidence_mult": 1.00,
        "format_bias": {"image": 0.98, "carousel": 1.06, "reels": 1.00, "story": 0.94, "ugc_reel": 1.04},
        "integration_bias": {"product_placement": 0.98, "soft_mention": 0.96, "dedicated": 1.05, "review": 1.08, "launch": 1.02},
        "pricing_note": "Topluluk sayfalarında yorum kalitesi ve aidiyet yüksek olduğu için carousel ve review paketleri daha anlamlıdır.",
    },
    "local": {
        "label": "Yerel / Şehir Sayfası",
        "description": "Lokal görünürlük ve bölgesel güven yaratan şehir veya semt odaklı hesap.",
        "awareness_mult": 0.98,
        "trust_mult": 1.08,
        "conversion_mult": 1.10,
        "engagement_mult": 1.02,
        "follower_confidence_mult": 1.00,
        "format_bias": {"image": 1.02, "carousel": 1.04, "reels": 0.98, "story": 1.00, "ugc_reel": 0.98},
        "integration_bias": {"product_placement": 1.00, "soft_mention": 0.98, "dedicated": 1.00, "review": 1.04, "launch": 1.10},
        "pricing_note": "Yerel sayfalarda bölgesel hedefleme avantajı olduğu için açılış ve duyuru kampanyaları ekstra değer taşır.",
    },
    "niche": {
        "label": "Niş İçerik Sayfası",
        "description": "Belirli bir ilgi alanında derin takipçi bağı kuran hedefli içerik hesabı.",
        "awareness_mult": 1.00,
        "trust_mult": 1.16,
        "conversion_mult": 1.18,
        "engagement_mult": 1.06,
        "follower_confidence_mult": 1.01,
        "format_bias": {"image": 1.00, "carousel": 1.08, "reels": 1.02, "story": 0.94, "ugc_reel": 1.06},
        "integration_bias": {"product_placement": 1.02, "soft_mention": 0.96, "dedicated": 1.06, "review": 1.12, "launch": 1.00},
        "pricing_note": "Niş sayfalar daha hedefli kitle taşıdığı için dönüşüm potansiyeli yüksek işlerde primlenir.",
    },
    "media": {
        "label": "Medya / Haber Sayfası",
        "description": "Hızlı erişim sağlayan ama kişisel tavsiye gücü sınırlı yayın tipi hesap.",
        "awareness_mult": 1.18,
        "trust_mult": 0.94,
        "conversion_mult": 0.88,
        "engagement_mult": 0.98,
        "follower_confidence_mult": 1.01,
        "format_bias": {"image": 1.02, "carousel": 1.00, "reels": 1.08, "story": 0.98, "ugc_reel": 0.90},
        "integration_bias": {"product_placement": 0.98, "soft_mention": 0.95, "dedicated": 0.92, "review": 0.86, "launch": 1.08},
        "pricing_note": "Medya tipi hesaplar awareness tarafında güçlüdür; doğal deneyim ve samimiyet gerektiren işlerde prim daha düşüktür.",
    },
    "curation": {
        "label": "Tematik Kürasyon Sayfası",
        "description": "Belirli estetik veya tema etrafında içerik toplayan kürasyon hesabı.",
        "awareness_mult": 1.08,
        "trust_mult": 0.96,
        "conversion_mult": 0.94,
        "engagement_mult": 1.00,
        "follower_confidence_mult": 0.99,
        "format_bias": {"image": 1.04, "carousel": 1.10, "reels": 0.96, "story": 0.92, "ugc_reel": 0.94},
        "integration_bias": {"product_placement": 1.00, "soft_mention": 0.97, "dedicated": 0.95, "review": 0.92, "launch": 0.98},
        "pricing_note": "Kurasyon sayfaları estetik uyuma duyarlıdır; görsel uyumu yüksek ürün yerleştirmeler daha verimli fiyatlanır.",
    },
    "youth": {
        "label": "Kampüs / Gençlik Sayfası",
        "description": "Hızlı trend tüketen genç kitleye erişen sosyal kampüs veya öğrenci odaklı sayfa.",
        "awareness_mult": 1.12,
        "trust_mult": 0.92,
        "conversion_mult": 0.93,
        "engagement_mult": 1.07,
        "follower_confidence_mult": 0.99,
        "format_bias": {"image": 0.96, "carousel": 0.98, "reels": 1.12, "story": 1.04, "ugc_reel": 1.02},
        "integration_bias": {"product_placement": 0.99, "soft_mention": 1.02, "dedicated": 0.94, "review": 0.90, "launch": 1.04},
        "pricing_note": "Gençlik sayfaları trend yayılımında güçlüdür; reels ve story bazlı işler daha yüksek ağırlıkla değerlenir.",
    },
}

SPONSORSHIP_PACKAGES = [
    {
        "id": "image_product",
        "label": "Tek Görsel - Product Placement",
        "description": "Ürün içerik akışına doğal şekilde yerleştirilir; marka görünür ama içerik tamamen reklam gibi hissettirmez.",
        "format_key": "image",
        "format_label": "Tek Görsel",
        "integration_key": "product_placement",
        "integration_label": "Product Placement",
        "anchor_bias": 0.92,
        "format_mult": 0.94,
        "integration_mult": 0.92,
        "awareness_weight": 0.55,
        "trust_weight": 0.20,
        "conversion_weight": 0.25,
    },
    {
        "id": "image_dedicated",
        "label": "Tek Görsel - Dedicated",
        "description": "Paylaşım doğrudan markaya ayrılır; mesaj net olduğu için güven ve niyet etkisi artar.",
        "format_key": "image",
        "format_label": "Tek Görsel",
        "integration_key": "dedicated",
        "integration_label": "Dedicated",
        "anchor_bias": 1.01,
        "format_mult": 1.02,
        "integration_mult": 1.10,
        "awareness_weight": 0.35,
        "trust_weight": 0.40,
        "conversion_weight": 0.25,
    },
    {
        "id": "carousel_product",
        "label": "Carousel - Product Placement",
        "description": "Birden fazla kare ile ürün hikâyesi daha doğal şekilde anlatılır; kaydırma davranışı nedeniyle dikkat süresi yüksektir.",
        "format_key": "carousel",
        "format_label": "Carousel",
        "integration_key": "product_placement",
        "integration_label": "Product Placement",
        "anchor_bias": 0.97,
        "format_mult": 1.03,
        "integration_mult": 0.95,
        "awareness_weight": 0.45,
        "trust_weight": 0.25,
        "conversion_weight": 0.30,
    },
    {
        "id": "carousel_dedicated",
        "label": "Carousel - Dedicated",
        "description": "Kare kare anlatım sayesinde marka mesajı daha detaylı aktarılır; özellikle topluluk ve niş sayfalarda etkilidir.",
        "format_key": "carousel",
        "format_label": "Carousel",
        "integration_key": "dedicated",
        "integration_label": "Dedicated",
        "anchor_bias": 1.05,
        "format_mult": 1.09,
        "integration_mult": 1.08,
        "awareness_weight": 0.35,
        "trust_weight": 0.35,
        "conversion_weight": 0.30,
    },
    {
        "id": "reels_soft",
        "label": "Video / Reels - Soft Mention",
        "description": "Marka akışın içinde geçici veya yumuşak bir şekilde görünür; izlenme odaklı kampanyalarda idealdir.",
        "format_key": "reels",
        "format_label": "Video / Reels",
        "integration_key": "soft_mention",
        "integration_label": "Soft Mention",
        "anchor_bias": 0.91,
        "format_mult": 1.15,
        "integration_mult": 1.00,
        "awareness_weight": 0.52,
        "trust_weight": 0.20,
        "conversion_weight": 0.28,
    },
    {
        "id": "reels_dedicated",
        "label": "Video / Reels - Dedicated",
        "description": "Tamamen markaya odaklı reels veya video; hem erişim hem de ikna etkisini birlikte fiyatlar.",
        "format_key": "reels",
        "format_label": "Video / Reels",
        "integration_key": "dedicated",
        "integration_label": "Dedicated",
        "anchor_bias": 1.04,
        "format_mult": 1.28,
        "integration_mult": 1.12,
        "awareness_weight": 0.36,
        "trust_weight": 0.34,
        "conversion_weight": 0.30,
    },
    {
        "id": "story_launch",
        "label": "3 Story Frame - Lansman / Duyuru",
        "description": "Kısa ömürlü ama hızlı aksiyon yaratan story seti; kampanya ve açılış iletişimlerinde kullanılır.",
        "format_key": "story",
        "format_label": "Story Seti",
        "integration_key": "launch",
        "integration_label": "Lansman",
        "format_mult": 0.72,
        "integration_mult": 0.96,
        "awareness_weight": 0.52,
        "trust_weight": 0.18,
        "conversion_weight": 0.30,
    },
    {
        "id": "ugc_review_reel",
        "label": "UGC Tarzı Deneyim Reels'i",
        "description": "Kullanıcı deneyimi gibi görünen, daha samimi anlatımlı review reels; güven ve niyet etkisi yüksektir.",
        "format_key": "ugc_reel",
        "format_label": "UGC Reels",
        "integration_key": "review",
        "integration_label": "Review",
        "anchor_bias": 1.18,
        "allowed_page_types": ["influencer", "community", "niche", "local", "youth"],
        "format_mult": 1.24,
        "integration_mult": 1.14,
        "awareness_weight": 0.30,
        "trust_weight": 0.34,
        "conversion_weight": 0.36,
    },
]

FORMAT_FALLBACKS = {
    "image": {"views_mult": 0.84, "likes_mult": 0.90, "comments_mult": 0.88},
    "carousel": {"views_mult": 0.96, "likes_mult": 1.00, "comments_mult": 1.05},
    "reels": {"views_mult": 1.18, "likes_mult": 0.92, "comments_mult": 0.95},
    "story": {"views_mult": 0.38, "likes_mult": 0.22, "comments_mult": 0.12},
    "ugc_reel": {"views_mult": 1.24, "likes_mult": 0.98, "comments_mult": 1.08},
}


def fmt(n):
    """Sayı formatlama: 1500 → '1.5K', 1200000 → '1.2M'"""
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,}".replace(",", ".")


def extract_username(input_text):
    input_text = input_text.strip().rstrip("/")
    match = re.match(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)", input_text)
    if match:
        u = match.group(1).lower()
        if u in ("accounts", "explore", "reels", "stories", "direct", "p", "reel"):
            return None
        return u
    clean = input_text.lstrip("@").lower()
    if re.match(r"^[a-z0-9_.]+$", clean) and len(clean) <= 30:
        return clean
    return None


def _get_session():
    """Session cookie'lerini diskten oku."""
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return None


def _write_session(data):
    """Atomically write the local credential file with owner-only permissions."""
    descriptor, temporary = tempfile.mkstemp(prefix=".session-", dir=DATA_DIR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.chmod(temporary, 0o600)
        os.replace(temporary, SESSION_FILE)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


class InstagramFetchError(str):
    """User-facing Instagram error with a stable API classification."""

    def __new__(
        cls,
        message,
        code="instagram_unavailable",
        http_status=503,
        suggest_login=False,
        retryable=False,
        retry_after=None,
    ):
        value = str.__new__(cls, message)
        value.code = code
        value.http_status = http_status
        value.suggest_login = suggest_login
        value.retryable = retryable
        value.retry_after = retry_after
        return value


def _save_session(username, cookies, validation_status=None):
    existing = _get_session() or {}
    same_session = (
        existing.get("cookies", {}).get("sessionid")
        and existing.get("cookies", {}).get("sessionid") == cookies.get("sessionid")
    )
    if validation_status is None:
        validation_status = existing.get("validation_status", "unknown") if same_session else "unknown"

    validated_at = existing.get("validated_at") if same_session else None
    if validation_status == "valid":
        validated_at = datetime.utcnow().isoformat()

    data = {
        "username": username,
        "cookies": cookies,
        "ts": datetime.utcnow().isoformat(),
        "validation_status": validation_status,
        "validated_at": validated_at,
    }
    _write_session(data)


def _set_session_validation(validation_status, expected_sessionid=None):
    session_data = _get_session()
    if not session_data:
        return False
    current_sessionid = session_data.get("cookies", {}).get("sessionid")
    if expected_sessionid and current_sessionid != expected_sessionid:
        return False
    session_data["validation_status"] = validation_status
    session_data["validated_at"] = datetime.utcnow().isoformat()
    _write_session(session_data)
    return True


def _get_normalized_avg(data_list):
    """Aykırı değerleri (outlier) filtreleyerek daha sağlıklı bir ortalama döndürür."""
    if not data_list:
        return 0
    if len(data_list) < 4:
        return sum(data_list) / len(data_list)
        
    sorted_data = sorted(data_list)
    q1 = sorted_data[len(sorted_data)//4]
    q3 = sorted_data[3*len(sorted_data)//4]
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    
    filtered = [x for x in data_list if lower <= x <= upper]
    if not filtered:
        return sum(data_list) / len(data_list)
    return sum(filtered) / len(filtered)

def _calculate_comment_quality(comments_list):
    """Yapay zeka kullanmadan yorumların kalitesini sezgisel olarak puanlar.
    Dönen skor 0 ile 100 arasındadır."""
    return _analyze_comment_quality(comments_list).get("score", 50)


def _extract_comment_keywords(text, limit=8):
    stop_words = {
        "bir", "ve", "ile", "ama", "fakat", "çok", "cok", "şu", "su", "bu", "şey", "sey", "için", "icin",
        "gibi", "olan", "olarak", "bence", "sanki", "yine", "bana", "sana", "biz", "siz", "ben", "sen",
        "da", "de", "mi", "mı", "mu", "mü", "ki", "ya", "veya", "hem", "daha", "en", "biraz",
    }
    tokens = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]{3,}", text.lower())
    filtered = [t for t in tokens if t not in stop_words]
    return [word for word, _ in Counter(filtered).most_common(limit)]


def _analyze_comment_quality(comments_list, context_text=""):
    if not comments_list:
        return {
            "score": 50,
            "sample_count": 0,
            "source": "empty",
            "summary": "Yorum bulunamadığı için skor nötr tutuldu.",
            "drivers": ["Yeterli yorum örneği olmadığı için sistem nötr başlangıç değeri kullandı."],
            "breakdown": {
                "derinlik": 50,
                "özgünlük": 50,
                "bağlam": 50,
                "niyet": 50,
                "duygu": 50,
                "spam_riski": 50,
            },
            "ratios": {},
        }

    positive_words = {
        "harika", "mükemmel", "güzel", "bayıldım", "süper", "iyi", "tebrikler", "başarılar", "lezzetli",
        "efsane", "kaliteli", "seviyorum", "başarılı", "muhteşem", "favorim", "şahane", "hızlı", "temiz"
    }
    negative_words = {
        "kötü", "berbat", "iğrenç", "şikayet", "pahalı", "bozuk", "rezalet", "beğenmedim", "vasat",
        "geç", "sorun", "soğuk", "eksik", "yakışmadı", "olmamış", "kırık"
    }
    generic_comments = {
        "harika", "süper", "mükemmel", "efsane", "çok iyi", "çok güzel", "muthis", "müthiş", "guzel", "iyi"
    }
    question_cues = {"nasıl", "nasil", "nerede", "hangi", "kaç", "kac", "fiyat", "link", "stok", "var", "ne zaman"}
    experience_cues = {
        "denedim", "kullandım", "aldım", "ictim", "içtim", "geldim", "geldik", "deneyeceğim", "tavsiye",
        "öneririm", "oneririm", "bekliyorum", "istiyorum", "sevdim", "bayıldım", "alıyorum"
    }
    alpha_pattern = re.compile(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]")
    repeated_pattern = re.compile(r"(.)\1{3,}")
    context_keywords = set(_extract_comment_keywords(context_text, limit=10))

    normalized_comments = []
    token_lengths = []
    meaningful_count = 0
    positive_count = 0
    negative_count = 0
    question_count = 0
    experience_count = 0
    relevance_count = 0
    generic_count = 0
    emoji_only_count = 0
    spam_count = 0

    for raw_comment in comments_list[:40]:
        comment = re.sub(r"\s+", " ", str(raw_comment or "")).strip()
        if not comment:
            continue

        lowered = comment.lower()
        tokens = re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]{2,}", lowered)
        token_lengths.append(len(tokens))
        normalized = " ".join(tokens)
        normalized_comments.append(normalized or lowered)

        has_alpha = bool(alpha_pattern.search(comment))
        emoji_only = not has_alpha
        if emoji_only:
            emoji_only_count += 1

        generic = normalized in generic_comments or (len(tokens) <= 2 and normalized in generic_comments)
        if generic:
            generic_count += 1

        question_signal = "?" in comment or any(cue in lowered for cue in question_cues)
        experience_signal = any(cue in lowered for cue in experience_cues)
        relevance_signal = bool(context_keywords.intersection(tokens)) if context_keywords else len(tokens) >= 4
        positive_signal = any(word in lowered for word in positive_words)
        negative_signal = any(word in lowered for word in negative_words)

        mentions = lowered.count("@")
        hashtags = lowered.count("#")
        url_signal = "http" in lowered or "www." in lowered
        repeated_signal = bool(repeated_pattern.search(lowered))
        very_short = len(tokens) == 0 or len(comment) < 4
        spam_signal = emoji_only or url_signal or repeated_signal or mentions >= 3 or hashtags >= 4 or very_short

        if len(tokens) >= 3 or len(comment) >= 18 or question_signal or experience_signal:
            meaningful_count += 1
        if positive_signal:
            positive_count += 1
        if negative_signal:
            negative_count += 1
        if question_signal:
            question_count += 1
        if experience_signal:
            experience_count += 1
        if relevance_signal:
            relevance_count += 1
        if spam_signal:
            spam_count += 1

    sample_count = max(len(normalized_comments), 1)
    duplicate_ratio = 1.0 - (len(set(normalized_comments)) / sample_count)
    meaningful_ratio = meaningful_count / sample_count
    positive_ratio = positive_count / sample_count
    negative_ratio = negative_count / sample_count
    question_ratio = question_count / sample_count
    experience_ratio = experience_count / sample_count
    relevance_ratio = relevance_count / sample_count
    generic_ratio = generic_count / sample_count
    emoji_only_ratio = emoji_only_count / sample_count
    spam_ratio = spam_count / sample_count
    avg_token_length = _mean(token_lengths)
    sample_coverage = min(sample_count / 24.0, 1.0)

    depth_score = _clamp((avg_token_length / 7.0) * 55 + (meaningful_ratio * 45), 12, 100)
    uniqueness_score = _clamp((1.0 - duplicate_ratio) * 82 + (meaningful_ratio * 18), 10, 100)
    relevance_score = _clamp((relevance_ratio * 70) + (experience_ratio * 30), 8, 100)
    intent_score = _clamp((question_ratio * 40) + (experience_ratio * 40) + (meaningful_ratio * 20), 10, 100)
    sentiment_score = _clamp(52 + (positive_ratio * 30) - (negative_ratio * 42), 5, 100)
    spam_health_score = _clamp(100 - ((spam_ratio * 52) + (generic_ratio * 22) + (emoji_only_ratio * 18) + (duplicate_ratio * 20)), 0, 100)

    weighted_score = (
        (depth_score * 0.21) +
        (uniqueness_score * 0.18) +
        (relevance_score * 0.19) +
        (intent_score * 0.17) +
        (sentiment_score * 0.12) +
        (spam_health_score * 0.13)
    )
    weighted_score = (weighted_score * 0.92) + (sample_coverage * 8)
    score = round(_clamp(weighted_score, 18, 96))

    drivers = []
    if meaningful_ratio >= 0.55:
        drivers.append("Yorumların önemli bölümü kısa tepki yerine cümle kuran, açıklayıcı içerik taşıyor.")
    if question_ratio >= 0.18:
        drivers.append("Yorumlarda soru ve bilgi isteme oranı yüksek; bu, gerçek ilgi sinyali sayılır.")
    if experience_ratio >= 0.18:
        drivers.append("Kullanım deneyimi veya tavsiye içeren yorumlar görülüyor; ticari ikna etkisi artar.")
    if duplicate_ratio >= 0.28:
        drivers.append("Yorum tekrar oranı yükseliyor; bu durum kaliteyi aşağı çeker.")
    if spam_ratio >= 0.20 or emoji_only_ratio >= 0.18:
        drivers.append("Emoji ağırlıklı veya çok kısa yorum oranı yüksek olduğu için spam riski artıyor.")
    if relevance_ratio >= 0.28:
        drivers.append("Yorumlar içerikte geçen konu ve kelimelere temas ediyor; bağlam uyumu güçlü.")
    if not drivers:
        drivers.append("Yorum yapısı dengeli ancak güçlü kalite farkı yaratacak kadar belirgin sinyal üretmiyor.")

    summary = (
        f"Skor {score}/100. Derinlik {round(depth_score)}/100, özgünlük {round(uniqueness_score)}/100, "
        f"bağlam uyumu {round(relevance_score)}/100 ve spam sağlığı {round(spam_health_score)}/100 seviyesinde."
    )

    return {
        "score": score,
        "sample_count": sample_count,
        "source": "comments",
        "summary": summary,
        "drivers": drivers[:4],
        "breakdown": {
            "derinlik": round(depth_score),
            "özgünlük": round(uniqueness_score),
            "bağlam": round(relevance_score),
            "niyet": round(intent_score),
            "duygu": round(sentiment_score),
            "spam_riski": round(spam_health_score),
        },
        "ratios": {
            "anlamlı_yorum_oranı": round(meaningful_ratio, 3),
            "tekrar_oranı": round(duplicate_ratio, 3),
            "soru_oranı": round(question_ratio, 3),
            "deneyim_oranı": round(experience_ratio, 3),
            "bağlam_oranı": round(relevance_ratio, 3),
            "spam_oranı": round(spam_ratio, 3),
        },
    }


def _get_comment_quality_for_shortcode(shortcode):
    try:
        comments = _fetch_comments_v1(shortcode, max_comments=20)
        if not comments:
            return 50
        return _calculate_comment_quality(comments)
    except Exception:
        return 50


def _estimate_comment_quality_from_post_signals(posts):
    usable_posts = [p for p in (posts or []) if (p.get("likes", 0) or 0) >= 0]
    if not usable_posts:
        return {
            "score": 50,
            "sample_count": 0,
            "source": "post_signals",
            "summary": "Yorum metinleri alınamadığında skor, etkileşim yapısındaki post sinyallerinden tahmin edilir.",
            "drivers": [],
            "breakdown": {
                "derinlik": 50,
                "özgünlük": 50,
                "bağlam": 50,
                "niyet": 50,
                "duygu": 50,
                "spam_riski": 50,
            },
            "ratios": {},
        }

    comments_values = [max(p.get("comments", 0) or 0, 0) for p in usable_posts]
    likes_values = [max(p.get("likes", 0) or 0, 0) for p in usable_posts]
    view_values = [_estimate_effective_views(p) for p in usable_posts]

    avg_comments = _get_normalized_avg(comments_values)
    avg_likes = _get_normalized_avg(likes_values)
    avg_views = max(_get_normalized_avg(view_values), 1)
    comment_per_1k_views = (avg_comments / avg_views) * 1000
    comment_like_ratio = avg_comments / max(avg_likes, 1)
    comments_consistency = _clamp(100 - (_coefficient_of_variation(comments_values) * 42), 36, 88)
    likes_consistency = _clamp(100 - (_coefficient_of_variation(likes_values) * 34), 40, 90)
    active_comment_share = sum(1 for p in usable_posts if (p.get("comments", 0) or 0) >= 6) / max(len(usable_posts), 1)

    depth_score = _clamp(34 + min(comment_per_1k_views, 4.2) * 7.5 + active_comment_share * 18, 34, 90)
    uniqueness_score = _clamp((comments_consistency * 0.62) + (likes_consistency * 0.24) + 8, 38, 90)
    relevance_score = _clamp(40 + min(comment_like_ratio, 0.11) * 220 + active_comment_share * 12, 36, 88)
    intent_score = _clamp(38 + min(comment_like_ratio, 0.10) * 260 + min(avg_comments, 45) * 0.35, 35, 92)
    sentiment_score = _clamp(48 + min(comment_like_ratio, 0.08) * 140 + active_comment_share * 8, 44, 82)
    spam_health_score = _clamp((comments_consistency * 0.55) + 24 - max(comment_per_1k_views - 4.8, 0) * 3, 42, 86)

    weighted_score = (
        (depth_score * 0.21) +
        (uniqueness_score * 0.18) +
        (relevance_score * 0.19) +
        (intent_score * 0.17) +
        (sentiment_score * 0.12) +
        (spam_health_score * 0.13)
    )
    score = round(_clamp(weighted_score, 42, 84))

    drivers = []
    if comment_like_ratio >= 0.045:
        drivers.append("Beğeniye kıyasla yorum oranı yüksek; bu genelde daha niyetli bir topluluğa işaret eder.")
    if active_comment_share >= 0.40:
        drivers.append("Gönderilerin önemli bölümünde anlamlı yorum hacmi görülüyor; skor nötrde bırakılmadı.")
    if comments_consistency < 52:
        drivers.append("Yorum hacmi postlar arasında çok dalgalı olduğu için skor yukarı taşınmadı.")

    return {
        "score": score,
        "sample_count": len(usable_posts),
        "source": "post_signals",
        "summary": "Yorum metinleri alınamadığında skor, yorum yoğunluğu ve etkileşim istikrarı gibi post sinyallerinden tahmin edilir.",
        "drivers": drivers,
        "breakdown": {
            "derinlik": round(depth_score),
            "özgünlük": round(uniqueness_score),
            "bağlam": round(relevance_score),
            "niyet": round(intent_score),
            "duygu": round(sentiment_score),
            "spam_riski": round(spam_health_score),
        },
        "ratios": {
            "yorum_1000_goruntuleme": round(comment_per_1k_views, 3),
            "yorum_begeni_orani": round(comment_like_ratio, 3),
            "aktif_yorumlu_gonderi_orani": round(active_comment_share, 3),
        },
    }


def _get_comment_quality_analysis_for_posts(posts):
    candidate_posts = [
        p for p in (posts or [])
        if p.get("shortcode") and ((p.get("comments", 0) or 0) > 0 or (p.get("likes", 0) or 0) > 0)
    ]
    candidate_posts = sorted(
        candidate_posts,
        key=lambda p: (p.get("comments", 0) * 6) + (p.get("likes", 0) or 0),
        reverse=True,
    )[:5]

    collected_comments = []
    context_parts = []
    for post in candidate_posts:
        comments = _fetch_comments_v1(post["shortcode"], max_comments=24)
        if comments:
            collected_comments.extend(comments)
            if post.get("caption"):
                context_parts.append(post["caption"])

    if len(collected_comments) >= 6:
        analysis = _analyze_comment_quality(collected_comments[:60], " ".join(context_parts))
        analysis["source"] = "live_comments"
        return analysis

    return _estimate_comment_quality_from_post_signals(posts)

def _fetch_real_csrftoken(sessionid):
    """Instagram'dan gerçek bir csrftoken alır (data/shared_data/ endpoint'i üzerinden)."""
    import requests as rq
    try:
        tmp = rq.Session()
        # requests, HTTP(S)_PROXY ortam değişkenlerini otomatik kullanır. Yerelde
        # kalmış/kapalı bir proxy Instagram'a ulaşılmadan ProxyError üretir.
        # Instagram için doğrudan bağlantı varsayılandır; gerçekten proxy
        # gerekiyorsa INSTAGRAM_TRUST_ENV_PROXY=1 ile tekrar etkinleştirilebilir.
        tmp.trust_env = os.getenv("INSTAGRAM_TRUST_ENV_PROXY", "0").lower() in ("1", "true", "yes")
        tmp.headers.update({
            "User-Agent": INSTAGRAM_USER_AGENT,
        })
        tmp.cookies.set("sessionid", sessionid, domain=".instagram.com")
        r = tmp.get("https://www.instagram.com/data/shared_data/", timeout=10)
        ct = tmp.cookies.get("csrftoken")
        if ct:
            return ct
    except Exception:
        pass
    return None

def _make_ig_session(cookies_dict, fetch_csrf=True):
    """Instagram API çağrıları için requests session oluşturur. csrftoken yoksa otomatik olarak alır."""
    import requests as rq
    # csrftoken eksikse Instagram'dan gerçek bir tane al
    if fetch_csrf and not cookies_dict.get("csrftoken") and cookies_dict.get("sessionid"):
        real_token = _fetch_real_csrftoken(cookies_dict["sessionid"])
        if real_token:
            cookies_dict["csrftoken"] = real_token
            # Kalıcı olarak kaydet
            try:
                sd = _get_session()
                if sd and sd.get("cookies"):
                    sd["cookies"]["csrftoken"] = real_token
                    _save_session(sd.get("username", "user"), sd["cookies"])
            except Exception:
                pass

    s = rq.Session()
    # Bozuk global HTTP(S)_PROXY ayarlarının Instagram çağrılarını yanlışlıkla
    # localhost gibi erişilemeyen bir proxy'ye göndermesini engelle.
    s.trust_env = os.getenv("INSTAGRAM_TRUST_ENV_PROXY", "0").lower() in ("1", "true", "yes")
    s.headers.update({
        "User-Agent": INSTAGRAM_USER_AGENT,
        "X-IG-App-ID": "936619743392459",
        "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Ch-Ua": f'"Not_A Brand";v="99", "Chromium";v="{INSTAGRAM_BROWSER_MAJOR}", "Google Chrome";v="{INSTAGRAM_BROWSER_MAJOR}"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    })
    for name, value in cookies_dict.items():
        s.cookies.set(name, value, domain=".instagram.com")
    if cookies_dict.get("csrftoken"):
        s.headers["X-CSRFToken"] = cookies_dict["csrftoken"]
    return s


def _instagram_connection_error_message(exc):
    """requests bağlantı hatalarını güvenli ve kullanıcıya anlaşılır biçimde sınıflandırır."""
    import requests as rq

    if isinstance(exc, rq.exceptions.ProxyError):
        return (
            "Instagram bağlantısı sistem proxy ayarı tarafından engellendi. "
            "Proxy ayarınızı kontrol edin veya INSTAGRAM_TRUST_ENV_PROXY=0 kullanın."
        )
    if isinstance(exc, (rq.exceptions.ConnectTimeout, rq.exceptions.ReadTimeout)):
        return "Instagram zamanında yanıt vermedi. Lütfen kısa bir süre sonra tekrar deneyin."
    if isinstance(exc, rq.exceptions.SSLError):
        return "Instagram ile güvenli HTTPS bağlantısı kurulamadı. Sistem saati ve sertifikaları kontrol edin."
    if isinstance(exc, rq.exceptions.ConnectionError):
        return "Instagram'a ağ bağlantısı kurulamadı. İnternet, DNS veya güvenlik duvarı ayarlarını kontrol edin."
    return "Instagram profil bilgisi alınırken beklenmeyen bir bağlantı hatası oluştu."


def _instagram_response_message(response):
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    return str(message).strip()[:240] if message else ""


def _instagram_message_is_rate_limited(message):
    text = str(message or "").casefold()
    return any(token in text for token in (
        "please wait",
        "try again later",
        "too many requests",
        "rate limit",
        "birka\u00e7 dakika bekle",
        "tekrar denemeden \u00f6nce",
        "\u00e7ok fazla istek",
    ))


def _validate_instagram_session(cookies_dict):
    """Validate a stored cookie with one request and no login redirects."""
    if not cookies_dict or not cookies_dict.get("sessionid"):
        return {
            "state": "invalid",
            "username": None,
            "message": "Session cookie bulunamad\u0131.",
            "upstream_status": None,
            "http_status": 401,
        }

    session = _make_ig_session(dict(cookies_dict), fetch_csrf=False)
    try:
        response = session.get(
            "https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
            allow_redirects=False,
            timeout=(5, 15),
        )
    except Exception as exc:
        return {
            "state": "transient",
            "username": None,
            "message": _instagram_connection_error_message(exc),
            "upstream_status": None,
            "http_status": 502,
        }

    status = response.status_code
    location = str(response.headers.get("Location") or "")
    message = _instagram_response_message(response)
    if status == 200:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict) and isinstance(payload, dict):
            data = payload.get("data")
            user = data.get("user") if isinstance(data, dict) else None
        username = user.get("username") if isinstance(user, dict) else None
        if username:
            return {
                "state": "valid",
                "username": username,
                "message": "",
                "upstream_status": status,
                "http_status": 200,
            }
        return {
            "state": "transient",
            "username": None,
            "message": "Instagram oturum do\u011frulama yan\u0131t\u0131 kullan\u0131c\u0131 bilgisi i\u00e7ermiyor.",
            "upstream_status": status,
            "http_status": 502,
        }

    if status == 429 or _instagram_message_is_rate_limited(message):
        state = "rate_limited"
        http_status = 429
    elif status in (301, 302, 303, 307, 308) and "/accounts/login" in location:
        state = "invalid"
        http_status = 401
    elif status in (401, 403):
        state = "invalid"
        http_status = 401
    elif status >= 500:
        state = "transient"
        http_status = 503
    else:
        state = "transient"
        http_status = 502

    return {
        "state": state,
        "username": None,
        "message": message,
        "upstream_status": status,
        "http_status": http_status,
    }


def _classify_instagram_auth_failure(cookies_dict, response=None):
    upstream_message = _instagram_response_message(response) if response is not None else ""
    if response is not None and (
        response.status_code == 429 or _instagram_message_is_rate_limited(upstream_message)
    ):
        return InstagramFetchError(
            "Instagram ge\u00e7ici olarak \u00e7ok fazla istek alg\u0131lad\u0131. "
            "Birka\u00e7 dakika bekleyip yeniden deneyin; oturumunuz silinmedi.",
            code="rate_limited",
            http_status=429,
            retryable=True,
            retry_after=60,
        )

    validation = _validate_instagram_session(cookies_dict)
    state = validation.get("state")
    expected_sessionid = (cookies_dict or {}).get("sessionid")

    if state == "invalid":
        _set_session_validation("invalid", expected_sessionid=expected_sessionid)
        return InstagramFetchError(
            "Instagram mevcut oturumu reddetti. Ayn\u0131 a\u011f ve taray\u0131c\u0131dan "
            "yeni bir session cookie ile tekrar giri\u015f yap\u0131n.",
            code="auth_required",
            http_status=401,
            suggest_login=True,
        )

    if state == "valid":
        _set_session_validation("valid", expected_sessionid=expected_sessionid)
        return InstagramFetchError(
            "Oturum ge\u00e7erli, ancak Instagram profil eri\u015fimini ge\u00e7ici olarak k\u0131s\u0131tlad\u0131. "
            "Birka\u00e7 dakika bekleyip yeniden deneyin.",
            code="rate_limited",
            http_status=429,
            retryable=True,
            retry_after=60,
        )

    if state == "rate_limited":
        return InstagramFetchError(
            "Instagram ge\u00e7ici olarak \u00e7ok fazla istek alg\u0131lad\u0131 (HTTP 429). "
            "Birka\u00e7 dakika bekleyip yeniden deneyin.",
            code="rate_limited",
            http_status=429,
            retryable=True,
            retry_after=60,
        )

    message = validation.get("message") or "Instagram oturumu \u015fu anda do\u011frulanamad\u0131."
    return InstagramFetchError(
        message,
        code="instagram_unavailable",
        http_status=int(validation.get("http_status") or 503),
        retryable=True,
    )


def _ig_get_with_retry(session, url, max_retries=3, **kwargs):
    """429 hatalarında exponential backoff ile retry yapar."""
    import time
    kwargs.setdefault("timeout", 20)
    for attempt in range(max_retries):
        resp = session.get(url, **kwargs)
        if resp.status_code != 429:
            return resp
        wait = (attempt + 1) * 5   # 5s, 10s, 15s
        time.sleep(wait)
    return resp


def _fetch_comments_v1(shortcode, max_comments=100):
    """v1 API üzerinden bir gönderinin yorumlarını çeker (Instaloader graphql yerine)."""
    import time, random
    sd = _get_session()
    if not sd or not sd.get("cookies", {}).get("sessionid"):
        return []
    
    s = _make_ig_session(sd.get("cookies", {}))
    comments = []
    
    try:
        # Shortcode'dan media_id'yi çöz
        r = s.get(f"https://www.instagram.com/api/v1/media/by/code/{shortcode}/", timeout=15)
        if r.status_code != 200:
            return []
        media_id = r.json().get("id", "")
        if not media_id:
            return []
        
        # Yorumları çek
        min_id = None
        while len(comments) < max_comments:
            url = f"https://www.instagram.com/api/v1/media/{media_id}/comments/"
            params = {"can_support_threading": "true"}
            if min_id:
                params["min_id"] = min_id
            
            resp = s.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                break
                
            data = resp.json()
            for c in data.get("comments", []):
                text = c.get("text", "")
                if text:
                    comments.append(text)
                    if len(comments) >= max_comments:
                        break
            
            if not data.get("has_more_comments"):
                break
            min_id = data.get("next_min_id")
            if not min_id:
                break
            time.sleep(random.uniform(1.0, 2.0))
    except Exception:
        pass
    
    return comments


def _coerce_nonnegative_count(value):
    """Normalize a counter without losing a valid zero value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _first_available_count(*values):
    for value in values:
        normalized = _coerce_nonnegative_count(value)
        if normalized is not None:
            return normalized
    return None


def _v1_item_to_dict(item):
    """Instagram v1 feed API item'ını uygulama formatına çevirir."""
    media_type = item.get("media_type")  # 1=photo, 2=video, 8=carousel
    product_type = item.get("product_type", "")
    is_video = media_type == 2

    if product_type == "clips":
        category = "Reels"
        typename = "Reels"
    elif is_video:
        category = "Video"
        typename = "GraphVideo"
    elif media_type == 8:
        category = "Carousel"
        typename = "GraphSidecar"
    else:
        category = "Fotoğraf"
        typename = "GraphImage"

    caption_obj = item.get("caption")
    caption_text = caption_obj.get("text", "") if isinstance(caption_obj, dict) else ""

    ts = item.get("taken_at")
    date_str = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""

    carousel_count = len(item.get("carousel_media", [])) if media_type == 8 else 1

    # The profile Reels grid displays play_count / ig_play_count.
    # view_count is a different, sometimes stale metric and stays separate.
    play_count = _first_available_count(item.get("play_count"), item.get("ig_play_count"))
    view_count = _coerce_nonnegative_count(item.get("view_count"))

    return {
        "shortcode": item.get("code", ""),
        "date": date_str,
        "timestamp": ts or 0,
        "typename": typename,
        "is_video": is_video,
        "category": category,
        "likes": item.get("like_count", 0),
        "comments": item.get("comment_count", 0),
        "video_view_count": view_count if is_video else None,
        "video_play_count": play_count if is_video else None,
        "video_duration": round(item.get("video_duration", 0), 1) if is_video and item.get("video_duration") else None,
        "caption": caption_text[:300],
        "hashtags": re.findall(r"#(\w+)", caption_text),
        "media_count": carousel_count,
        "url": f"https://www.instagram.com/p/{item.get('code', '')}/",
    }


def _merge_post_metrics(existing, enriched):
    """Merge richer metrics without overwriting reliable web likes/comments."""
    if not existing or not enriched:
        return existing

    play_count = _coerce_nonnegative_count(enriched.get("video_play_count"))
    if play_count is not None:
        existing["video_play_count"] = play_count
        existing["views"] = play_count

    for key in ("video_view_count", "video_duration", "views_source"):
        value = enriched.get(key)
        if value is not None:
            existing[key] = value

    # The Clips endpoint can mask hidden likes/comments; preserve web values.
    for key in ("likes", "comments"):
        if existing.get(key) in (None, -1) and enriched.get(key) not in (None, -1):
            existing[key] = enriched[key]

    for key in (
        "date", "timestamp", "typename", "category", "caption", "hashtags",
        "media_count", "url", "is_video",
    ):
        if existing.get(key) in (None, "", []):
            value = enriched.get(key)
            if value not in (None, "", []):
                existing[key] = value
    return existing


def _fetch_clips_play_counts(session, user_id, target_shortcodes=None, max_pages=4):
    """Fetch the play counters shown on the public profile Reels grid."""
    if not user_id:
        return {}

    import random
    import time

    targets = {str(code) for code in (target_shortcodes or []) if code}
    found = {}
    max_id = None
    page_limit = max(1, min(int(max_pages or 1), 8))

    for page in range(page_limit):
        body = {
            "include_feed_video": "true",
            "page_size": "12",
            "target_user_id": str(user_id),
        }
        if max_id:
            body["max_id"] = max_id

        try:
            response = session.post(
                "https://www.instagram.com/api/v1/clips/user/",
                data=body,
                timeout=(5, 20),
            )
        except Exception:
            break
        if response.status_code != 200:
            break

        try:
            payload = response.json()
        except Exception:
            break

        for item in payload.get("items") or []:
            media = item.get("media") if isinstance(item, dict) else None
            if not isinstance(media, dict):
                media = item if isinstance(item, dict) else {}
            shortcode = str(media.get("code") or "")
            if not shortcode:
                continue
            play_count = _first_available_count(
                media.get("play_count"), media.get("ig_play_count"), media.get("view_count")
            )
            if play_count is not None:
                found[shortcode] = play_count

        if targets and targets.issubset(found):
            break
        paging = payload.get("paging_info") or {}
        if not paging.get("more_available"):
            break
        max_id = paging.get("max_id")
        if not max_id:
            break
        if page + 1 < page_limit:
            time.sleep(random.uniform(0.7, 1.2))

    return found


def _post_datetime(post):
    timestamp = _coerce_nonnegative_count(post.get("timestamp"))
    if timestamp:
        try:
            return datetime.fromtimestamp(timestamp)
        except (OSError, OverflowError, ValueError):
            pass
    raw_date = post.get("date")
    if isinstance(raw_date, str) and raw_date:
        try:
            return datetime.strptime(raw_date[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def _finalize_instagram_posts(posts, max_posts, cutoff_date=None):
    """Deduplicate, filter and sort before applying the requested limit."""
    merged = []
    by_shortcode = {}
    for post in posts or []:
        shortcode = str(post.get("shortcode") or "")
        if shortcode and shortcode in by_shortcode:
            _merge_post_metrics(by_shortcode[shortcode], post)
            continue
        merged.append(post)
        if shortcode:
            by_shortcode[shortcode] = post

    if cutoff_date:
        filtered = []
        for post in merged:
            post_date = _post_datetime(post)
            if post_date is None or post_date >= cutoff_date:
                filtered.append(post)
        merged = filtered
    merged.sort(key=lambda post: _post_datetime(post) or datetime.min, reverse=True)
    return merged[:max_posts]


def _graphql_node_to_dict(node):
    """Instagram GraphQL node formatını uygulama formatına çevirir."""
    typename = node.get("__typename", "GraphImage")
    is_video = node.get("is_video", False)
    product_type = node.get("product_type", "")
    
    if product_type == "clips" or typename == "GraphVideo":
        category = "Reels" if product_type == "clips" else "Video"
        typename = "Reels" if product_type == "clips" else "GraphVideo"
    elif typename == "GraphSidecar":
        category = "Carousel"
    else:
        category = "Fotoğraf"
        typename = "GraphImage"

    caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
    caption_text = caption_edges[0]["node"]["text"] if caption_edges and "node" in caption_edges[0] else ""

    hashtags = [word.strip("#") for word in caption_text.split() if word.startswith("#")]
    dt = datetime.fromtimestamp(node.get("taken_at_timestamp", 0))

    # Beğeni gizli ise genelde -1 döner
    likes = node.get("edge_liked_by", {}).get("count", 0)

    return {
        "shortcode": node.get("shortcode", ""),
        "date": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": node.get("taken_at_timestamp", 0),
        "typename": typename,
        "is_video": is_video,
        "category": category,
        "likes": likes,
        "comments": node.get("edge_media_to_comment", {}).get("count", 0),
        "video_view_count": node.get("video_view_count") if is_video else None,
        "video_play_count": node.get("video_play_count") if is_video else None,
        "video_duration": node.get("video_duration"),
        "hashtags": hashtags,
        "media_count": len(node.get("edge_sidecar_to_children", {}).get("edges", [])) if typename == "GraphSidecar" else 1,
        "url": f"https://instagram.com/p/{node.get('shortcode', '')}/",
        "caption": caption_text,
    }


def _fetch_v1_profile_fallback(session, username):
    """web_profile_info rate-limitliyse kullanıcı araması + v1 info ile profili çözer."""
    try:
        search = session.get(
            "https://www.instagram.com/web/search/topsearch/",
            params={"query": username},
            timeout=(5, 15),
        )
        if search.status_code != 200:
            return None, search.status_code

        payload = search.json()
        matched_user = next(
            (
                entry.get("user", {})
                for entry in payload.get("users", [])
                if (entry.get("user", {}).get("username") or "").lower() == username.lower()
            ),
            None,
        )
        user_id = matched_user.get("pk") if matched_user else None
        if not user_id:
            return None, 404

        info = session.get(
            f"https://www.instagram.com/api/v1/users/{user_id}/info/",
            timeout=(5, 15),
        )
        if info.status_code != 200:
            return None, info.status_code

        info_payload = info.json()
        user = info_payload.get("user")
        if not user:
            return None, 404
        return user, 200
    except Exception:
        return None, None

def fetch_with_cookies(username, max_posts=50, cutoff_date=None):
    """Instaloader + v1 feed API ile profil ve post verisi çeker. Cutoff date verilirse o tarihe kadar tüm postları çeker (max_posts sınırını aşabilir veya max_posts ile sınırlanabilir, duruma göre ayarladık)"""
    import time
    sd = _get_session()
    if not sd or not sd.get("cookies", {}).get("sessionid"):
        return None, None, "no_session"

    sessionid = sd["cookies"]["sessionid"]
    if sd.get("validation_status") == "invalid":
        return None, None, InstagramFetchError(
            "Kay\u0131tl\u0131 Instagram oturumu ge\u00e7ersiz. Yeni bir session cookie ile tekrar giri\u015f yap\u0131n.",
            code="auth_required",
            http_status=401,
            suggest_login=True,
        )

    # Instaloader yerine doğrudan native web API kullanıyoruz (GraphQL 403 engellerini aşmak için)
    s = _make_ig_session(sd.get("cookies", {}))
    
    user_data = None
    profile_source = "web"
    last_profile_status = None
    for attempt in range(3):
        try:
            r = s.get(
                f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}",
                timeout=(5, 15),
            )
            last_profile_status = r.status_code

            # 429 aynı endpoint'i art arda çağırarak düzelmez; düşük hacimli v1 fallback'e geç.
            if r.status_code == 429:
                break
            if r.status_code == 503 or (r.status_code == 404 and attempt < 2):
                time.sleep(3 + attempt * 2)
                continue
            if r.status_code == 400 and "useragent mismatch" in r.text:
                break
            if r.status_code in (401, 403):
                return None, None, _classify_instagram_auth_failure(sd.get("cookies", {}), r)
            if r.status_code != 200:
                if attempt < 2:
                    time.sleep(3)
                    continue
                break

            data = r.json()
            if data.get("data", {}).get("user"):
                user_data = data["data"]["user"]
                break
            if attempt < 2:
                time.sleep(3)
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
                continue
            return None, None, InstagramFetchError(
                _instagram_connection_error_message(e),
                code="network_error",
                http_status=502,
                retryable=True,
            )

    if not user_data:
        fallback_user, fallback_status = _fetch_v1_profile_fallback(s, username)
        if fallback_user:
            user_data = fallback_user
            profile_source = "v1"
            last_profile_status = 200
        elif fallback_status in (401, 403):
            if last_profile_status == 429:
                # 429 on both endpoints. Let's try Instaloader as a last resort for profile.
                try:
                    import instaloader
                    L = instaloader.Instaloader()
                    # Try public instaloader profile fetch
                    profile = instaloader.Profile.from_username(L.context, username)
                    user_data = {
                        "username": profile.username,
                        "full_name": profile.full_name,
                        "biography": profile.biography,
                        "follower_count": profile.followers,
                        "following_count": profile.followees,
                        "media_count": profile.mediacount,
                        "is_private": profile.is_private,
                        "is_verified": profile.is_verified,
                        "is_business": profile.is_business_account,
                        "profile_pic_url": profile.profile_pic_url,
                        "id": profile.userid,
                        "pk": profile.userid
                    }
                    profile_source = "instaloader"
                    last_profile_status = 200
                except Exception:
                    return None, None, InstagramFetchError(
                        "Instagram geçici olarak çok fazla istek algıladı (HTTP 429). "
                        "Birkaç dakika bekleyip yeniden deneyin.",
                        code="rate_limited",
                        http_status=429,
                        retryable=True,
                        retry_after=60,
                    )
            else:
                return None, None, _classify_instagram_auth_failure(sd.get("cookies", {}))
        elif fallback_status == 404:
            return None, None, InstagramFetchError(
                f"'{username}' kullanıcısı bulunamadı.",
                code="not_found",
                http_status=404,
            )

    if not user_data:
        if last_profile_status == 429 or fallback_status == 429:
            # Let's try one more time with Instaloader just in case
            try:
                import instaloader
                L = instaloader.Instaloader()
                profile = instaloader.Profile.from_username(L.context, username)
                user_data = {
                    "username": profile.username,
                    "full_name": profile.full_name,
                    "biography": profile.biography,
                    "follower_count": profile.followers,
                    "following_count": profile.followees,
                    "media_count": profile.mediacount,
                    "is_private": profile.is_private,
                    "is_verified": profile.is_verified,
                    "is_business": profile.is_business_account,
                    "profile_pic_url": profile.profile_pic_url,
                    "id": profile.userid,
                    "pk": profile.userid
                }
                profile_source = "instaloader"
                last_profile_status = 200
            except Exception:
                return None, None, InstagramFetchError(
                    "Instagram geçici olarak çok fazla istek algıladı (HTTP 429). "
                    "Birkaç dakika bekleyip yeniden deneyin.",
                    code="rate_limited",
                    http_status=429,
                    retryable=True,
                    retry_after=60,
                )
        if last_profile_status == 503 or fallback_status == 503:
            return None, None, InstagramFetchError(
                "Instagram servisi geçici olarak kullanılamıyor (HTTP 503).",
                code="instagram_unavailable",
                http_status=503,
                retryable=True,
            )
        return None, None, InstagramFetchError(
            f"\'{username}\' profiline \u015fu anda ula\u015f\u0131lamad\u0131.",
            code="instagram_unavailable",
            http_status=502,
            retryable=True,
        )

    _set_session_validation("valid", expected_sessionid=sessionid)
    if profile_source == "v1":
        hd_pic = user_data.get("hd_profile_pic_url_info") or {}
        profile_info = {
            "username": user_data.get("username"),
            "full_name": user_data.get("full_name"),
            "biography": user_data.get("biography"),
            "followers": user_data.get("follower_count", 0),
            "following": user_data.get("following_count", 0),
            "post_count": user_data.get("media_count", 0),
            "is_private": user_data.get("is_private", False),
            "is_verified": user_data.get("is_verified", False),
            "is_business": user_data.get("is_business", False),
            "profile_pic_url": hd_pic.get("url") or user_data.get("profile_pic_url"),
        }
        user_id = user_data.get("pk") or user_data.get("id")
        timeline_edges = []
    else:
        profile_info = {
            "username": user_data.get("username"),
            "full_name": user_data.get("full_name"),
            "biography": user_data.get("biography"),
            "followers": user_data.get("edge_followed_by", {}).get("count", 0),
            "following": user_data.get("edge_follow", {}).get("count", 0),
            "post_count": user_data.get("edge_owner_to_timeline_media", {}).get("count", 0),
            "is_private": user_data.get("is_private", False),
            "is_verified": user_data.get("is_verified", False),
            "is_business": user_data.get("is_business_account", False),
            "profile_pic_url": user_data.get("profile_pic_url_hd") or user_data.get("profile_pic_url"),
        }
        user_id = user_data.get("id")
        timeline_edges = user_data.get("edge_owner_to_timeline_media", {}).get("edges", [])
    if profile_info["is_private"]:
        return profile_info, [], None

    posts_data = []
    
    # web_profile_info başarılıysa ilk gönderileri yeniden istemeden kullan.

    for edge in timeline_edges:
        if "node" in edge:
            posts_data.append(_graphql_node_to_dict(edge["node"]))
            
    # Enrich Reels before any early return. This is the same endpoint used
    # by the visible profile Reels grid.
    reels_targets = {
        post.get("shortcode")
        for post in posts_data
        if post.get("category") == "Reels"
        and (
            not cutoff_date
            or _post_datetime(post) is None
            or _post_datetime(post) >= cutoff_date
        )
    }
    if reels_targets:
        play_counts = _fetch_clips_play_counts(s, user_id, reels_targets)
        for post in posts_data:
            play_count = play_counts.get(post.get("shortcode"))
            if play_count is not None:
                _merge_post_metrics(post, {
                    "video_play_count": play_count,
                    "views_source": "instagram_clips_user",
                })

    finalized = _finalize_instagram_posts(posts_data, max_posts, cutoff_date)
    if len(finalized) >= max_posts:
        return profile_info, finalized, None

    # 2. Aşama: Daha fazla post isteniyorsa Web API üzerinden pagination yapalım
    max_id = None
    page = 0
    posts_by_shortcode = {p["shortcode"]: p for p in posts_data if p.get("shortcode")}

    while len(posts_data) < max_posts and page < 100:
        page += 1
        params = {"count": 12}
        if max_id:
            params["max_id"] = max_id

        try:
            resp = s.get(
                f"https://www.instagram.com/api/v1/feed/user/{user_id or username}/",
                params=params,
                timeout=20,
            )
        except Exception as e:
            break

        # Eğer API rate limite takılırsa, elimizdekilerle yetinelim ve hata döndürmeyelim
        if resp.status_code != 200:
            break

        try:
            feed = resp.json()
        except Exception:
            break

        items = feed.get("items", [])
        if not items:
            break

        for item in items:
            pd = _v1_item_to_dict(item)
            shortcode = pd.get("shortcode")
            if shortcode and shortcode in posts_by_shortcode:
                _merge_post_metrics(posts_by_shortcode[shortcode], pd)
            else:
                posts_data.append(pd)
                if shortcode:
                    posts_by_shortcode[shortcode] = pd

        if cutoff_date and len(posts_data) > 5:
            old_count = sum(
                1 for post in posts_data
                if _post_datetime(post) is not None and _post_datetime(post) < cutoff_date
            )
            if old_count > 5:
                break

        if not feed.get("more_available"):
            break
        max_id = feed.get("next_max_id")
        if not max_id:
            break
        import time
        import random
        # Sabit 2 saniye yerine rastgele bir bekleme (4 ile 8 saniye arası) ekliyoruz.
        # Bu, Instagram'ın otomatik bot tespit sistemine yakalanma riskini ciddi oranda düşürür.
        time.sleep(random.uniform(4.0, 8.0))

    return profile_info, _finalize_instagram_posts(posts_data, max_posts, cutoff_date), None


def get_demo_data(username="demo", platform="instagram"):
    import random
    from datetime import timedelta

    username = re.sub(r"[^A-Za-z0-9_.-]+", "_", username).strip("_")[:40] or "demo"
    profile_info = {
        "platform": platform,
        "username": username,
        "full_name": username.replace("_", " ").title(),
        "biography": "Demo hesap bilgileri",
        "followers": random.randint(5000, 100000),
        "following": random.randint(100, 1000),
        "post_count": random.randint(50, 500),
        "is_private": False, "is_verified": False, "is_business": True, "profile_pic_url": "",
    }
    captions = [
        "Yeni içerik serimiz başladı! #demo #yaratıcılık",
        "Sizin favoriniz hangisi? #demo #topluluk",
        "Kamera arkasından bir an #demo #hikaye",
        "Haftanın ipuçları #demo #rehber",
        "Birlikte keşfedelim #demo #keşfet",
        "Sık sorulan sorular #demo #sorucevap",
        "Yeni bir fikir #demo #ilham",
        "Adım adım anlatım #demo #öğren",
        "Güne merhaba #demo #günaydın",
        "Bu hafta neler oldu? #demo #özet",
    ]
    base = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0) - timedelta(days=1)
    posts = []
    for i in range(30):
        iv = random.random() > 0.4
        d = base - timedelta(days=i * 2 + random.randint(0, 1))
        lk = random.randint(80, 3000)
        cap = captions[i % len(captions)]
        tn = random.choice(["Reels", "GraphVideo"]) if iv else random.choice(["GraphImage", "GraphSidecar"])
        posts.append({
            "shortcode": f"DEMO{i:04d}", "date": d.strftime("%Y-%m-%d %H:%M"),
            "typename": tn, "is_video": iv,
            "category": "Reels" if tn == "Reels" else ("Video" if iv else "Fotoğraf"),
            "likes": lk, "comments": random.randint(2, 150),
            "video_view_count": random.randint(lk * 3, lk * 15) if iv else None,
            "video_play_count": random.randint(lk * 2, lk * 12) if iv else None,
            "video_duration": round(random.uniform(5, 90), 1) if iv else None,
            "caption": cap, "hashtags": re.findall(r"#(\w+)", cap),
            "media_count": random.randint(1, 5) if tn == "GraphSidecar" else 1,
            "url": f"https://www.instagram.com/p/DEMO{i:04d}/",
        })
    if platform != "instagram":
        profile_info["url"] = f"https://www.{platform}.com/@{username}"
        for i, post in enumerate(posts):
            post["platform"] = platform
            post["id"] = post["shortcode"]
            if platform == "youtube":
                post.update({"is_video": True, "typename": "YouTubeShort" if i % 3 == 0 else "YouTubeVideo",
                             "category": "Shorts" if i % 3 == 0 else "YouTube Video", "media_count": 1,
                             "video_play_count": random.randint(0, 20000),
                             "url": f"https://www.youtube.com/watch?v=DEMO{i:07d}", "shares": None})
            else:
                post.update({"typename": "TikTokVideo" if post["is_video"] else "TikTokPhoto",
                             "category": "TikTok Video" if post["is_video"] else "TikTok Photo",
                             "video_play_count": random.randint(0, 20000),
                             "url": f"https://www.tiktok.com/@{username}/video/DEMO{i:07d}",
                             "shares": random.randint(0, 100)})
    posts.sort(key=lambda x: x["date"], reverse=True)
    return profile_info, posts


# ============ ROUTES ============

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login_status")
def login_status():
    """Return the last locally known state without calling Instagram."""
    session_data = _get_session()
    has_session = bool(session_data and session_data.get("cookies", {}).get("sessionid"))
    session_state = (session_data or {}).get("validation_status", "unknown" if has_session else "missing")
    logged_in = has_session and session_state != "invalid"
    return jsonify({
        "logged_in": logged_in,
        "username": (session_data or {}).get("username", "") if has_session else "",
        "session_state": session_state,
        "suggest_login": not logged_in,
    })


@app.route("/api/login_cookie", methods=["POST"])
def api_login_cookie():
    data = request.get_json() or {}
    sessionid = str(data.get("sessionid") or "").strip()
    if not sessionid:
        return jsonify({"error": "sessionid de\u011feri gerekli."}), 400

    cookies = {"sessionid": sessionid}
    for key in ("csrftoken", "ds_user_id", "ig_did", "mid", "rur"):
        value = str(data.get(key) or "").strip()
        if value:
            cookies[key] = value

    validation = _validate_instagram_session(cookies)
    state = validation.get("state")
    if state != "valid":
        if state == "invalid":
            error = InstagramFetchError(
                "Ge\u00e7ersiz veya s\u00fcresi dolmu\u015f session cookie.",
                code="auth_required",
                http_status=401,
                suggest_login=True,
            )
        elif state == "rate_limited":
            error = InstagramFetchError(
                "Instagram ge\u00e7ici olarak \u00e7ok fazla istek alg\u0131lad\u0131. "
                "Birka\u00e7 dakika sonra tekrar deneyin.",
                code="rate_limited",
                http_status=429,
                retryable=True,
                retry_after=60,
            )
        else:
            error = InstagramFetchError(
                validation.get("message") or "Instagram oturumu do\u011frulanamad\u0131.",
                code="instagram_unavailable",
                http_status=int(validation.get("http_status") or 503),
                retryable=True,
            )
        payload = {
            "error": str(error),
            "error_code": error.code,
            "suggest_login": error.suggest_login,
            "retryable": error.retryable,
        }
        if error.retry_after:
            payload["retry_after"] = error.retry_after
        return jsonify(payload), error.http_status

    if not cookies.get("csrftoken"):
        csrf_token = _fetch_real_csrftoken(sessionid)
        if csrf_token:
            cookies["csrftoken"] = csrf_token

    username = validation.get("username") or "user"
    _save_session(username, cookies, validation_status="valid")
    return jsonify({"success": True, "username": username, "session_state": "valid"})


@app.route("/api/login_password", methods=["POST"])
def api_login_password():
    """Username + password ile giriş yapar — önce web API, sonra Instaloader dener."""
    import requests as rq
    data = request.get_json() or {}
    username = extract_username(str(data.get("username") or ""))
    password = str(data.get("password") or "")
    if not username or not password:
        return jsonify({"error": "Kullanıcı adı ve şifre gerekli."}), 400

    # Yöntem 1: Doğrudan Instagram Web Login API
    try:
        sess = rq.Session()
        sess.trust_env = os.getenv("INSTAGRAM_TRUST_ENV_PROXY", "0").lower() in ("1", "true", "yes")
        sess.headers.update({
            "User-Agent": INSTAGRAM_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/accounts/login/",
            "Origin": "https://www.instagram.com",
        })
        # CSRF token al
        r0 = sess.get("https://www.instagram.com/accounts/login/", timeout=10)
        csrf = sess.cookies.get("csrftoken", "")
        if not csrf:
            # Cookie'den bulunamazsa header/body'den dene
            import re as _re
            m = _re.search(r'"csrf_token":"([^"]+)"', r0.text)
            if m:
                csrf = m.group(1)

        if csrf:
            sess.headers.update({"X-CSRFToken": csrf})
            import time as _time
            login_payload = {
                "username": username,
                "enc_password": f"#PWD_INSTAGRAM_BROWSER:0:{int(_time.time())}:{password}",
                "queryParams": "{}",
                "optIntoOneTap": "false",
            }
            r_login = sess.post(
                "https://www.instagram.com/accounts/login/ajax/",
                data=login_payload,
                timeout=20,
            )
            login_data = r_login.json()

            if login_data.get("authenticated"):
                # Başarılı giriş
                cookies = {}
                for c in sess.cookies:
                    if c.domain and "instagram" in c.domain:
                        cookies[c.name] = c.value
                if cookies.get("sessionid"):
                    _save_session(username, cookies, validation_status="valid")
                    return jsonify({"success": True, "username": username})

            if login_data.get("checkpoint_url"):
                cp_url = login_data["checkpoint_url"]
                return jsonify({
                    "error": f"Instagram güvenlik doğrulaması istiyor. "
                             f"Lütfen tarayıcınızda https://www.instagram.com{cp_url} adresine gidip "
                             f"doğrulamayı tamamlayın, sonra tekrar deneyin veya Session Cookie ile giriş yapın."
                }), 400

            if login_data.get("two_factor_required"):
                return jsonify({"error": "Bu hesapta iki adımlı doğrulama aktif. Lütfen session cookie ile giriş yapın."}), 400

            if login_data.get("status") == "fail":
                msg = login_data.get("message", "")
                if "password" in msg.lower() or "credential" in msg.lower():
                    return jsonify({"error": "Kullanıcı adı veya şifre hatalı."}), 401
                return jsonify({"error": f"Instagram giriş hatası: {msg[:200]}"}), 400
    except Exception as web_err:
        pass  # Web login başarısız olursa Instaloader'a düş

    # Yöntem 2: Instaloader fallback
    try:
        L = instaloader.Instaloader()
        L.login(username, password)
        cookies = {}
        for cookie in L.context._session.cookies:
            if cookie.domain and "instagram" in cookie.domain:
                cookies[cookie.name] = cookie.value
        if not cookies.get("sessionid"):
            return jsonify({"error": "Giriş başarılı görünüyor ama sessionid alınamadı."}), 500
        _save_session(username, cookies, validation_status="valid")
        return jsonify({"success": True, "username": username})
    except instaloader.exceptions.BadCredentialsException:
        return jsonify({"error": "Kullanıcı adı veya şifre hatalı."}), 401
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        return jsonify({"error": "Bu hesapta iki adımlı doğrulama aktif. Lütfen session cookie ile giriş yapın."}), 400
    except instaloader.exceptions.ConnectionException as e:
        err_msg = str(e)[:200]
        if "checkpoint" in err_msg.lower() or "challenge" in err_msg.lower():
            return jsonify({"error": "Instagram güvenlik doğrulaması istiyor. Lütfen tarayıcıdan Instagram'a giriş yapıp doğrulamayı tamamlayın, ardından session cookie ile deneyin."}), 400
        return jsonify({"error": "Instagram'a bağlanılamadı. Tarayıcıdan giriş yapıp Session ID yöntemini deneyin."}), 503
    except Exception as e:
        return jsonify({"error": "Giriş tamamlanamadı. Tarayıcıdan giriş yapıp Session ID yöntemini deneyin."}), 500


@app.route("/api/logout", methods=["POST"])
def api_logout():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    return jsonify({"success": True})


def _canonical_content_type(platform, post):
    explicit = str(post.get("content_type") or "").strip()
    if explicit:
        return explicit
    category = str(post.get("category") or "").lower()
    typename = str(post.get("typename") or "").lower()

    if platform == "youtube":
        return "Shorts" if "short" in category or "short" in typename else "Video"
    if platform == "tiktok":
        return "Fotoğraf / Slayt" if any(token in category or token in typename for token in ("photo", "image", "slide")) else "Video"
    if "reel" in category or "reel" in typename:
        return "Reels"
    if "carousel" in category or "sidecar" in typename or (post.get("media_count") or 1) > 1:
        return "Carousel"
    if post.get("is_video") or "video" in category or "video" in typename:
        return "Video"
    return "Fotoğraf"


def _resolve_post_views(post):
    """Resolve the display counter without reviving stale Reel view counts."""
    for key in ("views", "video_play_count", "play_count", "ig_play_count"):
        value = _coerce_nonnegative_count(post.get(key))
        if value is not None:
            return value

    category = str(post.get("category") or "").lower()
    typename = str(post.get("typename") or "").lower()
    is_reel = "reel" in category or "reel" in typename or post.get("product_type") == "clips"
    if not is_reel:
        for key in ("video_view_count", "view_count"):
            value = _coerce_nonnegative_count(post.get(key))
            if value is not None:
                return value
    return None


def _normalize_post_contract(platform, post):
    post.setdefault("platform", platform)
    post["content_type"] = _canonical_content_type(platform, post)
    has_view_counter = post.get("is_video") or platform in {"youtube", "tiktok"}
    post["views"] = _resolve_post_views(post) if has_view_counter else None
    post["url"] = str(post.get("url") or "")
    return post


def _instagram_error_response(error, platform="instagram", suggest_demo=True):
    status = int(getattr(error, "http_status", 503) or 503)
    payload = {
        "error": str(error),
        "error_code": getattr(error, "code", f"{platform}_unavailable"),
        "platform": platform,
        "suggest_login": bool(getattr(error, "suggest_login", False)),
        "suggest_demo": bool(suggest_demo and platform == "instagram"),
        "retryable": bool(getattr(error, "retryable", False)),
    }
    retry_after = getattr(error, "retry_after", None)
    if retry_after:
        payload["retry_after"] = retry_after
    return jsonify(payload), status


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json() or {}
    raw_input = str(data.get("input", "")).strip()
    platform = str(data.get("platform", "instagram")).strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        return jsonify({"error": "Desteklenmeyen platform."}), 400
    if not raw_input:
        return jsonify({"error": "Lütfen profil, kullanıcı veya kanal bilgisi girin."}), 400

    default_days = 365 if platform == "youtube" else 90
    try:
        days = max(1, min(int(data.get("days", default_days)), 2555))
    except (TypeError, ValueError):
        days = default_days
    cutoff = datetime.now() - timedelta(days=days)
    default_max_posts = 180 if platform == "youtube" else 500
    try:
        max_posts = max(1, min(int(data.get("max_posts", default_max_posts)), default_max_posts))
    except (TypeError, ValueError):
        max_posts = default_max_posts
    demo_mode = bool(data.get("demo", False))
    meta = {"source": "demo" if demo_mode else platform, "days": days}

    if platform == "instagram":
        identifier = extract_username(raw_input)
        if not identifier:
            return jsonify({"error": "Geçersiz Instagram linki veya kullanıcı adı."}), 400
    else:
        identifier = raw_input

    if demo_mode:
        pi, pd = get_demo_data(identifier.lstrip("@"), platform=platform)
        pd = _finalize_instagram_posts(pd, max_posts, cutoff)
        pi["platform"] = platform
        for post in pd:
            post["platform"] = platform
        err = None
        meta.update({"synthetic": True, "note": "Demo tamamen yapay veridir; gerçek hesap ölçümü değildir."})
    elif platform == "instagram":
        pi, pd, err = fetch_with_cookies(identifier, max_posts=max_posts, cutoff_date=cutoff)
        meta.update({"source": "instagram_web", "truncated": len(pd or []) >= max_posts})
    else:
        adapter = fetch_youtube if platform == "youtube" else fetch_tiktok
        kwargs = {"max_posts": max_posts, "cutoff_date": cutoff, "user_agent": INSTAGRAM_USER_AGENT}
        if platform == "youtube":
            kwargs["api_key"] = YOUTUBE_API_KEY
        try:
            pi, pd, err, adapter_meta = adapter(identifier, **kwargs)
        except Exception:
            return jsonify({"error": f"{PLATFORM_LABELS[platform]} verilerine şu anda ulaşılamıyor.",
                            "platform": platform, "error_code": f"{platform}_unavailable", "suggest_login": False,
                            "retryable": True}), 502
        meta.update(adapter_meta or {})
        if err:
            status = int(meta.get("http_status") or 503)
            payload = {"error": str(err), "platform": platform, "error_code": meta.get("error_code") or f"{platform}_unavailable",
                       "suggest_login": False, "retryable": status >= 500 or status == 429}
            if meta.get("retry_after"):
                payload["retry_after"] = meta["retry_after"]
            return jsonify(payload), status

    if err == "no_session":
        return jsonify({
            "error": "Instagram'a erişmek için giriş yapmanız gerekiyor. "
                     "Sol alttaki 'Giriş Yap' butonuyla Instagram session cookie'nizi girin.",
            "suggest_login": True,
            "suggest_demo": True,
        }), 401
    if err:
        return _instagram_error_response(err, platform=platform, suggest_demo=True)

    pi = pi or {}
    pd = pd or []
    pi["platform"] = platform
    for post in pd:
        _normalize_post_contract(platform, post)

    valid_likes = [post["likes"] for post in pd if isinstance(post.get("likes"), (int, float)) and post["likes"] >= 0]
    valid_comments = [post["comments"] for post in pd if isinstance(post.get("comments"), (int, float)) and post["comments"] >= 0]
    valid_views = [post["views"] for post in pd if isinstance(post.get("views"), (int, float)) and post["views"] >= 0]
    complete_interactions = [
        post["likes"] + post["comments"]
        for post in pd
        if isinstance(post.get("likes"), (int, float))
        and post["likes"] >= 0
        and isinstance(post.get("comments"), (int, float))
        and post["comments"] >= 0
    ]
    avg_likes = _get_normalized_avg(valid_likes) if valid_likes else None
    avg_comments = _get_normalized_avg(valid_comments) if valid_comments else None
    avg_views = _get_normalized_avg(valid_views) if valid_views else None
    try:
        followers = max(int(pi.get("followers") or 0), 0)
    except (TypeError, ValueError):
        followers = 0
    engagement_rate = (
        (_get_normalized_avg(complete_interactions) / followers) * 100
        if complete_interactions and followers > 0 else None
    )
    stats = {
        "avg_likes": None if avg_likes is None else round(avg_likes),
        "avg_comments": None if avg_comments is None else round(avg_comments),
        "avg_views": None if avg_views is None else round(avg_views),
        "engagement_rate": None if engagement_rate is None else round(engagement_rate, 4),
        "analyzed_posts": len(pd),
        "likes_available": len(valid_likes),
        "comments_available": len(valid_comments),
        "views_available": len(valid_views),
        "engagement_sample": len(complete_interactions),
    }
    meta.setdefault("metric_coverage", {
        "likes": {"available": len(valid_likes), "total": len(pd)},
        "comments": {"available": len(valid_comments), "total": len(pd)},
        "views": {"available": len(valid_views), "total": len(pd)},
    })

    # Mevcut DB şemasında platform alanı yok; farklı platformları karıştırmamak için
    # geçmiş kaydını yalnız Instagram analizlerinde tutuyoruz.
    if not demo_mode and platform == "instagram":
        try:
            brand = Brand.query.filter_by(username=identifier).first()
            if not brand:
                try:
                    brand = Brand(username=identifier)
                    db.session.add(brand)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    brand = Brand.query.filter_by(username=identifier).first()
            
            if brand:
                history = AnalysisHistory(
                    brand_id=brand.id,
                    followers=followers,
                    avg_likes=stats.get("avg_likes") or 0.0,
                    avg_comments=stats.get("avg_comments") or 0.0,
                    avg_views=stats.get("avg_views") or 0.0,
                    engagement_rate=stats.get("engagement_rate") or 0.0,
                )
                db.session.add(history)
                db.session.commit()
        except Exception as exc:
            db.session.rollback()
            app.logger.warning("Analysis history could not be saved.")

    meta.update({
        "days": days,
        "cutoff": cutoff.strftime("%Y-%m-%d"),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    })
    return jsonify({
        "platform": platform,
        "profile": pi,
        "posts": pd,
        "stats": stats,
        "meta": meta,
        "demo": demo_mode,
    })

@app.route("/api/benchmarking", methods=["POST"])
def api_benchmarking():
    data = request.get_json()
    usernames = data.get("usernames", [])
    days = int(data.get("days", 30))
    
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    
    results = []
    for un in usernames[:8]: # Max 8 at a time
        un = extract_username(un.strip())
        if not un: continue
        
        pi, pd, err = fetch_with_cookies(un, max_posts=500, cutoff_date=cutoff_date)
        if err or not pi:
            continue
            
        valid_posts = []
        for p in pd:
            try:
                p_date = datetime.strptime(p["date"], "%Y-%m-%d %H:%M:%S")
                if p_date >= cutoff_date:
                    valid_posts.append(p)
            except:
                valid_posts.append(p)
        
        valid_likes = [p.get("likes", 0) for p in valid_posts if p.get("likes", -1) != -1]
        valid_comments = [p.get("comments", 0) for p in valid_posts]
        videos = [p for p in valid_posts if p.get("is_video") or p.get("typename") == "Reels"]
        view_counts = [_resolve_post_views(p) or 0 for p in videos]
        
        avg_likes = _get_normalized_avg(valid_likes) if valid_likes else 0
        avg_comments = _get_normalized_avg(valid_comments) if valid_comments else 0
        avg_views = _get_normalized_avg(view_counts) if videos else 0
        followers = pi.get("followers", 1) or 1
        
        eng_rate = ((avg_likes + avg_comments) / followers) * 100
        
        results.append({
            "username": pi["username"],
            "followers": followers,
            "avg_likes": round(avg_likes),
            "avg_comments": round(avg_comments),
            "avg_views": round(avg_views),
            "engagement_rate": round(eng_rate, 2),
            "post_count": len(valid_posts)
        })
        
    if results:
        max_views = max(r["avg_views"] for r in results) or 1
        max_likes = max(r["avg_likes"] for r in results) or 1
        max_comments = max(r["avg_comments"] for r in results) or 1
        max_followers = max(r["followers"] for r in results) or 1
        max_eng = max(r["engagement_rate"] for r in results) or 1
        
        weights = get_etki_oranlari()
        for r in results:
            n_views = (r["avg_views"] / max_views) * 100
            n_likes = (r["avg_likes"] / max_likes) * 100
            n_comments = (r["avg_comments"] / max_comments) * 100
            n_followers = (r["followers"] / max_followers) * 100
            n_eng = (r["engagement_rate"] / max_eng) * 100
            
            total_score = (n_views * weights.get("izlenme", 0.35)) + \
                          (n_likes * weights.get("begeni", 0.25)) + \
                          (n_comments * weights.get("yorum", 0.25)) + \
                          (n_followers * weights.get("takipci", 0.05)) + \
                          (n_eng * weights.get("etkilesim", 0.10))
            r["total_score"] = round(total_score, 1)

    results.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    return jsonify({"results": results})


@app.route("/api/trend_radar", methods=["POST"])
def api_trend_radar():
    if not gemini_client:
        return jsonify({"error": "Gemini API Key bulunamadı. Trend radarı çalışamaz."}), 500
        
    prompt = """
    Sen Türkiye'nin en iyi sosyal medya stratejistisin. Görevin şu an Türkiye'de Instagram Reels'ta en çok viral olan (en çok video üretilen sesler, komedi akımları vb.) trendleri bulmak.
    Google Arama yeteneğini kullanarak GÜNCEL trendleri tespit et ve bana en popüler akımları listele.
    Her akım için lütfen şunları sun:
    1. Akımın Adı ve İçeriği (Ne yapılıyor?)
    2. Viral Videoların Örnek Linkleri (Gerçek Instagram Reels bağlantıları)
    3. Kurumsal Markalar İçin Bu Akımlara Adaptasyon Taktikleri
    
    Sadece temiz HTML dökümü olarak ver. HTML tagleri kullan (<h3>, <ul>, <li>, <p>, <strong>, <a href="...">). Markdown sembolleri KULLANMA. Linkleri tıklanabilir yap.
    """
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}],
            )
        )
        html_out = response.text.replace("```html", "").replace("```", "").strip()
        return jsonify({"html": _safe_ai_html(html_out)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate_report", methods=["POST"])
def generate_report():
    data = request.get_json() or {}
    profile = data.get("profile", {})
    stats = data.get("stats", {})
    platform = str(data.get("platform") or profile.get("platform") or "instagram").lower()
    if platform not in SUPPORTED_PLATFORMS:
        return jsonify({"error": "Desteklenmeyen platform."}), 400
    platform_label = PLATFORM_LABELS[platform]
    
    if not gemini_client:
        return jsonify({"error": "Gemini API Key bulunamadı. Lütfen .env dosyasına ekleyin."}), 500

    prompt = f"""
    Sen üst düzey bir dijital pazarlama uzmanısın.
    Aşağıdaki {platform_label} hesap istatistiklerine bakarak bu hesap için yönetici özet raporu yaz.
    Hesap: @{profile.get('username')}
    Takipçi / Abone: {profile.get('followers')}
    Ortalama Beğeni: {stats.get('avg_likes')}
    Ortalama Yorum: {stats.get('avg_comments')}
    Ortalama Video İzlenme: {stats.get('avg_views')}
    Etkileşim Oranı: %{stats.get('engagement_rate')}
    
    Lütfen şu 3 başlıkta kısa, net ve çok profesyonel bir analiz yaz:
    1. Güçlü Yönler (3 madde)
    2. Gelişim Alanları (Zayıf Yönler) (3 madde)
    3. Gelecek Hafta İçin İçerik Stratejisi Önerisi
    
    Formatı temiz HTML stringi olarak ver, <b>, <ul>, <li> etiketleri kullan, markdown kullanma. Sadece HTML kodunu döndür.
    """
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        ai_html = response.text.replace("```html", "").replace("```", "").strip()
        
        html_content = render_template("report_template.html", profile=profile, stats=stats, platform_label=platform_label, ai_analysis=_safe_ai_html(ai_html))
        
        from playwright.sync_api import sync_playwright
        pdf_io = io.BytesIO()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(java_script_enabled=False)
            page.set_content(html_content, wait_until="networkidle")
            pdf_bytes = page.pdf(format="A4", print_background=True, margin={"top": "2cm", "bottom": "2cm", "left": "2cm", "right": "2cm"})
            browser.close()
            
        pdf_io.write(pdf_bytes)
        pdf_io.seek(0)
        un = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(profile.get("username") or "hesap"))[:80]
        return Response(pdf_io.getvalue(), mimetype="application/pdf", headers={"Content-Disposition": f'attachment; filename="{un}_analiz_raporu.pdf"'})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/export_csv", methods=["POST"])
def export_csv():
    data = request.get_json() or {}
    platform = str(data.get("platform", "instagram")).lower()
    if platform not in SUPPORTED_PLATFORMS:
        return jsonify({"error": "Desteklenmeyen platform."}), 400
    posts = data.get("posts", [])
    profile = data.get("profile", {})
    output = io.StringIO()
    output.write('\ufeff')
    w = csv.writer(output, delimiter=';')
    w.writerow(["Hesap", "Shortcode", "Tarih", "Tür", "Kategori", "Beğeni", "Yorum",
                "İzlenme", "Oynatma", "Süre(sn)", "Hashtagler", "Medya", "URL", "Platform", "Caption"])
    for p in posts:
        caption_full = str(p.get("caption") or "").replace("\n", " ").replace("\r", " ")
        w.writerow([_spreadsheet_text(value) for value in [
            profile.get("username", ""), p.get("shortcode", ""), p.get("date", ""),
            p.get("typename", ""), p.get("category", ""), p.get("likes", 0),
            p.get("comments", 0), _resolve_post_views(p) if _resolve_post_views(p) is not None else "", p.get("video_play_count", ""),
            p.get("video_duration", ""), ", ".join(p.get("hashtags", [])),
            p.get("media_count", 1), p.get("url", ""), platform, caption_full,
        ]])
    output.seek(0)
    un = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(profile.get("username") or "hesap"))[:80]
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{un}_verileri.csv"'})


@app.route("/api/export_excel", methods=["POST"])
def export_excel():
    """Normalize edilmiş platform verisini biçimlendirilmiş gerçek XLSX olarak döndürür."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.table import Table, TableStyleInfo

    data = request.get_json() or {}
    platform = str(data.get("platform", "instagram")).lower()
    if platform not in SUPPORTED_PLATFORMS:
        return jsonify({"error": "Desteklenmeyen platform."}), 400
    profile = data.get("profile") or {}
    posts = data.get("posts") or []
    stats = data.get("stats") or {}
    meta = data.get("meta") or {}
    if not profile:
        return jsonify({"error": "Excel için önce bir analiz çalıştırın."}), 400

    def safe_cell(value):
        if value is None:
            return ""
        if isinstance(value, str):
            value = value.replace("\x00", "")
            value = _spreadsheet_text(value)
        return value

    def summary_metric(value, percentage=False):
        if value is None:
            return "Mevcut değil"
        return (float(value or 0) / 100) if percentage else int(value or 0)

    def optional_int(value):
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return ""
        return normalized if normalized >= 0 else ""

    colors = {"instagram": "C13584", "tiktok": "00A9B7", "youtube": "FF0000"}
    platform_label = PLATFORM_LABELS[platform]
    accent = colors[platform]
    dark = "171717"
    light = "F5F5F5"
    white = "FFFFFF"
    thin_gray = Side(style="thin", color="D9D9D9")

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Özet"
    summary.sheet_view.showGridLines = False
    summary.merge_cells("A1:F2")
    summary["A1"] = f"{platform_label} Sosyal Medya Analiz Raporu"
    summary["A1"].font = Font(name="Aptos Display", size=20, bold=True, color=white)
    summary["A1"].fill = PatternFill("solid", fgColor=accent)
    summary["A1"].alignment = Alignment(horizontal="left", vertical="center")

    summary_rows = [
        ("Platform", platform_label),
        ("Hesap / Kanal", profile.get("full_name") or profile.get("username") or ""),
        ("Kullanıcı / Handle", profile.get("username") or ""),
        ("Takipçi / Abone", summary_metric(profile.get("followers"))),
        (
            "Toplam İçerik",
            int(profile["post_count"])
            if isinstance(profile.get("post_count"), (int, float))
            else "Mevcut değil",
        ),
        ("Analiz Dönemi (Gün)", int(meta.get("days") or data.get("period_days") or 0)),
        ("Analiz Edilen İçerik", len(posts)),
        ("Ortalama Beğeni", summary_metric(stats.get("avg_likes"))),
        ("Ortalama Yorum", summary_metric(stats.get("avg_comments"))),
        ("Ortalama İzlenme", summary_metric(stats.get("avg_views"))),
        ("Etkileşim Oranı", summary_metric(stats.get("engagement_rate"), percentage=True)),
        ("Veri Kaynağı", meta.get("metrics_source") or meta.get("source") or platform),
        ("Veri Notu", meta.get("note") or ""),
        ("Çekim Zamanı", meta.get("fetched_at") or datetime.now().isoformat(timespec="seconds")),
    ]
    start_row = 4
    for offset, (label, value) in enumerate(summary_rows):
        row = start_row + offset
        summary.cell(row, 1, label)
        summary.cell(row, 2, safe_cell(value))
        summary.cell(row, 1).font = Font(bold=True, color=dark)
        summary.cell(row, 1).fill = PatternFill("solid", fgColor=light)
        summary.cell(row, 1).border = Border(bottom=thin_gray)
        summary.cell(row, 2).border = Border(bottom=thin_gray)
    summary["B7"].number_format = "#,##0"
    for cell_ref in ("B8", "B10", "B11", "B12", "B13"):
        summary[cell_ref].number_format = "#,##0"
    summary["B14"].number_format = "0.0000%"
    summary["B16"].alignment = Alignment(wrap_text=True, vertical="top")
    summary["A16"].alignment = Alignment(vertical="top")
    summary.row_dimensions[16].height = 34
    summary.column_dimensions["A"].width = 27
    summary.column_dimensions["B"].width = 48
    summary.column_dimensions["C"].width = 3
    summary.freeze_panes = "A4"

    contents = workbook.create_sheet("İçerikler")
    contents.sheet_view.showGridLines = False
    headers = ["Tarih", "İçerik Linki", "Tür", "Beğeni", "Yorum", "İzlenme"]
    contents.append(headers)
    contents["G1"] = "Grafik Etiketi"
    for post in posts:
        normalized = _normalize_post_contract(platform, dict(post))
        raw_date = normalized.get("date") or ""
        parsed_date = raw_date
        if isinstance(raw_date, str) and raw_date:
            try:
                parsed_date = datetime.strptime(raw_date[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    parsed_date = datetime.strptime(raw_date[:16], "%Y-%m-%d %H:%M")
                except ValueError:
                    parsed_date = safe_cell(raw_date)
        content_type = safe_cell(normalized.get("content_type") or "")
        content_link = safe_cell(normalized.get("url") or "")
        contents.append([
            parsed_date,
            content_link,
            content_type,
            optional_int(normalized.get("likes")),
            optional_int(normalized.get("comments")),
            optional_int(normalized.get("views")),
        ])
        label_date = parsed_date.strftime("%Y-%m-%d") if isinstance(parsed_date, datetime) else str(raw_date)[:10]
        contents.cell(contents.max_row, 7, f"{label_date or 'Tarihsiz'} · {content_type}")

    for cell in contents[1][:6]:
        cell.font = Font(bold=True, color=white)
        cell.fill = PatternFill("solid", fgColor=accent)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    contents.row_dimensions[1].height = 26
    contents.freeze_panes = "A2"
    contents.auto_filter.ref = f"A1:F{max(len(posts) + 1, 1)}"
    widths = [20, 58, 20, 14, 14, 16]
    for index, width in enumerate(widths, start=1):
        contents.column_dimensions[chr(64 + index)].width = width
    contents.column_dimensions["G"].hidden = True
    for row in contents.iter_rows(min_row=2, max_col=6):
        row[0].number_format = "yyyy-mm-dd hh:mm"
        for numeric_index in (3, 4, 5):
            row[numeric_index].number_format = "#,##0"
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
        if row[1].value:
            row[1].hyperlink = str(row[1].value).lstrip("'")
            row[1].style = "Hyperlink"

    if posts:
        table_ref = f"A1:F{len(posts) + 1}"
        table = Table(displayName="SocialContentTable", ref=table_ref)
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        contents.add_table(table)
        chart_count = min(len(posts), 10)
        if chart_count > 0:
            chart = BarChart()
            chart.visible_cells_only = False
            chart.type = "bar"
            chart.style = 10
            chart.title = "Son İçeriklerin İzlenmeleri"
            chart.legend = None
            chart.y_axis.title = "İçerik"
            chart.x_axis.title = "İzlenme"
            data_ref = Reference(contents, min_col=6, min_row=1, max_row=chart_count + 1)
            category_ref = Reference(contents, min_col=7, min_row=2, max_row=chart_count + 1)
            chart.add_data(data_ref, titles_from_data=True)
            chart.set_categories(category_ref)
            chart.height = 8
            chart.width = 16
            summary.add_chart(chart, "D4")

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(profile.get("username") or "hesap"))[:80]
    filename = f"{platform}_{safe_name}_analiz.xlsx"
    return Response(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

# ============ SPONSORLUK ANALİZİ ============

def _clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def _mean(values):
    return (sum(values) / len(values)) if values else 0


def _coefficient_of_variation(values):
    if len(values) < 2:
        return 0
    avg = _mean(values)
    if avg <= 0:
        return 0
    variance = _mean([(v - avg) ** 2 for v in values])
    return math.sqrt(variance) / avg


def _round_price(value):
    value = max(float(value or 0), 250.0)
    if value < 2500:
        step = 50
    elif value < 10000:
        step = 100
    else:
        step = 250
    return int(round(value / step) * step)


def _get_period_label(days):
    label_map = {item["value"]: item["label"] for item in TIME_RANGE_OPTIONS}
    return label_map.get(days, f"Son {days} Gün")


def _resolve_page_type(page_type):
    key = (page_type or "influencer").strip().lower()
    return PAGE_TYPE_PROFILES.get(key, PAGE_TYPE_PROFILES["influencer"]), key if key in PAGE_TYPE_PROFILES else "influencer"


def _detect_format_key(post):
    if post.get("is_video") or post.get("category") in ("Reels", "Video"):
        return "reels"
    if post.get("category") == "Carousel" or ((post.get("media_count") or 1) > 1 and not post.get("is_video")):
        return "carousel"
    return "image"


def _estimate_effective_views(post):
    likes = max(post.get("likes", 0) or 0, 0)
    comments = max(post.get("comments", 0) or 0, 0)
    actual_views = max(_resolve_post_views(post) or 0, 0)
    format_key = _detect_format_key(post)

    if format_key == "carousel":
        derived_views = (likes * 8.4) + (comments * 24)
    elif format_key == "reels":
        derived_views = (likes * 6.2) + (comments * 26)
    else:
        derived_views = (likes * 7.5) + (comments * 22)

    if actual_views > 0:
        blended = (actual_views * 0.86) + (derived_views * 0.14)
        return max(blended, actual_views * 0.92)

    if format_key == "reels":
        return derived_views * 1.08
    if format_key == "carousel":
        return derived_views * 1.10
    return derived_views


def _build_format_metric(posts, followers, overall_views, overall_likes, overall_comments, format_key):
    format_posts = [p for p in posts if _detect_format_key(p) == format_key]
    fallback = FORMAT_FALLBACKS[format_key]

    if format_posts:
        avg_views = round(_get_normalized_avg([_estimate_effective_views(p) for p in format_posts]))
        avg_likes = round(_get_normalized_avg([max(p.get("likes", 0), 0) for p in format_posts]))
        avg_comments = round(_get_normalized_avg([max(p.get("comments", 0), 0) for p in format_posts]))
        is_estimated = False
    else:
        avg_views = round(overall_views * fallback["views_mult"])
        avg_likes = round(overall_likes * fallback["likes_mult"])
        avg_comments = round(overall_comments * fallback["comments_mult"])
        is_estimated = True

    engagement_rate = round(((avg_likes + avg_comments) / max(followers, 1)) * 100, 2)
    share_pct = round((len(format_posts) / len(posts)) * 100, 1) if posts else 0

    return {
        "format_key": format_key,
        "post_count": len(format_posts),
        "share_pct": share_pct,
        "avg_views": avg_views,
        "avg_likes": avg_likes,
        "avg_comments": avg_comments,
        "engagement_rate": engagement_rate,
        "is_estimated": is_estimated,
    }


def _build_story_metric(followers, overall_views, overall_likes, overall_comments):
    fallback = FORMAT_FALLBACKS["story"]
    avg_views = max(round(overall_views * fallback["views_mult"]), round(followers * 0.05))
    avg_likes = round(overall_likes * fallback["likes_mult"])
    avg_comments = round(overall_comments * fallback["comments_mult"])
    return {
        "format_key": "story",
        "post_count": 0,
        "share_pct": 0,
        "avg_views": avg_views,
        "avg_likes": avg_likes,
        "avg_comments": avg_comments,
        "engagement_rate": round(((avg_likes + avg_comments) / max(followers, 1)) * 100, 2),
        "is_estimated": True,
    }


def _build_ugc_metric(reels_metric, followers):
    fallback = FORMAT_FALLBACKS["ugc_reel"]
    avg_views = round(reels_metric["avg_views"] * fallback["views_mult"] / max(FORMAT_FALLBACKS["reels"]["views_mult"], 1))
    avg_likes = round(reels_metric["avg_likes"] * fallback["likes_mult"] / max(FORMAT_FALLBACKS["reels"]["likes_mult"], 0.01))
    avg_comments = round(reels_metric["avg_comments"] * fallback["comments_mult"] / max(FORMAT_FALLBACKS["reels"]["comments_mult"], 0.01))
    return {
        "format_key": "ugc_reel",
        "post_count": reels_metric["post_count"],
        "share_pct": reels_metric["share_pct"],
        "avg_views": avg_views,
        "avg_likes": avg_likes,
        "avg_comments": avg_comments,
        "engagement_rate": round(((avg_likes + avg_comments) / max(followers, 1)) * 100, 2),
        "is_estimated": True,
    }


def _calc_sponsorship(profile_info, posts, page_type, days):
    """Hesap tipine duyarlı, normalize edilmiş sponsorluk fiyat aralıkları üretir."""
    followers = profile_info.get("followers", 0) or 1
    page_profile, page_type_key = _resolve_page_type(page_type)

    valid_posts = [p for p in posts if (p.get("likes", -1) or -1) != -1]
    valid_posts = valid_posts or list(posts or [])
    if not valid_posts:
        return {
            "error": "Hesapta fiyatlama için yeterli gönderi bulunamadı.",
            "username": profile_info.get("username", ""),
            "page_type": page_type_key,
        }

    likes_values = [max(p.get("likes", 0) or 0, 0) for p in valid_posts]
    comments_values = [max(p.get("comments", 0) or 0, 0) for p in valid_posts]
    effective_view_values = [_estimate_effective_views(p) for p in valid_posts]
    videos = [p for p in valid_posts if p.get("is_video")]
    actual_video_views = [max(_resolve_post_views(p) or 0, 0) for p in videos if (_resolve_post_views(p) or 0) > 0]

    avg_likes = round(_get_normalized_avg(likes_values))
    avg_comments = round(_get_normalized_avg(comments_values))
    avg_effective_views = round(_get_normalized_avg(effective_view_values))
    avg_video_views = round(_get_normalized_avg(actual_video_views)) if actual_video_views else 0
    engagement_rate = round(((avg_likes + avg_comments) / followers) * 100, 2)
    comment_rate = avg_comments / max(avg_likes, 1)

    comment_quality_analysis = _get_comment_quality_analysis_for_posts(valid_posts)
    comment_quality_score = comment_quality_analysis.get("score", 50)

    view_cv = _coefficient_of_variation(effective_view_values)
    consistency_score = round(_clamp(100 - (view_cv * 45), 55, 94))
    confidence_score = round(_clamp(54 + min(len(valid_posts), 18) * 1.6 + (consistency_score - 60) * 0.45, 54, 95))
    engagement_multiplier = _clamp(0.92 + (min(engagement_rate, 8.5) / 24.0), 0.94, 1.28)
    comment_quality_multiplier = _clamp(0.90 + ((comment_quality_score - 50) / 250.0) + min(comment_rate, 0.12), 0.86, 1.18)
    follower_confidence = _clamp(0.96 + max(math.log10(max(followers, 1000)) - 3.0, 0) * 0.035, 0.96, 1.10)
    follower_confidence *= page_profile.get("follower_confidence_mult", 1.0)

    image_posts = [p for p in valid_posts if _detect_format_key(p) == "image"]
    carousel_posts = [p for p in valid_posts if _detect_format_key(p) == "carousel"]
    reels_posts = [p for p in valid_posts if _detect_format_key(p) == "reels"]

    image_metric = _build_format_metric(valid_posts, followers, avg_effective_views, avg_likes, avg_comments, "image") if image_posts else None
    carousel_metric = _build_format_metric(valid_posts, followers, avg_effective_views, avg_likes, avg_comments, "carousel") if carousel_posts else None
    reels_metric = _build_format_metric(valid_posts, followers, avg_effective_views, avg_likes, avg_comments, "reels") if reels_posts else None

    format_metrics_map = {}
    if image_metric:
        format_metrics_map["image"] = image_metric
    if carousel_metric:
        format_metrics_map["carousel"] = carousel_metric
    if reels_metric:
        format_metrics_map["reels"] = reels_metric
        format_metrics_map["ugc_reel"] = _build_ugc_metric(reels_metric, followers)

    offer_packages = []
    anchor_map = {}
    for package in SPONSORSHIP_PACKAGES:
        if package["format_key"] == "story":
            continue
        if package.get("allowed_page_types") and page_type_key not in package["allowed_page_types"]:
            continue
        metric = format_metrics_map.get(package["format_key"])
        if not metric:
            continue
        weighted_page_type = (
            (page_profile["awareness_mult"] * package["awareness_weight"]) +
            (page_profile["trust_mult"] * package["trust_weight"]) +
            (page_profile["conversion_mult"] * package["conversion_weight"])
        ) / (package["awareness_weight"] + package["trust_weight"] + package["conversion_weight"])

        base_media_value = (metric["avg_views"] / 1000.0) * DEFAULT_CPM_TL
        interaction_value = (metric["avg_likes"] * 0.10) + (metric["avg_comments"] * 1.65)
        raw_anchor = ((base_media_value * 0.72) + (interaction_value * 0.28))
        raw_anchor *= weighted_page_type
        raw_anchor *= page_profile["engagement_mult"]
        raw_anchor *= page_profile["format_bias"].get(package["format_key"], 1.0)
        raw_anchor *= page_profile["integration_bias"].get(package["integration_key"], 1.0)
        raw_anchor *= package["format_mult"]
        raw_anchor *= package["integration_mult"]
        raw_anchor *= package.get("anchor_bias", 1.0)
        raw_anchor *= engagement_multiplier
        raw_anchor *= comment_quality_multiplier
        raw_anchor *= follower_confidence

        sample_multiplier = _clamp(0.95 + min(metric["post_count"], 6) * 0.012, 0.92, 1.03)
        if metric["is_estimated"]:
            sample_multiplier *= 0.96
        raw_anchor *= sample_multiplier

        native_content_multiplier = 2.15
        if package["integration_key"] in ("dedicated", "review"):
            native_content_multiplier += 0.35
        elif package["integration_key"] == "launch":
            native_content_multiplier -= 0.05
        if package["format_key"] in ("reels", "ugc_reel"):
            native_content_multiplier += 0.20
        raw_anchor *= native_content_multiplier

        production_floor_map = {
            "image": 700,
            "carousel": 950,
            "reels": 1400,
            "story": 550,
            "ugc_reel": 1750,
        }
        floor_adjustment = _clamp(0.88 + (confidence_score / 100.0) * 0.18 + (engagement_rate / 100.0), 0.92, 1.18)
        raw_anchor = max(raw_anchor, production_floor_map[package["format_key"]] * weighted_page_type * floor_adjustment)

        if package["id"] == "reels_dedicated":
            soft_anchor = anchor_map.get("reels_soft")
            if soft_anchor:
                raw_anchor = max(raw_anchor, soft_anchor * 1.08)

        if package["id"] == "ugc_review_reel":
            soft_anchor = anchor_map.get("reels_soft")
            dedicated_anchor = anchor_map.get("reels_dedicated")
            if soft_anchor:
                raw_anchor = max(raw_anchor, soft_anchor * 1.12)
            if dedicated_anchor and page_type_key in ("influencer", "community", "niche", "local"):
                raw_anchor = max(raw_anchor, dedicated_anchor * 0.96)

        band_ratio = _clamp(0.07 + ((100 - consistency_score) / 500.0) + (0.03 if metric["post_count"] < 3 else 0), 0.07, 0.13)
        price_min = _round_price(raw_anchor * (1 - band_ratio))
        price_max = _round_price(raw_anchor * (1 + band_ratio))
        if price_max <= price_min:
            price_max = price_min + (100 if price_min < 10000 else 250)

        estimated_impressions = round(metric["avg_views"] * package["format_mult"] * page_profile["awareness_mult"])
        benchmark_cpm = round((raw_anchor / max(estimated_impressions, 1)) * 1000, 2)

        offer_packages.append({
            "id": package["id"],
            "label": package["label"],
            "description": package["description"],
            "format": package["format_label"],
            "integration_type": package["integration_label"],
            "estimated_impressions": int(max(estimated_impressions, metric["avg_views"])),
            "estimated_reach": int(max(estimated_impressions, metric["avg_views"])),
            "price_min": price_min,
            "price_max": price_max,
            "budget_min": price_min,
            "budget_max": price_max,
            "anchor_price": _round_price(raw_anchor),
            "max_fair_price": price_max,
            "benchmark_cpm": benchmark_cpm,
            "avg_views": metric["avg_views"],
            "avg_likes": metric["avg_likes"],
            "avg_comments": metric["avg_comments"],
            "engagement_rate": metric["engagement_rate"],
            "confidence_score": round(_clamp(confidence_score - (4 if metric["is_estimated"] else 0), 48, 95)),
            "is_estimated": metric["is_estimated"],
            "page_type_multiplier": round(weighted_page_type, 3),
        })
        anchor_map[package["id"]] = raw_anchor

    format_metrics = []
    if image_metric:
        format_metrics.append({
            "format_key": "image",
            "label": "Tek Görsel",
            **image_metric,
        })
    if carousel_metric:
        format_metrics.append({
            "format_key": "carousel",
            "label": "Carousel",
            **carousel_metric,
        })
    if reels_metric:
        format_metrics.append({
            "format_key": "reels",
            "label": "Video / Reels",
            **reels_metric,
        })

    analysis_notes = [
        f"Fiyatlama, seçilen {page_profile['label']} profil etkisine göre normalize edilmiş {len(valid_posts)} gönderi üzerinden hesaplandı.",
        "Takipçi sayısı ana fiyat belirleyici değil; yalnızca küçük bir güven katsayısı olarak modele dahil edildi.",
        f"Yorum kalitesi skoru {comment_quality_score}/100 ve içerik istikrarı skoru {consistency_score}/100 olarak fiyata yansıtıldı.",
        f"Referans reklam benchmark'ı olarak 1000 görüntüleme başı {DEFAULT_CPM_TL:.0f} TL kullanıldı.",
    ]

    result = {
        "username": profile_info.get("username", ""),
        "full_name": profile_info.get("full_name", "") or profile_info.get("username", ""),
        "followers": followers,
        "avg_likes": avg_likes,
        "avg_comments": avg_comments,
        "avg_views": avg_effective_views,
        "avg_video_views": avg_video_views,
        "engagement_rate": engagement_rate,
        "total_posts_analyzed": len(valid_posts),
        "page_type": page_type_key,
        "page_type_label": page_profile["label"],
        "page_type_note": page_profile["pricing_note"],
        "selected_days": days,
        "period_label": _get_period_label(days),
        "profile": {
            "username": profile_info.get("username", ""),
            "full_name": profile_info.get("full_name", "") or profile_info.get("username", ""),
            "followers": followers,
            "post_count": profile_info.get("post_count", len(valid_posts)),
            "profile_pic_url": profile_info.get("profile_pic_url", ""),
        },
        "overview": {
            "normalized_avg_views": avg_effective_views,
            "normalized_avg_likes": avg_likes,
            "normalized_avg_comments": avg_comments,
            "engagement_rate": engagement_rate,
            "comment_quality_score": comment_quality_score,
            "comment_quality_summary": comment_quality_analysis.get("summary", ""),
            "comment_quality_breakdown": comment_quality_analysis.get("breakdown", {}),
            "comment_quality_drivers": comment_quality_analysis.get("drivers", []),
            "comment_quality_source": comment_quality_analysis.get("source", "fallback"),
            "comment_quality_sample_count": comment_quality_analysis.get("sample_count", 0),
            "consistency_score": consistency_score,
            "confidence_score": confidence_score,
            "reels_share_pct": reels_metric["share_pct"] if reels_metric else 0,
            "carousel_share_pct": carousel_metric["share_pct"] if carousel_metric else 0,
            "image_share_pct": image_metric["share_pct"] if image_metric else 0,
        },
        "format_metrics": format_metrics,
        "offer_packages": offer_packages,
        "sponsorship_options": offer_packages,
        "analysis_notes": analysis_notes,
        "comment_quality_analysis": comment_quality_analysis,
        "benchmark_cpm": DEFAULT_CPM_TL,
    }

    return result


@app.route("/api/sponsorship", methods=["POST"])
def api_sponsorship():
    """Hedef sayfanın sponsorluk değerini analiz eder."""
    data = request.get_json()
    if not data or "input" not in data:
        return jsonify({"error": "Lütfen hedef hesap girin."}), 400

    username = extract_username(data["input"])
    if not username:
        return jsonify({"error": "Geçersiz kullanıcı adı."}), 400

    page_type = data.get("page_type") or data.get("category") or "influencer"
    days = int(data.get("days", 90))
    cutoff = datetime.now() - timedelta(days=days)
    demo_mode = data.get("demo", False)

    if demo_mode:
        pi, pd = get_demo_data(username)
        result = _calc_sponsorship(pi, pd, page_type, days)
        result["demo"] = True
        return jsonify(result)

    pi, pd, err = fetch_with_cookies(username, max_posts=500, cutoff_date=cutoff)
    if err == "no_session":
        return jsonify({"error": "Giriş yapmanız gerekiyor.", "suggest_login": True, "suggest_demo": True}), 401
    
    if err:
        return _instagram_error_response(err, suggest_demo=True)

    result = _calc_sponsorship(pi, pd, page_type, days)
    result["demo"] = False
    return jsonify(result)


# ============ HESAP ANALİZİ ============

def _analyze_brand(profile_info, posts):
    """Marka sayfası için detaylı analiz ve öneriler üretir."""
    followers = profile_info.get("followers", 1) or 1
    valid = [p for p in posts if (p.get("likes") or 0) >= 5]
    if not valid:
        return {"error": "Yeterli gönderi verisi yok."}

    # Kategori bazlı kırılım
    cats = {}
    for p in valid:
        c = p.get("category", "Diğer")
        if c not in cats:
            cats[c] = {"count": 0, "likes": [], "comments": [], "views": []}
        cats[c]["count"] += 1
        cats[c]["likes"].append(p.get("likes", 0))
        cats[c]["comments"].append(p.get("comments", 0))
        if p.get("is_video"):
            v = _resolve_post_views(p) or 0
            if v > 0:
                cats[c]["views"].append(v)

    cat_stats = {}
    for c, d in cats.items():
        cat_stats[c] = {
            "count": d["count"],
            "avg_likes": round(_get_normalized_avg(d["likes"])) if d["likes"] else 0,
            "avg_comments": round(_get_normalized_avg(d["comments"])) if d["comments"] else 0,
            "avg_views": round(_get_normalized_avg(d["views"])) if d["views"] else 0,
            "max_likes": max(d["likes"]) if d["likes"] else 0,
        }

    # Saat analizi
    hour_perf = {}
    for p in valid:
        try:
            h = int(p["date"].split(" ")[1].split(":")[0])
        except (IndexError, ValueError):
            continue
        if h not in hour_perf:
            hour_perf[h] = {"likes": [], "comments": []}
        hour_perf[h]["likes"].append(p.get("likes", 0))
        hour_perf[h]["comments"].append(p.get("comments", 0))

    best_hours = sorted(hour_perf.items(),
                        key=lambda x: _get_normalized_avg(x[1]["likes"]),
                        reverse=True)[:3]
    best_hours_data = [{"hour": h, "avg_likes": round(_get_normalized_avg(d["likes"]))} for h, d in best_hours]

    # Gün analizi
    day_perf = {}
    day_names_tr = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    for p in valid:
        try:
            dt = datetime.strptime(p["date"][:16], "%Y-%m-%d %H:%M")
            day = day_names_tr[dt.weekday()]
        except (ValueError, IndexError):
            continue
        if day not in day_perf:
            day_perf[day] = {"likes": [], "count": 0}
        day_perf[day]["likes"].append(p.get("likes", 0))
        day_perf[day]["count"] += 1

    day_stats = {d: {"count": v["count"], "avg_likes": round(_get_normalized_avg(v["likes"]))}
                 for d, v in day_perf.items()}

    # Hashtag analizi
    all_hashtags = []
    for p in valid:
        all_hashtags.extend(p.get("hashtags", []))
    top_hashtags = Counter(all_hashtags).most_common(15)

    # Caption uzunluğu vs etkileşim
    short_cap = [p for p in valid if len(p.get("caption", "")) < 100]
    long_cap = [p for p in valid if len(p.get("caption", "")) >= 100]
    cap_analysis = {
        "short_avg_likes": round(_get_normalized_avg([p["likes"] for p in short_cap])) if short_cap else 0,
        "long_avg_likes": round(_get_normalized_avg([p["likes"] for p in long_cap])) if long_cap else 0,
        "short_count": len(short_cap),
        "long_count": len(long_cap),
    }

    # Genel metrikler
    all_likes = [p.get("likes", 0) for p in valid]
    all_comments = [p.get("comments", 0) for p in valid]
    videos = [p for p in valid if p.get("is_video")]
    all_views = [_resolve_post_views(p) or 0 for p in videos]

    avg_likes = _get_normalized_avg(all_likes)
    avg_comments = _get_normalized_avg(all_comments)
    avg_views = _get_normalized_avg(all_views)
    eng_rate = ((avg_likes + avg_comments) / followers) * 100

    # Öneriler üret
    recommendations = []

    # 1. İçerik tipi önerisi
    if cat_stats:
        best_cat = max(cat_stats.items(), key=lambda x: x[1]["avg_likes"])
        worst_cat = min(cat_stats.items(), key=lambda x: x[1]["avg_likes"])
        if best_cat[0] != worst_cat[0]:
            ratio = best_cat[1]["avg_likes"] / max(worst_cat[1]["avg_likes"], 1)
            if ratio > 1.5:
                recommendations.append({
                    "priority": "high",
                    "title": f"{best_cat[0]} içeriklerinize ağırlık verin",
                    "detail": f"{best_cat[0]} gönderileriniz ortalama {best_cat[1]['avg_likes']} beğeni alırken, "
                              f"{worst_cat[0]} gönderileriniz sadece {worst_cat[1]['avg_likes']} beğeni alıyor. "
                              f"{best_cat[0]} oranını artırmanız etkileşimi {ratio:.1f}x artırabilir.",
                })

    # 2. Reels önerisi
    reels_stats = cat_stats.get("Reels", {})
    photo_stats = cat_stats.get("Fotoğraf", {})
    if reels_stats and photo_stats:
        if reels_stats.get("avg_views", 0) > photo_stats.get("avg_likes", 0) * 3:
            recommendations.append({
                "priority": "high",
                "title": "Reels paylaşım sıklığını artırın",
                "detail": f"Reels'leriniz ort. {reels_stats['avg_views']} izlenme alıyor — "
                          f"fotoğraflarınızın {reels_stats['avg_views'] // max(photo_stats['avg_likes'], 1)}x üzerinde. "
                          f"Haftada en az 3-4 reels hedefleyin.",
            })

    # 3. Paylaşım saati
    if best_hours_data:
        recommendations.append({
            "priority": "medium",
            "title": f"En iyi paylaşım saati: {best_hours_data[0]['hour']:02d}:00",
            "detail": f"Verilerinize göre en yüksek etkileşim saat {best_hours_data[0]['hour']:02d}:00'da "
                      f"(ort. {best_hours_data[0]['avg_likes']} beğeni). "
                      f"Paylaşımlarınızı bu saate planlamayı deneyin.",
        })

    # 4. Engagement rate değerlendirme
    if eng_rate < 1.0:
        recommendations.append({
            "priority": "high",
            "title": "Etkileşim oranınız düşük",
            "detail": f"Etkileşim oranınız %{eng_rate:.2f} — sektör ortalamasının altında. "
                      f"CTA (call-to-action) kullanın, soru sorun, anket/quiz paylaşın.",
        })
    elif eng_rate < 3.0:
        recommendations.append({
            "priority": "medium",
            "title": "Etkileşim oranı ortalamada",
            "detail": f"Etkileşim oranınız %{eng_rate:.2f}. Sektör ortalamasında. "
                      f"Yorum çekmek için açık uçlu sorular ve tartışma konuları deneyin.",
        })
    else:
        recommendations.append({
            "priority": "low",
            "title": "Etkileşim oranınız iyi",
            "detail": f"Etkileşim oranınız %{eng_rate:.2f} — ortalamanın üzerinde. Böyle devam edin!",
        })

    # 5. Hashtag çeşitliliği
    unique_tags = len(set(all_hashtags))
    if unique_tags < 10 and len(valid) > 10:
        recommendations.append({
            "priority": "medium",
            "title": "Hashtag çeşitliliğinizi artırın",
            "detail": f"Son {len(valid)} gönderide sadece {unique_tags} farklı hashtag kullanmışsınız. "
                      f"Her gönderi için hedef kitlenize uygun 5-10 farklı hashtag deneyin.",
        })

    # 6. Caption uzunluğu
    if cap_analysis["long_avg_likes"] > cap_analysis["short_avg_likes"] * 1.3 and cap_analysis["long_count"] >= 3:
        recommendations.append({
            "priority": "low",
            "title": "Daha uzun caption'lar yazın",
            "detail": f"Uzun caption'lı gönderileriniz ort. {cap_analysis['long_avg_likes']} beğeni alırken, "
                      f"kısa olanlar {cap_analysis['short_avg_likes']} alıyor. Hikaye anlatımı etkileşimi artırıyor.",
        })

    # 7. Paylaşım sıklığı
    if len(valid) >= 2:
        try:
            dates = sorted([datetime.strptime(p["date"][:16], "%Y-%m-%d %H:%M") for p in valid])
            span_days = max((dates[-1] - dates[0]).days, 1)
            freq = len(valid) / (span_days / 7)  # haftalık
            if freq < 2:
                recommendations.append({
                    "priority": "high",
                    "title": "Paylaşım sıklığını artırın",
                    "detail": f"Haftada ortalama {freq:.1f} gönderi paylaşıyorsunuz. "
                              f"Algoritma düzenli paylaşımı ödüllendiriyor. Haftada 4-5 hedefleyin.",
                })
        except ValueError:
            pass

    # ======== YENİ: Viral İçerik Tespiti ========
    avg_eng = avg_likes + avg_comments
    viral_posts = []
    for p in valid:
        p_eng = (p.get("likes", 0) + p.get("comments", 0))
        if avg_eng > 0 and p_eng > avg_eng * 2.5:
            viral_posts.append({
                "shortcode": p.get("shortcode", ""),
                "date": p.get("date", ""),
                "category": p.get("category", ""),
                "likes": p.get("likes", 0),
                "comments": p.get("comments", 0),
                "views": _resolve_post_views(p) or 0,
                "multiplier": round(p_eng / avg_eng, 1),
                "caption_preview": (p.get("caption", "") or "")[:120],
                "url": p.get("url", ""),
                "hashtags": p.get("hashtags", []),
            })
    viral_posts.sort(key=lambda x: x["multiplier"], reverse=True)

    # Viral pattern analiz — hangi ortak özellikler var
    viral_patterns = []
    if viral_posts:
        vc = Counter(vp["category"] for vp in viral_posts)
        top_vcat = vc.most_common(1)[0] if vc else None
        if top_vcat and top_vcat[1] >= 2:
            viral_patterns.append(f"Viral içeriklerinizin çoğu ({top_vcat[1]}/{len(viral_posts)}) {top_vcat[0]} formatında")

        viral_tags = []
        for vp in viral_posts:
            viral_tags.extend(vp.get("hashtags", []))
        vtc = Counter(viral_tags).most_common(3)
        if vtc:
            viral_patterns.append("Viral postlarda en çok kullanılan hashtag'ler: " + ", ".join(f"#{t}" for t, _ in vtc))

        viral_hours = []
        for vp in viral_posts:
            try:
                viral_hours.append(int(vp["date"].split(" ")[1].split(":")[0]))
            except (IndexError, ValueError):
                pass
        if viral_hours:
            avg_vh = round(sum(viral_hours) / len(viral_hours))
            viral_patterns.append(f"Viral içerikler genelde saat {avg_vh:02d}:00 civarı paylaşılmış")

    # ======== YENİ: İçerik Mix Optimizasyonu ========
    total_valid = len(valid)
    content_mix = {}
    optimal_mix = {"Reels": 0.45, "Fotoğraf": 0.25, "Carousel": 0.20, "Video": 0.10}
    for cat, data in cat_stats.items():
        current_pct = round(data["count"] / total_valid * 100, 1) if total_valid else 0
        opt_pct = optimal_mix.get(cat, 0.1) * 100
        content_mix[cat] = {
            "count": data["count"],
            "current_pct": current_pct,
            "optimal_pct": round(opt_pct, 1),
            "avg_likes": data["avg_likes"],
            "gap": round(opt_pct - current_pct, 1),
        }

    mix_recommendations = []
    for cat, mx in content_mix.items():
        if mx["gap"] > 10:
            mix_recommendations.append(f"{cat} oranınızı artırın: şu an %{mx['current_pct']}, ideal %{mx['optimal_pct']}")
        elif mx["gap"] < -15:
            mix_recommendations.append(f"{cat} oranınız yüksek (%{mx['current_pct']}), diğer formatlara alan açın")

    # ======== YENİ: Büyüme Potansiyeli ========
    growth_data = {}
    if len(valid) >= 5:
        try:
            dates = sorted([(datetime.strptime(p["date"][:16], "%Y-%m-%d %H:%M"), p) for p in valid], key=lambda x: x[0])
            span_days = max((dates[-1][0] - dates[0][0]).days, 1)
            weekly_freq = len(valid) / (span_days / 7)

            # Son 10 vs ilk 10 post karşılaştırma (trend)
            recent = [p for _, p in dates[-10:]]
            older = [p for _, p in dates[:10]]
            recent_avg = _get_normalized_avg([p.get("likes", 0) for p in recent])
            older_avg = _get_normalized_avg([p.get("likes", 0) for p in older])
            trend_pct = round(((recent_avg - older_avg) / max(older_avg, 1)) * 100, 1)

            # Tahmini aylık yeni takipçi (engagement → follow dönüşüm oranı ~%2-5)
            monthly_engagement = avg_eng * weekly_freq * 4.3
            estimated_monthly_follows = round(monthly_engagement * 0.03)  # %3 dönüşüm

            growth_data = {
                "weekly_frequency": round(weekly_freq, 1),
                "trend_pct": trend_pct,
                "trend_direction": "up" if trend_pct > 5 else ("down" if trend_pct < -5 else "stable"),
                "recent_avg_likes": round(recent_avg),
                "older_avg_likes": round(older_avg),
                "estimated_monthly_new_followers": estimated_monthly_follows,
                "span_days": span_days,
            }
        except (ValueError, IndexError):
            pass

    # ======== YENİ: Reklam Bütçe Optimizasyonu ========
    ad_budget_analysis = {}
    if followers > 0 and avg_eng > 0:
        current_monthly_reach = avg_eng * (growth_data.get("weekly_frequency", 3) * 4.3) if growth_data else avg_eng * 12
        # Aynı erişimi reklam ile almak için
        ads_monthly_cost = (current_monthly_reach / 1000) * DEFAULT_CPM_TL

        # Farklı bütçe senaryoları
        scenarios = []
        for budget in [500, 1000, 2500, 5000, 10000]:
            paid_reach = (budget / DEFAULT_CPM_TL) * 1000
            total_reach = current_monthly_reach + paid_reach
            boost_pct = round((paid_reach / max(current_monthly_reach, 1)) * 100, 1)
            estimated_new_followers = round(paid_reach * 0.005)  # %0.5 follow oranı
            scenarios.append({
                "budget_tl": budget,
                "paid_reach": round(paid_reach),
                "total_reach": round(total_reach),
                "boost_pct": boost_pct,
                "estimated_new_followers": estimated_new_followers,
            })

        ad_budget_analysis = {
            "organic_monthly_reach": round(current_monthly_reach),
            "organic_reach_ad_value": round(ads_monthly_cost),
            "cpm_used": DEFAULT_CPM_TL,
            "scenarios": scenarios,
        }

    # ======== YENİ: Dijital Pazarlama Strateji Önerileri ========
    strategy_tips = []

    brand_name = profile_info.get("username", "marka")

    # UGC stratejisi
    if eng_rate > 1.5:
        strategy_tips.append({
            "category": "UGC",
            "title": "Kullanıcı İçerik Kampanyası Başlatın",
            "detail": f"Etkileşim oranınız %{eng_rate:.2f} ile iyi seviyede. "
                      f"Müşterilerinizden markanıza özel bir hashtag ile içerik paylaşmalarını isteyin. "
                      f"En iyi UGC'leri resmi hesaptan paylaşmak hem içerik üretim maliyetini düşürür "
                      f"hem topluluk hissi yaratır.",
        })
    else:
        strategy_tips.append({
            "category": "UGC",
            "title": "Etkileşimi Artırarak UGC'ye Zemin Hazırlayın",
            "detail": f"Etkileşim oranınız %{eng_rate:.2f}. Önce soru soran, anket içeren, "
                      f"kaydet/paylaş çağrısı yapan postlarla etkileşimi yükseltin. "
                      f"Sonra UGC kampanyasına geçiş yapın.",
        })

    # Reels stratejisi
    reels_ct = cat_stats.get("Reels", {}).get("count", 0)
    reels_ratio = (reels_ct / total_valid * 100) if total_valid > 0 else 0
    if reels_ratio < 30:
        strategy_tips.append({
            "category": "Reels",
            "title": "Reels-First Stratejiye Geçin",
            "detail": f"İçeriklerinizin sadece %{reels_ratio:.0f}'ı Reels. Instagram algoritması "
                      f"Reels'i öne çıkarıyor — keşfet sayfasında organik erişim potansiyeli "
                      f"feed postlarının 3-5x üzerinde. Hedef: paylaşımların en az %40-50'si Reels olsun. "
                      f"Kahve hazırlama, latte art, behind-the-scenes tarzı kısa videolar çok etkili.",
        })

    # Influencer micro-collab
    strategy_tips.append({
        "category": "Influencer",
        "title": "Mikro-Influencer İşbirlikleri Planlayın",
        "detail": f"{fmt(followers)} takipçi ile büyümek için 5K-50K arası food/lifestyle "
                  f"mikro-influencer'larla barter (ürün karşılığı içerik) veya küçük bütçeli "
                  f"işbirlikleri yapın. Sponsorluk ROI sekmesinden maliyetleri karşılaştırabilirsiniz.",
    })

    # Carousel derinlik analizi
    carousel_stats = cat_stats.get("Carousel", {})
    if carousel_stats and carousel_stats.get("avg_likes", 0) > 0:
        carousel_vs_photo = carousel_stats.get("avg_likes", 0) / max(cat_stats.get("Fotoğraf", {}).get("avg_likes", 1), 1)
        if carousel_vs_photo > 1.2:
            strategy_tips.append({
                "category": "Carousel",
                "title": "Carousel Gönderileri Etkili — Daha Fazla Kullanın",
                "detail": f"Carousel gönderileriniz fotoğraflardan {carousel_vs_photo:.1f}x daha fazla beğeni alıyor. "
                          f"Eğitici içerikler, ürün karşılaştırmaları ve adım adım tarifler için "
                          f"carousel formatını tercih edin. Kaydetme (save) oranını artırır.",
            })

    # Hashtag stratejisi
    if top_hashtags and len(top_hashtags) >= 3:
        top_3 = [f"#{t}" for t, _ in top_hashtags[:3]]
        strategy_tips.append({
            "category": "Hashtag",
            "title": "Hashtag Stratejinizi Çeşitlendirin",
            "detail": f"En çok kullandığınız hashtagler: {', '.join(top_3)}. "
                      f"Bunlara ek olarak niş ve yerel hashtagler (şehir adı, bölge, etkinlik) "
                      f"kullanarak keşfet algoritmasında daha fazla görünürlük kazanın. "
                      f"Her gönderide 8-15 arası hashtag ideal.",
        })

    # Sezonsal içerik önerisi
    strategy_tips.append({
        "category": "Sezonsal",
        "title": "Sezonsal & Trend İçeriklere Uyum Sağlayın",
        "detail": "Hesabınızın konusuna uygun mevsimsel içerikler "
                  "ve güncel trendlere (viral sesler, challenge'lar) hızlı adaptasyon "
                  "keşfet görünürlüğünü ciddi artırır. İçerik takvimini sezonlara göre planlayın.",
    })

    # Topluluk yönetimi
    if avg_comments > 5:
        strategy_tips.append({
            "category": "Topluluk",
            "title": "Yorum Yönetimi ile Topluluk Oluşturun",
            "detail": f"Ortalama {round(avg_comments)} yorum alıyorsunuz. Her yoruma kişisel ve samimi "
                      f"yanıtlar verin — bu algoritma sinyali güçlendirir ve takipçi bağlılığını artırır. "
                      f"İlk 30 dakikadaki yanıtlar özellikle önemli.",
        })

    # Yerel SEO / konum
    strategy_tips.append({
        "category": "Yerel",
        "title": "Konum Etiketlerini Stratejik Kullanın",
        "detail": "Konumla ilgili içeriklerinizde uygun konumu etiketleyin. Konum bazlı keşfette "
                  "yakın kullanıcılara erişim sağlayabilirsiniz. Uygun konum etiketi "
                  "organik yerel erişimi ciddi artırır.",
    })

    # Hikaye stratejisi
    strategy_tips.append({
        "category": "Stories",
        "title": "Günlük Hikaye Rutini Oluşturun",
        "detail": "Hedef kitlenize uygun bir hikaye rutini deneyin: süreçler, ipuçları, "
                  "müşteri anları, poll/quiz, behind-the-scenes. Hikayeler feed'den bağımsız "
                  "olarak sürekli görünürlük sağlar ve profil ziyaretlerini artırır.",
    })

    # Dönüşüm
    if followers > 5000:
        strategy_tips.append({
            "category": "Dönüşüm",
            "title": "Instagram → Web Sitesi/Sipariş Dönüşümünü Artırın",
            "detail": "Profil bağlantınızdan ilgili web sitesi, ürün ve "
                      "kampanya sayfalarına yönlendirin. Her 3-4 gönderiden birinde CTA ile "
                      "'Link bio'da' yazın. Stories'de swipe-up/link sticker ile doğrudan "
                      "ürün sayfasına gönderin.",
        })

    return {
        "profile": profile_info,
        "summary": {
            "total_analyzed": len(valid),
            "avg_likes": round(avg_likes),
            "avg_comments": round(avg_comments),
            "avg_views": round(avg_views),
            "engagement_rate": round(eng_rate, 2),
            "followers": followers,
        },
        "category_breakdown": cat_stats,
        "best_hours": best_hours_data,
        "day_stats": day_stats,
        "top_hashtags": [{"tag": t, "count": c} for t, c in top_hashtags],
        "caption_analysis": cap_analysis,
        "recommendations": sorted(recommendations, key=lambda r: {"high": 0, "medium": 1, "low": 2}.get(r["priority"], 3)),
        "viral_posts": viral_posts[:10],
        "viral_patterns": viral_patterns,
        "content_mix": content_mix,
        "mix_recommendations": mix_recommendations,
        "growth_data": growth_data,
        "ad_budget_analysis": ad_budget_analysis,
        "strategy_tips": strategy_tips,
    }


@app.route("/api/brand_analysis", methods=["POST"])
def api_brand_analysis():
    """Kullanıcının açıkça seçtiği Instagram hesabı için tam analiz."""
    data = request.get_json() or {}
    username = extract_username(data.get("input", ""))
    if not username:
        return jsonify({"error": "Analiz edilecek Instagram hesabını girin."}), 400
    days = int(data.get("days", 90))
    cutoff = datetime.now() - timedelta(days=days)
    demo_mode = data.get("demo", False)

    if demo_mode:
        pi, pd = get_demo_data(username)
        result = _analyze_brand(pi, pd)
        result["demo"] = True
        return jsonify(result)

    pi, pd, err = fetch_with_cookies(username, max_posts=500, cutoff_date=cutoff)
    if err == "no_session":
        return jsonify({"error": "Giriş yapmanız gerekiyor.", "suggest_login": True, "suggest_demo": True}), 401
    if err:
        return _instagram_error_response(err, platform="instagram", suggest_demo=True)

    result = _analyze_brand(pi, pd)
    result["demo"] = False
    return jsonify(result)


@app.route("/api/config")
def api_config():
    """Frontend'e varsayılan config'i döndür."""
    return jsonify({
        "default_cpm": DEFAULT_CPM_TL,
        "our_brand": OUR_BRAND_USERNAME,
        "sponsorship_types": {item["id"]: {"label": item["label"], "desc": item["description"]} for item in SPONSORSHIP_PACKAGES},
        "sponsorship_packages": [
            {
                "id": item["id"],
                "label": item["label"],
                "description": item["description"],
                "format": item["format_label"],
                "integration_type": item["integration_label"],
            }
            for item in SPONSORSHIP_PACKAGES
        ],
        "page_type_options": {
            key: {
                "label": value["label"],
                "description": value["description"],
                "pricing_note": value["pricing_note"],
            }
            for key, value in PAGE_TYPE_PROFILES.items()
        },
        "time_ranges": TIME_RANGE_OPTIONS,
        "platform_time_ranges": PLATFORM_TIME_RANGES,
        "platforms": ["instagram", "tiktok", "youtube"],
        "ai_enabled": gemini_client is not None,
        "quick_brands": QUICK_BRANDS,
        "etki_oranlari": get_etki_oranlari()
    })


@app.route("/api/proxy_pic")
def proxy_pic():
    """İzin verilen sosyal ağ CDN'lerindeki profil görsellerini güvenli biçimde iletir."""
    import requests as rq
    from urllib.parse import urlparse

    url = request.args.get("url", "")
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return "", 404
    allowed_suffixes = (
        "instagram.com", "cdninstagram.com", "fbcdn.net", "ggpht.com", "ytimg.com", "googleusercontent.com",
        "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "byteimg.com", "ibytedtos.com", "muscdn.com",
    )
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443) or not any(
        hostname == suffix or hostname.endswith("." + suffix) for suffix in allowed_suffixes
    ):
        return "", 404
    try:
        session = rq.Session()
        session.trust_env = False
        response = session.get(url, headers={"User-Agent": INSTAGRAM_USER_AGENT}, timeout=(5, 15), allow_redirects=False)
        if response.status_code != 200:
            return "", 502
        content_type = response.headers.get("Content-Type", "image/jpeg")
        if not content_type.lower().startswith("image/"):
            return "", 502
        return Response(
            response.content,
            mimetype=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except Exception:
        return "", 502

@app.route("/api/compare", methods=["POST"])
def api_compare():
    """Birden fazla hesabı tek seferde çekip karşılaştırma verisi döndürür."""
    import time as _time
    data = request.get_json() or {}
    usernames = data.get("usernames", [])
    if not usernames or len(usernames) < 2:
        return jsonify({"error": "En az 2 hesap gerekli."}), 400
    if len(usernames) > 5:
        return jsonify({"error": "En fazla 5 hesap karşılaştırılabilir."}), 400

    results = []
    for i, raw in enumerate(usernames):
        uname = extract_username(raw)
        if not uname:
            continue
        pi, pd, err = fetch_with_cookies(uname, 12)
        if err or not pi:
            continue

        # Calculate only from observed values; absent counters remain unavailable.
        posts = pd or []
        likes = [_coerce_nonnegative_count(post.get("likes")) for post in posts]
        comments = [_coerce_nonnegative_count(post.get("comments")) for post in posts]
        likes = [value for value in likes if value is not None]
        comments = [value for value in comments if value is not None]
        views = [_resolve_post_views(post) for post in posts]
        views = [value for value in views if value is not None]
        n_posts = len(posts)
        n_vids = sum(bool(post.get("is_video")) for post in posts)
        avg_likes = round(_get_normalized_avg(likes)) if likes else None
        avg_comments = round(_get_normalized_avg(comments)) if comments else None
        avg_views = round(_get_normalized_avg(views)) if views else None
        followers = _coerce_nonnegative_count(pi.get("followers"))
        complete = [post["likes"] + post["comments"] for post in posts
                    if _coerce_nonnegative_count(post.get("likes")) is not None
                    and _coerce_nonnegative_count(post.get("comments")) is not None]
        eng_rate = round(_get_normalized_avg(complete) / followers * 100, 2) if complete and followers else None

        # İçerik dağılımı
        cats = {}
        for p in (pd or []):
            c = p.get("category", "Diğer")
            cats[c] = cats.get(c, 0) + 1
        content_dist = {k: round(v / n_posts * 100, 1) for k, v in cats.items()} if n_posts else {}

        # En iyi post
        best_post = None
        comment_quality_score = 50
        if pd:
            bp = max(pd, key=lambda p: ((_coerce_nonnegative_count(p.get("likes")) or 0) + (_coerce_nonnegative_count(p.get("comments")) or 0)))
            best_post = {
                "shortcode": bp.get("shortcode", ""),
                "url": bp.get("url", ""),
                "likes": bp.get("likes", 0),
                "comments": bp.get("comments", 0),
                "category": bp.get("category", ""),
                "date": bp.get("date", ""),
            }
            if best_post["shortcode"]:
                comment_quality_score = _get_comment_quality_for_shortcode(best_post["shortcode"])

        results.append({
            "username": pi.get("username"),
            "full_name": pi.get("full_name"),
            "profile_pic_url": pi.get("profile_pic_url"),
            "followers": pi.get("followers", 0),
            "following": pi.get("following", 0),
            "post_count": pi.get("post_count", 0),
            "is_verified": pi.get("is_verified", False),
            "is_business": pi.get("is_business", False),
            "avg_likes": avg_likes,
            "avg_comments": avg_comments,
            "avg_views": avg_views,
            "engagement_rate": eng_rate,
            "video_ratio": round(n_vids / n_posts * 100, 1) if n_posts else 0,
            "content_distribution": content_dist,
            "best_post": best_post,
            "comment_quality_score": comment_quality_score,
            "posts_analyzed": n_posts,
        })

        # Rate limit arasında bekle
        if i < len(usernames) - 1:
            _time.sleep(3)

    if results:
        max_views = max((r.get("avg_views") or 0) for r in results) or 1
        max_likes = max((r.get("avg_likes") or 0) for r in results) or 1
        max_comments = max((r.get("avg_comments") or 0) for r in results) or 1
        max_followers = max((r.get("followers") or 0) for r in results) or 1
        max_eng = max((r.get("engagement_rate") or 0) for r in results) or 1
        
        weights = get_etki_oranlari()
        for r in results:
            n_views = ((r.get("avg_views") or 0) / max_views) * 100
            n_likes = ((r.get("avg_likes") or 0) / max_likes) * 100
            n_comments = ((r.get("avg_comments") or 0) / max_comments) * 100
            n_followers = ((r.get("followers") or 0) / max_followers) * 100
            n_eng = ((r.get("engagement_rate") or 0) / max_eng) * 100
            
            # Yorum kalitesini yoruma olan etkiye doğrudan çarpalım (Kaliteli yorum daha değerli)
            q_multiplier = r["comment_quality_score"] / 100.0
            
            total_score = (n_views * weights.get("izlenme", 0.35)) + \
                          (n_likes * weights.get("begeni", 0.25)) + \
                          (n_comments * weights.get("yorum", 0.25) * q_multiplier) + \
                          (n_followers * weights.get("takipci", 0.05)) + \
                          (n_eng * weights.get("etkilesim", 0.10))
            r["total_score"] = round(total_score, 1)

    results.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    if len(results) < 2:
        return jsonify({"error": "Yeterli hesap verisi çekilemedi."}), 503

    return jsonify({"accounts": results})

@app.route("/api/analyze_comments", methods=["POST"])
def analyze_comments():
    data = request.get_json()
    input_val = data.get("input", "").strip()
    if not input_val:
        return jsonify({"error": "Link veya shortcode eksik."}), 400
        
    shortcode = input_val
    if "instagram.com/p/" in input_val or "instagram.com/reel/" in input_val:
        m = re.search(r"/(?:p|reel)/([^/?]+)", input_val)
        if m:
            shortcode = m.group(1)
            
    if not gemini_client:
        return jsonify({"error": "Gemini API Key tanımlı değil."}), 500

    try:
        comments = _fetch_comments_v1(shortcode, max_comments=30)
            
        if not comments:
            return jsonify({"error": "Bu gönderide yorum bulunamadı."}), 404
            
        prompt = f"""Sen uzman bir sosyal medya veri analistisin. 
Aşağıdaki Instagram yorumlarını incele ve tam olarak 3 kategoriye ayır:
1. genuine_count: Organik, gerçek kullanıcı etkileşimi veya markaya duyulan sevgi/beğeni mesajları.
2. commercial_count: Fiyat soran, satın alma niyeti belirten veya müşteri hizmetleri (şikayet/talep) mesajları.
3. spam_count: Sadece emoji atan, bot hesap, "profilime bak", "takip et" gibi gereksiz mesajlar.

Sonucu SADECE geçerli bir JSON formatında şu yapıda döndür, Markdown kullanma, sadece JSON:
{{
    "genuine_count": <sayı>,
    "commercial_count": <sayı>,
    "spam_count": <sayı>,
    "summary_html": "<Genel bir analiz özeti (1 paragraf, Türkçe HTML formatında)>"
}}

YORUMLAR:
{json.dumps(comments, ensure_ascii=False)}
"""
        
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        
        text_resp = response.text.strip()
        if text_resp.startswith("```json"):
            text_resp = text_resp[7:]
        if text_resp.endswith("```"):
            text_resp = text_resp[:-3]
            
        result = json.loads(text_resp.strip())
        result["total_analyzed"] = len(comments)
        
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/content_dna", methods=["POST"])
def content_dna():
    data = request.get_json()
    username = extract_username(data.get("input", ""))
    if not username:
        return jsonify({"error": "Geçersiz kullanıcı adı."}), 400

    time_range = str(data.get("timeRange", "90"))
    try:
        days = int(time_range)
        cutoff_date = datetime.now() - timedelta(days=days)
    except ValueError:
        cutoff_date = datetime.now() - timedelta(days=90)

    if not gemini_client:
        return jsonify({"error": "Gemini API Key tanımlı değil."}), 500

    try:
        # Instaloader graphql yerine native v1 API kullanarak 403 hatalarını önlüyoruz
        pi, posts_raw, err = fetch_with_cookies(username, max_posts=500, cutoff_date=cutoff_date)
        if err:
            return _instagram_error_response(err, platform="instagram", suggest_demo=False)
        if not posts_raw:
            return jsonify({"error": "Hiç gönderi bulunamadı."}), 404

        # posts_raw'ı content_dna formatına dönüştür
        posts = []
        for p in posts_raw:
            views = _resolve_post_views(p)
            posts.append({
                "shortcode": p.get("shortcode", ""),
                "views": views,
                "url": p.get("url", ""),
                "caption": p.get("caption", ""),
                "likes": p.get("likes", 0),
                "comments_count": p.get("comments", 0),
            })
            
        if not posts:
            return jsonify({"error": "Hiç gönderi bulunamadı."}), 404
            
        measured = [post for post in posts if post["views"] is not None]
        if len(measured) >= 2:
            ranked = sorted(measured, key=lambda post: post["views"], reverse=True)
            ranking_note = "Sıralama, izlenmesi mevcut gönderilerin ölçülen izlenme sayısına göredir."
        else:
            ranked = [post for post in posts if _coerce_nonnegative_count(post["likes"]) is not None
                      and _coerce_nonnegative_count(post["comments_count"]) is not None]
            ranked.sort(key=lambda post: post["likes"] + post["comments_count"], reverse=True)
            ranking_note = "Yeterli izlenme verisi yok; sıralama görünen beğeni ve yorum toplamına göredir. Eksik izlenmeler tahmin edilmez."
        if not ranked:
            return jsonify({"error": "Sıralama için yeterli ölçülmüş metrik bulunamadı."}), 422

        best_posts = ranked[:5]
        worst_posts = ranked[-5:]
        
        import requests
        from PIL import Image
        from io import BytesIO
        
        contents = [
            ranking_note + "\n",
            "Sen uzman bir içerik stratejistisin. Aşağıda sana bir Instagram hesabının EN İYİ PERFORMANS GÖSTEREN 5 gönderisi ile EN KÖTÜ PERFORMANS GÖSTEREN 5 gönderisinin kapak fotoğraflarını, açıklama metinlerini (caption) ve gelen YORUMLARIN TAMAMINI veriyorum.\n\n"
            "Lütfen her bir gönderiyi kendi özelinde detaylıca analiz et. (Görsel, metin ve yorumlara bakarak neden başarılı veya başarısız olduğunu açıkla).\n"
            "Ayrıca en sonda tüm bu gönderileri kapsayan genel bir strateji ve hesaba özel yaratıcı bir tavsiye sun.\n\n"
        ]
        
        def _get_post_thumbnail(shortcode):
            """Gönderi thumbnail'ini oembed API üzerinden alır."""
            try:
                r = requests.get(
                    f"https://www.instagram.com/p/{shortcode}/media/?size=m",
                    timeout=10,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    img = Image.open(BytesIO(r.content))
                    img.thumbnail((300, 300))
                    return img
            except Exception:
                pass
            return None
        
        for i, bp in enumerate(best_posts):
            img = _get_post_thumbnail(bp["shortcode"])
            comments_text = _fetch_comments_v1(bp["shortcode"], max_comments=100)
            contents.append(f"EN İYİ GÖNDERİ id:{bp['shortcode']} (Beğeni: {bp.get('likes', 0)} | Metin: {bp['caption'][:200]}... | Yorumlar: {json.dumps(comments_text, ensure_ascii=False)}):")
            if img:
                contents.append(img)
            
        for i, wp in enumerate(worst_posts):
            img = _get_post_thumbnail(wp["shortcode"])
            comments_text = _fetch_comments_v1(wp["shortcode"], max_comments=100)
            contents.append(f"EN KÖTÜ GÖNDERİ id:{wp['shortcode']} (Beğeni: {wp.get('likes', 0)} | Metin: {wp['caption'][:200]}... | Yorumlar: {json.dumps(comments_text, ensure_ascii=False)}):")
            if img:
                contents.append(img)
            
        contents.append("""Lütfen bu verileri kıyaslayarak bana SADECE aşağıdaki JSON formatında bir rapor döndür. (Markdown kullanma, sadece JSON)
{
    "best_posts_analysis": {
        "shortcode_1": "Bu gönderi başarılı çünkü...",
        "shortcode_2": "Bu gönderi başarılı çünkü..."
    },
    "worst_posts_analysis": {
        "shortcode_3": "Bu gönderi başarısız çünkü...",
        "shortcode_4": "Bu gönderi başarısız çünkü..."
    },
    "overall_strategy": "Tüm postları kapsayan hesaba özel detaylı strateji ve öneriler..."
}
Not: JSON anahtarlarında gönderilerin id değerlerini (shortcode) kullan.
""")
        
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
        )
        
        text_resp = response.text.strip()
        if text_resp.startswith("```json"):
            text_resp = text_resp[7:]
        if text_resp.endswith("```"):
            text_resp = text_resp[:-3]
            
        ai_analysis = json.loads(text_resp.strip())
        
        # Frontend için datayı hazırla
        final_response = {
            "ranking_note": ranking_note,
            "best_posts": [],
            "worst_posts": [],
            "overall_strategy": ai_analysis.get("overall_strategy", "")
        }
        
        best_analysis = ai_analysis.get("best_posts_analysis", {})
        for bp in best_posts:
            bp_copy = bp.copy()
            bp_copy["thumbnail_url"] = f"https://www.instagram.com/p/{bp['shortcode']}/media/?size=m"
            bp_copy["ai_analysis"] = best_analysis.get(bp["shortcode"], "Analiz bulunamadı.")
            final_response["best_posts"].append(bp_copy)
            
        worst_analysis = ai_analysis.get("worst_posts_analysis", {})
        for wp in worst_posts:
            wp_copy = wp.copy()
            wp_copy["thumbnail_url"] = f"https://www.instagram.com/p/{wp['shortcode']}/media/?size=m"
            wp_copy["ai_analysis"] = worst_analysis.get(wp["shortcode"], "Analiz bulunamadı.")
            final_response["worst_posts"].append(wp_copy)
            
        return jsonify(final_response)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
if __name__ == "__main__":
    print("=" * 50)
    print("  Social Media Analysis for Everyone")
    print("  http://localhost:3060")
    print("=" * 50)
    app.run(debug=False, host="127.0.0.1", port=int(os.getenv("PORT", "3060")))
