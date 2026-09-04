# US/JP/KR WordPress finance automation

This is a separate WordPress implementation of the existing US/Japan plan. It does not modify or reconnect the old Blogger project. WordPress.com Free sites can use the public REST API; this implementation uses the WordPress.com API proxy and OAuth2 instead of requiring a paid plugin plan.

## Pipeline

1. Read free, locale-specific Google Trending Searches and Google News RSS snapshots and ask the research model to choose the most timely, useful, click-worthy topic. Topics can be finance or another current-interest area; recent WordPress posts are supplied to avoid repetition.
2. Use one web-search-backed research call to benchmark up to five leading result pages, identify coverage gaps, and collect complete primary/authoritative URLs, dates, and a concrete growth plan. The prompt forbids copying or translating a competing article.
3. Generate a native-language article with the writing model, adding SEO title/slug/excerpt, semantic headings, a concise lead, FAQ, internal-link opportunities, balanced typography/layout HTML, update notes, risks, and a general-information notice.
4. Generate and upload three distinct original PNG visuals per article (one featured overview plus comparison and checklist inline placements) using only Python standard-library drawing; no image API is called.
5. Run a separate reviewer prompt scoring accuracy, source quality, freshness, originality, search intent, depth, clarity, HTML/layout, SEO, and safety.
6. Revise weak sections up to `MAX_REVISIONS` (default 4). Do not publish below `QUALITY_THRESHOLD` (default 90).
7. Send the approved article to WordPress REST API as `draft` by default, or `publish` when explicitly enabled.
8. Run `daily_report.py` each morning to read publication status and WordPress.com stats without changing any content.

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
runs at 06:00 KST and commits a read-only Markdown report under `reports/`.

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

