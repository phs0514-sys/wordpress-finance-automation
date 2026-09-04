# US/JP/KR WordPress finance automation

This is a separate WordPress implementation of the existing US/Japan plan. It does not modify or reconnect the old Blogger project. WordPress.com Free sites can use the public REST API; this implementation uses the WordPress.com API proxy and OAuth2 instead of requiring a paid plugin plan.

## Pipeline

1. Read 30–50 locale-specific Google Trending Searches and Google News RSS candidates, then score each opportunity by interest, velocity, search intent, SERP feasibility, title CTR potential, durability, and site fit rather than raw popularity alone. A weekly strategy file supplies learned category and publish-slot weights while preserving a 70/20/10 proven/adjacent/experiment mix.
2. Use a web-search-backed research call to benchmark up to five leading result pages, official sources, and recent sources. The brief records common coverage, optional coverage, outdated claims, disagreements to verify, and at least two concrete original-value additions absent from the benchmark pages.
3. Enforce a three-day hard exclusion for overlapping events, entities, primary keywords, and search intent. When an older URL already owns the intent and needs fresh facts, the research action can be `update`; otherwise the engine creates a new URL. A final pre-publish guard blocks near-duplicate titles.
4. Generate a native-language article with one of six layout branches (news, comparison, howto, timeline, explainer, checklist), SEO fields, FAQ, contextual internal links, update notes, risks, and a general-information notice. New articles add reverse links to up to three related older posts.
5. Generate and upload two distinct, topic-related PNG icons per article using only Python standard-library drawing; no image API is called. Both are compact, centered inline figures (`max-width: 360px`, responsive width) so they remain aligned and readable on mobile.
6. Run a separate Luna/Terra reviewer prompt with the 20/20/15/10/10/10/10/5 score breakdown, originality-count gate (at least two additions), fact/SEO/layout checks, and the three-day duplicate check. Revise weak sections up to `MAX_REVISIONS` (default 4); do not publish below `QUALITY_THRESHOLD` (default 90).
7. Append article metadata and empty 24h/72h/7d/28d metric windows to `data/article_history.json`. The file is committed by GitHub Actions; it contains no credentials and is the input to the weekly strategy engine.
8. Run `daily_report.py` each morning to read publication status, WordPress.com stats, optional Search Console query/page/country/device data, and update the history windows. If GSC is not connected, the report says “GSC 토큰 미설정” rather than treating missing data as zero.

The first version intentionally has no affiliate links and no personalized investment recommendations. The `--seed-pages` option creates About, Privacy, Contact, and Editorial Policy pages for the selected locale.

Research uses `gpt-5.6-luna` with medium reasoning; article writing and review use
`gpt-5.6-terra` with medium reasoning. These are configured in the workflows and
can be overridden locally with `OPENAI_RESEARCH_MODEL`,
`OPENAI_RESEARCH_REASONING`, `OPENAI_WRITING_MODEL`, and
`OPENAI_WRITING_REASONING`.

## GitHub-only production path

The production path is GitHub Actions, so the user's computer does not need to
be powered on. `publish.yml` runs one batch at 08:00, 11:50, 16:00, and 20:00
KST. Each batch independently creates, reviews, and publishes one article for
the US, Japan, and Korea sites (12 posts per day total). `daily-report.yml`
runs at 06:00 KST, stores Search Console windows when configured, and commits
the Markdown report plus `data/article_history.json`. `weekly-strategy.yml`
runs Sunday 22:00 KST and commits only `data/weekly_strategy.json`; it never
edits authentication, WordPress connections, data collection, or engine code.

The workflows deliberately contain no credentials. Put the API and
WordPress.com values in GitHub Actions Secrets after the repository is created.
The current quality gate remains 90 points with up to four revisions, and the
publish status is explicit (`publish`) in the workflow rather than hidden in a
local file.

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
```

The script uses only the Python standard library. Its trend call reads the
keyless Google News RSS feed, its research call enables the Responses API
web-search tool, then it calls `POST /v1/responses` for writing/review and the
WordPress.com `public-api.wordpress.com/wp/v2/sites/<site>/...` endpoints for
posts/pages. Self-hosted WordPress remains supported with
`WP_*_MODE=self_hosted`.

`daily_report.py` uses the WordPress.com stats endpoints in read-only mode. Site-level visitors and views are reported for the last completed local day; the per-post endpoint exposes views rather than unique visitors, so the report labels those values as cumulative post views. If a site has stats disabled, the report shows “확인 불가” instead of treating missing data as zero.

## Only user-owned prerequisites

- A WordPress.com application with Client ID and Client Secret, created through the [Applications Manager](https://developer.wordpress.com/apps/).
- A WordPress.com Application Password (recommended when 2FA is enabled), or a short-lived WordPress.com access token. Store secrets locally and do not paste them into chat.
- An OpenAI API key and API billing/credits. ChatGPT subscription billing and API billing are separate.
- Optional: Search Console property ownership and Analytics measurement ID after each domain exists.
- Optional: a read-only Google Search Console bearer token in `GSC_TOKEN` or a locale-specific `GSC_US_TOKEN`, `GSC_JP_TOKEN`, or `GSC_KR_TOKEN`, plus `GSC_SITE_URL` when the property URL differs from the WordPress URL.

