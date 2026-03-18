#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError, Page, sync_playwright

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIRMED_PATH = DATA_DIR / "fallen.json"
REVIEW_PATH = DATA_DIR / "pending_review.json"
META_PATH = DATA_DIR / "meta.json"

SEARCH_URL = "https://www.war.gov/News/Releases/Search/casualty/"
BASE_URL = "https://www.war.gov"
TIMEOUT_MS = 30_000
NA = "N/A"
VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


@dataclass
class SiteRecord:
    name: str
    age: str
    hometown: str
    branch: str
    reported_location: str
    incident_date: str
    release_date: str
    release_title: str
    source_url: str
    status: str
    notes: str = NA


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    cleaned_path = parsed.path.rstrip("/") + "/"
    cleaned = parsed._replace(path=cleaned_path, query="", fragment="")
    return urlunparse(cleaned)


def text_or_na(value: Optional[str]) -> str:
    if not value:
        return NA
    cleaned = re.sub(r"\s+", " ", value).strip(" .")
    return cleaned or NA


def settle_page(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightError:
        page.wait_for_timeout(1_000)


def fetch_page(page: Page, url: str, sleep_s: float = 0.8) -> None:
    time.sleep(sleep_s)
    response = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    settle_page(page)

    status = response.status if response else None
    if status == 403 and "war.gov" in url:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        page.wait_for_timeout(1_000)
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=TIMEOUT_MS,
            referer=f"{BASE_URL}/News/Releases/",
        )
        settle_page(page)
        status = response.status if response else None

    if status and status >= 400:
        raise RuntimeError(f"{status} while loading {url}")


def locator_text(page: Page, selector: str) -> Optional[str]:
    locator = page.locator(selector)
    if locator.count() == 0:
        return None

    try:
        value = locator.first.inner_text(timeout=2_000)
    except PlaywrightError:
        try:
            value = locator.first.text_content(timeout=2_000)
        except PlaywrightError:
            return None

    return text_or_na(value)


def extract_release_title(page: Page) -> str:
    for selector in ("h1", "main h1", "article h1"):
        value = locator_text(page, selector)
        if value != NA:
            return value

    try:
        return text_or_na(page.title())
    except PlaywrightError:
        return NA


def extract_page_text(page: Page) -> str:
    return locator_text(page, "body") or NA


def extract_release_date(page_text: str) -> str:
    match = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", page_text)
    if not match:
        return NA
    raw = match.group(1)
    try:
        return datetime.strptime(raw, "%B %d, %Y").date().isoformat()
    except ValueError:
        return raw


def extract_article_body(page: Page) -> str:
    selectors = (
        "article",
        "main",
        '[itemprop="articleBody"]',
        'div[class*="article"]',
    )
    for selector in selectors:
        value = locator_text(page, selector)
        if value != NA and len(value) > 200:
            return value

    try:
        paragraphs = [text_or_na(value) for value in page.locator("p").all_inner_texts()]
    except PlaywrightError:
        paragraphs = []
    paragraphs = [value for value in paragraphs if value != NA]
    return text_or_na("\n".join(paragraphs))


def infer_branch(title: str, body: str) -> str:
    haystack = f"{title} {body}".lower()
    if "air force" in haystack or "airman" in haystack or "airmen" in haystack:
        return "Air Force"
    if "army" in haystack or "soldier" in haystack:
        return "Army"
    if "marine" in haystack:
        return "Marine Corps"
    if "navy" in haystack or "sailor" in haystack:
        return "Navy"
    if "space force" in haystack or "guardian" in haystack:
        return "Space Force"
    if "coast guard" in haystack:
        return "Coast Guard"
    return NA


