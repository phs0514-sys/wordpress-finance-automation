"""Read-only daily publication and WordPress.com stats report.

The report deliberately distinguishes site-level visitors from per-post views:
WordPress.com's stats API exposes visitors for a site, while the per-post
endpoint exposes views.  Missing stats permissions are reported as unavailable
instead of being treated as zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import engine


LOCALES = {
    "us": "미국",
    "jp": "일본",
    "kr": "한국",
}
try:
    REPORT_ZONE = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:  # Windows Python may not bundle the IANA database.
    REPORT_ZONE = timezone(timedelta(hours=9), "Asia/Seoul")


def get_json(url: str, auth: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": auth}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Keep diagnostics useful without echoing request headers or tokens.
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:240]}") from exc


def visits_report(settings: engine.Settings, auth: str) -> tuple[str | None, int | None, int | None, str | None]:
    query = urllib.parse.urlencode({"unit": "day", "quantity": "2"})
    url = f"https://public-api.wordpress.com/rest/v1/sites/{settings.wp_site_ref}/stats/visits?{query}"
    try:
        data = get_json(url, auth).get("data", [])
        if not data:
            return None, None, None, None
        # The API returns the requested days in chronological order.  At the
        # morning report time, the first row is the last completed calendar day
        # and the final row may still be in progress.
        row = data[-2] if len(data) >= 2 else data[-1]
        return str(row[0]), int(row[1]), int(row[2]), None
    except Exception as exc:  # stats permissions differ by WordPress.com site
        return None, None, None, str(exc)


def post_views(settings: engine.Settings, auth: str, post_ids: list[int]) -> tuple[dict[int, int], str | None]:
    if not post_ids:
        return {}, None
    result: dict[int, int] = {}
    try:
        # Keep URLs short enough for the API as the sites grow.
        for start in range(0, len(post_ids), 40):
            chunk = post_ids[start : start + 40]
            query = urllib.parse.urlencode({"post_ids": ",".join(str(post_id) for post_id in chunk)})
            url = f"https://public-api.wordpress.com/rest/v1.1/sites/{settings.wp_site_ref}/stats/views/posts?{query}"
            payload = get_json(url, auth)
            for item in payload.get("posts", []):
                result[int(item.get("ID"))] = int(item.get("views", 0))
        return result, None
    except Exception as exc:
        return {}, str(exc)


def clean_title(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("rendered", "")
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def site_report(locale: str) -> str:
    settings = engine.Settings.from_env(locale)
    auth = engine.wp_auth_header(settings)
    posts_url = engine.wp_endpoint(
        settings,
        "posts?per_page=100&orderby=date&order=desc&_fields=id,title,link,status,date,modified",
    )
    posts = get_json(posts_url, auth)
    published = [post for post in posts if post.get("status") == "publish"]
    ids = [int(post["id"]) for post in posts if post.get("id") is not None]
    views, views_error = post_views(settings, auth, ids)
    stat_date, site_views, visitors, visits_error = visits_report(settings, auth)

    lines = [f"## {LOCALES[locale]} 블로그 ({settings.wp_url})"]
    lines.append(f"- 발행 상태: 공개 {len(published)}편 / 전체 글 {len(posts)}편")
    if stat_date is None:
        lines.append(f"- 사이트 방문자수: 확인 불가 ({visits_error or '데이터 없음'})")
    else:
        lines.append(f"- {stat_date} 사이트 전체: 방문자 {visitors:,}명, 조회수 {site_views:,}회")
    if views_error:
        lines.append(f"- 글별 조회수: 확인 불가 ({views_error})")
    else:
        lines.append("- 글별 방문자수는 WordPress.com API가 제공하지 않아 글별 조회수로 표시합니다.")
    lines.append("")
    lines.append("| 상태 | 글 | 게시일 | 누적 조회수 | 링크 |\n|---|---|---|---:|---|")
    for post in posts:
        post_id = int(post.get("id", 0))
        status = "공개" if post.get("status") == "publish" else str(post.get("status", "-"))
        published_at = str(post.get("date", ""))[:10] or "-"
        link = post.get("link", "")
        title = clean_title(post.get("title", "(제목 없음)"))
        view_value = f"{views[post_id]:,}" if post_id in views else "-"
        lines.append(f"| {status} | {title} | {published_at} | {view_value} | [열기]({link}) |")
    if not posts:
        lines.append("| - | 아직 글이 없습니다 | - | - | - |")
    return "\n".join(lines)


def main() -> int:
    # Scheduled runs on Windows may inherit cp949.  Reports contain native
    # English, Japanese, and Korean titles, so always emit UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    engine.load_dotenv()
    # GitHub-hosted runners are normally UTC; pin the report to the user's
    # requested Korea time instead of depending on the runner's timezone.
    now = datetime.now(REPORT_ZONE)
    # The report describes the last completed local calendar day.
    report_day = (now - timedelta(days=1)).date().isoformat()
    print(f"# 금융 블로그 일일 리포트 ({report_day})")
    print("\n발행 성공 여부와 WordPress.com 통계를 읽기 전용으로 집계했습니다.\n")
    for locale in LOCALES:
        try:
            print(site_report(locale))
        except Exception as exc:
            print(f"## {LOCALES[locale]} 블로그\n- 리포트 오류: {exc}")
        print("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

