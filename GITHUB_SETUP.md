# GitHub Actions setup

This project is designed to run from a GitHub **public** repository so
standard GitHub-hosted runners remain free. The repository contains no
credentials. Keep all values below in **Settings → Secrets and variables →
Actions → New repository secret**.

Required secrets:

- `OPENAI_API_KEY`
- `WP_US_URL`, `WP_US_SITE_REF`, `WP_US_USERNAME`, `WP_US_APPLICATION_PASSWORD`, `WP_US_CLIENT_ID`, `WP_US_CLIENT_SECRET` (or `WP_US_ACCESS_TOKEN`)
- `WP_JP_URL`, `WP_JP_SITE_REF`
- `WP_KR_URL`, `WP_KR_SITE_REF`

Optional Search Console secrets (enable query/page/country/device metrics):

- `GSC_TOKEN` or locale-specific `GSC_US_TOKEN`, `GSC_JP_TOKEN`, `GSC_KR_TOKEN`
- `GSC_SITE_URL` when the Search Console property URL differs from the site URL

The workflow maps the shared WordPress.com credentials from the US values to
Japan and Korea. If the WordPress.com account uses a bearer access token,
store it as `WP_US_ACCESS_TOKEN` and leave the client/password secrets empty.
Never commit `.env`, access tokens, application passwords, or OpenAI keys.

## Schedule

`publish.yml` runs only the existing (legacy) three locales at 08:00,
11:50, 16:00, and 20:00 KST, unchanged. The new sites are intentionally
removed from that workflow. `new-site-issue-cycle.yml` runs every 30 minutes
and, for each new site, collects Google Trends/News signals, scores and
researches the best issue, then runs writing, independent review, quality gates,
and publish/update when the score is at least 85. It records every scan and
stops publication after two operations per site per local day while continuing
to scan. `daily-report.yml` writes a read-only report to `reports/` at 06:00
KST and updates history. `seo-health.yml` records read-only technical SEO
checks. `weekly-strategy.yml` runs Sunday 22:00 KST and stores confidence-aware
recommendations in `data/weekly_strategy.json`. All workflows support manual
`workflow_dispatch` runs.

The starting quality target is 85/100 with at most four bounded revisions. The
article pipeline reads free locale-specific Google Trending Searches/News RSS
snapshots, targets up to five current search results without making five a hard
minimum, then uses `gpt-5.6-luna` (medium reasoning) for research/fact review
and `gpt-5.6-terra` (medium reasoning) for writing/editorial review. OpenAI API usage is
billed separately from GitHub Actions. Each approved post receives two
distinct topic-related standard-library-generated PNG icons as compact,
centered inline figures; no image-generation API key is needed. Topics are
scored for realistic click opportunity, exclude overlapping posts from the last
three days, may update an older URL when the intent is already owned, and record
confidence-aware publish-slot performance for the weekly strategy file. Hard
truth/technical gates can hold any score, and a three-failure (consecutive or
within five runs) kill switch pauses publishing without changing code.

