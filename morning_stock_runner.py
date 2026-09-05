"""Run the independent 04:00 local-time stock-analysis publication for legacy finance sites."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
LOCAL_ZONES = {"us": "America/New_York", "jp": "Asia/Tokyo", "kr": "Asia/Seoul"}
MORNING_HOUR = 4
MORNING_TOLERANCE_MINUTES = int(os.getenv("MORNING_SLOT_TOLERANCE_MINUTES", "45"))
STATE_PATH = ROOT / "data" / "morning_stock_runs.json"


def local_now(locale: str) -> datetime:
    return datetime.now(ZoneInfo(LOCAL_ZONES[locale]))


def due_now(locale: str) -> tuple[bool, str, str]:
    now = local_now(locale)
    minute = now.hour * 60 + now.minute
    target = MORNING_HOUR * 60
    due = target <= minute <= target + MORNING_TOLERANCE_MINUTES
    return due, now.date().isoformat(), now.strftime("%Y-%m-%d %H:%M %Z")


def load_state() -> dict[str, object]:
    if not STATE_PATH.exists():
        return {"locales": {}}
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"locales": {}}
    return value if isinstance(value, dict) else {"locales": {}}


def save_state(state: dict[str, object]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_locale(locale: str, state: dict[str, object]) -> dict[str, object]:
    due, local_date, local_time = due_now(locale)
    if not due:
        return {"locale": locale, "ok": True, "skipped": True, "reason": "outside_local_0400_window", "local_time": local_time}
    locale_state = state.setdefault("locales", {})
    if not isinstance(locale_state, dict):
        locale_state = {}
        state["locales"] = locale_state
    previous = locale_state.get(locale) if isinstance(locale_state.get(locale), dict) else {}
    if previous.get("local_date") == local_date and previous.get("status") == "published":
        return {"locale": locale, "ok": True, "skipped": True, "reason": "already_published_today", "local_time": local_time}

    command = [sys.executable, str(ROOT / "engine.py"), "--locale", locale]
    child_env = {**os.environ, "PUBLISH_SLOT_INDEX": "4", "PUBLISH_FORCE_RESUME": "1"}
    try:
        completed = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True, encoding="utf-8", env=child_env, timeout=1500)
    except Exception as exc:
        return {"locale": locale, "ok": False, "error": str(exc), "local_time": local_time}

    result: dict[str, object] = {"locale": locale, "ok": completed.returncode == 0, "local_date": local_date, "local_time": local_time}
    output, error = completed.stdout.strip(), completed.stderr.strip()
    if output:
        try:
            result["result"] = json.loads(output)
        except json.JSONDecodeError:
            result["result"] = output[-3000:]
    if completed.returncode != 0 and error:
        result["error"] = error[-3000:]
    if completed.returncode == 0:
        locale_state[locale] = {"local_date": local_date, "status": "published", "updated_at": datetime.utcnow().isoformat() + "Z"}
        save_state(state)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locale", choices=("all", "us", "jp", "kr"), default="all")
    args = parser.parse_args()
    locales = ("us", "jp", "kr") if args.locale == "all" else (args.locale,)
    state = load_state()
    results = [run_locale(locale, state) for locale in locales]
    print(json.dumps({"mode": "independent_local_0400_stock_analysis", "locales": locales, "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(bool(item.get("ok")) for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
