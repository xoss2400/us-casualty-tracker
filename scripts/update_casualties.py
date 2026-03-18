#!/usr/bin/env python3
"""
Scrape official casualty-related release pages from war.gov and extract
casualty records into JSON.

Usage:
    python update_casualties.py

Output:
    data/fallen.json
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SEARCH_URL = "https://www.war.gov/News/Releases/Search/casualty/"
BASE_URL = "https://www.war.gov"
OUTPUT_PATH = Path("data/fallen.json")

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
class CasualtyRecord:
    name: str
    age: str = "N/A"
    hometown: str = "N/A"
    reported_location: str = "N/A"
    service_branch: str = "N/A"
    release_title: str = "N/A"
    release_date: str = "N/A"
    article_url: str = "N/A"


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


def fetch_html(session: requests.Session, url: str, sleep_s: float = 1.0) -> str:
    time.sleep(sleep_s)
    resp = session.get(url, timeout=30)
    if resp.status_code == 403:
        # Retry once with a slightly different Referer and no cached connection assumptions.
        headers = dict(session.headers)
        headers["Referer"] = BASE_URL
        headers["Connection"] = "close"
        time.sleep(2.0)
        resp = session.get(url, headers=headers, timeout=30)

    resp.raise_for_status()
    return resp.text


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    cleaned = parsed._replace(query="", fragment="")
    return urlunparse(cleaned)


def discover_article_links(session: requests.Session, max_pages: int = 20) -> List[str]:
    """
    Walk the casualty search pages and collect release article URLs.
    Uses href pattern matching instead of brittle classes.
    """
    article_links: set[str] = set()

    for page in range(1, max_pages + 1):
        url = SEARCH_URL if page == 1 else f"{SEARCH_URL}?Page={page}"
        try:
            html = fetch_html(session, url, sleep_s=0.6)
        except Exception as exc:
            print(f"[WARN] search page failed: {url} -> {exc}")
            continue

        soup = BeautifulSoup(html, "html.parser")

        found_this_page = 0
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            full = urljoin(BASE_URL, href)
            full = canonicalize_url(full)

            # Official release articles on this site consistently use this path pattern.
            if "/News/Releases/Release/Article/" in full:
                article_links.add(full)
                found_this_page += 1

        print(f"[INFO] page {page}: found {found_this_page} article links")

        # If a page yields nothing, assume we've gone past the useful range.
        if found_this_page == 0 and page > 1:
            break

    return sorted(article_links)


def text_or_na(value: Optional[str]) -> str:
    if not value:
        return "N/A"
    value = re.sub(r"\s+", " ", value).strip()
    return value if value else "N/A"


def extract_release_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return text_or_na(h1.get_text(" ", strip=True))
    if soup.title:
        title = soup.title.get_text(" ", strip=True)
        title = title.split(">")[0].strip()
        return text_or_na(title)
    return "N/A"


def extract_release_date(page_text: str) -> str:
    # Matches dates like: March 11, 2026
    m = re.search(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b", page_text)
    if not m:
        return "N/A"
    raw = m.group(1)
    try:
        return datetime.strptime(raw, "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return raw


def extract_article_body(soup: BeautifulSoup) -> str:
    # Try common containers first
    candidates = [
        soup.find("article"),
        soup.find("main"),
        soup.find(attrs={"itemprop": "articleBody"}),
        soup.find(class_=re.compile(r"article", re.I)),
    ]

    for node in candidates:
        if node:
            text = node.get_text("\n", strip=True)
            if len(text) > 200:
                return text

    # Fallback: join all paragraph text
    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    joined = "\n".join(p for p in paragraphs if p)
    return text_or_na(joined)


def infer_branch(title: str, body: str) -> str:
    hay = f"{title} {body}".lower()
    if "air force" in hay or "airman" in hay or "airmen" in hay:
        return "Air Force"
    if "army" in hay or "soldier" in hay:
        return "Army"
    if "marine" in hay:
        return "Marine Corps"
    if "navy" in hay or "sailor" in hay:
        return "Navy"
    if "space force" in hay or "guardian" in hay:
        return "Space Force"
    if "coast guard" in hay:
        return "Coast Guard"
    return "N/A"


def extract_location(body: str) -> str:
    # Broad pattern for "in X, Y."
    patterns = [
        r"\bon [A-Z][a-z]+ \d{1,2}, \d{4}, in ([A-Z][A-Za-z .'-]+(?:,\s*[A-Z][A-Za-z .'-]+)?)\.",
        r"\bdied .*? in ([A-Z][A-Za-z .'-]+(?:,\s*[A-Z][A-Za-z .'-]+)?)\.",
        r"\bkilled .*? in ([A-Z][A-Za-z .'-]+(?:,\s*[A-Z][A-Za-z .'-]+)?)\.",
    ]
    for pat in patterns:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            return text_or_na(m.group(1))
    return "N/A"


def normalize_name(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    # You can make this more aggressive later if you want to remove rank prefixes.
    return raw


def extract_records_from_body(title: str, release_date: str, article_url: str, body: str) -> List[CasualtyRecord]:
    records: List[CasualtyRecord] = []
    location = extract_location(body)
    branch = infer_branch(title, body)

    # 1) Explicit "Killed were:" / "Killed was:" list sections
    list_section = re.search(r"Killed (?:were|was):\s*(.+)", body, re.IGNORECASE | re.DOTALL)
    if list_section:
        chunk = list_section.group(1)

        # Split into candidate lines/sentences
        candidates = re.split(r"[\n\r]+|(?<=\.)\s+(?=[A-Z])", chunk)
        for item in candidates:
            item = item.strip(" •-\t")
            if not item:
                continue

            m = re.search(
                r"^(?P<name>.+?),\s*(?P<age>\d{1,2}),\s*of\s+(?P<hometown>[^.]+)\.?",
                item,
            )
            if m:
                records.append(
                    CasualtyRecord(
                        name=normalize_name(m.group("name")),
                        age=text_or_na(m.group("age")),
                        hometown=text_or_na(m.group("hometown")),
                        reported_location=location,
                        service_branch=branch,
                        release_title=title,
                        release_date=release_date,
                        article_url=article_url,
                    )
                )

        if records:
            return records

    # 2) Single-person sentence pattern like:
    # "Chief Warrant Officer 3 Robert M. Marzan, 54, of Sacramento, Calif., ..."
    for m in re.finditer(
        r"(?P<name>[A-Z][A-Za-z0-9 .'\-]+?),\s*(?P<age>\d{1,2}),\s*of\s+(?P<hometown>[^,\.]+(?:,\s*[^,\.]+)*)",
        body,
    ):
        name = normalize_name(m.group("name"))
        if len(name.split()) < 2:
            continue
        records.append(
            CasualtyRecord(
                name=name,
                age=text_or_na(m.group("age")),
                hometown=text_or_na(m.group("hometown")),
                reported_location=location,
                service_branch=branch,
                release_title=title,
                release_date=release_date,
                article_url=article_url,
            )
        )

    # Deduplicate by name within the article
    deduped = {}
    for rec in records:
        deduped[rec.name] = rec
    if deduped:
        return list(deduped.values())

    # 3) Fallback: if we cannot parse structured casualty lines, return no records.
    return []


def parse_article(session: requests.Session, url: str) -> List[CasualtyRecord]:
    html = fetch_html(session, url, sleep_s=1.0)
    soup = BeautifulSoup(html, "html.parser")

    title = extract_release_title(soup)
    body = extract_article_body(soup)
    page_text = soup.get_text("\n", strip=True)
    release_date = extract_release_date(page_text)

    records = extract_records_from_body(title, release_date, url, body)
    return records


def load_existing(path: Path) -> List[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def merge_records(existing: List[dict], new_records: Iterable[CasualtyRecord]) -> List[dict]:
    merged = {(row.get("article_url", ""), row.get("name", "")): row for row in existing}

    for rec in new_records:
        key = (rec.article_url, rec.name)
        merged[key] = asdict(rec)

    rows = list(merged.values())
    rows.sort(
        key=lambda r: (
            r.get("release_date", ""),
            r.get("name", ""),
        ),
        reverse=True,
    )
    return rows


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    session = build_session()

    links = discover_article_links(session, max_pages=15)
    print(f"[INFO] total article links discovered: {len(links)}")

    extracted: List[CasualtyRecord] = []
    for i, url in enumerate(links, start=1):
        try:
            records = parse_article(session, url)
            print(f"[INFO] {i}/{len(links)} parsed {len(records)} records from {url}")
            extracted.extend(records)
        except Exception as exc:
            print(f"[WARN] failed article: {url} -> {exc}")

    existing = load_existing(OUTPUT_PATH)
    merged = merge_records(existing, extracted)
    OUTPUT_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[DONE] wrote {len(merged)} total records to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()