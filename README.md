# U.S. Service Member Casualty Tracker

A simple GitHub Pages site backed by GitHub Actions.

It polls the official Defense releases RSS feed, fetches new release pages, and updates a small JSON dataset for the frontend.

## What this starter does

- hosts a static memorial/record site on GitHub Pages
- checks the official releases feed every 5 minutes
- stores confirmed entries in `data/fallen.json`
- stores ambiguous official releases in `data/pending_review.json`
- writes `N/A` for unavailable datapoints

## Current data rule

A record is added to the public site when the updater can parse a name from an official casualty-style release.

Only `name` is treated as required.
Everything else can be `N/A`.

If the updater cannot confidently parse the name, it places the release in `pending_review.json` instead of publishing it as confirmed.

## Data fields

```json
[
  {
    "name": "Example Name",
    "age": "N/A",
    "hometown": "N/A",
    "branch": "N/A",
    "reported_location": "N/A",
    "incident_date": "N/A",
    "release_date": "2026-03-17",
    "release_title": "DoD Identifies ...",
    "source_url": "https://...",
    "status": "confirmed",
    "notes": "N/A"
  }
]
```

## Important note on birthplace

Official DoD casualty releases usually provide wording like `of City, State` or `from City, State`.
That is safer to label as `hometown` than `birthplace`, so this starter uses `hometown`.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/update_casualties.py
python -m http.server 8000
```

Then open `http://localhost:8000`.

## Deploy on GitHub Pages

This repo includes two GitHub Actions workflows:

- `.github/workflows/update-data.yml` polls the official feed on a `*/5 * * * *` cron and commits any JSON changes.
- `.github/workflows/deploy-pages.yml` deploys the static site to GitHub Pages on every push to `main`.

After creating the repository:

1. Push this folder to a GitHub repository with `main` as the default branch.
2. In GitHub, open **Settings → Pages**.
3. Set **Source** to **GitHub Actions**.
4. In **Settings → Actions → General**, allow workflows to have **Read and write permissions** so the updater can commit refreshed JSON files.

## Tuning the updater

In `scripts/update_casualties.py`, you can adjust:

- `FEED_URL` if the Defense site changes feeds
- the regex patterns if release wording changes
- the display fields in `app.js`

## Editorial caution

This repo is intentionally conservative. The site should behave like an official-release tracker, not a rumor tracker. Public naming can lag behind an incident because releases typically happen only after next-of-kin notification and public posting.
