import datetime as dt
import json
import os

import pytest

import gs_tom


class FakePdfPath:
    def read_bytes(self):
        return b"%PDF test"


def test_parse_card_date_and_detail_date_precedence():
    assert gs_tom.parse_card_date("Mar 23, 2026") == "2026-03-23"
    html = """
    <html><head>
      <script>window.__DATA__ = {"publishDate":"2026-03-23T00:00:00"};</script>
    </head></html>
    """
    assert gs_tom.extract_date_from_detail_html(html) == "2026-03-23"


def test_extract_detail_title_summary_and_pdf():
    html = """
    <html>
      <head>
        <meta property="og:title" content="Top of Mind: Data Reliability | Goldman Sachs">
        <meta name="description" content="A concise summary.">
      </head>
      <body>
        <a href="/pdfs/insights/goldman-sachs-research/data/report.pdf">Read the Report</a>
      </body>
    </html>
    """
    assert gs_tom.extract_title_from_detail_html(html) == "Data Reliability"
    assert gs_tom.extract_summary_from_detail_html(html) == "A concise summary."
    assert (
        gs_tom.find_pdf_link_from_detail_html(html, "https://www.goldmansachs.com/insights/top-of-mind/data")
        == "https://www.goldmansachs.com/pdfs/insights/goldman-sachs-research/data/report.pdf"
    )


def test_formatting_and_sanitization():
    subject = gs_tom.format_subject("2026-03-23", "Top of Mind: AI / Markets?")
    filename = gs_tom.format_attachment_filename("2026-03-23", "Top of Mind: AI / Markets?")
    assert subject == "2026-03-23 GS ToM AI / Markets?"
    assert filename == "2026-03-23 GS ToM AI Markets.pdf"


def test_recipient_parsing_and_config_validation():
    assert gs_tom.parse_recipient_list("a@example.com, b@example.com,, ") == [
        "a@example.com",
        "b@example.com",
    ]
    config = gs_tom.get_resend_config(
        {
            "RESEND_API_KEY": "re_test",
            "RESEND_FROM": "GS <gs@example.com>",
            "RESEND_TO": "a@example.com,b@example.com",
        }
    )
    assert config.to == ["a@example.com", "b@example.com"]
    with pytest.raises(RuntimeError):
        gs_tom.get_resend_config({})


def test_sao_paulo_fallback_date():
    utc_time = dt.datetime(2026, 4, 29, 1, 30, tzinfo=dt.timezone.utc)
    assert gs_tom.sao_paulo_today(utc_time) == "2026-04-28"


def test_hub_candidate_from_raw():
    candidate = gs_tom.candidate_from_hub_raw(
        "https://www.goldmansachs.com/insights/top-of-mind/sample-report",
        "Sample report title",
        "Top of Mind\nMar 23, 2026\nSample report title",
    )
    assert candidate is not None
    assert candidate.page_url == "https://www.goldmansachs.com/insights/top-of-mind/sample-report"
    assert candidate.hub_title == "Sample report title"
    assert candidate.hub_date == "2026-03-23"


def test_processed_and_sorting_state_behavior():
    old = gs_tom.ReportCandidate("https://example.com/old", date="2026-01-01")
    new = gs_tom.ReportCandidate("https://example.com/new", date="2026-02-01")
    assert sorted([new, old], key=gs_tom.sort_key_oldest) == [old, new]
    state = {old.page_url: {"status": "skipped-migration"}}
    assert gs_tom.processed(state, old)
    assert not gs_tom.processed(state, new)

    selected, skipped = gs_tom.select_migration_candidates([new, old])
    assert selected == new
    assert skipped == [old]


def test_latest_deepseek_pro_model_selection():
    models = [
        "deepseek-v4-pro",
        "deepseek-v4.1-flash",
        "deepseek-v4.2-pro",
        "deepseek-v4.3-pro-vision",
    ]
    assert gs_tom.latest_deepseek_pro_model(models) == "deepseek-v4.2-pro"


def test_opencode_summary_uses_selected_model_and_stable_session(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "Resumo"}, "finish_reason": "stop"}]}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(gs_tom.requests, "post", post)
    candidate = gs_tom.ReportCandidate("https://example.com/report", title="Report", date="2026-01-01")
    config = gs_tom.OpenCodeConfig("key", "https://example.com/v1", "latest-deepseek-pro")

    assert gs_tom.summarize_report(candidate, "x" * 500, config, "deepseek-v4.2-pro") == "Resumo"
    assert calls[0][1]["json"]["model"] == "deepseek-v4.2-pro"
    prompt = calls[0][1]["json"]["messages"][0]["content"]
    assert "250 a 350 palavras" in prompt
    assert len(calls[0][1]["headers"]["x-opencode-session"]) == 64


