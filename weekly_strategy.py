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

    def averages(values: dict[str, list[float]]) -> dict[str, object]:
        return {key: {"posts": len(items), "avg_7d_clicks": round(sum(items) / len(items), 2)} for key, items in values.items()}

    return {
        "posts": len(history),
        "by_category": averages(by_category),
        "by_headline": averages(by_headline),
        "by_layout": averages(by_layout),
        "by_locale": averages(by_locale),
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
            "You are the weekly editorial strategy lead for three localized blogs. Analyze the supplied performance snapshot and return exactly one JSON object with generated_at, status, content_mix, category_weights, headline_patterns, layout_recommendations, update_candidates, experiments, and next_week_actions. "
            "Keep the 70/20/10 mix: 70% proven topic groups, 20% adjacent groups, 10% experiments. Adjust weights only when there are enough observations; otherwise keep neutral weights. Recommend updates to existing URLs when their intent is already covered. Never propose changes to authentication, secrets, WordPress connections, GitHub Actions core code, or data-collection code."
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

