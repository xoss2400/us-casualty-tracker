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

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIRMED_PATH = DATA_DIR / "fallen.json"
REVIEW_PATH = DATA_DIR / "pending_review.json"
META_PATH = DATA_DIR / "meta.json"

SEARCH_URL = "https://www.war.gov/News/Releases/Search/casualty/"
BASE_URL = "https://www.war.gov"
TIMEOUT = 30
NA = "N/A"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": SEARCH_URL,
}


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


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch_html(session: requests.Session, url: str, sleep_s: float = 0.8) -> str:
    time.sleep(sleep_s)
    response = session.get(url, timeout=TIMEOUT)

    if response.status_code == 403 and "war.gov" in url:
        warm_headers = dict(session.headers)
        warm_headers["Referer"] = BASE_URL
        session.get(f"{BASE_URL}/", headers=warm_headers, timeout=TIMEOUT)

        retry_headers = dict(session.headers)
        retry_headers["Referer"] = f"{BASE_URL}/News/Releases/"
        retry_headers["Connection"] = "close"
        time.sleep(1.5)
        response = session.get(url, headers=retry_headers, timeout=TIMEOUT)

    response.raise_for_status()
    return response.text


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    cleaned_path = parsed.path.rstrip("/") + "/"
    cleaned = parsed._replace(path=cleaned_path, query="", fragment="")
    return urlunparse(cleaned)


def discover_article_links(session: requests.Session, max_pages: int = 20) -> list[str]:
    article_links: set[str] = set()

    for page in range(1, max_pages + 1):
        url = SEARCH_URL if page == 1 else f"{SEARCH_URL}?Page={page}"
        try:
            html = fetch_html(session, url, sleep_s=0.5)
        except Exception as exc:
            print(f"[WARN] search page failed: {url} -> {exc}")
            continue

        soup = BeautifulSoup(html, "html.parser")
        found_this_page = 0

        for anchor in soup.find_all("a", href=True):
            full = canonicalize_url(urljoin(BASE_URL, anchor["href"].strip()))
            if "/News/Releases/Release/Article/" not in full:
                continue
            article_links.add(full)
            found_this_page += 1

        print(f"[INFO] page {page}: found {found_this_page} article links")
        if found_this_page == 0 and page > 1:
            break

    return sorted(article_links)


def text_or_na(value: Optional[str]) -> str:
    if not value:
        return NA
    cleaned = re.sub(r"\s+", " ", value).strip(" .")
    return cleaned or NA


def extract_release_title(soup: BeautifulSoup) -> str:
    heading = soup.find("h1")
    if heading:
        return text_or_na(heading.get_text(" ", strip=True))
    if soup.title:
        return text_or_na(soup.title.get_text(" ", strip=True).split(">")[0])
    return NA


def extract_release_date(page_text: str) -> str:
    match = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", page_text)
    if not match:
        return NA
    raw = match.group(1)
    try:
        return datetime.strptime(raw, "%B %d, %Y").date().isoformat()
    except ValueError:
        return raw


def extract_article_body(soup: BeautifulSoup) -> str:
    candidates = [
        soup.find("article"),
        soup.find("main"),
        soup.find(attrs={"itemprop": "articleBody"}),
        soup.find(class_=re.compile(r"article", re.IGNORECASE)),
    ]

    for node in candidates:
        if not node:
            continue
        text = node.get_text("\n", strip=True)
        if len(text) > 200:
            return text

    paragraphs = [paragraph.get_text(" ", strip=True) for paragraph in soup.find_all("p")]
    return text_or_na("\n".join(line for line in paragraphs if line))


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


def parse_article(session: requests.Session, url: str) -> tuple[list[SiteRecord], Optional[SiteRecord]]:
    html = fetch_html(session, url, sleep_s=0.8)
    soup = BeautifulSoup(html, "html.parser")

    title = extract_release_title(soup)
    body = extract_article_body(soup)
    page_text = soup.get_text("\n", strip=True)
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
    session = build_session()

    confirmed_existing = load_json(CONFIRMED_PATH, [])
    review_existing = load_json(REVIEW_PATH, [])
    meta = load_json(META_PATH, {})

    links = discover_article_links(session, max_pages=15)
    print(f"[INFO] total article links discovered: {len(links)}")

    confirmed_records = list(confirmed_existing)
    review_records = list(review_existing)

    for index, url in enumerate(links, start=1):
        try:
            confirmed, review = parse_article(session, url)
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
