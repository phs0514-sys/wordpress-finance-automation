"""Run country-local scheduled publishing slots for configured legacy locales."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import engine


ROOT = Path(__file__).resolve().parent
LOCAL_ZONES = {"us": "America/New_York", "jp": "Asia/Tokyo", "kr": "Asia/Seoul"}
SLOTS = ((8, 0), (11, 50), (16, 0), (20, 0))
SCHEDULE_TOLERANCE_MINUTES = int(os.getenv("PUBLISH_SLOT_TOLERANCE_MINUTES", "30"))
BACKFILL_START_MINUTE = 20 * 60 + 30


def is_due_in_local_time(locale: str, slot_index: int) -> tuple[bool, str]:
    """Accept delayed GitHub cron starts within the grace window while enforcing each site's local clock."""
    now = datetime.now(ZoneInfo(LOCAL_ZONES[locale]))
    target_hour, target_minute = SLOTS[slot_index]
    actual = now.hour * 60 + now.minute
    target = target_hour * 60 + target_minute
    delta = actual - target
    due = 0 <= delta <= SCHEDULE_TOLERANCE_MINUTES
    return due, now.strftime("%Y-%m-%d %H:%M %Z")


def local_today(locale: str) -> str:
    return datetime.now(ZoneInfo(LOCAL_ZONES[locale])).date().isoformat()


def published_today(locale: str) -> int:
    """Count the site's published posts dated today in its own local timezone."""
    settings = engine.Settings.from_env(locale)
    rows = engine.wp_recent_posts(settings, limit=100)
    today = local_today(locale)
    return sum(1 for row in rows if row.get("status") == "publish" and row.get("date") == today)


def in_backfill_window(locale: str) -> tuple[bool, str]:
    now = datetime.now(ZoneInfo(LOCAL_ZONES[locale]))
    actual = now.hour * 60 + now.minute
    return actual >= BACKFILL_START_MINUTE, now.strftime("%Y-%m-%d %H:%M %Z")


def run_locale(
    locale: str,
    topic: str | None = None,
    slot_index: int = 0,
    require_local_slot: bool = False,
) -> dict[str, object]:
    if require_local_slot:
        due, local_time = is_due_in_local_time(locale, slot_index)
        if not due:
            return {
                "locale": locale,
                "ok": True,
                "skipped": True,
                "reason": "outside_local_slot",
                "local_time": local_time,
                "slot_index": slot_index,
            }
    command = [sys.executable, str(ROOT / "engine.py"), "--locale", locale]
    if topic:
        command.extend(["--topic", topic])
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PUBLISH_SLOT_INDEX": str(slot_index)},
            timeout=1500,
        )
    except Exception as exc:
        return {"locale": locale, "topic": topic or "dynamic", "ok": False, "error": str(exc)}
    result: dict[str, object] = {
        "locale": locale,
        "topic": topic or "dynamic",
        "ok": completed.returncode == 0,
        "slot_index": slot_index,
    }
    output, error = completed.stdout.strip(), completed.stderr.strip()
    if output:
        try:
            result["result"] = json.loads(output)
            if isinstance(result["result"], dict) and result["result"].get("topic"):
                result["topic"] = result["result"]["topic"]
        except json.JSONDecodeError:
            result["result"] = output[-2000:]
    if completed.returncode != 0 and error:
        result["error"] = error[-2000:]
    return result


def run_daily_backfill(locale: str, target: int) -> dict[str, object]:
    """Fill the missing portion of the daily quota after the final slot."""
    allowed, local_time = in_backfill_window(locale)
    if not allowed:
        return {
            "locale": locale,
            "ok": True,
            "skipped": True,
            "reason": "before_daily_backfill_window",
            "local_time": local_time,
            "target_daily_posts": target,
        }

    try:
        count = published_today(locale)
    except Exception as exc:
        return {
            "locale": locale,
            "ok": False,
            "reason": "daily_count_failed",
            "error": str(exc)[:1000],
            "target_daily_posts": target,
        }

    attempts: list[dict[str, object]] = []
    max_attempts = max(target + 4, target * 2)
    while count < target and len(attempts) < max_attempts:
        result = run_locale(locale, slot_index=3, require_local_slot=False)
        attempts.append(result)
        if not result.get("ok"):
            error_text = str(result.get("error", "")).lower()
            hard_block = any(marker in error_text for marker in (
                "archived",
                "suspended",
                "unauthorized",
                "credentials",
                "api calls to this endpoint have been disabled",
            ))
            if hard_block:
                return {
                    "locale": locale,
                    "ok": False,
                    "reason": "backfill_publish_blocked",
                    "published_today": count,
                    "target_daily_posts": target,
                    "attempts": attempts,
                }
            # Research/API hiccups and overlap rejections are retryable.
            # Keep the retry count bounded so hard gates still fail clearly.
            continue
        try:
            updated_count = published_today(locale)
        except Exception as exc:
            attempts.append({
                "locale": locale,
                "ok": False,
                "reason": "daily_count_failed_after_publish",
                "error": str(exc)[:1000],
            })
            continue
        if updated_count <= count:
            attempts.append({
                "locale": locale,
                "ok": False,
                "reason": "publication_did_not_increase_daily_count",
                "published_today": updated_count,
            })
            continue
        count = updated_count

    return {
        "locale": locale,
        "ok": count >= target,
        "status": "DAILY_QUOTA_MET" if count >= target else "DAILY_QUOTA_UNMET",
        "published_today": count,
        "target_daily_posts": target,
        "attempts": attempts,
        "local_time": local_time,
    }

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot-index", type=int, default=0)
    parser.add_argument("--locale", choices=("all", "us", "jp", "kr"), default="all")
    parser.add_argument(
        "--require-local-slot",
        action="store_true",
        help="Skip locales whose local clock is outside this slot",
    )
    parser.add_argument(
        "--ensure-daily-quota",
        action="store_true",
        help="After the final local slot, publish only the missing posts needed to reach the daily quota",
    )
    parser.add_argument(
        "--target-daily-posts",
        type=int,
        default=int(os.getenv("LEGACY_DAILY_TARGET", "4")),
    )
    args = parser.parse_args()
    if not 0 <= args.slot_index <= 3:
        raise SystemExit("slot-index must be between 0 and 3")
    if args.target_daily_posts < 1:
        raise SystemExit("target-daily-posts must be at least 1")

    locales = ("us", "jp", "kr") if args.locale == "all" else (args.locale,)
    if args.ensure_daily_quota:
        results = [run_daily_backfill(locale, args.target_daily_posts) for locale in locales]
        payload = {
            "mode": "daily_backfill",
            "target_daily_posts": args.target_daily_posts,
            "locales": locales,
            "results": results,
        }
    else:
        results = [
            run_locale(locale, slot_index=args.slot_index, require_local_slot=args.require_local_slot)
            for locale in locales
        ]
        payload = {
            "mode": "scheduled_slot",
            "slot_index": args.slot_index,
            "local_zones": {locale: LOCAL_ZONES[locale] for locale in locales},
            "locales": locales,
            "results": results,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(bool(item.get("ok")) for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
