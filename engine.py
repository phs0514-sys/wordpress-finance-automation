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
from datetime import datetime, timedelta, timezone
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
        }
        if not values["wp_site_ref"] and values["wp_url"]:
            values["wp_site_ref"] = values["wp_url"].split("//", 1)[-1]
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
    trends = google_news_snapshot(locale)
    generated_at = datetime.now(timezone.utc).isoformat()
    source_checklist = "Find at least three complete primary or authoritative URLs for the selected topic. Match source types to the topic (government, regulator, public agency, standards body, exchange, university, or first-party issuer). Prefer current pages and state the page date or last-updated date when visible."
    prompt = (
        "You are a local trend and search-intent research editor. Use exactly one web search tool call, with multiple native-language queries if useful, then stop. "
        "The topic may be finance or any other genuinely useful current-interest subject; choose what is most likely to earn qualified clicks in this locale at the stated moment, while avoiding sensational or unsafe claims. "
        "Use the Google News RSS snapshot as a trend signal, but verify it and do not treat headlines as facts. Identify exactly five leading/relevant search-result pages for the chosen query when five can be verified; rank them 1-5 and describe only their coverage/structure, never copy wording. "
        "Return one compact JSON object only with: topic, click_potential (0-100), search_intent, angle, freshness, benchmark_sources (array of up to 5 objects with rank,title,url,what_it_covers), official_sources (array of complete url,title,claim objects), synthesis_points (array), gaps (array), growth_plan (array of concrete future content/measurement actions), focus_keyword, related_keywords (array), and outline (array). "
        + source_checklist + " Use the requested locale and language. Do not invent, truncate, or guess URLs. Do not provide personalized financial, medical, legal, or safety advice."
    )
    input_payload = {
        "locale": locale,
        "language": language,
        "generated_at_utc": generated_at,
        "topic_override": topic_override or "",
        "google_news_snapshot": trends,
        "recent_published_posts": recent_posts,
        "source_policy": source_policy,
    }
    research_text = openai_text(
        settings,
        prompt,
        json.dumps(input_payload, ensure_ascii=False),
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
            json.dumps(input_payload, ensure_ascii=False),
            use_web_search=True,
            max_output_tokens=9000,
            model=settings.research_model,
            reasoning_effort=settings.research_reasoning,
        ))
    selected_topic = str(research.get("topic") or topic_override or "Current local search trend and how to verify it")
    return selected_topic, research


