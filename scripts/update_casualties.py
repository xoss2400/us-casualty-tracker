#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIRMED_PATH = DATA_DIR / "fallen.json"
REVIEW_PATH = DATA_DIR / "pending_review.json"
META_PATH = DATA_DIR / "meta.json"

FEED_URL = "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945&max=10"
USER_AGENT = "us-casualty-tracker/1.0"
TIMEOUT = 30
NA = "N/A"

BRANCH_KEYWORDS = {
    "Army": ["army", "soldier"],
    "Air Force": ["air force", "airman"],
    "Marine Corps": ["marine corps", "marine"],
    "Navy": ["navy", "sailor"],
    "Space Force": ["space force", "guardian"],
    "Coast Guard": ["coast guard"],
}

NAME_AGE_PATTERNS = [
    re.compile(
        r"(?P<name>[A-Z][A-Za-z\.\-\' ]+?),\s*(?P<age>\d{1,2}),\s+(?:of|from)\s+(?P<hometown>[A-Za-z\.\-\' ]+,\s*[A-Za-z\.\-\' ]+),\s+died",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<name>[A-Z][A-Za-z\.\-\' ]+?),\s*(?:age\s*)?(?P<age>\d{1,2}),\s+(?:of|from)\s+(?P<hometown>[A-Za-z\.\-\' ]+,\s*[A-Za-z\.\-\' ]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<name>[A-Z][A-Za-z\.\-\' ]+?)\s+died\s+",
        re.IGNORECASE,
    ),
]

DEATH_PATTERNS = [
    re.compile(
        r"died\s+(?P<date>[A-Za-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?)\s+(?:in|at)\s+(?P<location>[^\.]+?)(?:\.|,\s+while|,\s+during|,\s+from|,\s+of\s+wounds)",
        re.IGNORECASE,
    ),
    re.compile(
        r"died\s+(?P<date>[A-Za-z]+\.?\s+\d{1,2}(?:,\s*\d{4})?),\s+(?:in|at)\s+(?P<location>[^\.]+?)(?:\.|,\s+while|,\s+during|,\s+from|,\s+of\s+wounds)",
        re.IGNORECASE,
    ),
]


@dataclass
class CasualtyRecord:
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
    return json.loads(path.read_text())


def save_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def request(url: str) -> requests.Response:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def parse_rss_items(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for item in root.findall(".//item"):
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "pub_date": (item.findtext("pubDate") or "").strip(),
                "description": (item.findtext("description") or "").strip(),
            }
        )
    return items


def looks_like_casualty_release(item: dict) -> bool:
    title = item["title"].lower()
    description = item["description"].lower()
    return (
        "casualt" in title
        or (
            "identifies" in title
            and any(word in description for word in ["death of", "died", "airman", "soldier", "marine", "sailor", "guardian"])
        )
    )


def extract_page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)
    return re.sub(r"\n+", "\n", text)


def normalize_whitespace(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value).strip(" .")


def na(value: Optional[object]) -> str:
    if value is None:
        return NA
    if isinstance(value, str):
        cleaned = normalize_whitespace(value)
        return cleaned if cleaned else NA
    return str(value)


def infer_branch(text: str, title: str) -> str:
    blob = f"{title}\n{text}".lower()
    for branch, keywords in BRANCH_KEYWORDS.items():
        if any(keyword in blob for keyword in keywords):
            return branch
    return NA


def parse_release_date(item: dict) -> str:
    try:
        return parsedate_to_datetime(item["pub_date"]).date().isoformat()
    except Exception:
        return NA


def parse_name_age_hometown(text: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
    for pattern in NAME_AGE_PATTERNS:
        match = pattern.search(text)
        if match:
            name = normalize_whitespace(match.groupdict().get("name"))
            age = int(match.group("age")) if match.groupdict().get("age") else None
            hometown = normalize_whitespace(match.groupdict().get("hometown"))
            if name:
                return name, age, hometown
    return None, None, None


def parse_incident(text: str) -> tuple[Optional[str], Optional[str]]:
    for pattern in DEATH_PATTERNS:
        match = pattern.search(text)
        if match:
            death_date = normalize_whitespace(match.group("date"))
            location = normalize_whitespace(match.group("location"))
            return death_date, location
    return None, None


def iso_or_na(natural_date: Optional[str]) -> str:
    if not natural_date:
        return NA
    cleaned = natural_date.replace(".", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return na(natural_date)


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") + "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def make_record(item: dict, text: str, *, status: str, notes: str = NA) -> CasualtyRecord:
    name, age, hometown = parse_name_age_hometown(text)
    incident_date_raw, location = parse_incident(text)
    return CasualtyRecord(
        name=na(name),
        age=na(age),
        hometown=na(hometown),
        branch=infer_branch(text, item["title"]),
        reported_location=na(location),
        incident_date=iso_or_na(incident_date_raw),
        release_date=parse_release_date(item),
        release_title=na(item["title"]),
        source_url=canonicalize_url(item["link"]),
        status=status,
        notes=na(notes),
    )


def parse_release(item: dict) -> Optional[CasualtyRecord]:
    html = request(item["link"]).text
    text = extract_page_text(html)
    lowered = text.lower()

    if "announced today the death" not in lowered and "died" not in lowered:
        return None

    name, _, _ = parse_name_age_hometown(text)
    if not name:
        return make_record(item, text, status="review", notes="Could not confidently parse a name from the official release.")

    return make_record(item, text, status="confirmed")


def dedupe(records: Iterable[dict]) -> list[dict]:
    seen = set()
    unique = []
    for record in records:
        key = (record.get("source_url"), record.get("name"), record.get("release_title"), record.get("status"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def main() -> None:
    confirmed = load_json(CONFIRMED_PATH, [])
    review = load_json(REVIEW_PATH, [])
    meta = load_json(META_PATH, {})

    seen_urls = {item.get("source_url") for item in confirmed + review}
    rss_text = request(FEED_URL).text
    items = [item for item in parse_rss_items(rss_text) if looks_like_casualty_release(item)]

    for item in items:
        url = canonicalize_url(item["link"])
        if url in seen_urls:
            continue
        try:
            record = parse_release(item)
        except Exception as exc:
            record = CasualtyRecord(
                name=NA,
                age=NA,
                hometown=NA,
                branch=NA,
                reported_location=NA,
                incident_date=NA,
                release_date=parse_release_date(item),
                release_title=na(item["title"]),
                source_url=url,
                status="review",
                notes=f"Updater failed on this official release: {exc}",
            )

        if record is None:
            continue

        payload = asdict(record)
        if record.status == "confirmed":
            confirmed.append(payload)
        else:
            review.append(payload)
        seen_urls.add(url)

    confirmed = sorted(dedupe(confirmed), key=lambda x: (x.get("release_date") or "", x.get("name") or ""), reverse=True)
    review = sorted(dedupe(review), key=lambda x: (x.get("release_date") or "", x.get("release_title") or ""), reverse=True)

    meta["last_updated_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    meta["source_feed"] = FEED_URL
    meta["scope"] = "Officially identified deceased U.S. service members from Defense release pages"

    save_json(CONFIRMED_PATH, confirmed)
    save_json(REVIEW_PATH, review)
    save_json(META_PATH, meta)

    print(f"confirmed={len(confirmed)} review={len(review)}")


if __name__ == "__main__":
    main()
