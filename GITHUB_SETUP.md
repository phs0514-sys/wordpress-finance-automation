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

The workflow maps the shared WordPress.com credentials from the US values to
Japan and Korea. If the WordPress.com account uses a bearer access token,
store it as `WP_US_ACCESS_TOKEN` and leave the client/password secrets empty.
Never commit `.env`, access tokens, application passwords, or OpenAI keys.

## Schedule

`publish.yml` runs all three locales at 08:00, 11:50, 16:00, and 20:00 KST.
`daily-report.yml` writes a read-only report to `reports/` at 06:00 KST.
Both workflows also support a manual `workflow_dispatch` run.

The quality gate is 90/100 with at most four revisions. The article pipeline
reads a free locale-specific Google News RSS snapshot, benchmarks up to five
current search results, then uses `gpt-5.6-luna` (medium reasoning) for research
and `gpt-5.6-terra` (medium reasoning) for writing/review. OpenAI API usage is
billed separately from GitHub Actions. Each approved post receives three
standard-library-generated PNG visuals; no image-generation API key is needed.