def test_error_notification_is_sanitized_and_sent_once(tmp_path):
    sent = []
    env = {
        "TELEGRAM_BOT_TOKEN": "telegram-secret",
        "TELEGRAM_ERROR_CHAT_ID": "-1001",
        "OPENCODE_API_KEY": "opencode-secret",
    }

    def sender(_token, _chat_id, message):
        sent.append(message)
        return "42"

    state_path = tmp_path / "state.json"
    error = "OpenCode rejected opencode-secret and telegram-secret"
    assert gs_tom.notify_error_once(state_path, error, env=env, sender=sender)
    assert not gs_tom.notify_error_once(state_path, error, env=env, sender=sender)
    assert len(sent) == 1
    assert "secret" not in sent[0]


def test_summary_markdown_renders_as_safe_formatted_html():
    candidate = gs_tom.ReportCandidate(
        page_url="https://www.goldmansachs.com/insights/top-of-mind/sample",
        title="Sample Report",
        date="2026-03-23",
        ai_summary="## Tese central\n\nTexto <script>alert(1)</script>.\n\n## Riscos\n\n- Risco um\n- Risco dois",
        summary_model="deepseek-v4-pro",
    )

    output = gs_tom.build_summary_html(candidate, "GS ToM Sample")

    assert "<h2>Tese central</h2>" in output
    assert "<p>Texto &lt;script&gt;alert(1)&lt;/script&gt;.</p>" in output
    assert "<li>Risco um</li>" in output
    assert "## Tese central" not in output
    assert "max-width: 720px" in output
    assert "font-family: -apple-system, sans-serif" in output


def test_resend_payload_with_attachment():
    candidate = gs_tom.ReportCandidate(
        page_url="https://www.goldmansachs.com/insights/top-of-mind/sample",
        title="Sample",
        date="2026-03-23",
        summary="Summary",
        ai_summary="## Tese\n\nAção < risco.",
        summary_model="deepseek-v4.2-pro",
        pdf_url="https://www.goldmansachs.com/pdfs/sample.pdf",
    )
    config = gs_tom.ResendConfig("re_test", "GS <gs@example.com>", ["to@example.com"])
    params = gs_tom.build_resend_params(config, candidate, FakePdfPath(), "2026-03-23 GS ToM Sample.pdf", True)
    assert params["subject"] == "2026-03-23 GS ToM Sample"
    assert params["to"] == ["to@example.com"]
    assert params["attachments"][0]["filename"] == "2026-03-23 GS ToM Sample.pdf"
    assert params["attachments"][0]["content"].startswith("JVBER")
    assert "deepseek-v4.2-pro" in params["html"]
    assert "Ação &lt; risco." in params["html"]


def test_telegram_album_contains_html_summary_and_pdf(tmp_path, monkeypatch):
    summary_path = tmp_path / "summary.html"
    pdf_path = tmp_path / "report.pdf"
    summary_path.write_text("<html>summary</html>", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.7")
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ok": True,
                "result": [
                    {"message_id": 10, "media_group_id": "album-1"},
                    {"message_id": 11, "media_group_id": "album-1"},
                ],
            }

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(gs_tom.requests, "post", post)
    config = gs_tom.TelegramConfig("token", "chat")
    message_ids, media_group_id = gs_tom.send_telegram_album(
        config, summary_path, pdf_path, "Report — 2026-01-01"
    )

    assert message_ids == ["10", "11"]
    assert media_group_id == "album-1"
    assert calls[0][0].endswith("/bottoken/sendMediaGroup")
    media = json.loads(calls[0][1]["data"]["media"])
    assert [item["media"] for item in media] == ["attach://summary", "attach://report"]
    assert set(calls[0][1]["files"]) == {"summary", "report"}


