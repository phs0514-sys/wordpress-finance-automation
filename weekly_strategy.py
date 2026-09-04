"""Build a read-only weekly strategy recommendation from the performance store."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import sys

import engine


def summarize(history: list[dict[str, object]]) -> dict[str, object]:
    by_category: dict[str, list[float]] = defaultdict(list)
    by_headline: dict[str, list[float]] = defaultdict(list)
    by_layout: dict[str, list[float]] = defaultdict(list)
    by_locale: dict[str, list[float]] = defaultdict(list)
    by_slot: dict[str, list[float]] = defaultdict(list)
    confidence_counts = defaultdict(int)
    value_rows: list[dict[str, object]] = []
    for row in history:
        metrics = row.get("metrics", {})
        seven = metrics.get("7d", {}) if isinstance(metrics, dict) else {}
        clicks = float(seven.get("clicks", 0) or 0) if isinstance(seven, dict) else 0.0
        category = str(row.get("category", "other")) or "other"
        headline = str(row.get("headline_type", "other")) or "other"
        layout = str(row.get("layout_type", "explainer")) or "explainer"
        locale = str(row.get("locale", "unknown")) or "unknown"
        by_category[category].append(clicks)
        by_headline[headline].append(clicks)
        by_layout[layout].append(clicks)
        by_locale[locale].append(clicks)
        by_slot[str(row.get("publish_slot", "0"))].append(clicks)
        impressions = float(seven.get("impressions", 0) or 0) if isinstance(seven, dict) else 0.0
        ctr = float(seven.get("ctr", 0) or 0) if isinstance(seven, dict) else 0.0
        position = float(seven.get("position", 0) or 0) if isinstance(seven, dict) else 0.0
        try:
            measured = datetime.fromisoformat(str(seven.get("measured_at", "")).replace("Z", "+00:00")) if isinstance(seven, dict) and seven.get("measured_at") else None
            age_days = max(0.0, (datetime.now(timezone.utc) - measured).total_seconds() / 86400) if measured else 0.0
        except (TypeError, ValueError):
            age_days = 0.0
        if impressions >= 1000 and age_days >= 7:
            confidence = "HIGH"
        elif impressions >= 100 and age_days >= 3:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        confidence_counts[confidence] += 1
        # Four dimensions avoid letting raw traffic force every future story
        # toward one category. Revenue is intentionally unknown until a
        # monetization connector supplies it.
        value_rows.append({
            "topic": row.get("topic", ""),
            "locale": row.get("locale", ""),
            "experiment_group": row.get("experiment_group", "optimized"),
            "confidence": confidence,
            "traffic_score": round(min(100.0, clicks * 2.0), 1),
            "authority_score": round(min(100.0, float(row.get("sources_count", 0) or 0) * 12 + float(row.get("originality_count", 0) or 0) * 8), 1),
            "engagement_score": round(min(100.0, ctr * 1000.0), 1),
            "revenue_score": None,
            "impressions": impressions,
            "position": position,
        })

    def averages(values: dict[str, list[float]]) -> dict[str, object]:
        return {key: {"posts": len(items), "avg_7d_clicks": round(sum(items) / len(items), 2)} for key, items in values.items()}

    usage = engine.load_json_file(engine.SOURCE_USAGE_PATH, {})
    source_usage = []
    if isinstance(usage, dict):
        total = sum(int(value.get("times_used", 0) or 0) for value in usage.values() if isinstance(value, dict)) or 1
        for domain, value in sorted(usage.items(), key=lambda item: int(item[1].get("times_used", 0) or 0) if isinstance(item[1], dict) else 0, reverse=True)[:20]:
            if not isinstance(value, dict):
                continue
            count = int(value.get("times_used", 0) or 0)
            source_usage.append({"domain": domain, "times_used": count, "share": round(count / total, 3)})
    return {
        "posts": len(history),
        "by_category": averages(by_category),
        "by_headline": averages(by_headline),
        "by_layout": averages(by_layout),
        "by_locale": averages(by_locale),
        "by_publish_slot": averages(by_slot),
        "confidence_counts": dict(confidence_counts),
        "article_value_dimensions": value_rows[-100:],
        "source_usage": source_usage,
        "recent_topics": [row.get("topic", "") for row in history[-20:]],
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    engine.load_dotenv()
    history = engine.load_article_history()
    if not history:
        strategy = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "insufficient_data",
            "content_mix": {"proven": 0.70, "adjacent": 0.20, "experiment": 0.10},
            "category_weights": {},
            "next_week_actions": ["Collect at least seven days of post and Search Console data before changing category weights."],
        }
    else:
        settings = engine.Settings.from_env("us")
        snapshot = summarize(history)
        prompt = (
            "You are the weekly editorial strategy lead for three localized blogs. Analyze the supplied performance snapshot and return exactly one JSON object with generated_at, status, content_mix, category_weights, headline_patterns, layout_recommendations, publish_slot_weights, update_candidates, experiments, confidence_notes, and next_week_actions. Use by_publish_slot to recommend which existing slot should receive stronger topics; do not rewrite the GitHub cron automatically. "
            "Treat 24h as Early Signal, 72h as Preliminary Evaluation, 7d as Main Evaluation, and 28d as Long-term Evaluation. Weight HIGH-confidence samples much more than LOW-confidence samples; never react to a tiny impression sample. Keep a starting 70/20/10 proven/adjacent/experiment mix but allow 65–80% exploit and 20–35% explore, with no category receiving less than 5–10% exploration. Compare control vs optimized experiment groups when enough data exists. Recommend KEEP, UPDATE, MERGE_CANDIDATE, or NOINDEX_CANDIDATE classifications only; never delete or redirect automatically. Recommend updates to existing URLs when their intent is already covered. Never propose changes to authentication, secrets, WordPress connections, GitHub Actions core code, or data-collection code."
        )
        response = engine.openai_text(settings, prompt, json.dumps(snapshot, ensure_ascii=False), max_output_tokens=5000, json_output=True, model=settings.research_model, reasoning_effort=settings.research_reasoning)
        try:
            strategy = engine.parse_json(response)
        except ValueError:
            strategy = {"status": "parse_error", "next_week_actions": ["Keep current weights until the next successful strategy run."]}
        strategy["generated_at"] = datetime.now(timezone.utc).isoformat()
        strategy["source_posts"] = len(history)
        strategy.setdefault("content_mix", {"proven": 0.70, "adjacent": 0.20, "experiment": 0.10})
    engine.save_json_file(engine.STRATEGY_PATH, strategy)
    print(json.dumps({"status": strategy.get("status", "ok"), "source_posts": len(history), "strategy_path": str(engine.STRATEGY_PATH)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

