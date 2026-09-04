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


def gsc_query(settings: engine.Settings, start_date: str, end_date: str, dimensions: list[str], row_limit: int = 25, page_url: str | None = None) -> tuple[list[dict[str, object]], str | None]:
    """Read Search Console query/page/country/device rows when a token exists."""
    if not settings.gsc_token:
        return [], "GSC 토큰 미설정"
    body: dict[str, object] = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "rowLimit": row_limit,
        "dataState": "all",
    }
    if page_url:
        body["dimensionFilterGroups"] = [{"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}]
    site = urllib.parse.quote(settings.gsc_site_url or settings.wp_url, safe="")
    url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
    try:
        payload = engine.http_json(url, body, {"Authorization": f"Bearer {settings.gsc_token}"})
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else [], None
    except Exception as exc:
        # Do not expose the token or request headers in reports.
        return [], f"GSC 조회 오류: {str(exc)[:220]}"


def gsc_summary(settings: engine.Settings, posts: list[dict[str, object]], now: datetime) -> tuple[dict[str, object], str | None]:
    if not settings.gsc_token:
        return {}, "GSC 토큰 미설정"
    # Search Console data can be preliminary for the latest couple of days.
    end = (now.date() - timedelta(days=2))
    windows = {
        "24h": (end, end),
        "72h": (end - timedelta(days=2), end),
        "7d": (end - timedelta(days=6), end),
        "28d": (end - timedelta(days=27), end),
    }
    page_metrics: dict[str, dict[str, dict[str, float]]] = {}
    for window, (start, finish) in windows.items():
        rows, error = gsc_query(settings, start.isoformat(), finish.isoformat(), ["page"], row_limit=250)
        if error:
            return {}, error
        for row in rows:
            if not isinstance(row, dict):
                continue
            keys = row.get("keys", [])
            page = str(keys[0]) if isinstance(keys, list) and keys else ""
            if page:
                page_metrics.setdefault(page, {})[window] = {
                    "clicks": float(row.get("clicks", 0) or 0),
                    "impressions": float(row.get("impressions", 0) or 0),
                    "ctr": float(row.get("ctr", 0) or 0),
                    "position": float(row.get("position", 0) or 0),
                }
    dimension_rows: dict[str, list[dict[str, object]]] = {}
    for dimension in ("query", "country", "device"):
        rows, error = gsc_query(settings, windows["7d"][0].isoformat(), end.isoformat(), [dimension], row_limit=10)
        if error:
            return {}, error
        dimension_rows[dimension] = rows
    return {"end_date": end.isoformat(), "preliminary_through": end.isoformat(), "pages": page_metrics, "dimensions": dimension_rows}, None


def update_history_metrics(gsc: dict[str, object]) -> None:
    if not gsc or not isinstance(gsc.get("pages"), dict):
        return
    history = engine.load_article_history()
    pages = gsc["pages"]
    for row in history:
        url = str(row.get("url", ""))
        metrics = pages.get(url) if isinstance(pages, dict) else None
        if not isinstance(metrics, dict):
            continue
        existing = row.setdefault("metrics", {})
        if not isinstance(existing, dict):
            existing = {}
            row["metrics"] = existing
        for window, values in metrics.items():
            existing[window] = values
    engine.save_json_file(engine.HISTORY_PATH, history)


def diagnose_metrics(gsc: dict[str, object]) -> list[str]:
    """Turn Search Console patterns into concrete next actions."""
    pages = gsc.get("pages", {}) if isinstance(gsc, dict) else {}
    if not isinstance(pages, dict):
        return []
    diagnoses: list[str] = []
    for url, windows in pages.items():
        seven = windows.get("7d", {}) if isinstance(windows, dict) else {}
        if not isinstance(seven, dict):
            continue
        impressions = float(seven.get("impressions", 0) or 0)
        clicks = float(seven.get("clicks", 0) or 0)
        ctr = float(seven.get("ctr", 0) or 0)
        position = float(seven.get("position", 0) or 0)
        if impressions >= 100 and ctr < 0.02:
            diagnoses.append(f"제목/스니펫 개선 후보: {url} (노출 {impressions:.0f}, CTR {ctr * 100:.1f}%) → 다음 검수에서 제목 5안 생성")
        elif 8 <= position <= 20 and ctr >= 0.02:
            diagnoses.append(f"우선 육성: {url} (평균 {position:.1f}위) → 본문 보강 및 내부링크 3~5개")
        elif 1 <= position <= 5 and clicks >= 5:
            diagnoses.append(f"성공 주제 확장: {url} (평균 {position:.1f}위, 클릭 {clicks:.0f}) → 후속 키워드 클러스터")
        elif impressions < 10:
            diagnoses.append(f"색인/주제 점검: {url} (7일 노출 {impressions:.0f}) → 새 글 남발보다 색인 상태 확인")
    return diagnoses[:12]


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
    gsc, gsc_error = gsc_summary(settings, posts, datetime.now(REPORT_ZONE))
    if gsc:
        update_history_metrics(gsc)

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
    if gsc_error:
        lines.append(f"- Google Search Console: {gsc_error}")
    else:
        dimensions = gsc.get("dimensions", {})
        def top_dimension(name: str) -> str:
            rows = dimensions.get(name, []) if isinstance(dimensions, dict) else []
            if not isinstance(rows, list):
                return "-"
            values = []
            for row in rows[:3]:
                if not isinstance(row, dict):
                    continue
                keys = row.get("keys", [])
                label = str(keys[0]) if isinstance(keys, list) and keys else "-"
                values.append(f"{label} ({float(row.get('clicks', 0) or 0):.0f} clicks)")
            return ", ".join(values) or "-"
        lines.append(f"- Google Search Console (최신 집계 종료일 {gsc.get('end_date')}): 24h/72h/7d/28d 페이지 성과 저장 완료")
        lines.append("- GSC 최신 24시간 수치는 잠정치일 수 있으며, 72시간·7일·28일 수치가 누적될수록 안정적으로 해석합니다.")
        lines.append(f"- 7일 주요 Query: {top_dimension('query')}")
        lines.append(f"- 7일 주요 국가/기기: {top_dimension('country')} / {top_dimension('device')}")
        diagnoses = diagnose_metrics(gsc)
        if diagnoses:
            lines.append("- 자동 진단:")
            lines.extend(f"  - {item}" for item in diagnoses)
    lines.append("")
    lines.append("| 상태 | 글 | 게시일 | 누적 조회수 | GSC 7일 노출/클릭 | 평균위치 | 링크 |\n|---|---|---|---:|---:|---:|---|")
    gsc_pages = gsc.get("pages", {}) if isinstance(gsc, dict) else {}
    for post in posts:
        post_id = int(post.get("id", 0))
        status = "공개" if post.get("status") == "publish" else str(post.get("status", "-"))
        published_at = str(post.get("date", ""))[:10] or "-"
        link = post.get("link", "")
        title = clean_title(post.get("title", "(제목 없음)"))
        view_value = f"{views[post_id]:,}" if post_id in views else "-"
        page_metrics = gsc_pages.get(str(link), {}) if isinstance(gsc_pages, dict) else {}
        seven = page_metrics.get("7d", {}) if isinstance(page_metrics, dict) else {}
        impressions = f"{float(seven.get('impressions', 0)):.0f}" if isinstance(seven, dict) and seven else "-"
        clicks = f"{float(seven.get('clicks', 0)):.0f}" if isinstance(seven, dict) and seven else "-"
        position = f"{float(seven.get('position', 0)):.1f}" if isinstance(seven, dict) and seven else "-"
        lines.append(f"| {status} | {title} | {published_at} | {view_value} | {impressions}/{clicks} | {position} | [열기]({link}) |")
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

