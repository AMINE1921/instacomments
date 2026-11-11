<!-- PROJECT LOGO -->
<br />
<p align="center">
  <a href="https://github.com/AMINE1921/instacomments">
    <img src="./public/logo512.png" alt="Logo" width="80" height="80">
  </a>

  <h3 align="center">instaComments</h3>

  <p align="center">
    A simple CLI to fetch Instagram comments (parents and optional replies) and export them to JSON, CSV, or TXT.
    <br />
    This project is based on <a href="https://www.python.org/">Python</a> and uses <a href="https://requests.readthedocs.io/">Requests</a>, <a href="https://tqdm.github.io/">tqdm</a>, and <a href="https://saurabh-kumar.com/python-dotenv/">python-dotenv</a>.
    <br />
  </p>
  <p align="center">
    <a href="https://github.com/AMINE1921"><img src="https://img.shields.io/badge/github-%23100000.svg?&style=for-the-badge&logo=github&logoColor=white"> </a>
    <img src="https://img.shields.io/badge/discord-AMINE%235328-%237289DA.svg?&style=for-the-badge&logo=discord&logoColor=white">
  </p>
</p>
<br />
<br />

## InstaComments: Instagram Comment Scraper

A Python script that fetches parent comments (and optionally all their replies) from an Instagram Reel or Post using your session cookies, and exports them as JSON, CSV, or TXT.

---

### Features

- Fetches parent comments from Reels and Posts, with optional replies (all reply pages, not only the preview).
- Two backends: legacy GraphQL (`query_hash`) and the web `api/v1` endpoint, with automatic fallback.
- Resilient: request timeouts, automatic retries with exponential backoff on 429/5xx (honours `Retry-After`), configurable delay between requests.
- Never loses work: on an error or `Ctrl+C`, the comments fetched so far are still written to disk.
- Filters and limits (min likes, max comments), deduplication, ISO 8601 dates.
- Excel-friendly CSV (UTF-8 with BOM), optional one-row-per-reply CSV.

---

### Requirements

- Python 3.9+
- An Instagram account session (cookies)

```bash
python -m venv venv
source venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the values from your logged-in instagram.com cookies:

```
SESSIONID="..."
DS_USER_ID="..."
CSRFTOKEN="..."
MID="..."
```

If Instagram rotates its GraphQL query hashes, override them with `IG_PARENT_QUERY_HASH` / `IG_REPLY_QUERY_HASH` in `.env`, or use `--api v1`.

---

### Usage

Interactive prompt:

```bash
python instacomments.py
```

Full example:

```bash
python instacomments.py \
	--url "https://www.instagram.com/reel/SHORTCODE/" \
	--data-format detailed \
	--output out/comments.csv \
	--include-replies --flatten-replies \
	--max-comments 200 \
	--min-likes 2 \
	--delay 2
```

Options:

- `--url` Instagram Reel or Post URL (`/reel/`, `/reels/`, `/p/`, `/tv/`)
- `--output` Output file path (default: `listComments.<format>`)
- `--file-format` `json`, `csv`, or `txt`. Inferred from the `--output` extension when omitted (fallback: `json`)
- `--data-format` `usernames` or `detailed` (default: `usernames`)
- `--api` `auto` (GraphQL, then fallback to v1), `graphql`, or `v1` (default: `auto`)
- `--per-page` Comments per page for GraphQL (default: 50)
- `--max-comments` Stop after N parent comments
- `--min-likes` Minimum like count for a parent comment
- `--include-replies` Fetch replies for each parent comment (in `usernames` mode, reply authors are included)
- `--flatten-replies` Detailed CSV only: one row per reply, linked to its parent via `parent_id`
- `--dedupe/--no-dedupe` Duplicate removal (by username in `usernames` mode, by id in `detailed` mode; default: on)
- `--delay` Seconds between requests, plus up to 50% random jitter (default: 1.5)
- `--retries` Retries on 429/5xx with exponential backoff (default: 5)
- `--no-progress` Disable the progress bar

Output examples:

- usernames + json: `["user1", "user2", ...]` (sorted case-insensitively)
- detailed + json: `[{ id, username, text, like_count, created_at, replies: [...] }, ...]` with `created_at` in ISO 8601 UTC

Exit codes: `0` success, `1` fetch/write error (partial results still saved), `2` bad input or missing credentials, `130` interrupted.

---

### Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

---

### Notes

- Private content or expired cookies cause missing data; refresh your cookies from instagram.com.
- Too many requests trigger rate limits (HTTP 429). The script slows down and retries, but does not bypass them; raise `--delay` on large posts.
- With `--no-dedupe`, the usernames output may contain repeated entries.
