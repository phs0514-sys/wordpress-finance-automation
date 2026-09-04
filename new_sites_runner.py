"""Scan and process the three new sites on a 30-minute issue cycle.

The legacy sites keep their four daily publication slots in ``publish.yml``.
This runner is intentionally separate: every 30 minutes it discovers and
scores fresh issues for each new site, records the scan, and only runs the
expensive writing/review/publish path when the opportunity clears the
configured threshold and the per-site daily cap has not been reached.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import engine


ROOT = Path(__file__).resolve().parent
SCAN_PATH = engine.DATA_DIR / "new_site_issue_scan.json"
LOCALES = ("us", "jp", "kr")


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def published_today(locale: str) -> int:
    """Count today's published/update operations in the local timezone."""
    today = datetime.now(timezone.utc).astimezone(engine.LOCAL_ZONE).date()
    count = 0
    for row in engine.load_article_history():
        if not isinstance(row, dict) or row.get("locale") != locale:
            continue
        timestamp = _parse_timestamp(row.get("created_at") or row.get("publish_time"))
        if timestamp and timestamp.astimezone(engine.LOCAL_ZONE).date() == today:
            count += 1
    return count


def _save_scan(locale: str, payload: dict[str, Any]) -> None:
    store = engine.load_json_file(SCAN_PATH, {})
    if not isinstance(store, dict):
        store = {}
    store[locale] = payload
    engine.save_json_file(SCAN_PATH, store)


def _target_url(brief: dict[str, Any]) -> str:
    target = str(brief.get("target_post_id") or "")
    if not target.isdigit():
        return ""
    for row in brief.get("recent_published_posts", []) if isinstance(brief.get("recent_published_posts"), list) else []:
        if isinstance(row, dict) and str(row.get("id")) == target:
            return str(row.get("url", ""))
    return ""


def process_locale(locale: str, min_score: int, max_daily_posts: int) -> dict[str, Any]:
    settings = engine.Settings.from_env(locale)
    generated_at = datetime.now(timezone.utc).isoformat()
    scan: dict[str, Any] = {
        "locale": locale,
        "generated_at": generated_at,
        "site_url": settings.wp_url,
        "status": "SCORING",
    }
    try:
        topic, brief = engine.collect_research(settings, locale)
        score = int(brief.get("opportunity_score") or brief.get("click_potential") or 0)
        scan.update({
            "topic": topic,
            "opportunity_score": score,
            "click_potential": brief.get("click_potential"),
            "action": brief.get("action", "new"),
            "category": brief.get("category", ""),
            "status": "WATCH" if score < min_score else "RESEARCH",
            "candidate_pool_size": len(brief.get("candidate_pool", [])) if isinstance(brief.get("candidate_pool"), list) else None,
            "benchmark_sources": len(brief.get("benchmark_sources", [])) if isinstance(brief.get("benchmark_sources"), list) else 0,
            "official_sources": len(brief.get("official_sources", [])) if isinstance(brief.get("official_sources"), list) else 0,
            "trend_snapshot": brief.get("google_trends_snapshot", []),
            "news_snapshot": brief.get("google_news_snapshot", []),
        })
        today_count = published_today(locale)
        scan["published_today"] = today_count
        if today_count >= max_daily_posts:
            scan.update({"status": "CAP_REACHED", "reason": f"daily cap {max_daily_posts} reached"})
            engine.record_publish_outcome(True)
            _save_scan(locale, scan)
            return {"locale": locale, "ok": True, "status": scan["status"], "topic": topic, "score": score}
        if score < min_score:
            scan["reason"] = f"score below minimum {min_score}"
            engine.record_publish_outcome(True)
            _save_scan(locale, scan)
            return {"locale": locale, "ok": True, "status": scan["status"], "topic": topic, "score": score}

        article, brief = engine.create_article(settings, locale, topic, brief)
        article, review = engine.review_and_revise(settings, locale, topic, article, brief)
        final_article = engine.parse_json(article)
        recent_3d = brief.get("recent_3d_posts", []) if isinstance(brief, dict) else []
        if isinstance(recent_3d, list) and engine.topic_overlaps_recent(
            str(final_article.get("title", topic)),
            {"focus_keyword": final_article.get("seo", {}).get("focus_keyword", "") if isinstance(final_article.get("seo"), dict) else "", "search_intent": topic, "angle": ""},
            recent_3d,
        ):
            raise RuntimeError("Final article title overlapped a post from the last 3 days; publication was blocked")
        action = str(brief.get("action", "new"))
        target_url = _target_url(brief) if action == "update" else ""
        content_issues = engine.validate_article_content(final_article, brief, target_url)
        if content_issues:
            raise RuntimeError("Pre-publish hard gate failed: " + "; ".join(content_issues))
        if action == "update" and brief.get("target_post_id"):
            result = engine.wp_update(settings, int(brief["target_post_id"]), article, topic, locale, brief)
        else:
            action = "new"
            result = engine.wp_create(settings, article, topic, locale, brief)
        reverse_links, reverse_link_errors = engine.add_reverse_internal_links(settings, result, brief) if action == "new" else (0, [])
        result["_reverse_links_added"] = reverse_links
        result["_reverse_link_errors"] = reverse_link_errors
        result["_content_version"] = engine.record_article_version(locale, final_article, brief, result, action)
        engine.record_article_history(locale, final_article, brief, review, result, action)
        engine.record_source_usage(brief, locale)
        engine.record_publish_outcome(True)
        scan.update({
            "status": "PUBLISHED" if action == "new" else "UPDATED",
            "action": action,
            "post_id": result.get("id"),
            "url": result.get("link"),
            "quality_score": review.get("score") if isinstance(review, dict) else None,
            "images_uploaded": result.get("_images_uploaded", 0),
        })
        _save_scan(locale, scan)
        return {"locale": locale, "ok": True, "status": scan["status"], "topic": topic, "score": score, "post_id": result.get("id")}
    except Exception as exc:
        message = str(exc)
        scan.update({"status": "ERROR", "error": message[:1000]})
        try:
            engine.record_publish_outcome(False, message)
        finally:
            _save_scan(locale, scan)
        return {"locale": locale, "ok": False, "status": "ERROR", "error": message[:1000]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the 30-minute issue cycle for new sites")
    parser.add_argument("--locale", choices=("all", *LOCALES), default="all")
    parser.add_argument("--min-score", type=int, default=int(os.getenv("NEW_SITE_MIN_SCORE", "85")))
    parser.add_argument("--max-daily-posts", type=int, default=int(os.getenv("NEW_SITE_MAX_DAILY_POSTS", "2")))
    args = parser.parse_args()
    locales = LOCALES if args.locale == "all" else (args.locale,)
    results = [process_locale(locale, args.min_score, args.max_daily_posts) for locale in locales]
    print(json.dumps({"cycle": "30m", "min_score": args.min_score, "max_daily_posts": args.max_daily_posts, "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(bool(row.get("ok")) for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

