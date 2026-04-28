# GS Top of Mind Downloader

Daily Goldman Sachs Top of Mind notifier for GitHub Actions. The script renders the
Goldman Sachs hub and detail pages with Playwright, downloads the PDF, and sends an
email with Resend.

## GitHub Actions setup

Create these repository secrets:

- `RESEND_API_KEY`
- `RESEND_FROM`
- `RESEND_TO`

`RESEND_TO` accepts one or more comma-separated recipients. The workflow uses
`GITHUB_TOKEN` with `contents: write` to commit `gs_top_of_mind_state.json`, so
repository Actions permissions must allow write access.

The first run starts from an empty state file. It emails only the newest discovered
report, then records older reports as `skipped-bootstrap` so they are not sent later.
After that, daily runs email all newly discovered reports.

The scheduled workflow runs every day at `15:00 UTC`. You can also start it manually
from the Actions tab.

## Local run

Install dependencies and Chromium:

```bash
pip install -r requirements.txt
playwright install chromium
```

Set the same Resend environment variables used by GitHub Actions, then run:

```bash
python gs_tom.py
```

PDFs are staged in the project-local `output/` folder, which is intentionally ignored
by git.

## Tests

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run unit tests:

```bash
pytest
```

Optional live Playwright checks are committed but disabled by default. They verify the
current Goldman Sachs site can expose hub cards, card dates, detail titles, detail
dates, and PDF links:

```bash
RUN_LIVE_GS_TESTS=1 pytest
```