def create_article(settings: Settings, locale: str, topic: str, brief: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    language, _ = locale_rules(locale)
    article_input = json.dumps({"output_format": "json", "locale": locale, "topic": topic, "research": brief}, ensure_ascii=False)
    article_instructions = (
        "You are an independent high-trust writer. Write an original, useful article in the requested language about the selected current topic. "
        "Use the five benchmark pages only to understand search intent, coverage gaps, and useful structure; never imitate, translate, or copy any competitor wording. "
        "Use only claims supported by the research brief and cite complete official URLs from official_sources; never invent, truncate, or guess URLs. "
        "Optimize for search without keyword stuffing: a clear native-language title, a concise meta-style excerpt, a readable slug, one primary focus keyword, natural secondary terms, descriptive H2/H3 headings, an answer-first opening, short paragraphs, a useful FAQ, and one or two concrete internal-link suggestions only when URLs are present in recent_published_posts. "
        "Design the HTML like a polished editorial feature rather than an AI dump: use a calm typographic hierarchy (title handled by WordPress, H2 for major sections, H3 for details), a short lead paragraph, compact callout/summary blocks, readable tables or checklists only when they clarify a decision, generous paragraph rhythm, and restrained emphasis. Do not add inline font sizes, fake author claims, repetitive transition phrases, generic clickbait, or decorative emoji. Vary sentence length and include specific practical examples so the voice feels edited by a human. "
        "Include the information date, what is still uncertain, risks/limitations appropriate to the topic, a short update plan based on growth_plan, and a clear notice that this is general information rather than personalized professional advice. "
        "Return exactly one JSON object with string fields title, slug, excerpt, html; array field sources; and object field seo containing meta_description, focus_keyword, related_keywords, and faq_questions. HTML must be complete, valid, balanced HTML with no markdown, dangling tags, or cut-off sentences."
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
    article = json.dumps(parsed_article, ensure_ascii=False)
    return article, brief


def review_and_revise(settings: Settings, locale: str, topic: str, article: str, brief: str) -> tuple[str, dict[str, Any]]:
    current = article
    review: dict[str, Any] = {}
    for attempt in range(settings.max_revisions + 1):
        review_instructions = "You are a strict independent editor and fact checker. Return exactly one json object and no markdown or prose, with score (0-100), pass (boolean), issues (array), required_fixes (array), and rationale. Check every number, date, rule, and source against the evidence; flag unsupported or personalized financial, medical, legal, or safety advice; check that benchmark synthesis is original, the information date is clear, the HTML is balanced, and the title/excerpt/slug/headings/FAQ are useful for search without keyword stuffing. Do not demand numeric thresholds when the article is a methodology guide and tells readers how to verify current values from cited primary sources."
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
        if bool(review.get("pass")) and score >= settings.quality_threshold:
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
        "_fields": "title,link,date,modified",
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
            "title": str(title).strip(),
            "url": str(row.get("link", "")).strip(),
            "date": str(row.get("date", ""))[:10],
            "modified": str(row.get("modified", ""))[:10],
        })
    return [row for row in result if row["title"] and row["url"]]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def generated_cover_png(locale: str, title: str, variant: int = 0) -> bytes:
    """Create one original finance-chart visual without a paid image API.

    It is intentionally generated in-process from standard-library primitives:
    a locale palette, a deterministic market line, bars, and a savings coin.
    This keeps the GitHub-only deployment free while giving every post three
    distinct non-stock visuals. The HTML alt text carries the article-specific
    description for accessibility and SEO.
    """
    width, height = 1200, 675
    palettes = {
        "us": ((16, 45, 79), (54, 166, 160), (245, 190, 66)),
        "jp": ((38, 35, 78), (207, 78, 112), (241, 190, 73)),
        "kr": ((18, 58, 74), (35, 157, 154), (250, 184, 74)),
    }
    start, accent, gold = palettes.get(locale, palettes["us"])
    seed = hashlib.sha256(f"{locale}:{title}:{variant}".encode("utf-8")).digest()
    pixels = bytearray()

    def color_at(x: int, y: int) -> tuple[int, int, int]:
        mix = y / max(1, height - 1)
        return tuple(int(start[i] * (1 - mix) + accent[i] * mix * 0.35) for i in range(3))

    for y in range(height):
        pixels.append(0)  # filter byte
        for x in range(width):
            pixels.extend(color_at(x, y))

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

    # A translucent-looking panel (alpha compositing done directly on pixels).
    rectangle(72, 72, 1128, 603, (18, 35, 61))
    # Grid and rising line chart.
    grid = (64, 91, 112)
    for x in range(150, 1060, 150):
        for y in range(160, 525):
            if (y - 160) % 6 < 2:
                set_pixel(x, y, grid)
    for y in range(160, 526, 90):
        for x in range(150, 1060):
            if (x - 150) % 6 < 2:
                set_pixel(x, y, grid)
    points: list[tuple[int, int]] = []
    value = 0.35
    for index in range(13):
        value += ((seed[index] / 255) - 0.42) * 0.12
        value = max(0.12, min(0.88, value))
        points.append((170 + index * 72, int(500 - value * 300)))
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        steps = max(abs(x1 - x0), abs(y1 - y0))
        for step in range(steps + 1):
            x = round(x0 + (x1 - x0) * step / steps)
            y = round(y0 + (y1 - y0) * step / steps)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    set_pixel(x + dx, y + dy, accent)
    # Comparison bars and a coin motif.
    bar_sets = (
        (120, 190, 150, 235, 180),
        (170, 115, 220, 145, 205),
        (95, 230, 135, 185, 245),
    )
    for index, bar in enumerate(bar_sets[variant % len(bar_sets)]):
        rectangle(175 + index * 72, 500 - bar, 207 + index * 72, 500, (79, 128, 153))
    for radius in range(46, 0, -1):
        for angle in range(360):
            # A cheap circle rasterizer is enough for a compact cover graphic.
            x = 970 + round(radius * math.cos(math.radians(angle)))
            y = 220 + round(radius * math.sin(math.radians(angle)))
            set_pixel(x, y, gold if radius > 8 else (255, 226, 130))

    raw = bytes(pixels)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + _png_chunk(b"IDAT", zlib.compress(raw, 9)) + _png_chunk(b"IEND", b"")


