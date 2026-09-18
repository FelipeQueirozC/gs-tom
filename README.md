# GS Top of Mind

Daily Goldman Sachs Top of Mind collector for the OptiPlex.

The collector renders Goldman Sachs pages with Playwright. It downloads the newest PDF and extracts text with `pdftotext`. OpenCode creates a 250–350-word Brazilian-Portuguese investor summary. Resend sends the summary and PDF by email. Telegram receives one album containing the summary HTML and full PDF.

## Model selection

The default `latest-deepseek-pro` setting queries the OpenCode `/models` endpoint before each new summary. The collector selects the highest versioned non-vision DeepSeek Pro model. It uses `deepseek-v4-pro` only when model discovery fails.

Set `OPENCODE_SUMMARIZER_MODEL` to a model ID only when a fixed override is necessary.

## OptiPlex deployment

Install system packages and Python dependencies:

```bash
sudo apt-get install -y python3-venv poppler-utils
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Copy `.env.example` to `.env`. Shared secrets come from `/etc/collector-env/common.env`.

Required project values:

```text
RESEND_FROM
RESEND_TO
```

Install the systemd units:

```bash
sudo sh deploy/install.sh
```

The timer runs daily at 12:30 in `America/Sao_Paulo`. Runtime state stays in `var/gs_top_of_mind_state.json`.

For the first OptiPlex run, use controlled catch-up:

```bash
.venv/bin/python gs_tom.py --migration-catch-up
```

This command sends only the newest pending report. It marks older pending reports as `skipped-migration` after successful delivery.

## Configuration

Shared secrets:

- `RESEND_API_KEY`
- `OPENCODE_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_DELIVERY_CHAT_ID`
- `TELEGRAM_ERROR_CHAT_ID`

Optional values:

- `OPENCODE_BASE_URL`
- `OPENCODE_SUMMARIZER_MODEL`

Failures send one deduplicated and sanitized Telegram notification to the error chat. Delivery state is saved after each provider call. A retry sends only a missing email or Telegram album.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
RUN_LIVE_GS_TESTS=1 pytest
```

GitHub Actions has manual execution only. Do not enable its schedule while the OptiPlex timer is active.
