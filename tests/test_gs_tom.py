import datetime as dt
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
    state = {old.page_url: {"status": "skipped-bootstrap"}}
    assert gs_tom.processed(state, old)
    assert not gs_tom.processed(state, new)


def test_resend_payload_with_attachment():
    candidate = gs_tom.ReportCandidate(
        page_url="https://www.goldmansachs.com/insights/top-of-mind/sample",
        title="Sample",
        date="2026-03-23",
        summary="Summary",
        pdf_url="https://www.goldmansachs.com/pdfs/sample.pdf",
    )
    config = gs_tom.ResendConfig("re_test", "GS <gs@example.com>", ["to@example.com"])
    params = gs_tom.build_resend_params(config, candidate, FakePdfPath(), "2026-03-23 GS ToM Sample.pdf", True)
    assert params["subject"] == "2026-03-23 GS ToM Sample"
    assert params["to"] == ["to@example.com"]
    assert params["attachments"][0]["filename"] == "2026-03-23 GS ToM Sample.pdf"
    assert params["attachments"][0]["content"].startswith("JVBER")


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
