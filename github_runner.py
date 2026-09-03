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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
try:
    KST = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:  # Windows Python may not bundle the IANA database.
    KST = timezone(timedelta(hours=9), "Asia/Seoul")

TOPICS = {
    "us": (
        "How to compare total costs of broad-market ETFs using standardized disclosures",
        "A practical checklist for reviewing IRA and 401(k) contribution rules from official sources",
        "How to verify Treasury and Federal Reserve rate data before making a personal-finance decision",
        "Dividend tax reporting basics using IRS and broker documents",
    ),
    "jp": (
        "NISAで投資信託のコストを一次資料から比較する手順",
        "iDeCoの拠出と受け取りを公式資料で確認するチェックリスト",
        "日本銀行と金融庁の公開データで金利情報を確認する方法",
        "投資信託の分配金と税務を公式資料で読み解く手順",
    ),
    "kr": (
        "ISA 계좌의 세제와 납입 한도를 공식 자료로 확인하는 방법",
        "연금저축과 IRP의 공제 요건을 국세청·금융감독원 자료로 검증하기",
        "한국은행 기준금리와 ECOS 통계를 읽는 실전 체크리스트",
        "ETF 분배금과 세금 정보를 금융당국 공시로 확인하는 방법",
    ),
}


def topic_for(locale: str, slot_index: int, now: datetime | None = None) -> str:
    moment = now or datetime.now(KST)
    choices = TOPICS[locale]
    return choices[(moment.date().toordinal() * 4 + slot_index) % len(choices)]


def run_locale(locale: str, topic: str) -> dict[str, object]:
    command = [sys.executable, str(ROOT / "engine.py"), "--locale", locale, "--topic", topic]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=1500,
        )
    except Exception as exc:
        return {"locale": locale, "topic": topic, "ok": False, "error": str(exc)}
    result: dict[str, object] = {"locale": locale, "topic": topic, "ok": completed.returncode == 0}
    output = completed.stdout.strip()
    error = completed.stderr.strip()
    if output:
        try:
            result["result"] = json.loads(output)
        except json.JSONDecodeError:
            result["result"] = output[-2000:]
    if completed.returncode != 0 and error:
        result["error"] = error[-2000:]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot-index", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.slot_index <= 3:
        raise SystemExit("slot-index must be between 0 and 3")
    results = [run_locale(locale, topic_for(locale, args.slot_index)) for locale in ("us", "jp", "kr")]
    print(json.dumps({"slot_index": args.slot_index, "timezone": "Asia/Seoul", "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(bool(item.get("ok")) for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

