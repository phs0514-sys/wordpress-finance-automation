"""Evidence-led US/JP/KR finance publisher for WordPress.

Secrets are read from environment variables or a local .env file.  The default
publish mode is draft so a new site cannot accidentally mass-publish.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from html import escape as html_escape
import json
import math
import os
import re
import struct
import sys
from datetime import date, datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request
import zlib
import xml.etree.ElementTree as ET
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
HISTORY_PATH = DATA_DIR / "article_history.json"
STRATEGY_PATH = DATA_DIR / "weekly_strategy.json"
try:
    LOCAL_ZONE = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:  # Windows Python may not bundle the IANA database.
    LOCAL_ZONE = timezone(timedelta(hours=9), "Asia/Seoul")


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_json_file(path: Path, default: Any) -> Any:
    """Read a local JSON data file, treating a first run as an empty store."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json_file(path: Path, value: Any) -> None:
    """Atomically persist operational data without ever writing credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_article_history() -> list[dict[str, Any]]:
    value = load_json_file(HISTORY_PATH, [])
    return value if isinstance(value, list) else []


def record_article_history(
    locale: str,
    article: dict[str, Any],
    brief: dict[str, Any],
    review: dict[str, Any],
    post: dict[str, Any],
    action: str,
) -> dict[str, Any]:
    """Append a durable performance row for later 24h/72h/7d/28d learning."""
    seo = article.get("seo") if isinstance(article.get("seo"), dict) else {}
    review_detail = review.get("review") if isinstance(review.get("review"), dict) else review
    history = load_article_history()
    row = {
        "locale": locale,
        "post_id": post.get("id"),
        "url": post.get("link", ""),
        "topic": brief.get("topic") or article.get("title", ""),
        "category": brief.get("category", ""),
        "primary_keyword": seo.get("focus_keyword") or brief.get("focus_keyword", ""),
        "secondary_keywords": seo.get("related_keywords") or brief.get("related_keywords", []),
        "trend_score": brief.get("trend_score", brief.get("click_potential", 0)),
        "opportunity_score": brief.get("opportunity_score", brief.get("click_potential", 0)),
        "serp_competition": brief.get("serp_competition", brief.get("competition", 0)),
        "headline_type": brief.get("headline_type", ""),
        "word_count": len(re.findall(r"\w+", re.sub(r"<[^>]+>", " ", str(article.get("html", ""))), flags=re.UNICODE)),
        "layout_type": article.get("layout_type") or brief.get("layout_type", "explainer"),
        "image_type": post.get("_image_kinds", []),
        "sources_count": len(article.get("sources", [])) if isinstance(article.get("sources"), list) else 0,
        "original_value_count": len(brief.get("original_value", [])) if isinstance(brief.get("original_value"), list) else 0,
        "publish_time": post.get("date") or datetime.now(timezone.utc).isoformat(),
        "action": action,
        "quality_score": review.get("score", review_detail.get("score")),
        "quality_breakdown": review_detail.get("breakdown", {}),
        "originality_count": review_detail.get("originality_count", 0),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "24h": {"impressions": None, "clicks": None, "ctr": None, "position": None},
            "72h": {"impressions": None, "clicks": None, "ctr": None, "position": None},
            "7d": {"impressions": None, "clicks": None, "ctr": None, "position": None},
            "28d": {"impressions": None, "clicks": None, "ctr": None, "position": None},
        },
    }
    # Idempotency protects a retry from adding the same post twice.
    history = [item for item in history if not (item.get("locale") == locale and str(item.get("post_id")) == str(row.get("post_id")))]
    history.append(row)
    save_json_file(HISTORY_PATH, history)
    return row


@dataclass(frozen=True)
class Settings:
    openai_key: str
    model: str
    research_model: str
    writing_model: str
    research_reasoning: str
    writing_reasoning: str
    publish_mode: str
    quality_threshold: int
    max_revisions: int
    wp_url: str
    wp_mode: str
    wp_site_ref: str
    wp_username: str
    wp_password: str
    wp_client_id: str
    wp_client_secret: str
    wp_access_token: str
    gsc_token: str
    gsc_site_url: str

    @classmethod
    def from_env(cls, locale: str) -> "Settings":
        prefix = locale.upper()
        values: dict[str, Any] = {
            "openai_key": os.getenv("OPENAI_API_KEY", ""),
            # Keep `model` as a backwards-compatible alias while making the
            # requested two-stage model split explicit. The old OPENAI_MODEL
            # value is intentionally ignored so a stale local .env cannot
            # silently switch the requested Luna/Terra pair.
            "model": os.getenv("OPENAI_WRITING_MODEL", "gpt-5.6-terra"),
            "research_model": os.getenv("OPENAI_RESEARCH_MODEL", "gpt-5.6-luna"),
            "writing_model": os.getenv("OPENAI_WRITING_MODEL", "gpt-5.6-terra"),
            "research_reasoning": os.getenv("OPENAI_RESEARCH_REASONING", "medium"),
            "writing_reasoning": os.getenv("OPENAI_WRITING_REASONING", "medium"),
            "publish_mode": os.getenv("PUBLISH_MODE", "draft").lower(),
            "quality_threshold": int(os.getenv("QUALITY_THRESHOLD", "90")),
            "max_revisions": int(os.getenv("MAX_REVISIONS", "4")),
            "wp_url": os.getenv(f"WP_{prefix}_URL", "").rstrip("/"),
            "wp_mode": os.getenv(f"WP_{prefix}_MODE", os.getenv("WP_US_MODE", "wpcom")).lower(),
            "wp_site_ref": os.getenv(f"WP_{prefix}_SITE_REF", ""),
            # US/JP/KR sites share one WordPress.com account/application.
            # Keep secrets in one place and let a locale inherit them.
            "wp_username": os.getenv(f"WP_{prefix}_USERNAME", os.getenv("WP_US_USERNAME", "")),
            "wp_password": os.getenv(f"WP_{prefix}_APPLICATION_PASSWORD", os.getenv("WP_US_APPLICATION_PASSWORD", "")),
            "wp_client_id": os.getenv(f"WP_{prefix}_CLIENT_ID", os.getenv("WP_US_CLIENT_ID", "")),
            "wp_client_secret": os.getenv(f"WP_{prefix}_CLIENT_SECRET", os.getenv("WP_US_CLIENT_SECRET", "")),
            "wp_access_token": os.getenv(f"WP_{prefix}_ACCESS_TOKEN", os.getenv("WP_US_ACCESS_TOKEN", "")),
            # Search Console is optional: publishing continues when no
            # read-only GSC bearer token has been configured yet.
            "gsc_token": os.getenv(f"GSC_{prefix}_TOKEN", os.getenv("GSC_TOKEN", "")),
            "gsc_site_url": os.getenv(f"GSC_{prefix}_SITE_URL", os.getenv("GSC_SITE_URL", "")),
        }
        if not values["wp_site_ref"] and values["wp_url"]:
            values["wp_site_ref"] = values["wp_url"].split("//", 1)[-1]
        if not values["gsc_site_url"]:
            values["gsc_site_url"] = values["wp_url"]
        missing = [key for key in ("openai_key", "wp_url") if not values[key]]
        if missing:
            raise ValueError(f"Missing configuration: {', '.join(missing)}")
        if values["wp_mode"] not in {"wpcom", "self_hosted"}:
            raise ValueError("WP_*_MODE must be wpcom or self_hosted")
        if values["wp_mode"] == "wpcom" and not values["wp_access_token"] and not all(values[key] for key in ("wp_client_id", "wp_client_secret", "wp_username", "wp_password")):
            raise ValueError("For wpcom mode provide WP_*_ACCESS_TOKEN or WordPress.com client ID/secret, username, and Application Password")
        if values["wp_mode"] == "self_hosted" and not all(values[key] for key in ("wp_username", "wp_password")):
            raise ValueError("For self_hosted mode provide WP_*_USERNAME and WP_*_APPLICATION_PASSWORD")
        if values["publish_mode"] not in {"draft", "publish"}:
            raise ValueError("PUBLISH_MODE must be draft or publish")
        if not 0 <= values["quality_threshold"] <= 100:
            raise ValueError("QUALITY_THRESHOLD must be between 0 and 100")
        return cls(**values)


def http_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    try:
        # Evidence searches can legitimately take over a minute; keep enough
        # headroom for the Responses API while still bounding a hung request.
        # Web-search-backed generation can be slow on the first request, and
        # scheduled runs should fail only after a generous bounded timeout.
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:800]}") from exc


def http_get_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:800]}") from exc


def multipart_json(url: str, body: bytes, content_type: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type, **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:800]}") from exc


def form_json(url: str, values: dict[str, str]) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(values).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:800]}") from exc


def openai_text(
    settings: Settings,
    instructions: str,
    input_text: str,
    use_web_search: bool = False,
    max_output_tokens: int | None = None,
    json_output: bool = False,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    # Bound model output so a search-backed evidence call cannot run away with
    # an unbounded response on a free-form finance prompt.
    payload: dict[str, Any] = {
        "model": model or settings.model,
        "instructions": instructions,
        "input": input_text,
        "max_output_tokens": max_output_tokens or (12000 if use_web_search else 5000),
        "reasoning": {"effort": reasoning_effort or settings.writing_reasoning},
    }
    if use_web_search:
        # Keep evidence collection bounded for scheduled runs.
        payload["tools"] = [{"type": "web_search", "search_context_size": "high"}]
        payload["max_tool_calls"] = 1
    elif json_output:
        # JSON mode is incompatible with built-in web search, but keeps the
        # article and review responses valid when they contain HTML/quotes.
        payload["text"] = {"format": {"type": "json_object"}, "verbosity": "low"}
    data = http_json(
        "https://api.openai.com/v1/responses",
        payload,
        {"Authorization": f"Bearer {settings.openai_key}"},
    )
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    chunks = [content.get("text", "") for item in data.get("output", []) for content in item.get("content", []) if content.get("type") in {"output_text", "text"}]
    text = "\n".join(chunks).strip()
    if not text:
        raise RuntimeError("OpenAI returned no text")
    return text


def parse_json(text: str) -> dict[str, Any]:
    # Strip a possible UTF-8 BOM and surrounding whitespace before decoding.
    # The Responses API can occasionally include the BOM when content is
    # assembled from multiple output chunks.
    cleaned = text.lstrip("\ufeff").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S)
    decoder = json.JSONDecoder()
    # Responses may include a short preamble or trailing explanation. Decode
    # the first complete object instead of greedily matching every brace.
    for match in re.finditer(r"\{", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Model did not return valid JSON")


def parse_article_output(text: str) -> dict[str, Any]:
    """Normalize either a JSON article or the bounded text format."""
    try:
        value = parse_json(text)
        if value.get("title") and value.get("html"):
            return value
    except ValueError:
        pass
    cleaned = text.strip()
    title_match = re.search(r"(?im)^TITLE\s*:\s*(.+)$", cleaned)
    excerpt_match = re.search(r"(?im)^EXCERPT\s*:\s*(.+)$", cleaned)
    body_match = re.search(r"(?ims)^BODY_HTML\s*:\s*\n?(.*?)(?:\n\s*SOURCES\s*:|\Z)", cleaned)
    if not (title_match and body_match):
        raise ValueError("Model did not return a recognizable article")
    body = body_match.group(1).strip()
    if body.startswith("```") and body.endswith("```"):
        body = re.sub(r"^```(?:html)?\s*|\s*```$", "", body, flags=re.I | re.S).strip()
    if "<" not in body:
        body = "".join(f"<p>{html_escape(part.strip())}</p>" for part in re.split(r"\n\s*\n", body) if part.strip())
    sources_match = re.search(r"(?ims)^SOURCES\s*:\s*\n?(.*)$", cleaned)
    sources = [line.strip(" -*\t") for line in sources_match.group(1).splitlines() if line.strip()] if sources_match else []
    return {"title": title_match.group(1).strip(), "excerpt": excerpt_match.group(1).strip() if excerpt_match else "", "html": body, "sources": sources}


def locale_rules(locale: str) -> tuple[str, str]:
    if locale == "us":
        return "US English", "the relevant US government, regulator, public agency, standards body, or primary issuer for the selected topic (for example SEC, FTC, FDA, CDC, NOAA, IRS, Federal Reserve, Treasury, or official state agencies)"
    if locale == "jp":
        return "natural Japanese", "the relevant Japanese ministry, regulator, public agency, standards body, exchange, central bank, or primary issuer for the selected topic (for example 金融庁, 日本銀行, JPX, 財務省, 厚生労働省, 気象庁, or official local agencies)"
    if locale == "kr":
        return "natural Korean", "the relevant Korean ministry, regulator, public agency, standards body, exchange, central bank, or primary issuer for the selected topic (for example 금융위원회, 금융감독원, 한국은행, 국세청, KRX, 통계청, 질병관리청, 기상청, or official local agencies)"
    raise ValueError("locale must be us, jp, or kr")


GOOGLE_NEWS_CONFIG = {
    "us": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    "jp": {"hl": "ja", "gl": "JP", "ceid": "JP:ja"},
    "kr": {"hl": "ko", "gl": "KR", "ceid": "KR:ko"},
}
GOOGLE_TRENDS_GEO = {"us": "US", "jp": "JP", "kr": "KR"}


def _post_date(row: dict[str, Any]) -> date | None:
    """Parse a WordPress date without allowing a malformed row to stop a run."""
    raw = str(row.get("date") or row.get("modified") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None


_TOPIC_STOPWORDS = {
    "2026", "2025", "today", "latest", "update", "news", "guide", "tips",
    "how", "what", "why", "best", "compare", "comparison", "review", "rules",
    "cost", "costs", "fee", "fees", "change", "changes", "한국", "미국", "일본",
    "관련", "정보", "정리", "최신", "방법", "확인", "비교", "차이", "변화", "안내",
    "今日", "最新", "情報", "解説", "速報", "について", "比較", "違い", "変更", "費用", "手数料",
}


def _topic_terms(value: str) -> set[str]:
    """Extract meaningful multilingual terms for a conservative overlap check."""
    plain = re.sub(r"<[^>]+>", " ", unicodedata.normalize("NFKC", value).casefold())
    tokens = re.findall(r"[a-z0-9][a-z0-9-]{1,}|[\uac00-\ud7a3]{2,}|[\u3040-\u30ff\u3400-\u9fff]{2,}", plain)
    return {
        token.strip("-")
        for token in tokens
        if len(token.strip("-")) >= 2 and token.strip("-") not in _TOPIC_STOPWORDS
    }


def topic_overlaps_recent(topic: str, research: dict[str, Any], recent_posts: list[dict[str, str]]) -> bool:
    """Return true when a candidate repeats a recent story or search intent."""
    if not recent_posts:
        return False
    candidate_text = " ".join(
        str(research.get(key, "")) for key in ("focus_keyword", "search_intent", "angle")
    ) + " " + topic
    focus_terms = _topic_terms(str(research.get("focus_keyword", "")))
    candidate_norm = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7a3]+", "", unicodedata.normalize("NFKC", topic).casefold())
    candidate_terms = _topic_terms(candidate_text)
    for row in recent_posts:
        recent_title = str(row.get("title", ""))
        recent_norm = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7a3]+", "", unicodedata.normalize("NFKC", recent_title).casefold())
        if candidate_norm and recent_norm and (candidate_norm == recent_norm or len(candidate_norm) >= 14 and (candidate_norm in recent_norm or recent_norm in candidate_norm)):
            return True
        recent_terms = _topic_terms(recent_title)
        shared = candidate_terms & recent_terms
        # A repeated explicit focus keyword is a hard collision even when the
        # surrounding headline has been translated or rephrased.
        if any(term in recent_terms and (len(term) >= 3 or any("\uac00" <= char <= "\ud7a3" or "\u3040" <= char <= "\u9fff" for char in term)) for term in focus_terms):
            return True
        # Two shared meaningful terms usually indicate the same event/entity;
        # a single long distinctive term catches repeated named events.
        if len(shared) >= 2 or any(len(term) >= 5 for term in shared):
            return True
    return False


def google_news_snapshot(locale: str, limit: int = 12) -> list[dict[str, str]]:
    """Read a small, keyless Google News RSS snapshot for trend discovery.

    Google Programmable Search requires a paid/managed API key in many setups;
    the public News RSS feed provides a free, locale-specific signal that is
    then verified by the research model's web-search call.
    """
    params = urllib.parse.urlencode(GOOGLE_NEWS_CONFIG[locale])
    request = urllib.request.Request(
        f"https://news.google.com/rss?{params}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; FinanceResearchBot/1.0)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(response.read())
    except Exception:
        return []
    rows: list[dict[str, str]] = []
    for item in root.findall("./channel/item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        source = item.find("source")
        source_name = (source.text or "").strip() if source is not None else ""
        if title and link:
            rows.append({"title": title, "url": link, "published": published, "source": source_name})
    return rows


def google_trends_snapshot(locale: str, limit: int = 20) -> list[dict[str, str]]:
    """Read Google's keyless daily-trending-search RSS for the locale."""
    request = urllib.request.Request(
        f"https://trends.google.com/trending/rss?geo={GOOGLE_TRENDS_GEO[locale]}",
        headers={"User-Agent": "Mozilla/5.0 (compatible; FinanceResearchBot/1.0)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(response.read())
    except Exception:
        return []
    rows: list[dict[str, str]] = []
    for item in root.findall("./channel/item")[:limit]:
        title = (item.findtext("title") or "").strip()
        traffic = ""
        for child in item:
            if child.tag.endswith("approx_traffic"):
                traffic = (child.text or "").strip()
                break
        if title:
            rows.append({"term": title, "approx_traffic": traffic})
    return rows


def _traffic_value(value: str) -> float:
    match = re.search(r"([\d,.]+)\s*([KMB])?", str(value).upper())
    if not match:
        return 0.0
    number = float(match.group(1).replace(",", ""))
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(match.group(2) or "", 1)
    # Log scaling keeps a single viral term from crowding out every other
    # candidate while still rewarding a rising high-interest signal.
    return min(100.0, math.log10(max(1.0, number * multiplier)) * 11.0)


def build_candidate_pool(news: list[dict[str, str]], trends: list[dict[str, str]], limit: int = 50) -> list[dict[str, Any]]:
    """Combine locale-specific RSS signals into a de-duplicated 30–50 pool."""
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [*trends, *news]:
        title = str(row.get("term") or row.get("title") or "").strip()
        if not title:
            continue
        key = re.sub(r"\W+", "", unicodedata.normalize("NFKC", title).casefold(), flags=re.UNICODE)
        if len(key) < 3 or key in seen:
            continue
        seen.add(key)
        pool.append({
            "candidate": title,
            "source": "trends" if row.get("term") else "news",
            "signal_score": round(_traffic_value(str(row.get("approx_traffic", ""))), 1),
            "url": str(row.get("url", "")),
            "published": str(row.get("published", "")),
        })
        if len(pool) >= limit:
            break
    return pool


def calculate_opportunity_score(components: dict[str, Any] | None, fallback: Any = 0) -> int:
    """Score a topic for this site rather than rewarding raw popularity alone."""
    components = components if isinstance(components, dict) else {}
    try:
        fallback_value = float(fallback or 0)
    except (TypeError, ValueError):
        fallback_value = 0.0
    weights = {
        "interest": 0.20,
        "velocity": 0.15,
        "search_intent": 0.15,
        "serp_feasibility": 0.20,
        "ctr_potential": 0.10,
        "durability": 0.10,
        "site_fit": 0.10,
    }
    values: dict[str, float] = {}
    for key in weights:
        try:
            values[key] = max(0.0, min(100.0, float(components.get(key, fallback_value))))
        except (TypeError, ValueError):
            values[key] = max(0.0, min(100.0, fallback_value))
    try:
        penalty = max(0.0, min(30.0, float(components.get("repeat_penalty", 0))))
    except (TypeError, ValueError):
        penalty = 0.0
    return max(0, min(100, round(sum(values[key] * weight for key, weight in weights.items()) - penalty)))


def collect_research(settings: Settings, locale: str, topic_override: str | None = None) -> tuple[str, dict[str, Any]]:
    """Choose a current topic and build a benchmarked evidence brief.

    One research call uses the locale's Google News snapshot plus web search to
    choose the highest-potential current topic, identify five leading results,
    and synthesize gaps and primary-source facts for the writer.
    """
    language, source_policy = locale_rules(locale)
    try:
        recent_posts = wp_recent_posts(settings, limit=20)
    except Exception:
        recent_posts = []
    recent_cutoff = datetime.now(timezone.utc).date() - timedelta(days=3)
    recent_3d = [
        row for row in recent_posts
        if _post_date(row) is not None and _post_date(row) >= recent_cutoff
    ]
    trends = google_news_snapshot(locale)
    trending_searches = google_trends_snapshot(locale)
    candidate_pool = build_candidate_pool(trends, trending_searches)
    weekly_strategy = load_json_file(STRATEGY_PATH, {})
    article_history = [row for row in load_article_history() if isinstance(row, dict) and row.get("locale") == locale]
    generated_at = datetime.now(timezone.utc).isoformat()
    source_checklist = "Find at least three complete primary or authoritative URLs for the selected topic. Match source types to the topic (government, regulator, public agency, standards body, exchange, university, or first-party issuer). Prefer current pages and state the page date or last-updated date when visible."
    prompt = (
        "You are a local trend and search-intent research editor. Use exactly one web search tool call, with multiple native-language queries if useful, then stop. "
        "The topic may be finance or any other genuinely useful current-interest subject; do not constrain the category. Choose the best opportunity for this site, not merely the most popular headline. Evaluate the candidate pool using: interest × velocity × search intent × SERP feasibility × title CTR potential × durability × site fit, minus repetition penalty. A lower-volume rising query with weak or stale results should beat a massive query dominated by authoritative publishers. Use article_history and weekly_strategy performance signals to prefer topic/headline/layout patterns that actually earned clicks, while preserving a 70% proven / 20% adjacent / 10% experiment mix. Apply weekly_strategy category weights when they have enough observations. Avoid sensational or unsafe claims. Use Google's trending-search terms and news headlines as signals, then validate intent, competition, and facts with web search. "
        "Use the Google News RSS snapshot as a trend signal, but verify it and do not treat headlines as facts. Identify exactly five leading/relevant search-result pages for the chosen query when five can be verified; rank them 1-5 and describe only their coverage/structure, never copy wording. "
        "The recent_3d_posts list is a hard exclusion window. Do not choose the same topic or an overlapping story as any post in that list: avoid the same event, entity, policy, product, incident, primary keyword, or search intent even when the title is reworded. If a trend is too close, discard it and select another currently trending query. Exact-title reuse is forbidden. "
        "Return one compact JSON object only with: topic, action (new or update), target_post_id (when action=update), category, layout_type (news, comparison, howto, timeline, explainer, or checklist), click_potential (0-100), opportunity_score (0-100), opportunity_components (interest, velocity, search_intent, serp_feasibility, ctr_potential, durability, site_fit, repeat_penalty), search_intent, angle, freshness, headline_type, serp_competition (0-100), benchmark_sources (array of up to 5 objects with rank,title,url,what_it_covers), official_sources (array of complete url,title,claim objects), synthesis_points (array), gaps (array), original_value (at least two concrete additions absent from the benchmark pages), growth_plan (array of concrete future content/measurement actions), focus_keyword, related_keywords (array), and outline (array). "
        + source_checklist + " Use the requested locale and language. Do not invent, truncate, or guess URLs. Do not provide personalized financial, medical, legal, or safety advice."
    )
    input_payload = {
        "locale": locale,
        "language": language,
        "generated_at_utc": generated_at,
        "topic_override": topic_override or "",
        "google_news_snapshot": trends,
        "google_trending_searches": trending_searches,
        "candidate_pool": candidate_pool,
        "weekly_strategy": weekly_strategy,
        "article_history": article_history[-100:],
        "recent_published_posts": recent_posts,
        "recent_3d_posts": recent_3d,
        "topic_exclusion_rule": "No same or overlapping topic, event/entity, primary keyword, or search intent as a post dated within the last 3 days.",
        "source_policy": source_policy,
    }
    research: dict[str, Any] = {}
    selected_topic = ""
    previous_candidates: list[dict[str, str]] = []
    for selection_attempt in range(3):
        attempt_payload = dict(input_payload)
        if selection_attempt:
            attempt_payload["previous_candidate"] = selected_topic
            attempt_payload["retry_instruction"] = "The previous candidate was rejected for topic overlap. Choose a materially different trending topic and search intent now."
            attempt_payload["previous_candidates"] = previous_candidates
        research_text = openai_text(
            settings,
            prompt,
            json.dumps(attempt_payload, ensure_ascii=False),
            use_web_search=True,
            max_output_tokens=9000,
            model=settings.research_model,
            reasoning_effort=settings.research_reasoning,
        )
        try:
            research = parse_json(research_text)
        except ValueError:
            research = parse_json(openai_text(
                settings,
                prompt + " Output valid JSON only, with double quotes and no code fence.",
                json.dumps(attempt_payload, ensure_ascii=False),
                use_web_search=True,
                max_output_tokens=9000,
                model=settings.research_model,
                reasoning_effort=settings.research_reasoning,
            ))
        selected_topic = str(research.get("topic") or topic_override or "Current local search trend and how to verify it")
        exclusion_rows = [*recent_3d, *previous_candidates]
        if not topic_overlaps_recent(selected_topic, research, exclusion_rows):
            break
        previous_candidates.append({"title": selected_topic})
    else:
        raise RuntimeError("Research topic overlapped a post from the last 3 days after three selections")
    components = research.get("opportunity_components") if isinstance(research, dict) else {}
    research["opportunity_score"] = calculate_opportunity_score(components, research.get("click_potential", 0))
    research["trend_score"] = research.get("trend_score", research.get("click_potential", 0))
    original_value = research.get("original_value")
    if not isinstance(original_value, list) or len(original_value) < 2:
        gaps = research.get("gaps") if isinstance(research.get("gaps"), list) else []
        research["original_value"] = [str(item) for item in gaps[:2]]
    action = str(research.get("action", "new")).lower()
    try:
        target_id = int(research.get("target_post_id")) if research.get("target_post_id") is not None else None
    except (TypeError, ValueError):
        target_id = None
    valid_targets = {
        int(row["id"]): row for row in recent_posts
        if str(row.get("id", "")).isdigit() and row not in recent_3d
    }
    if action == "update" and target_id in valid_targets:
        research["action"] = "update"
        research["target_post_id"] = target_id
    else:
        research["action"] = "new"
        research.pop("target_post_id", None)
    # Carry the exact exclusion list into the writing/review stages as well,
    # so a headline cannot be changed into a near-duplicate after research.
    research["recent_published_posts"] = recent_posts
    research["recent_3d_posts"] = recent_3d
    research["recent_3d_exclusion_count"] = len(recent_3d)
    research["topic_exclusion_applied"] = True
    return selected_topic, research


def create_article(settings: Settings, locale: str, topic: str, brief: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    language, _ = locale_rules(locale)
    layout_type = str(brief.get("layout_type", "explainer")).lower()
    layout_guidance = {
        "news": "Use a news layout: answer first, what happened, why it matters, verified facts, what remains uncertain, and what to watch next.",
        "comparison": "Use a comparison layout: conclusion first, a compact comparison table, criterion-by-criterion analysis, and who each option suits.",
        "howto": "Use a practical layout: problem, immediate answer, numbered steps, pitfalls, and a short FAQ.",
        "timeline": "Use a timeline layout: current status, dated sequence of verified events, confirmed versus unconfirmed claims, and next dates.",
        "checklist": "Use a checklist layout: decision summary, checklist grouped by stage, stop-and-verify warnings, and FAQ.",
        "explainer": "Use an explainer layout: plain-language answer, key concepts, evidence, practical example, limitations, and FAQ.",
    }.get(layout_type, "Use an explainer layout with a plain-language answer, evidence, practical example, limitations, and FAQ.")
    article_input = json.dumps({"output_format": "json", "locale": locale, "topic": topic, "research": brief}, ensure_ascii=False)
    article_instructions = (
        "You are an independent high-trust writer. Write an original, useful article in the requested language about the selected current topic. "
        "Use the five benchmark pages only to understand search intent, coverage gaps, and useful structure; never imitate, translate, or copy any competitor wording. "
        "Use only claims supported by the research brief and cite complete official URLs from official_sources; never invent, truncate, or guess URLs. "
        "Treat research.recent_3d_posts as a hard exclusion list: the title, focus keyword, opening, and search intent must not repeat or materially overlap any item dated within the last three days. "
        "The research brief's original_value list must be reflected as at least two concrete additions that are not merely longer summaries of the benchmark pages. "
        "Optimize for search without keyword stuffing: a clear native-language title, a concise meta-style excerpt, a readable slug, one primary focus keyword, natural secondary terms, descriptive H2/H3 headings, an answer-first opening, short paragraphs, a useful FAQ, and one or two concrete internal-link suggestions only when URLs are present in recent_published_posts. "
        f"Design the HTML like a polished editorial feature rather than an AI dump. Selected layout type: {layout_type}. {layout_guidance} Use a calm typographic hierarchy (title handled by WordPress, H2 for major sections, H3 for details), generous paragraph rhythm, and restrained emphasis. Do not add inline font sizes, fake author claims, repetitive transition phrases, generic clickbait, or decorative emoji. Vary sentence length and include specific practical examples so the voice feels edited by a human. "
        "Include the information date, what is still uncertain, risks/limitations appropriate to the topic, a short update plan based on growth_plan, and a clear notice that this is general information rather than personalized professional advice. "
        "Return exactly one JSON object with string fields title, slug, excerpt, html, layout_type; array field sources; and object field seo containing meta_description, focus_keyword, related_keywords, and faq_questions. HTML must be complete, valid, balanced HTML with no markdown, dangling tags, or cut-off sentences."
    )
    article_text = openai_text(
        settings,
        article_instructions,
        article_input,
        max_output_tokens=7000,
        json_output=True,
        model=settings.writing_model,
        reasoning_effort=settings.writing_reasoning,
    )
    try:
        parsed_article = parse_article_output(article_text)
    except ValueError:
        # A transient truncated/non-JSON response should not consume the
        # entire scheduled slot. Ask once more with a stricter JSON contract.
        parsed_article = parse_article_output(openai_text(settings, article_instructions + " Output only valid JSON, with no preamble or code fence.", article_input, max_output_tokens=7000, json_output=True, model=settings.writing_model, reasoning_effort=settings.writing_reasoning))
    parsed_article.setdefault("layout_type", layout_type)
    article = json.dumps(parsed_article, ensure_ascii=False)
    return article, brief


def review_and_revise(settings: Settings, locale: str, topic: str, article: str, brief: str) -> tuple[str, dict[str, Any]]:
    current = article
    review: dict[str, Any] = {}
    for attempt in range(settings.max_revisions + 1):
        review_instructions = "You are a strict independent editor and fact checker. Return exactly one json object and no markdown or prose, with score (0-100), pass (boolean), breakdown object, originality_count, issues (array), required_fixes (array), and rationale. The breakdown must score Fact accuracy /20, Original value /20, Search intent /15, SEO /10, Readability /10, Naturalness /10, Freshness /10, Layout /5. Check every number, date, rule, and source against the evidence; flag unsupported or personalized financial, medical, legal, or safety advice; check that benchmark synthesis is original, the information date is clear, the HTML is balanced, and the title/excerpt/slug/headings/FAQ are useful for search without keyword stuffing. Confirm that at least two concrete additions from evidence.original_value are present and not just longer summaries. Compare the draft with evidence.recent_3d_posts and fail it if the title, focus keyword, event/entity, or search intent materially overlaps a post from the last three days. Do not demand numeric thresholds when the article is a methodology guide and tells readers how to verify current values from cited primary sources."
        # The Responses API's json_object mode requires the literal word
        # `json` in the request input. Keep it lowercase to satisfy the API
        # validator across model versions.
        review_input = json.dumps({"output_format": "json", "locale": locale, "topic": topic, "draft": current, "evidence": brief}, ensure_ascii=False)
        try:
            review = parse_json(openai_text(
                settings,
                review_instructions,
                review_input,
                max_output_tokens=3000,
                json_output=True,
                model=settings.writing_model,
                reasoning_effort=settings.writing_reasoning,
            ))
        except (RuntimeError, ValueError):
            # Models can occasionally emit a non-JSON preamble despite the
            # structured-output request. Retry once with an even tighter
            # contract before treating the review as a failed run.
            try:
                review = parse_json(openai_text(
                    settings,
                    review_instructions + " Use double quotes, no trailing commas, and do not wrap the object in backticks.",
                    review_input,
                    max_output_tokens=3000,
                    json_output=True,
                    model=settings.writing_model,
                    reasoning_effort=settings.writing_reasoning,
                ))
            except (RuntimeError, ValueError):
                # Treat an unparseable reviewer response as a failed gate and
                # let the normal revision loop request a fresh review next.
                review = {"score": 0, "pass": False, "issues": ["Reviewer response was not valid JSON"], "required_fixes": ["Recheck every claim and source against the evidence"], "rationale": ""}
        score = int(review.get("score", 0))
        original_value = brief.get("original_value", []) if isinstance(brief, dict) else []
        original_count = review.get("originality_count")
        try:
            original_count = int(original_count)
        except (TypeError, ValueError):
            original_count = len(original_value) if isinstance(original_value, list) else 0
        if not isinstance(original_value, list) or len(original_value) < 2:
            original_count = 0
        else:
            original_count = min(original_count, len(original_value))
        review["originality_count"] = original_count
        review["breakdown"] = review.get("breakdown", {}) if isinstance(review.get("breakdown"), dict) else {}
        if bool(review.get("pass")) and score >= settings.quality_threshold and original_count >= 2:
            return current, {"score": score, "attempts": attempt, "review": review}
        if attempt == settings.max_revisions:
            break
        current = openai_text(settings, "Revise only weak sections of this article. Apply every required_fix in the review, preserve supported facts, remove unsupported claims, add only complete URLs from the evidence, repair all HTML, and keep the language native and human. Preserve the SEO fields (title, slug, excerpt, seo) and improve layout/typography cues through clean semantic HTML rather than decorative AI-sounding filler. Return the complete revised JSON article with title, slug, excerpt, html, sources, and seo; return no prose outside the object.", json.dumps({"output_format": "json", "draft": current, "review": review, "evidence": brief}, ensure_ascii=False), max_output_tokens=7000, json_output=True, model=settings.writing_model, reasoning_effort=settings.writing_reasoning)
    raise RuntimeError(f"Quality gate failed after {settings.max_revisions} revisions: {review}")


def wp_auth_header(settings: Settings) -> str:
    if settings.wp_mode == "self_hosted":
        token = base64.b64encode(f"{settings.wp_username}:{settings.wp_password}".encode()).decode()
        return f"Basic {token}"
    if settings.wp_access_token:
        return f"Bearer {settings.wp_access_token}"
    token = form_json("https://public-api.wordpress.com/oauth2/token", {"client_id": settings.wp_client_id, "client_secret": settings.wp_client_secret, "grant_type": "password", "username": settings.wp_username, "password": settings.wp_password}).get("access_token")
    if not token:
        raise RuntimeError("WordPress.com OAuth token exchange returned no access token")
    return f"Bearer {token}"


def wp_endpoint(settings: Settings, resource: str) -> str:
    if settings.wp_mode == "self_hosted":
        return f"{settings.wp_url}/wp-json/wp/v2/{resource}"
    return f"https://public-api.wordpress.com/wp/v2/sites/{settings.wp_site_ref}/{resource}"


def wp_recent_posts(settings: Settings, limit: int = 20) -> list[dict[str, str]]:
    """Return recent published titles/links to prevent duplication and add internal links."""
    query = urllib.parse.urlencode({
        "per_page": str(limit),
        "orderby": "date",
        "order": "desc",
        "_fields": "id,title,slug,link,date,modified,status",
    })
    payload = http_get_json(wp_endpoint(settings, f"posts?{query}"), {"Authorization": wp_auth_header(settings)})
    rows = payload.get("posts", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    result: list[dict[str, str]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        title = row.get("title", "")
        if isinstance(title, dict):
            title = title.get("rendered", "")
        result.append({
            "id": str(row.get("id", "")),
            "title": str(title).strip(),
            "url": str(row.get("link", "")).strip(),
            "slug": str(row.get("slug", "")).strip(),
            "date": str(row.get("date", ""))[:10],
            "modified": str(row.get("modified", ""))[:10],
            "status": str(row.get("status", "publish")),
        })
    return [row for row in result if row["title"] and row["url"]]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _icon_kind(title: str, article_html: str = "", variant: int = 0) -> str:
    """Choose a semantic icon pair from the article's own language.

    The matcher is deliberately small and deterministic: it gives the two
    visuals different but complementary meanings without calling an image API
    or depending on a font/rendering package.
    """
    text = f"{title} {article_html[:2400]}".casefold()
    groups = (
        ("currency", ("환율", "원화", "달러", "엔화", "유로", "為替", "円相場", "ドル", "ユーロ", "currency", "exchange", "dollar", "yen", "won", "euro")),
        ("home", ("주택", "부동산", "전세", "월세", "모기지", "住宅", "不動産", "家賃", "住宅ローン", "mortgage", "housing", "rent", "home")),
        ("security", ("보안", "사기", "피싱", "詐欺", "フィッシング", "セキュリティ", "個人情報", "fraud", "scam", "security", "privacy", "identity")),
        ("policy", ("정책", "법안", "규정", "공시", "세법", "政策", "法律", "規制", "開示", "税制", "regulation", "policy", "filing", "disclosure", "tax")),
        ("calendar", ("일정", "마감", "기한", "신청", "연휴", "日程", "締切", "期限", "申請", "連休", "deadline", "schedule", "calendar")),
        ("chart", ("주식", "주가", "채권", "etf", "펀드", "투자", "시장", "株", "株価", "債券", "投資", "市場", "指数", "ファンド", "stock", "bond", "fund", "invest", "market", "index")),
        ("calculator", ("계산", "예산", "저축", "비용", "수수료", "금리", "대출", "計算", "予算", "貯蓄", "費用", "手数料", "金利", "ローン", "budget", "saving", "cost", "fee", "interest", "loan", "rate")),
        ("news", ("뉴스", "속보", "화제", "트렌드", "ニュース", "速報", "話題", "トレンド", "最新", "今日", "news", "trend", "update", "latest", "today")),
        ("sports", ("스포츠", "경기", "선수", "리그", "スポーツ", "試合", "選手", "リーグ", "sports", "game", "player", "league")),
        ("product", ("제품", "신제품", "스펙", "가격", "상품", "製品", "新製品", "仕様", "価格", "商品", "product", "spec", "price", "device")),
        ("timeline", ("타임라인", "사건", "발생", "경과", "経緯", "時系列", "事件", "timeline", "incident", "recap")),
        ("travel", ("여행", "관광", "항공", "호텔", "旅行", "観光", "航空", "ホテル", "travel", "tourism", "flight", "hotel")),
        ("lifestyle", ("생활", "요리", "건강", "정리", "생활비", "暮らし", "料理", "健康", "生活", "lifestyle", "recipe", "wellness")),
        ("entertainment", ("연예", "배우", "가수", "영화", "드라마", "芸能", "俳優", "歌手", "映画", "ドラマ", "entertainment", "actor", "movie", "series")),
    )
    primary = next((kind for kind, words in groups if any(word in text for word in words)), "insight")
    if variant == 0:
        return primary
    complements = {
        # Every secondary icon uses a different renderer from the primary
        # icon.  This prevents visually identical pairs for policy/news/
        # security/insight stories even when their semantic labels differ.
        "currency": "chart", "home": "calculator", "security": "document", "policy": "magnifier",
        "calendar": "document", "chart": "calculator", "calculator": "chart", "news": "chart",
        "insight": "chart", "sports": "chart", "product": "calculator", "timeline": "calendar",
        "travel": "compass", "lifestyle": "document", "entertainment": "calendar",
        "shield": "document", "document": "magnifier", "magnifier": "chart",
    }
    return complements.get(primary, "compass")


def generated_icon_png(locale: str, title: str, article_html: str = "", variant: int = 0) -> bytes:
    """Create a polished topic-related icon using only stdlib pixel drawing."""
    width, height = 1200, 675
    palettes = {
        "us": ((238, 244, 247), (19, 74, 95), (37, 144, 145), (217, 158, 64)),
        "jp": ((248, 242, 244), (66, 48, 87), (190, 74, 113), (219, 155, 64)),
        "kr": ((239, 247, 246), (18, 67, 77), (28, 144, 139), (222, 157, 63)),
    }
    background, ink, accent, gold = palettes.get(locale, palettes["us"])
    kind = _icon_kind(title, article_html, variant)
    seed = hashlib.sha256(f"{locale}:{title}:{kind}:{variant}".encode("utf-8")).digest()
    pixels = bytearray()

    def blend(a: tuple[int, int, int], b: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        return tuple(int(a[i] * (1 - amount) + b[i] * amount) for i in range(3))

    for y in range(height):
        pixels.append(0)
        tone = 0.06 + (y / height) * 0.10
        row_color = blend(background, accent, tone)
        for _ in range(width):
            pixels.extend(row_color)

    def set_pixel(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < width and 0 <= y < height:
            pos = y * (width * 3 + 1) + 1 + x * 3
            pixels[pos : pos + 3] = bytes(color)

    def rectangle(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for yy in range(max(0, y0), min(height, y1)):
            row = yy * (width * 3 + 1) + 1
            for xx in range(max(0, x0), min(width, x1)):
                pos = row + xx * 3
                pixels[pos : pos + 3] = bytes(color)

    def line(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], width_px: int = 7) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        radius = max(0, width_px // 2)
        for step in range(steps + 1):
            x = round(x0 + (x1 - x0) * step / steps)
            y = round(y0 + (y1 - y0) * step / steps)
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    set_pixel(x + dx, y + dy, color)

    def circle(cx: int, cy: int, radius: int, color: tuple[int, int, int], fill: bool = True, width_px: int = 7) -> None:
        r2 = radius * radius
        inner = max(0, radius - width_px) ** 2
        for yy in range(cy - radius, cy + radius + 1):
            for xx in range(cx - radius, cx + radius + 1):
                d2 = (xx - cx) ** 2 + (yy - cy) ** 2
                if d2 <= r2 and (fill or d2 >= inner):
                    set_pixel(xx, yy, color)

    def rounded_card(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        # A compact rounded rectangle made from a body and four circles.
        radius = 30
        rectangle(x0 + radius, y0, x1 - radius, y1, color)
        rectangle(x0, y0 + radius, x1, y1 - radius, color)
        for cx, cy in ((x0 + radius, y0 + radius), (x1 - radius, y0 + radius), (x0 + radius, y1 - radius), (x1 - radius, y1 - radius)):
            circle(cx, cy, radius, color)

    def draw_chart() -> None:
        line(390, 455, 390, 220, ink, 9)
        line(390, 455, 825, 455, ink, 9)
        bars = (128 + seed[0] % 72, 188 + seed[1] % 84, 116 + seed[2] % 86, 240 + seed[3] % 76)
        for idx, bar in enumerate(bars):
            x = 450 + idx * 86
            rectangle(x, 455 - bar, x + 48, 455, blend(accent, ink, 0.18 if idx % 2 else 0.35))
        points = [(430, 405), (525, 355), (615, 382), (710, 278), (808, 235)]
        for p0, p1 in zip(points, points[1:]):
            line(*p0, *p1, gold, 9)
        for x, y in points:
            circle(x, y, 12, gold)
        line(760, 240, 820, 235, gold, 9)
        line(806, 223, 820, 235, gold, 9)
        line(806, 247, 820, 235, gold, 9)

    def draw_calculator() -> None:
        rounded_card(430, 185, 770, 500, ink)
        rectangle(480, 230, 720, 295, background)
        line(665, 265, 705, 265, accent, 7)
        for row in range(3):
            for col in range(3):
                x, y = 490 + col * 75, 335 + row * 48
                rounded_card(x, y, x + 47, y + 28, blend(accent, background, 0.18))
        rounded_card(705, 335, 740, 470, gold)

    def draw_currency() -> None:
        for offset, color in ((0, gold), (45, accent), (90, blend(gold, accent, 0.35))):
            circle(600 + offset, 355 - offset // 3, 78, color)
            circle(600 + offset, 355 - offset // 3, 59, background, fill=False, width_px=7)
            line(600 + offset, 324 - offset // 3, 600 + offset, 387 - offset // 3, background, 8)
            line(582 + offset, 335 - offset // 3, 618 + offset, 335 - offset // 3, background, 6)
            line(582 + offset, 375 - offset // 3, 618 + offset, 375 - offset // 3, background, 6)

    def draw_home() -> None:
        line(400, 330, 600, 180, ink, 11)
        line(600, 180, 800, 330, ink, 11)
        line(435, 315, 435, 485, ink, 11)
        line(765, 315, 765, 485, ink, 11)
        line(435, 485, 765, 485, ink, 11)
        rectangle(550, 390, 650, 485, accent)
        rectangle(485, 340, 535, 390, gold)
        rectangle(665, 340, 715, 390, gold)

    def draw_shield() -> None:
        points = [(600, 175), (790, 240), (750, 420), (600, 515), (450, 420), (410, 240)]
        for p0, p1 in zip(points, points[1:] + points[:1]):
            line(*p0, *p1, ink, 11)
        line(500, 345, 570, 415, accent, 15)
        line(570, 415, 710, 275, accent, 15)

    def draw_document() -> None:
        rectangle(460, 165, 740, 505, background)
        line(460, 165, 670, 165, ink, 10)
        line(740, 235, 740, 505, ink, 10)
        line(460, 505, 740, 505, ink, 10)
        line(460, 165, 460, 505, ink, 10)
        line(670, 165, 740, 235, ink, 10)
        line(670, 165, 670, 235, ink, 8)
        line(670, 235, 740, 235, ink, 8)
        for y in (285, 330, 375):
            line(510, y, 690, y, blend(ink, background, 0.35), 7)
        line(510, 425, 550, 465, accent, 12)
        line(550, 465, 665, 350, accent, 12)

    def draw_magnifier() -> None:
        circle(565, 320, 135, accent, fill=False, width_px=16)
        line(665, 420, 790, 545, ink, 22)
        for idx, bar in enumerate((95, 145, 200)):
            rectangle(475 + idx * 67, 435 - bar, 515 + idx * 67, 435, gold if idx == 2 else ink)

    def draw_calendar() -> None:
        rounded_card(420, 190, 780, 485, background)
        rectangle(420, 190, 780, 270, accent)
        for x in (500, 700):
            line(x, 165, x, 225, ink, 14)
        for row in range(3):
            line(470, 315 + row * 48, 730, 315 + row * 48, blend(ink, background, 0.25), 5)
        for col in range(4):
            line(505 + col * 60, 292, 505 + col * 60, 442, blend(ink, background, 0.25), 5)
        circle(625, 363, 18, gold)

    def draw_compass() -> None:
        circle(600, 345, 150, ink, fill=False, width_px=11)
        circle(600, 345, 16, gold)
        line(600, 225, 660, 390, accent, 15)
        line(660, 390, 600, 345, ink, 9)
        line(600, 465, 600, 345, ink, 7)

    # Soft card and a restrained accent rail make the icon feel editorial.
    rounded_card(130, 82, 1070, 590, (255, 255, 255))
    rectangle(130, 82, 164, 590, accent)
    for idx in range(5):
        circle(990 + (idx % 2) * 22, 145 + idx * 35, 5 + seed[idx] % 5, blend(accent, gold, 0.35))
    {
        "chart": draw_chart,
        "calculator": draw_calculator,
        "currency": draw_currency,
        "home": draw_home,
        "security": draw_shield,
        "shield": draw_shield,
        "policy": draw_document,
        "document": draw_document,
        "magnifier": draw_magnifier,
        "calendar": draw_calendar,
        "compass": draw_compass,
        "news": draw_magnifier,
        "insight": draw_compass,
        "sports": draw_chart,
        "product": draw_calculator,
        "timeline": draw_calendar,
        "travel": draw_compass,
        "lifestyle": draw_document,
        "entertainment": draw_calendar,
    }.get(kind, draw_compass)()

    raw = bytes(pixels)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b"")


def generated_cover_png(locale: str, title: str, variant: int = 0) -> bytes:
    """Backward-compatible alias for callers that used the old image helper."""
    return generated_icon_png(locale, title, "", variant)


def upload_generated_icon(settings: Settings, title: str, locale: str, article_html: str = "", variant: int = 0) -> tuple[int | None, str]:
    """Upload one free, topic-related generated PNG and return (media_id, URL)."""
    auth = wp_auth_header(settings)
    kind = _icon_kind(title, article_html, variant)
    boundary = "----CodexFinance" + hashlib.sha256(f"{title}:{kind}:{variant}".encode("utf-8")).hexdigest()[:16]
    image = generated_icon_png(locale, title, article_html, variant)
    purposes = ("topic", "detail")
    purpose = purposes[variant % len(purposes)]
    alt = f"{title} — original {kind} icon illustration related to the article topic"
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"media[]\"; filename=\"finance-{kind}-{purpose}.png\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"),
        image,
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"attrs[0][title]\"\r\n\r\n{title} — {kind} icon ({purpose})\r\n".encode("utf-8"),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"attrs[0][alt]\"\r\n\r\n{alt}\r\n".encode("utf-8"),
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    if settings.wp_mode == "self_hosted":
        url = f"{settings.wp_url}/wp-json/wp/v2/media"
    else:
        url = f"https://public-api.wordpress.com/rest/v1.1/sites/{settings.wp_site_ref}/media/new"
    data = multipart_json(url, b"".join(chunks), f"multipart/form-data; boundary={boundary}", {"Authorization": auth})
    media_items = data.get("media") if isinstance(data, dict) else None
    if not isinstance(media_items, list) or not media_items:
        # Self-hosted wp/v2/media returns the media object itself.
        media_items = [data] if isinstance(data, dict) and data.get("id") else []
    media = media_items[0] if isinstance(media_items[0], dict) else {}
    media_id = media.get("ID", media.get("id"))
    source_url = media.get("URL") or media.get("source_url") or ""
    if media_id is None or not source_url:
        raise RuntimeError("WordPress media upload returned no media ID or URL")
    return int(media_id), str(source_url)


def seo_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"[^\w\s-]", "", normalized, flags=re.UNICODE)
    normalized = re.sub(r"[-\s]+", "-", normalized).strip("-")
    return normalized[:96].strip("-")


def insert_figure_after_first_paragraph(html: str, figure: str) -> str:
    match = re.search(r"</p>", html, flags=re.I)
    if not match:
        return figure + html
    return html[: match.end()] + figure + html[match.end() :]


def insert_figure_before_heading(html: str, figure: str, heading_index: int) -> str:
    matches = list(re.finditer(r"<h2\b", html, flags=re.I))
    if not matches:
        return html + figure
    index = matches[min(heading_index, len(matches) - 1)].start()
    return html[:index] + figure + html[index:]


def compose_image_layout(html: str, figures: list[str]) -> str:
    """Place up to two compact visuals at useful editorial points in the body."""
    body = html.strip()
    if not figures:
        return body
    body = insert_figure_after_first_paragraph(body, figures[0])
    if len(figures) > 1:
        body = insert_figure_before_heading(body, figures[1], 1)
    return body


def build_visuals(settings: Settings, title: str, locale: str, article_html: str) -> tuple[list[tuple[int, str]], list[str]]:
    """Upload two semantic visuals and return media records plus compact figures."""
    media: list[tuple[int, str]] = [upload_generated_icon(settings, title, locale, article_html, variant) for variant in range(2)]
    figures: list[str] = []
    for index, (_, media_url) in enumerate(media):
        kind = _icon_kind(title, article_html, index)
        alt = html_escape(f"{title} — original {kind} icon illustration related to the article topic", quote=True)
        figures.append(
            f'<figure class="wp-block-image aligncenter finance-inline-visual finance-inline-icon finance-inline-visual-{index + 1}" '
            'style="max-width:360px;width:100%;margin:1.5rem auto;text-align:center;">'
            f'<img src="{html_escape(media_url, quote=True)}" alt="{alt}" width="360" height="203" '
            'loading="lazy" decoding="async" sizes="(max-width: 600px) 100vw, 360px" '
            'style="display:block;width:100%;height:auto;margin:0 auto;" />'
            '</figure>'
        )
    return media, figures


def related_posts_html(article: dict[str, Any], brief: dict[str, Any], max_links: int = 5) -> str:
    """Build 2–5 contextual internal links from the site's existing posts."""
    candidates = brief.get("recent_published_posts", []) if isinstance(brief, dict) else []
    if not isinstance(candidates, list):
        return ""
    article_terms = _topic_terms(" ".join(str(article.get(key, "")) for key in ("title", "html")))
    ranked: list[tuple[int, dict[str, Any]]] = []
    target_id = str(brief.get("target_post_id", "")) if isinstance(brief, dict) else ""
    for row in candidates:
        if not isinstance(row, dict) or not row.get("url") or not row.get("title") or (target_id and str(row.get("id")) == target_id):
            continue
        score = len(article_terms & _topic_terms(str(row.get("title", ""))))
        if score:
            ranked.append((score, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = [row for _, row in ranked[:max_links]]
    if not selected:
        return ""
    items = "".join(f'<li><a href="{html_escape(str(row["url"]), quote=True)}">{html_escape(str(row["title"]))}</a></li>' for row in selected)
    return f'<aside class="finance-related" style="margin:2rem 0;padding:1rem 1.25rem;border-left:3px solid #2d8f8f;background:#f5f8f8;"><h2>Related reading</h2><ul>{items}</ul></aside>'


def article_payload(settings: Settings, article: dict[str, Any], topic: str, locale: str, brief: dict[str, Any], action: str = "new") -> tuple[dict[str, Any], list[tuple[int, str]]]:
    title = str(article.get("title", topic))
    article_html = str(article.get("html", ""))
    media, figures = build_visuals(settings, title, locale, article_html)
    content = compose_image_layout(article_html, figures)
    links = related_posts_html(article, brief)
    if links:
        content += links
    if action == "update":
        stamp = datetime.now(LOCAL_ZONE).strftime("%B %d, %Y").replace(" 0", " ")
        content = f'<p class="finance-updated"><strong>Updated: {html_escape(stamp)}</strong></p>' + content
    excerpt = str(article.get("excerpt", "")).strip()
    if len(excerpt) > 300:
        excerpt = excerpt[:297].rstrip() + "..."
    payload = {
        "title": title,
        "slug": seo_slug(str(article.get("slug", ""))) or seo_slug(title),
        # Keep both visuals in the article body so the responsive width rule
        # also applies to the first icon.  A featured image is intentionally
        # omitted: WordPress themes often render it full-bleed on mobile.
        "content": content,
        "excerpt": excerpt,
        "status": settings.publish_mode,
    }
    return payload, media


def wp_create(settings: Settings, article_json: str, topic: str, locale: str, brief: dict[str, Any] | None = None) -> dict[str, Any]:
    article = parse_json(article_json)
    payload, media = article_payload(settings, article, topic, locale, brief or {}, action="new")
    post = http_json(wp_endpoint(settings, "posts"), payload, {"Authorization": wp_auth_header(settings)})
    post["_images_uploaded"] = len(media)
    post["_image_kinds"] = [_icon_kind(str(article.get("title", topic)), str(article.get("html", "")), index) for index in range(len(media))]
    return post


def wp_update(settings: Settings, post_id: int, article_json: str, topic: str, locale: str, brief: dict[str, Any] | None = None) -> dict[str, Any]:
    """Update an existing URL when the research engine selects UPDATE."""
    article = parse_json(article_json)
    payload, media = article_payload(settings, article, topic, locale, brief or {}, action="update")
    post = http_json(wp_endpoint(settings, f"posts/{int(post_id)}"), payload, {"Authorization": wp_auth_header(settings)})
    post["_images_uploaded"] = len(media)
    post["_image_kinds"] = [_icon_kind(str(article.get("title", topic)), str(article.get("html", "")), index) for index in range(len(media))]
    post["_action"] = "update"
    return post


def add_reverse_internal_links(settings: Settings, new_post: dict[str, Any], brief: dict[str, Any], max_links: int = 3) -> tuple[int, list[str]]:
    """Link a few strong existing pages back to a newly published article."""
    post_id = new_post.get("id")
    new_url = str(new_post.get("link", ""))
    if not post_id or not new_url:
        return 0, []
    article = {"title": brief.get("topic", ""), "html": brief.get("focus_keyword", "")}
    candidates = brief.get("recent_published_posts", []) if isinstance(brief, dict) else []
    if not isinstance(candidates, list):
        return 0, []
    ranked: list[tuple[int, dict[str, Any]]] = []
    article_terms = _topic_terms(f"{article['title']} {article['html']}")
    for row in candidates:
        if not isinstance(row, dict) or str(row.get("id")) == str(post_id) or not str(row.get("id", "")).isdigit():
            continue
        score = len(article_terms & _topic_terms(str(row.get("title", ""))))
        if score:
            ranked.append((score, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    updated = 0
    errors: list[str] = []
    auth = wp_auth_header(settings)
    for _, row in ranked[:max_links]:
        try:
            target_id = int(row["id"])
            existing = http_get_json(wp_endpoint(settings, f"posts/{target_id}"), {"Authorization": auth})
            content_value = existing.get("content", "") if isinstance(existing, dict) else ""
            if isinstance(content_value, dict):
                content_value = content_value.get("raw") or content_value.get("rendered") or ""
            content = str(content_value)
            if new_url in content:
                continue
            link_html = f'<p class="finance-newer-reading"><a href="{html_escape(new_url, quote=True)}">Newer related reading: {html_escape(str(new_post.get("title", brief.get("topic", ""))))}</a></p>'
            http_json(wp_endpoint(settings, f"posts/{target_id}"), {"content": content + link_html}, {"Authorization": auth})
            updated += 1
        except Exception as exc:
            errors.append(f"post {row.get('id')}: {exc}")
    return updated, errors


def policy_pages(locale: str) -> list[tuple[str, str]]:
    if locale == "us":
        return [("About", "We publish independent, evidence-led personal-finance explainers for US readers."), ("Privacy Policy", "We collect only information needed to operate, secure, measure, and support this site. We do not sell personal information."), ("Contact", "For corrections, source questions, or privacy requests, use the contact form and include the article URL."), ("Editorial Policy", "We prioritize primary sources, disclose dates and assumptions, separate facts from opinion, correct errors, and do not provide individualized investment advice.")]
    if locale == "jp":
        return [("\u3053\u306e\u30b5\u30a4\u30c8\u306b\u3064\u3044\u3066", "\u5f53\u30b5\u30a4\u30c8\u306f\u3001\u65e5\u672c\u306e\u8aad\u8005\u5411\u3051\u306b\u4e00\u6b21\u8cc7\u6599\u3092\u91cd\u8996\u3057\u305f\u91d1\u878d\u89e3\u8aac\u3092\u63d0\u4f9b\u3057\u307e\u3059\u3002"), ("\u30d7\u30e9\u30a4\u30d0\u30b7\u30fc\u30dd\u30ea\u30b7\u30fc", "\u5f53\u30b5\u30a4\u30c8\u306f\u3001\u904b\u55b6\u3068\u5b89\u5168\u306b\u5fc5\u8981\u306a\u7bc4\u56f2\u3067\u60c5\u5831\u3092\u53d6\u308a\u6271\u3044\u307e\u3059\u3002"), ("\u304a\u554f\u3044\u5408\u308f\u305b", "\u8a02\u6b63\u4f9d\u983c\u3084\u51fa\u5178\u306b\u95a2\u3059\u308b\u8cea\u554f\u306f\u304a\u554f\u3044\u5408\u308f\u305b\u30d5\u30a9\u30fc\u30e0\u304b\u3089\u304a\u9001\u308a\u304f\u3060\u3055\u3044\u3002"), ("\u7de8\u96c6\u65b9\u91dd", "\u4e00\u6b21\u8cc7\u6599\u3092\u512a\u5148\u3057\u3001\u57fa\u6e96\u65e5\u3068\u524d\u63d0\u3092\u660e\u8a18\u3057\u3001\u8aa4\u308a\u3092\u8a02\u6b63\u3057\u307e\u3059\u3002\u500b\u5225\u306e\u6295\u8cc7\u52a9\u8a00\u306f\u884c\u3044\u307e\u305b\u3093\u3002")]
    return [("\uc0ac\uc774\ud2b8 \uc18c\uac1c", "\uc774 \uc0ac\uc774\ud2b8\ub294 \ud55c\uad6d \ub3c5\uc790\ub97c \uc704\ud574 1\ucc28 \uc790\ub8cc\uc640 \uacf5\uac1c \ub370\uc774\ud130\ub97c \ubc14\ud0d5\uc73c\ub85c \uae08\uc735\u00b7\uc7ac\ud14c\ud06c \uc815\ubcf4\ub97c \uc124\uba85\ud569\ub2c8\ub2e4."), ("\uac1c\uc778\uc815\ubcf4\ucc98\ub9ac\ubc29\uce68", "\uc0ac\uc774\ud2b8 \uc6b4\uc601\uacfc \ubcf4\uc548, \ubc29\ubb38 \ud1b5\uacc4\uc5d0 \ud544\uc694\ud55c \ubc94\uc704\uc5d0\uc11c\ub9cc \uc815\ubcf4\ub97c \ucc98\ub9ac\ud558\uba70 \uac1c\uc778\uc815\ubcf4\ub97c \ud310\ub9e4\ud558\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4."), ("\ubb38\uc758\ud558\uae30", "\uc624\ub958 \uc218\uc815\uc774\ub098 \ucd9c\ucc98 \ubb38\uc758\ub294 \ubb38\uc758 \uc591\uc2dd\uc73c\ub85c \uae00 \uc8fc\uc18c\uc640 \ud568\uaed8 \ubcf4\ub0b4 \uc8fc\uc138\uc694."), ("\ud3b8\uc9d1 \uc815\ucc45", "\uacf5\uc2dd 1\ucc28 \uc790\ub8cc\ub97c \uc6b0\uc120\ud558\uace0 \uae30\uc900\uc77c\uacfc \uc804\uc81c\ub97c \uba85\uc2dc\ud558\uba70, \uc624\ub958\ub97c \ud655\uc778\ud558\uba74 \uc218\uc815\ud569\ub2c8\ub2e4. \uac1c\uc778\ubcc4 \ud22c\uc790 \uc790\ubb38\uc740 \uc81c\uacf5\ud558\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4.")]


def seed_pages(settings: Settings, locale: str) -> int:
    for title, content in policy_pages(locale):
        payload = {"title": title, "content": f"<p>{content}</p>", "status": settings.publish_mode}
        http_json(wp_endpoint(settings, "pages"), payload, {"Authorization": wp_auth_header(settings)})
    return 4


def main() -> int:
    # Windows consoles may default to cp949; generated finance text can
    # contain Unicode punctuation, so always emit UTF-8 diagnostics/results.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--locale", choices=("us", "jp", "kr"), required=True)
    parser.add_argument("--topic", help="Optional fixed topic; omit to select a current local trend at run time")
    parser.add_argument("--seed-pages", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings.from_env(args.locale)
        topic, brief = collect_research(settings, args.locale, args.topic)
        article, brief = create_article(settings, args.locale, topic, brief)
        article, review = review_and_revise(settings, args.locale, topic, article, brief)
        final_article = parse_json(article)
        recent_3d = brief.get("recent_3d_posts", []) if isinstance(brief, dict) else []
        if isinstance(recent_3d, list) and topic_overlaps_recent(
            str(final_article.get("title", topic)),
            {"focus_keyword": final_article.get("seo", {}).get("focus_keyword", "") if isinstance(final_article.get("seo"), dict) else "", "search_intent": topic, "angle": ""},
            recent_3d,
        ):
            raise RuntimeError("Final article title overlapped a post from the last 3 days; publication was blocked")
        action = str(brief.get("action", "new"))
        if action == "update" and brief.get("target_post_id"):
            result = wp_update(settings, int(brief["target_post_id"]), article, topic, args.locale, brief)
        else:
            action = "new"
            result = wp_create(settings, article, topic, args.locale, brief)
        reverse_links, reverse_link_errors = add_reverse_internal_links(settings, result, brief) if action == "new" else (0, [])
        result["_reverse_links_added"] = reverse_links
        result["_reverse_link_errors"] = reverse_link_errors
        record_article_history(args.locale, final_article, brief, review, result, action)
        pages = seed_pages(settings, args.locale) if args.seed_pages else 0
        print(json.dumps({
            "topic": topic,
            "action": action,
            "opportunity_score": brief.get("opportunity_score"),
            "click_potential": brief.get("click_potential"),
            "benchmark_sources": len(brief.get("benchmark_sources", [])) if isinstance(brief.get("benchmark_sources"), list) else 0,
            "growth_plan": brief.get("growth_plan", []),
            "id": result.get("id"),
            "link": result.get("link"),
            "status": result.get("status"),
            "images_uploaded": result.get("_images_uploaded", 0),
            "image_kinds": result.get("_image_kinds", []),
            "reverse_links_added": result.get("_reverse_links_added", 0),
            "reverse_link_errors": result.get("_reverse_link_errors", []),
            "quality": review,
            "pages_created": pages,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