def extract_location(body: str) -> str:
    patterns = [
        r"\bon [A-Z][a-z]+ \d{1,2}, \d{4}, in ([A-Z][A-Za-z .'-]+(?:,\s*[A-Z][A-Za-z .'-]+)?)\.",
        r"\bdied .*? in ([A-Z][A-Za-z .'-]+(?:,\s*[A-Z][A-Za-z .'-]+)?)\.",
        r"\bkilled .*? in ([A-Z][A-Za-z .'-]+(?:,\s*[A-Z][A-Za-z .'-]+)?)\.",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            return text_or_na(match.group(1))
    return NA


def extract_incident_date(body: str) -> str:
    patterns = [
        r"\b(?:died|were killed|was killed)\s+(?:on\s+)?([A-Z][a-z]+ \d{1,2}, \d{4})\b",
        r"\bon ([A-Z][a-z]+ \d{1,2}, \d{4}), in ",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1)
        try:
            return datetime.strptime(raw, "%B %d, %Y").date().isoformat()
        except ValueError:
            return raw
    return NA


def normalize_name(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip(" .")


def make_record(
    *,
    name: str,
    age: str = NA,
    hometown: str = NA,
    branch: str = NA,
    reported_location: str = NA,
    incident_date: str = NA,
    release_date: str = NA,
    release_title: str = NA,
    source_url: str = NA,
    status: str,
    notes: str = NA,
) -> SiteRecord:
    return SiteRecord(
        name=text_or_na(name),
        age=text_or_na(age),
        hometown=text_or_na(hometown),
        branch=text_or_na(branch),
        reported_location=text_or_na(reported_location),
        incident_date=text_or_na(incident_date),
        release_date=text_or_na(release_date),
        release_title=text_or_na(release_title),
        source_url=canonicalize_url(source_url) if source_url != NA else NA,
        status=status,
        notes=text_or_na(notes),
    )


def build_context(playwright) -> tuple[Browser, BrowserContext]:
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="en-US",
        viewport=VIEWPORT,
    )
    context.set_default_timeout(TIMEOUT_MS)
    return browser, context


def warm_context(context: BrowserContext) -> None:
    page = context.new_page()
    try:
        fetch_page(page, BASE_URL, sleep_s=0.2)
    finally:
        page.close()


def discover_article_links(context: BrowserContext, max_pages: int = 20) -> list[str]:
    article_links: set[str] = set()
    page = context.new_page()

    try:
        for index in range(1, max_pages + 1):
            url = SEARCH_URL if index == 1 else f"{SEARCH_URL}?Page={index}"
            try:
                fetch_page(page, url, sleep_s=0.5)
                hrefs = page.locator("a[href]").evaluate_all("els => els.map(el => el.href)")
            except Exception as exc:
                print(f"[WARN] search page failed: {url} -> {exc}")
                continue

            found_this_page = 0
            for href in hrefs:
                full = canonicalize_url(urljoin(BASE_URL, href.strip()))
                if "/News/Releases/Release/Article/" not in full:
                    continue
                article_links.add(full)
                found_this_page += 1

            print(f"[INFO] page {index}: found {found_this_page} article links")
            if found_this_page == 0 and index > 1:
                break
    finally:
        page.close()

    return sorted(article_links)


def extract_records_from_body(title: str, release_date: str, article_url: str, body: str) -> list[SiteRecord]:
    records: list[SiteRecord] = []
    location = extract_location(body)
    branch = infer_branch(title, body)
    incident_date = extract_incident_date(body)

    list_section = re.search(r"Killed (?:were|was):\s*(.+)", body, re.IGNORECASE | re.DOTALL)
    if list_section:
        chunk = list_section.group(1)
        candidates = re.split(r"[\n\r]+|(?<=\.)\s+(?=[A-Z])", chunk)
        for item in candidates:
            item = item.strip(" •-\t")
            if not item:
                continue

            match = re.search(
                r"^(?P<name>.+?),\s*(?P<age>\d{1,2}),\s*of\s+(?P<hometown>[^.]+)\.?",
                item,
            )
            if not match:
                continue

            records.append(
                make_record(
                    name=normalize_name(match.group("name")),
                    age=match.group("age"),
                    hometown=match.group("hometown"),
                    branch=branch,
                    reported_location=location,
                    incident_date=incident_date,
                    release_date=release_date,
                    release_title=title,
                    source_url=article_url,
                    status="confirmed",
                )
            )

        if records:
            return records

    for match in re.finditer(
        r"(?P<name>[A-Z][A-Za-z0-9 .'\-]+?),\s*(?P<age>\d{1,2}),\s*of\s+(?P<hometown>[^,\.]+(?:,\s*[^,\.]+)*)",
        body,
    ):
        name = normalize_name(match.group("name"))
        if len(name.split()) < 2:
            continue

        records.append(
            make_record(
                name=name,
                age=match.group("age"),
                hometown=match.group("hometown"),
                branch=branch,
                reported_location=location,
                incident_date=incident_date,
                release_date=release_date,
                release_title=title,
                source_url=article_url,
                status="confirmed",
            )
        )

    deduped = {record.name: record for record in records}
    return list(deduped.values())


def title_from_url(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if not slug:
        return NA
    return text_or_na(slug.replace("-", " ").title())


def parse_article(context: BrowserContext, url: str) -> tuple[list[SiteRecord], Optional[SiteRecord]]:
    page = context.new_page()
    try:
        fetch_page(page, url, sleep_s=0.8)
        title = extract_release_title(page)
        body = extract_article_body(page)
        page_text = extract_page_text(page)
    finally:
        page.close()

    release_date = extract_release_date(page_text)
    branch = infer_branch(title, body)

    records = extract_records_from_body(title, release_date, url, body)
    if records:
        return records, None

    review_record = make_record(
        name=NA,
        branch=branch,
        release_date=release_date,
        release_title=title,
        source_url=url,
        status="review",
        notes="Could not confidently parse a casualty name from the official release.",
    )
    return [], review_record


def dedupe(records: Iterable[dict]) -> list[dict]:
    seen = set()
    unique = []
    for record in records:
        key = (
            record.get("source_url"),
            record.get("name"),
            record.get("release_title"),
            record.get("status"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    confirmed_existing = load_json(CONFIRMED_PATH, [])
    review_existing = load_json(REVIEW_PATH, [])
    meta = load_json(META_PATH, {})

    with sync_playwright() as playwright:
        browser, context = build_context(playwright)
        try:
            warm_context(context)
            links = discover_article_links(context, max_pages=15)
            print(f"[INFO] total article links discovered: {len(links)}")

            confirmed_records = list(confirmed_existing)
            review_records = list(review_existing)

            for index, url in enumerate(links, start=1):
                try:
                    confirmed, review = parse_article(context, url)
                    print(f"[INFO] {index}/{len(links)} parsed {len(confirmed)} records from {url}")
                    confirmed_records.extend(asdict(record) for record in confirmed)
                    if review:
                        review_records.append(asdict(review))
                except Exception as exc:
                    print(f"[WARN] failed article: {url} -> {exc}")
                    review_records.append(
                        asdict(
                            make_record(
                                name=NA,
                                release_title=title_from_url(url),
                                source_url=url,
                                status="review",
                                notes=f"Updater failed on this official release: {exc}",
                            )
                        )
                    )
        finally:
            context.close()
            browser.close()

    confirmed_records = sorted(
        dedupe(confirmed_records),
        key=lambda row: (row.get("release_date", ""), row.get("name", "")),
        reverse=True,
    )
    confirmed_urls = {row.get("source_url") for row in confirmed_records}

    review_records = [
        row for row in dedupe(review_records) if row.get("source_url") not in confirmed_urls
    ]
    review_records = sorted(
        review_records,
        key=lambda row: (row.get("release_date", ""), row.get("release_title", "")),
        reverse=True,
    )

    meta["last_updated_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    meta["source_feed"] = SEARCH_URL
    meta["scope"] = "Officially identified deceased U.S. service members from Defense release pages"

    save_json(CONFIRMED_PATH, confirmed_records)
    save_json(REVIEW_PATH, review_records)
    save_json(META_PATH, meta)
    print(f"[DONE] confirmed={len(confirmed_records)} review={len(review_records)}")


if __name__ == "__main__":
    main()