def test_prepares_html_summary_artifact_before_delivery(tmp_path, monkeypatch):
    candidate = gs_tom.ReportCandidate(
        page_url="https://www.goldmansachs.com/insights/top-of-mind/sample",
        title="Sample",
        date="2026-03-23",
        pdf_url="https://www.goldmansachs.com/pdfs/sample.pdf",
    )
    state = {}
    state_path = tmp_path / "state.json"

    def download(_url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7")
        return path.stat().st_size

    monkeypatch.setattr(gs_tom, "download_pdf", download)
    monkeypatch.setattr(gs_tom, "extract_pdf_text", lambda _path: "report text")
    monkeypatch.setattr(
        gs_tom, "summarize_report", lambda *_args: "## Tese central\n\nResumo compacto"
    )

    pdf_path, summary_path, _size = gs_tom.prepare_candidate(
        candidate,
        tmp_path,
        state,
        state_path,
        gs_tom.OpenCodeConfig("key", "https://example.com/v1", "model"),
        "deepseek-v4.2-pro",
    )

    assert pdf_path.is_file()
    assert summary_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "<h2>Tese central</h2>" in summary_path.read_text(encoding="utf-8")
    assert state[candidate.page_url]["status"] == "prepared"

    summary_path.write_text("old renderer", encoding="utf-8")
    gs_tom.prepare_candidate(
        candidate,
        tmp_path,
        state,
        state_path,
        gs_tom.OpenCodeConfig("key", "https://example.com/v1", "model"),
        "deepseek-v4.2-pro",
    )
    assert "<h2>Tese central</h2>" in summary_path.read_text(encoding="utf-8")


def test_partial_delivery_retry_does_not_repeat_email(tmp_path):
    candidate = gs_tom.ReportCandidate(
        page_url="https://www.goldmansachs.com/insights/top-of-mind/sample",
        title="Sample",
        date="2026-03-23",
        ai_summary="Resumo",
        summary_model="deepseek-v4-pro",
        pdf_url="https://www.goldmansachs.com/pdfs/sample.pdf",
    )
    pdf_path = tmp_path / "report.pdf"
    summary_path = tmp_path / "summary.html"
    pdf_path.write_bytes(b"%PDF-1.7")
    summary_path.write_text("<html>Resumo</html>", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = {
        candidate.page_url: {
            "status": "prepared",
            "file": str(pdf_path),
            "summary_file": str(summary_path),
            "attachment_sent": True,
        }
    }
    email_calls = []
    telegram_calls = []

    def email_sender(_params, _key):
        email_calls.append(True)
        return "email-1"

    def telegram_sender(*_args):
        telegram_calls.append(True)
        if len(telegram_calls) == 1:
            raise RuntimeError("Telegram unavailable")
        return ["10", "11"], "album-1"

    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        gs_tom.deliver_prepared_candidate(
            candidate,
            pdf_path,
            summary_path,
            state,
            state_path,
            gs_tom.ResendConfig("key", "from@example.com", ["to@example.com"]),
            gs_tom.TelegramConfig("token", "chat"),
            email_sender=email_sender,
            telegram_sender=telegram_sender,
        )

    gs_tom.deliver_prepared_candidate(
        candidate,
        pdf_path,
        summary_path,
        state,
        state_path,
        gs_tom.ResendConfig("key", "from@example.com", ["to@example.com"]),
        gs_tom.TelegramConfig("token", "chat"),
        email_sender=email_sender,
        telegram_sender=telegram_sender,
    )

    assert len(email_calls) == 1
    assert len(telegram_calls) == 2
    assert state[candidate.page_url]["status"] == "ok"
    assert state[candidate.page_url]["telegram_message_ids"] == ["10", "11"]


def test_resend_payload_link_only():
    candidate = gs_tom.ReportCandidate(
        page_url="https://www.goldmansachs.com/insights/top-of-mind/sample",
        title="Sample",
        date="2026-03-23",
        pdf_url="https://www.goldmansachs.com/pdfs/sample.pdf",
    )
    config = gs_tom.ResendConfig("re_test", "GS <gs@example.com>", ["to@example.com"])
    params = gs_tom.build_resend_params(config, candidate, FakePdfPath(), "unused.pdf", False)
    assert "attachments" not in params
    assert "larger than the attachment limit" in params["html"]
    assert "Direct PDF link" in params["html"]


@pytest.mark.skipif(os.environ.get("RUN_LIVE_GS_TESTS") != "1", reason="live GS Playwright tests disabled")
def test_live_playwright_extracts_hub_cards_and_detail_metadata():
    pytest.importorskip("playwright.sync_api")
    gs_tom.require_playwright()
    fallback_date = gs_tom.sao_paulo_today()
    with gs_tom.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=gs_tom.HEADERS["User-Agent"])
        hub_page = context.new_page()
        try:
            cards = gs_tom.extract_hub_cards_with_page(hub_page)
        finally:
            hub_page.close()
        assert cards
        sampled = [card for card in cards if card.hub_title and card.hub_date][:3]
        assert sampled, "expected at least one card with title and card date"
        for card in sampled:
            assert card.page_url.startswith("https://www.goldmansachs.com/insights/top-of-mind/")
            assert card.hub_title
            assert card.hub_date

        detail_page = context.new_page()
        try:
            detail = gs_tom.extract_detail_with_page(detail_page, sampled[0], fallback_date)
        finally:
            detail_page.close()
            browser.close()

    assert detail.display_title
    assert detail.date
    assert detail.date_source == "detail"
    assert detail.pdf_url.startswith("https://")
    assert gs_tom.is_allowed_host(detail.pdf_url)
