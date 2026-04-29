#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Goldman Sachs Top of Mind email notifier."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - exercised when runtime deps are missing
    PlaywrightTimeoutError = Exception
    sync_playwright = None


HUB_URL = "https://www.goldmansachs.com/insights/top-of-mind"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DEST = SCRIPT_DIR / "output"
STATE_FILE = SCRIPT_DIR / "gs_top_of_mind_state.json"
SAO_PAULO_TZ = "America/Sao_Paulo"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.7",
    "Referer": "https://www.goldmansachs.com/",
}

PLAYWRIGHT_TIMEOUT_MS = 30_000
PLAYWRIGHT_RETRIES = 2
HTTP_TIMEOUT = 45
HTTP_RETRIES = 3
SLEEP_BETWEEN = 1.0
ATTACHMENT_RAW_LIMIT_BYTES = 30_000_000
PDF_HOST_ALLOW = ("goldmansachs.com",)

MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)
CARD_DATE_RE = re.compile(rf"\b({MONTH_PATTERN})\s+\d{{1,2}},\s+\d{{4}}\b", re.I)
ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
DETAIL_DATE_RE = re.compile(
    r'"(?:publishDate|datePublished)"\s*:\s*"([^"]+)"',
    re.I,
)


@dataclass
class ReportCandidate:
    page_url: str
    hub_title: str = ""
    hub_date: str = ""
    title: str = ""
    date: str = ""
    summary: str = ""
    pdf_url: str = ""
    title_source: str = ""
    date_source: str = ""
    detail_error: str = ""

    @property
    def display_title(self) -> str:
        return clean_title(self.title or self.hub_title or "Top of Mind")

    @property
    def effective_date(self) -> str:
        return self.date or self.hub_date


@dataclass
class ResendConfig:
    api_key: str
    from_email: str
    to: List[str]


@dataclass
class ProcessResult:
    emailed: int = 0
    skipped: int = 0
    failures: List[str] | None = None

    @property
    def failed(self) -> bool:
        return bool(self.failures)


def log_setup(verbosity: int) -> None:
    level = logging.INFO if verbosity == 0 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_state(path: Path) -> Dict[str, Dict[str, Any]]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logging.warning("State file is invalid JSON; starting with empty state.")
            return {}
    return {}


