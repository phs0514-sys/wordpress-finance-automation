"""Read-only technical SEO and page-health monitor for the three sites.

The monitor never changes WordPress.  It records actionable warnings and a
small set of hard failures (HTTP errors, accidental noindex, broken required
links, or missing sitemap) for the publishing kill switch.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import engine


LOCALES = {"us": "미국", "jp": "일본", "kr": "한국"}
HEALTH_PATH = engine.DATA_DIR / "technical_health.json"


def fetch(url: str) -> tuple[int, str, dict[str, str], str]:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FinanceSEOHealth/1.0)"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return int(response.status), str(response.geturl()), {str(k).lower(): str(v) for k, v in response.headers.items()}, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), str(exc.geturl() or url), {}, exc.read().decode("utf-8", errors="replace")[:500]
    except Exception as exc:
        return 0, url, {}, str(exc)


def check_page(url: str, home_url: str) -> dict[str, object]:
    status, final_url, headers, html = fetch(url)
    issues: list[str] = []
    if status != 200:
        issues.append(f"HTTP status {status or 'unreachable'}")
    if re.search(r"<meta[^>]+name=[\"']robots[\"'][^>]+content=[\"'][^\"']*noindex", html, flags=re.I):
        issues.append("accidental noindex")
    canonicals = re.findall(r"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)", html, flags=re.I)
    if len(canonicals) > 1:
        issues.append("duplicate canonical tags")
    if canonicals and not re.match(r"^https?://", canonicals[0]):
        issues.append("relative canonical")
    max_image_preview = bool(re.search(r"max-image-preview\s*:\s*large", html, flags=re.I))
    image_count = len(re.findall(r"<img\b", html, flags=re.I))
    missing_images = len(re.findall(r"<img\b(?![^>]+\bsrc=)[^>]*>", html, flags=re.I))
    if missing_images:
        issues.append(f"{missing_images} image(s) missing src")
    links = re.findall(r"<a\b[^>]+href=[\"']([^\"'#]+)", html, flags=re.I)
    internal = [urllib.parse.urljoin(home_url + "/", link) for link in links if link.startswith(home_url) or link.startswith("/")]
    return {
        "url": url,
        "status": status,
        "final_url": final_url,
        "content_type": headers.get("content-type", ""),
        "canonical": canonicals[0] if canonicals else None,
        "max_image_preview_large": max_image_preview,
        "images": image_count,
        "internal_links": len(internal),
        "internal_urls": internal[:20],
        "issues": issues,
        "html_bytes": len(html.encode("utf-8")),
        "structured_data": {name: bool(re.search(name, html, flags=re.I)) for name in ("Article", "BreadcrumbList", "Organization")},
    }


def locale_health(locale: str) -> dict[str, object]:
    settings = engine.Settings.from_env(locale)
    home = settings.wp_url.rstrip("/")
    pages: list[dict[str, object]] = [check_page(home, home)]
    try:
        posts = engine.wp_recent_posts(settings, limit=20)
    except Exception as exc:
        posts = []
        pages[0]["issues"] = list(pages[0].get("issues", [])) + [f"WordPress API read failed: {str(exc)[:180]}"]
    for row in posts:
        url = str(row.get("url", ""))
        if url:
            pages.append(check_page(url, home))
    sitemap_status, _, _, sitemap_body = fetch(f"{home}/sitemap.xml")
    robots_status, _, _, robots_body = fetch(f"{home}/robots.txt")
    issues = [f"{page['url']}: {item}" for page in pages for item in page.get("issues", [])]
    checked_links: set[str] = set()
    for page in pages:
        for link in page.get("internal_urls", []) if isinstance(page.get("internal_urls"), list) else []:
            if link in checked_links or len(checked_links) >= 30:
                continue
            checked_links.add(link)
            link_status, _, _, _ = fetch(str(link))
            if link_status < 200 or link_status >= 400:
                issues.append(f"broken internal link {link} (HTTP {link_status or 'unreachable'})")
    if pages and not any(bool(page.get("max_image_preview_large")) for page in pages):
        issues.append("max-image-preview:large not detected (theme/SEO setting check)")
    if sitemap_status != 200:
        issues.append(f"sitemap.xml HTTP status {sitemap_status or 'unreachable'}")
    if robots_status != 200:
        issues.append(f"robots.txt HTTP status {robots_status or 'unreachable'}")
    if re.search(r"(?im)^\s*disallow:\s*/\s*$", robots_body):
        issues.append("robots.txt blocks the whole site")
    # Broken internal links are sampled conservatively to avoid hammering the
    # host; page-level status errors remain hard failures above.
    critical = any("HTTP status" in item or "accidental noindex" in item or "sitemap.xml" in item or "robots.txt blocks" in item or "broken internal link" in item for item in issues)
    return {
        "locale": locale,
        "url": home,
        "checked_pages": len(pages),
        "sitemap_status": sitemap_status,
        "robots_status": robots_status,
        "core_web_vitals": "not collected by the standard-library monitor; use PageSpeed/Search Console field data",
        "structured_data_expectation": "Article/BreadcrumbList/Organization should be supplied by the active WordPress theme/SEO integration",
        "critical": critical,
        "issues": issues[:40],
        "pages": pages,
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    engine.load_dotenv()
    result: dict[str, object] = {"generated_at": datetime.now(timezone.utc).isoformat(), "locales": {}, "critical": False}
    for locale, label in LOCALES.items():
        try:
            value = locale_health(locale)
        except Exception as exc:
            value = {"locale": locale, "critical": True, "issues": [f"{label} monitor error: {str(exc)[:180]}"]}
        result["locales"][locale] = value
        result["critical"] = bool(result["critical"] or value.get("critical"))
    engine.save_json_file(HEALTH_PATH, result)
    print(json.dumps({"critical": result["critical"], "health_path": str(HEALTH_PATH), "locales": {key: {"critical": value.get("critical"), "issues": value.get("issues", [])[:5]} for key, value in result["locales"].items()}}, ensure_ascii=False, indent=2))
    return 1 if result["critical"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

