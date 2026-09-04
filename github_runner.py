"""Run one scheduled publishing slot for all configured locales.

GitHub Actions supplies the secrets and invokes this script four times per
day.  Each locale is a separate subprocess so a temporary failure in one
WordPress site does not prevent diagnostics for the other two sites.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

def run_locale(locale: str, topic: str | None = None, slot_index: int = 0) -> dict[str, object]:
    # With no fixed topic the engine reads Google News and performs a fresh
    # web-search-backed opportunity scan at the exact run time.
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
    result: dict[str, object] = {"locale": locale, "topic": topic or "dynamic", "ok": completed.returncode == 0}
    output = completed.stdout.strip()
    error = completed.stderr.strip()
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
    args = parser.parse_args()
    if not 0 <= args.slot_index <= 3:
        raise SystemExit("slot-index must be between 0 and 3")
    locales = ("us", "jp", "kr") if args.locale == "all" else (args.locale,)
    results = [run_locale(locale, slot_index=args.slot_index) for locale in locales]
    print(json.dumps({"slot_index": args.slot_index, "timezone": "Asia/Seoul", "locales": locales, "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(bool(item.get("ok")) for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