def save_state(path: Path, state: Dict[str, Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def parse_recipient_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def get_resend_config(env: Optional[Dict[str, str]] = None) -> ResendConfig:
    env = env or os.environ
    api_key = (env.get("RESEND_API_KEY") or "").strip()
    from_email = (env.get("RESEND_FROM") or "").strip()
    to = parse_recipient_list(env.get("RESEND_TO") or "")
    missing = []
    if not api_key:
        missing.append("RESEND_API_KEY")
    if not from_email:
        missing.append("RESEND_FROM")
    if not to:
        missing.append("RESEND_TO")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return ResendConfig(api_key=api_key, from_email=from_email, to=to)


def sao_paulo_today(now: Optional[dt.datetime] = None) -> str:
    tz = ZoneInfo(SAO_PAULO_TZ)
    now = now or dt.datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(tz).date().isoformat()


def parse_date_iso(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    match = ISO_DATE_RE.search(value)
    if match:
        try:
            return dt.date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            return None
    return None


def parse_card_date(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    match = CARD_DATE_RE.search(value)
    if not match:
        return None
    raw = match.group(0)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Sept %d, %Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    if raw.lower().startswith("sept"):
        try:
            normalized = "Sep" + raw[4:]
            return dt.datetime.strptime(normalized, "%b %d, %Y").date().isoformat()
        except ValueError:
            return None
    return None


def clean_title(title: str) -> str:
    title = html.unescape(title or "")
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"\s*[\-|]\s*Goldman Sachs\s*$", "", title, flags=re.I).strip()
    title = re.sub(r"^Top\s+of\s+Mind\s*:\s*", "", title, flags=re.I).strip()
    return title or "Top of Mind"


def compact_for_log(value: str, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def log_value(value: str, limit: int = 180) -> str:
    return compact_for_log(value, limit) or "<missing>"


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180].rstrip(". ")


def format_subject(date_iso: str, title: str) -> str:
    return f"{date_iso} GS ToM {clean_title(title)}".strip()


def format_attachment_filename(date_iso: str, title: str) -> str:
    return f"{sanitize_filename(format_subject(date_iso, title))}.pdf"


def normalize_report_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def is_allowed_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in PDF_HOST_ALLOW)


def is_report_url(url: str) -> bool:
    normalized = normalize_report_url(url)
    return (
        "/insights/top-of-mind/" in normalized
        and normalized.lower() != HUB_URL.lower().rstrip("/")
    )


def iter_jsonish_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_jsonish_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_jsonish_values(child)


def extract_date_from_detail_html(html_text: str) -> Optional[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for obj in iter_jsonish_values(data):
            if not isinstance(obj, dict):
                continue
            for key in ("publishDate", "datePublished"):
                iso = parse_date_iso(str(obj.get(key, "")))
                if iso:
                    return iso

    for match in DETAIL_DATE_RE.finditer(html_text):
        iso = parse_date_iso(match.group(1))
        if iso:
            return iso
    return None


def extract_title_from_detail_html(html_text: str) -> Optional[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    for attrs in (
        {"property": "og:title"},
        {"name": "twitter:title"},
        {"name": "title"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return clean_title(str(tag["content"]))
    if soup.title and soup.title.string:
        return clean_title(soup.title.string)
    h1 = soup.find("h1")
    if h1:
        return clean_title(h1.get_text(" ", strip=True))
    return None


def extract_summary_from_detail_html(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for attrs in (
        {"name": "description"},
        {"property": "og:description"},
        {"name": "twitter:description"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return re.sub(r"\s+", " ", str(tag["content"])).strip()
    return ""


def find_pdf_link_from_detail_html(html_text: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    anchors: List[Tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"].strip())
        text = anchor.get_text(" ", strip=True).lower()
        if is_allowed_host(href):
            anchors.append((href, text))

    def is_pdfish_href(url: str) -> bool:
        path = urlparse(url).path.lower()
        return path.endswith(".pdf") or "/pdfs/" in path

    for href, text in anchors:
        if ("read the report" in text or "read report" in text) and is_allowed_host(href):
            return href
    for href, _text in anchors:
        if is_pdfish_href(href):
            return href
    return None


def extract_hub_title(anchor_text: str, container_text: str) -> str:
    text = anchor_text.strip() or container_text.strip()
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    skip = {
        "top of mind",
        "read more",
        "read the report",
        "share",
        "subscribe",
    }
    candidates = []
    for line in lines:
        if not line or line.lower() in skip:
            continue
        if parse_card_date(line):
            continue
        if len(line) < 4:
            continue
        candidates.append(line)
    if not candidates:
        return ""
    return clean_title(candidates[0])


def candidate_from_hub_raw(href: str, anchor_text: str, container_text: str) -> Optional[ReportCandidate]:
    full_url = normalize_report_url(urljoin(HUB_URL, href))
    if not is_report_url(full_url):
        return None
    combined = "\n".join([anchor_text or "", container_text or ""])
    hub_date = parse_card_date(combined) or ""
    hub_title = extract_hub_title(anchor_text or "", container_text or "")
    return ReportCandidate(page_url=full_url, hub_title=hub_title, hub_date=hub_date)


def require_playwright() -> Any:
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Install dependencies and run `playwright install chromium`.")
    return sync_playwright


def goto_with_retry(page: Any, url: str, wait_until: str = "domcontentloaded") -> None:
    last_error: Optional[Exception] = None
    for attempt in range(1, PLAYWRIGHT_RETRIES + 1):
        try:
            logging.info("Playwright opening page: %s", url)
            logging.debug("Playwright goto attempt %d/%d: %s", attempt, PLAYWRIGHT_RETRIES, url)
            page.goto(url, wait_until=wait_until, timeout=PLAYWRIGHT_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
                logging.debug("Playwright network idle reached: %s", url)
            except Exception:
                logging.debug("Network idle was not reached for %s; continuing to selector checks.", url)
            logging.info("Playwright loaded page: %s", url)
            return
        except Exception as exc:
            last_error = exc
            logging.warning("Playwright load failed for %s (attempt %d): %s", url, attempt, exc)
            time.sleep(SLEEP_BETWEEN * attempt)
    raise RuntimeError(f"Playwright failed to load {url}: {last_error}")


def extract_hub_cards_with_page(page: Any, hub_url: str = HUB_URL) -> List[ReportCandidate]:
    logging.info("Extracting Top of Mind hub cards from: %s", hub_url)
    goto_with_retry(page, hub_url)
    try:
        page.wait_for_selector('a[href*="/insights/top-of-mind/"]', timeout=PLAYWRIGHT_TIMEOUT_MS)
        logging.info("Located Top of Mind report links on hub page.")
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Timed out waiting for Top of Mind report links on hub page.") from exc

    raw_links = page.eval_on_selector_all(
        'a[href*="/insights/top-of-mind/"]',
        """els => els.map(a => {
            const container = a.closest('article, li, [class*="card"], [class*="Card"], [data-testid], section, div');
            return {
                href: a.href || a.getAttribute('href') || '',
                anchorText: a.innerText || a.textContent || '',
                containerText: container ? (container.innerText || container.textContent || '') : ''
            };
        })""",
    )
    logging.info("Hub page returned %d raw Top of Mind links before dedupe.", len(raw_links))

    candidates_by_url: Dict[str, ReportCandidate] = {}
    for item in raw_links:
        candidate = candidate_from_hub_raw(
            str(item.get("href", "")),
            str(item.get("anchorText", "")),
            str(item.get("containerText", "")),
        )
        if not candidate:
            continue
        existing = candidates_by_url.get(candidate.page_url)
        if not existing:
            candidates_by_url[candidate.page_url] = candidate
            continue
        if not existing.hub_title and candidate.hub_title:
            existing.hub_title = candidate.hub_title
        if not existing.hub_date and candidate.hub_date:
            existing.hub_date = candidate.hub_date

    candidates = sorted(candidates_by_url.values(), key=lambda c: c.page_url)
    if not candidates:
        raise RuntimeError("Playwright discovered zero Top of Mind report cards.")
    logging.info("Discovered %d unique Top of Mind report cards.", len(candidates))
    for index, candidate in enumerate(candidates, start=1):
        logging.info(
            "Hub card %d/%d: title=%s date=%s page=%s",
            index,
            len(candidates),
            log_value(candidate.hub_title),
            log_value(candidate.hub_date),
            candidate.page_url,
        )
    return candidates


def extract_detail_with_page(page: Any, candidate: ReportCandidate, fallback_date: str) -> ReportCandidate:
    logging.info("Extracting report detail page: %s", candidate.page_url)
    goto_with_retry(page, candidate.page_url)
    page.wait_for_selector("body", timeout=PLAYWRIGHT_TIMEOUT_MS)
    logging.info("Located report detail body: %s", candidate.page_url)
    html_text = page.content()
    detail_title = extract_title_from_detail_html(html_text)
    detail_date = extract_date_from_detail_html(html_text)
    title = detail_title or candidate.hub_title
    candidate.title = clean_title(title or candidate.hub_title)
    candidate.date = detail_date or candidate.hub_date or fallback_date
    candidate.title_source = "detail" if detail_title else "hub"
    candidate.date_source = "detail" if detail_date else ("hub" if candidate.hub_date else "fallback")
    candidate.summary = extract_summary_from_detail_html(html_text)
    candidate.pdf_url = find_pdf_link_from_detail_html(html_text, candidate.page_url) or ""
    logging.info(
        "Detail extracted: title=%s title_source=%s date=%s date_source=%s summary=%s pdf=%s",
        log_value(candidate.title),
        candidate.title_source or "<missing>",
        log_value(candidate.date),
        candidate.date_source or "<missing>",
        log_value(candidate.summary, 140),
        log_value(candidate.pdf_url),
    )
    if not candidate.pdf_url:
        raise RuntimeError(f"No Goldman Sachs PDF link found on {candidate.page_url}")
    return candidate


def discover_and_enrich_candidates(fallback_date: str) -> Tuple[List[ReportCandidate], List[str]]:
    require_playwright()
    failures: List[str] = []
    enriched: List[ReportCandidate] = []
    logging.info("Starting Playwright browser for Goldman Sachs discovery.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        hub_page = context.new_page()
        try:
            candidates = extract_hub_cards_with_page(hub_page)
        finally:
            hub_page.close()

        for candidate in candidates:
            detail_page = context.new_page()
            try:
                logging.info(
                    "Inspecting detail page %d/%d: %s",
                    len(enriched) + 1,
                    len(candidates),
                    candidate.page_url,
                )
                enriched.append(extract_detail_with_page(detail_page, candidate, fallback_date))
            except Exception as exc:
                candidate.detail_error = str(exc)
                failures.append(f"{candidate.page_url}: {exc}")
                logging.error("Detail extraction failed for %s: %s", candidate.page_url, exc)
                enriched.append(candidate)
            finally:
                detail_page.close()
        logging.info(
            "Finished Playwright discovery: enriched=%d failures=%d.",
            len(enriched),
            len(failures),
        )
        browser.close()
    return enriched, failures


def http_get_stream(url: str) -> requests.Response:
    last_error: Optional[Exception] = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            logging.info("Downloading URL (attempt %d/%d): %s", attempt, HTTP_RETRIES, url)
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=HTTP_TIMEOUT,
                allow_redirects=True,
                stream=True,
            )
            if 200 <= response.status_code < 400:
                logging.info("Download response OK: status=%d url=%s", response.status_code, response.url or url)
                return response
            last_error = RuntimeError(f"HTTP {response.status_code}")
            logging.warning("HTTP %s for %s (attempt %d)", response.status_code, url, attempt)
        except Exception as exc:
            last_error = exc
            logging.warning("GET error for %s (attempt %d): %s", url, attempt, exc)
        time.sleep(SLEEP_BETWEEN * attempt)
    raise RuntimeError(f"Failed to download {url}: {last_error}")


def download_pdf(pdf_url: str, dest_path: Path) -> int:
    if not is_allowed_host(pdf_url):
        raise RuntimeError(f"Refusing non-Goldman PDF URL: {pdf_url}")

    logging.info("Downloading PDF: %s", pdf_url)
    response = http_get_stream(pdf_url)
    final_url = response.url or pdf_url
    if not is_allowed_host(final_url):
        raise RuntimeError(f"PDF redirect left allowed Goldman hosts: {final_url}")

    content_type = (response.headers.get("content-type") or "").lower()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

    first_chunk = b""
    total = 0
    with open(tmp_path, "wb") as handle:
        for chunk in response.iter_content(chunk_size=1 << 15):
            if not chunk:
                continue
            if not first_chunk:
                first_chunk = chunk[:8]
            handle.write(chunk)
            total += len(chunk)

    pdf_header = first_chunk.startswith(b"%PDF")
    pdf_content_type = "application/pdf" in content_type or "binary/octet-stream" in content_type
    if not total or not (pdf_header or pdf_content_type):
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Downloaded content does not look like a PDF: {pdf_url}")

    tmp_path.replace(dest_path)
    logging.info("Saved PDF: %s (%d bytes)", dest_path, total)
    return total


def build_email_html(candidate: ReportCandidate, subject: str, attachment_sent: bool) -> str:
    title = html.escape(candidate.display_title)
    date_iso = html.escape(candidate.date)
    summary = html.escape(candidate.summary) if candidate.summary else ""
    page_link = html.escape(candidate.page_url, quote=True)
    pdf_link = html.escape(candidate.pdf_url, quote=True)
    note = ""
    if not attachment_sent:
        note = (
            "<p>The PDF was larger than the attachment limit, so this email includes links "
            "instead of the PDF attachment.</p>"
            f'<p><a href="{pdf_link}">Direct PDF link</a></p>'
        )
    summary_html = f"<p>{summary}</p>" if summary else ""
    return f"""
    <html>
      <body>
        <div style="font-family:Segoe UI, Arial, sans-serif; font-size:14px; line-height:1.45;">
          <p><strong>{html.escape(subject)}</strong></p>
          <p>{date_iso} - {title}</p>
          {summary_html}
          <p><a href="{page_link}">Goldman Sachs report page</a></p>
          {note}
        </div>
      </body>
    </html>
    """.strip()


def build_resend_params(
    config: ResendConfig,
    candidate: ReportCandidate,
    pdf_path: Path,
    attachment_filename: str,
    attachment_sent: bool,
) -> Dict[str, Any]:
    subject = format_subject(candidate.date, candidate.display_title)
    params: Dict[str, Any] = {
        "from": config.from_email,
        "to": config.to,
        "subject": subject,
        "html": build_email_html(candidate, subject, attachment_sent),
    }
    if attachment_sent:
        attachment_content = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
        params["attachments"] = [
            {
                "content": attachment_content,
                "filename": attachment_filename,
            }
        ]
    return params


def send_resend_email(params: Dict[str, Any], api_key: str) -> str:
    import resend

    resend.api_key = api_key
    response = resend.Emails.send(params)
    if isinstance(response, dict):
        email_id = str(response.get("id") or "")
    else:
        email_id = str(getattr(response, "id", "") or "")
    if not email_id:
        raise RuntimeError("Resend did not return an email id.")
    return email_id


def state_record(
    candidate: ReportCandidate,
    status: str,
    file_path: Optional[Path] = None,
    email_id: str = "",
    attachment_sent: Optional[bool] = None,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "status": status,
        "title": candidate.display_title,
        "date": candidate.date or candidate.hub_date,
        "page": candidate.page_url,
        "pdf": candidate.pdf_url,
        "summary": candidate.summary,
        "hub_title": candidate.hub_title,
        "hub_date": candidate.hub_date,
        "title_source": candidate.title_source,
        "date_source": candidate.date_source,
        "ts": time.time(),
    }
    if file_path:
        record["file"] = str(file_path)
    if email_id:
        record["email_id"] = email_id
    if attachment_sent is not None:
        record["attachment_sent"] = attachment_sent
    if candidate.detail_error:
        record["detail_error"] = candidate.detail_error
    return record


def sort_key_newest(candidate: ReportCandidate) -> Tuple[str, str]:
    return (candidate.effective_date or "", candidate.page_url)


def sort_key_oldest(candidate: ReportCandidate) -> Tuple[str, str]:
    return (candidate.effective_date or "", candidate.page_url)


def processed(state: Dict[str, Dict[str, Any]], candidate: ReportCandidate) -> bool:
    return candidate.page_url in state and state[candidate.page_url].get("status") in {
        "ok",
        "skipped-bootstrap",
    }


def send_candidate(
    candidate: ReportCandidate,
    dest_dir: Path,
    config: ResendConfig,
) -> Tuple[Path, str, bool, int]:
    if not candidate.pdf_url:
        raise RuntimeError(f"No PDF URL available for {candidate.page_url}")
    if not candidate.date:
        raise RuntimeError(f"No date available for {candidate.page_url}")

    attachment_filename = format_attachment_filename(candidate.date, candidate.display_title)
    pdf_path = dest_dir / attachment_filename
    size = download_pdf(candidate.pdf_url, pdf_path)
    attachment_sent = size <= ATTACHMENT_RAW_LIMIT_BYTES
    params = build_resend_params(config, candidate, pdf_path, attachment_filename, attachment_sent)
    email_id = send_resend_email(params, config.api_key)
    return pdf_path, email_id, attachment_sent, size


def mark_bootstrap_skipped(
    candidates: Sequence[ReportCandidate],
    state: Dict[str, Dict[str, Any]],
    state_path: Path,
) -> int:
    skipped = 0
    for candidate in candidates:
        if candidate.page_url in state:
            continue
        state[candidate.page_url] = state_record(candidate, "skipped-bootstrap")
        skipped += 1
    if skipped:
        save_state(state_path, state)
    return skipped


def process(dest_dir: Path, state_path: Path, state: Dict[str, Dict[str, Any]], config: ResendConfig) -> ProcessResult:
    failures: List[str] = []
    result = ProcessResult(failures=failures)
    fallback_date = sao_paulo_today()
    candidates, detail_failures = discover_and_enrich_candidates(fallback_date)
    if not candidates:
        raise RuntimeError("No Top of Mind report candidates discovered.")

    if not state:
        logging.info("Empty state detected; running bootstrap mode.")
        newest = max(candidates, key=sort_key_newest)
        if newest.detail_error:
            raise RuntimeError(f"Newest report could not be inspected: {newest.detail_error}")
        logging.info("Bootstrap newest report: %s (%s)", newest.display_title, newest.date)
        pdf_path, email_id, attachment_sent, size = send_candidate(newest, dest_dir, config)
        state[newest.page_url] = state_record(newest, "ok", pdf_path, email_id, attachment_sent)
        save_state(state_path, state)
        result.emailed += 1
        logging.info(
            "Sent bootstrap email id=%s attachment=%s size=%d bytes",
            email_id,
            attachment_sent,
            size,
        )
        older = [candidate for candidate in candidates if candidate.page_url != newest.page_url]
        result.skipped = mark_bootstrap_skipped(older, state, state_path)
        logging.info("Bootstrap skipped %d older reports.", result.skipped)
        return result

    new_candidates = [candidate for candidate in candidates if not processed(state, candidate)]
    if not new_candidates:
        logging.info("No new reports found.")
        return result

    detail_failure_urls = {failure.split(":", 1)[0] for failure in detail_failures}
    sendable = []
    for candidate in new_candidates:
        if candidate.page_url in detail_failure_urls or candidate.detail_error:
            failures.append(f"{candidate.page_url}: {candidate.detail_error or 'detail extraction failed'}")
        else:
            sendable.append(candidate)

    for candidate in sorted(sendable, key=sort_key_oldest):
        try:
            logging.info("Sending report: %s (%s)", candidate.display_title, candidate.date)
            pdf_path, email_id, attachment_sent, size = send_candidate(candidate, dest_dir, config)
            state[candidate.page_url] = state_record(candidate, "ok", pdf_path, email_id, attachment_sent)
            save_state(state_path, state)
            result.emailed += 1
            logging.info("Sent email id=%s attachment=%s size=%d bytes", email_id, attachment_sent, size)
        except Exception as exc:
            failures.append(f"{candidate.page_url}: {exc}")
            logging.error("Failed to process %s: %s", candidate.page_url, exc)

    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GS Top of Mind Resend notifier")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="Destination folder for staged PDFs")
    parser.add_argument("--state", default=str(STATE_FILE), help="Path to state JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    log_setup(verbosity=1 if args.verbose else 0)
    dest_dir = Path(args.dest).expanduser().resolve()
    state_path = Path(args.state).expanduser().resolve()

    try:
        config = get_resend_config()
        state = load_state(state_path)
        logging.info("Hub: %s", HUB_URL)
        logging.info("Dest: %s", dest_dir)
        logging.info("State: %s", state_path)
        logging.info("Recipients configured: %d", len(config.to))
        result = process(dest_dir, state_path, state, config)
        logging.info("Done. Emails sent: %d. Bootstrap skipped: %d.", result.emailed, result.skipped)
        if result.failed:
            for failure in result.failures or []:
                logging.error("Failure: %s", failure)
            return 1
        return 0
    except Exception as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
