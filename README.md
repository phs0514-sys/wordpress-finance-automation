# US/JP/KR WordPress finance automation

This is a separate WordPress implementation of the existing US/Japan plan. It does not modify or reconnect the old Blogger project. WordPress.com Free sites can use the public REST API; this implementation uses the WordPress.com API proxy and OAuth2 instead of requiring a paid plugin plan.

## Pipeline

1. Read 30–50 locale-specific Google Trending Searches and Google News RSS candidates, then score each opportunity by interest, velocity, search intent, SERP feasibility, title CTR potential, durability, and site fit rather than raw popularity alone. A weekly strategy file supplies learned category and publish-slot weights while preserving a 70/20/10 proven/adjacent/experiment mix.
2. Use a web-search-backed research call to benchmark up to five leading result pages, official sources, and recent sources. Five is a target rather than a brittle minimum: a fast-moving story can publish with fewer trustworthy sources. The brief records common coverage, optional coverage, outdated claims, disagreements to verify, claim-to-source mappings, and original-value additions absent from the benchmark pages.
3. Enforce a three-day hard exclusion for overlapping events, entities, primary keywords, and search intent. When an older URL already owns the intent and needs fresh facts, the research action can be `update`; otherwise the engine creates a new URL. A final pre-publish guard blocks near-duplicate titles.
4. Generate a native-language article with one of six layout branches (news, comparison, howto, timeline, explainer, checklist), SEO fields, optional FAQ, contextual internal links, update notes, risks, and a general-information notice. New articles add reverse links to up to three related older posts.
5. Generate and upload two distinct, topic-related PNG icons per article using only Python standard-library drawing; no image API is called. Both are compact, centered inline figures (`max-width: 360px`, responsive width) so they remain aligned and readable on mobile.
6. Run a Terra editorial review followed by an independent Luna fact review, with intent-specific weights (news, evergreen, and comparison), a fact-accuracy floor, hard HTML/source/disclosure/noindex gates, and the three-day duplicate check. `85+` is the starting soft auto-publish target; `78–84` receives one targeted correction, while lower scores remain held. `MAX_REVISIONS` (default 4) bounds retries without overriding a hard gate.
7. Append article metadata, claim expirations, source usage, content/prompt/engine/strategy versions, control/optimized experiment assignment, cost tokens, and empty 24h/72h/7d/28d metric windows to `data/article_history.json`. Redacted article snapshots in `data/article_versions.json` support a human-invoked rollback; no credentials are stored.
8. Run `daily_report.py` each morning to read publication status, WordPress.com stats, optional Search Console query/page/country/device data, and update the history windows. The report labels 24h/72h/7d/28d as Early/Preliminary/Main/Long-term signals, includes sample-size-aware diagnoses, and never treats missing GSC data as zero.
9. Run `seo_health.py` before publishing to check HTTP 200, canonical/noindex, robots, sitemap, image/link integrity, and structured-data presence. A fresh critical snapshot pauses publishing until a human resumes it; the code never self-edits authentication or core workflows.

The first version intentionally has no affiliate links and no personalized investment recommendations. The `--seed-pages` option creates About, Privacy, Contact, and Editorial Policy pages for the selected locale.

Research uses `gpt-5.6-luna` with medium reasoning; article writing and review use
`gpt-5.6-terra` with medium reasoning. These are configured in the workflows and
can be overridden locally with `OPENAI_RESEARCH_MODEL`,
`OPENAI_RESEARCH_REASONING`, `OPENAI_WRITING_MODEL`, and
`OPENAI_WRITING_REASONING`.

## GitHub-only production path

The production path is GitHub Actions, so the user's computer does not need to
be powered on. The schedules are deliberately separated:

- `publish.yml` keeps the legacy three-site publication slots at 08:00, 11:50,
  16:00, and 20:00 KST. It no longer runs the new sites.
- `new-site-issue-cycle.yml` scans Google Trends/News every 30 minutes for the
  three new sites, records the candidate and evidence snapshot, then runs the
  full research → writing → Terra/Luna review → quality gate → publish/update
  path only when the opportunity score is at least 85. A per-site daily cap of
  two operations prevents mass publication; scans continue after the cap.
- `daily-report.yml` runs at 06:00 KST and reports both site groups. `weekly-
  strategy.yml` runs Sunday 22:00 KST and commits only
  `data/weekly_strategy.json`.

The new cycle and legacy publication share one concurrency group so history
files cannot be pushed concurrently. A failure in one locale is recorded and
does not stop the other locales from being scanned.

The workflows deliberately contain no credentials. Put the API and
WordPress.com values in GitHub Actions Secrets after the repository is created.
The starting quality target is 85 points with up to four bounded revisions, and
the publish status is explicit (`publish`) in the workflow. A three-failure
kill switch (three consecutive or three of the last five failed runs) writes
`data/publish_control.json`; set `PUBLISH_FORCE_RESUME=1`
only after reviewing the reported cause.

GitHub Actions itself can be free on a public repository, but OpenAI API calls
still use the API account's separate credits/billing. A zero-hosting-cost
deployment therefore does not mean zero AI API usage cost.

## Run

```powershell
Copy-Item .env.example .env
# Fill the values in .env using a local secret manager or environment variables.
python engine.py --locale us --topic "How to compare total costs of two broad-market ETFs"
python engine.py --locale jp --topic "Compare NISA fund fees" --seed-pages
python daily_report.py
# After reviewing a stored snapshot, restore it explicitly (never automatic):
python engine.py --locale us --rollback-post-id 123 --rollback-version abcdef0123456789
```

The script uses only the Python standard library. Its trend call reads the
keyless Google News RSS feed, its research call enables the Responses API
web-search tool, then it calls `POST /v1/responses` for writing/review and the
WordPress.com `public-api.wordpress.com/wp/v2/sites/<site>/...` endpoints for
posts/pages. Self-hosted WordPress remains supported with
`WP_*_MODE=self_hosted`.

`daily_report.py` uses the WordPress.com stats endpoints in read-only mode. Site-level visitors and views are reported for the last completed local day; the per-post endpoint exposes views rather than unique visitors, so the report labels those values as cumulative post views. If a site has stats disabled, the report shows “확인 불가” instead of treating missing data as zero.

The generated PNG source is 1200×675 (16:9) and is displayed as a compact,
responsive 360px inline icon so mobile pages remain readable. A theme/SEO
integration should expose `max-image-preview:large` and Article/BreadcrumbList/
Organization structured data; the health monitor reports their presence but does
not inject unsafe scripts into post bodies.

## Only user-owned prerequisites

- A WordPress.com application with Client ID and Client Secret, created through the [Applications Manager](https://developer.wordpress.com/apps/).
- A WordPress.com Application Password (recommended when 2FA is enabled), or a short-lived WordPress.com access token. Store secrets locally and do not paste them into chat.
- An OpenAI API key and API billing/credits. ChatGPT subscription billing and API billing are separate.
- Optional: Search Console property ownership and Analytics measurement ID after each domain exists.
- Optional: a read-only Google Search Console bearer token in `GSC_TOKEN` or a locale-specific `GSC_US_TOKEN`, `GSC_JP_TOKEN`, or `GSC_KR_TOKEN`, plus `GSC_SITE_URL` when the property URL differs from the WordPress URL.