def upload_generated_cover(settings: Settings, title: str, locale: str, variant: int = 0) -> tuple[int | None, str]:
    """Upload one free generated PNG and return (media_id, public_url)."""
    auth = wp_auth_header(settings)
    boundary = "----CodexFinance" + hashlib.sha256(f"{title}:{variant}".encode("utf-8")).hexdigest()[:16]
    image = generated_cover_png(locale, title, variant)
    purposes = ("overview", "comparison", "checklist")
    purpose = purposes[variant % len(purposes)]
    alt = f"{title} — original {purpose} chart illustration"
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"media[]\"; filename=\"finance-{purpose}.png\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8"),
        image,
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"attrs[0][title]\"\r\n\r\n{title} — {purpose}\r\n".encode("utf-8"),
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
    """Place three visuals at useful editorial points in the article body."""
    body = html.strip()
    if not figures:
        return body
    body = insert_figure_after_first_paragraph(body, figures[0])
    if len(figures) > 1:
        body = insert_figure_before_heading(body, figures[1], 1)
    if len(figures) > 2:
        faq = re.search(r"<h2\b[^>]*>[^<]*(?:FAQ|자주|よくある|질문)[^<]*</h2>", body, flags=re.I)
        if faq:
            body = body[: faq.start()] + figures[2] + body[faq.start() :]
        else:
            count = len(re.findall(r"<h2\b", body, flags=re.I))
            body = insert_figure_before_heading(body, figures[2], max(0, count - 1))
    return body


def wp_create(settings: Settings, article_json: str, topic: str, locale: str) -> dict[str, Any]:
    article = parse_json(article_json)
    title = str(article.get("title", topic))
    media: list[tuple[int, str]] = [upload_generated_cover(settings, title, locale, variant) for variant in range(3)]
    figures: list[str] = []
    for index, (_, media_url) in enumerate(media):
        purpose = ("overview", "comparison", "checklist")[index]
        alt = html_escape(f"{title} — original {purpose} chart illustration", quote=True)
        figures.append(f'<figure class="wp-block-image size-large finance-inline-visual finance-inline-visual-{index + 1}"><img src="{html_escape(media_url, quote=True)}" alt="{alt}" loading="lazy" /></figure>')
    excerpt = str(article.get("excerpt", "")).strip()
    if len(excerpt) > 300:
        excerpt = excerpt[:297].rstrip() + "..."
    payload = {
        "title": title,
        "slug": seo_slug(str(article.get("slug", ""))) or seo_slug(title),
        # WordPress renders featured_media above the article. Keep the total
        # visible visuals to three: one featured overview plus two inline
        # visuals placed around the body/FAQ sections.
        "content": compose_image_layout(str(article.get("html", "")), figures[1:]),
        "excerpt": excerpt,
        "status": settings.publish_mode,
        "featured_media": media[0][0],
    }
    post = http_json(wp_endpoint(settings, "posts"), payload, {"Authorization": wp_auth_header(settings)})
    post["_images_uploaded"] = len(media)
    return post


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
        result = wp_create(settings, article, topic, args.locale)
        pages = seed_pages(settings, args.locale) if args.seed_pages else 0
        print(json.dumps({
            "topic": topic,
            "click_potential": brief.get("click_potential"),
            "benchmark_sources": len(brief.get("benchmark_sources", [])) if isinstance(brief.get("benchmark_sources"), list) else 0,
            "growth_plan": brief.get("growth_plan", []),
            "id": result.get("id"),
            "link": result.get("link"),
            "status": result.get("status"),
            "images_uploaded": result.get("_images_uploaded", 0),
            "quality": review,
            "pages_created": pages,
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

