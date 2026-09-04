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


ROOT = Path(__file__).resolve().parent
LOCAL_ZONES = {"us": "America/New_York", "jp": "Asia/Tokyo", "kr": "Asia/Seoul"}
SLOTS = ((8, 0), (11, 50), (16, 0), (20, 0))
SCHEDULE_TOLERANCE_MINUTES = 12


def is_due_in_local_time(locale: str, slot_index: int) -> tuple[bool, str]:
    """Accept delayed GitHub cron starts while enforcing each site's local clock."""
    now = datetime.now(ZoneInfo(LOCAL_ZONES[locale]))
    target_hour, target_minute = SLOTS[slot_index]
    actual = now.hour * 60 + now.minute
    target = target_hour * 60 + target_minute
    delta = actual - target
    due = 0 <= delta <= SCHEDULE_TOLERANCE_MINUTES
    return due, now.strftime("%Y-%m-%d %H:%M %Z")


def run_locale(locale: str, topic: str | None = None, slot_index: int = 0, require_local_slot: bool = False) -> dict[str, object]:
    if require_local_slot:
        due, local_time = is_due_in_local_time(locale, slot_index)
        if not due:
            return {"locale": locale, "ok": True, "skipped": True, "reason": "outside_local_slot", "local_time": local_time, "slot_index": slot_index}
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
    result: dict[str, object] = {"locale": locale, "topic": topic or "dynamic", "ok": completed.returncode == 0, "slot_index": slot_index}
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot-index", type=int, default=0)
    parser.add_argument("--locale", choices=("all", "us", "jp", "kr"), default="all")
    parser.add_argument("--require-local-slot", action="store_true", help="Skip locales whose local clock is outside this slot")
    args = parser.parse_args()
    if not 0 <= args.slot_index <= 3:
        raise SystemExit("slot-index must be between 0 and 3")
    locales = ("us", "jp", "kr") if args.locale == "all" else (args.locale,)
    results = [run_locale(locale, slot_index=args.slot_index, require_local_slot=args.require_local_slot) for locale in locales]
    print(json.dumps({"slot_index": args.slot_index, "local_zones": {locale: LOCAL_ZONES[locale] for locale in locales}, "locales": locales, "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(bool(item.get("ok")) for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
