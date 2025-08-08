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
    <a href="http://discordapp.com/channels/@AMINE#5328"><img src="https://img.shields.io/badge/discord-%237289DA.svg?&style=for-the-badge&logo=discord&logoColor=white"> </a>
  </p>
</p>
<br />
<br />

## InstaComments: Instagram Comment Scraper

A simple, robust Python script to fetch parent comments (and optionally replies) from an Instagram Reel or Post using your session cookies. Output can be JSON, CSV, or TXT.

---

### Features

- Fetch parent comments for Reels and Posts, with optional replies.
- Pagination until completion with a progress bar.
- CLI with options for data shape and output format.
- Filters and limits (min likes, max comments, per-page).

---

### Requirements

- Python 3.9+
- An Instagram account session (cookies)

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

Create `.env` (or copy `.env.example`) and fill in values from your logged-in instagram.com cookies:

```
SESSIONID="..."
DS_USER_ID="..."
CSRFTOKEN="..."
MID="..."
```

---

### Usage

Interactive prompt:

```bash
python instacomments.py
```

Command line options:

```bash
python instacomments.py \
	--url "https://www.instagram.com/reel/SHORTCODE/" \
	--data-format usernames \
	--file-format json \
	--output out/usernames.json \
	--per-page 50 \
	--max-comments 200 \
	--min-likes 0 \
	--include-replies \
	--dedupe
```

Options:

- `--url` Instagram Reel or Post URL (https://www.instagram.com/reel/... or /p/...)
- `--output` Output file path (default: `listComments.json`)
- `--data-format` Data shape: `usernames` or `detailed` (default: `usernames`)
- `--file-format` Output format: `json`, `csv`, or `txt` (default: `json`)
- `--per-page` Comments per page (default: 50)
- `--max-comments` Stop after N parent comments (default: unlimited)
- `--min-likes` Minimum like count to include a parent comment
- `--include-replies` Include replies for each parent comment
- `--dedupe/--no-dedupe` Enable/disable duplicate removal (default: dedupe enabled)
	- In `usernames` mode: dedupe by username
	- In `detailed` mode: dedupe by parent comment `id`
- `--no-progress` Disable progress bar

Output examples:

- usernames + json: `["user1", "user2", ...]`
- detailed + json: `[{ id, username, text, like_count, created_at, replies: [...] }, ...]`

---

### Notes

- Private content or expired cookies will cause missing data; refresh cookies from instagram.com.
- Too many requests can trigger rate limits (HTTP 429). The script does not bypass rate limits.
- If you use `--no-dedupe`, the usernames output may contain repeated entries when duplicates exist.

