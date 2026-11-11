"""InstaComments: fetch Instagram comments (and optionally replies) and export them.

Two backends are available:
  - graphql: the legacy /graphql/query endpoint driven by a query_hash
  - v1:      the private /api/v1/media/<id>/comments/ endpoint used by the web app
`--api auto` (the default) tries graphql first and falls back to v1 if it fails.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_PARENT_QUERY_HASH = "97b41c52301f77ce508f55e66d17620e"
DEFAULT_REPLY_QUERY_HASH = "1ee91c32fc020d44158a3192eda98247"
IG_APP_ID = "936619743392459"
BASE_URL = "https://www.instagram.com"
DEFAULT_OUTPUT_STEM = "listComments"
FILE_FORMATS = ("json", "csv", "txt")
REQUIRED_ENV = ("SESSIONID", "DS_USER_ID", "CSRFTOKEN", "MID")
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
REQUEST_TIMEOUT = (5, 30)  # (connect, read) seconds


class InstagramError(Exception):
    """Raised when Instagram returns an error or an unexpected payload."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Comment:
    id: Optional[str]
    username: Optional[str]
    text: Optional[str]
    like_count: Optional[int]
    created_at: Optional[str]  # ISO 8601 (UTC)
    replies: List["Comment"] = field(default_factory=list)

    def to_dict(self, include_replies: bool) -> Dict[str, Any]:
        d = asdict(self)
        if include_replies:
            d["replies"] = [r.to_dict(include_replies=False) for r in self.replies]
        else:
            d.pop("replies")
        return d


def to_iso(ts: Any) -> Optional[str]:
    """Convert a Unix timestamp to an ISO 8601 UTC string."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def extract_media(url: str) -> Optional[Tuple[str, str]]:
    """Return (media_type, shortcode) if URL is valid, else None. media_type is "reel" or "p"."""
    m = re.search(r"instagram\.com/(?:[^/?#]+/)?(reels?|p|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    kind = m.group(1)
    media_type = "reel" if kind.startswith("reel") else kind
    return media_type, m.group(2)


def shortcode_to_media_id(shortcode: str) -> str:
    """Decode an Instagram shortcode (base64-like) into its numeric media id."""
    media_id = 0
    for ch in shortcode[:11]:  # longer shortcodes (private posts) carry a suffix
        idx = SHORTCODE_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"Invalid shortcode character: {ch!r}")
        media_id = media_id * 64 + idx
    return str(media_id)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def load_credentials() -> Dict[str, str]:
    load_dotenv()
    creds = {name: os.getenv(name) or "" for name in REQUIRED_ENV}
    missing = [n for n, v in creds.items() if not v]
    if missing:
        raise InstagramError(
            "Missing environment variables: " + ", ".join(missing) + " (see .env.example)"
        )
    return creds


def build_session(referer_url: str, creds: Dict[str, str], retries: int = 5) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=2,  # 0, 4, 8, 16, 32s ...
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; SM-A125F) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "X-IG-App-ID": IG_APP_ID,
            "X-CSRFToken": creds["CSRFTOKEN"],
            "Referer": referer_url,
        }
    )
    session.cookies.update(
        {
            "sessionid": creds["SESSIONID"],
            "ds_user_id": creds["DS_USER_ID"],
            "csrftoken": creds["CSRFTOKEN"],
            "mid": creds["MID"],
        }
    )
    return session


def get_json(session: requests.Session, url: str, params: Optional[Dict[str, Any]] = None) -> Dict:
    """GET a JSON payload, raising InstagramError with a meaningful message on failure."""
    try:
        r = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise InstagramError(f"Network error: {e}") from e

    try:
        data = r.json()
    except ValueError:
        data = None

    if r.status_code != 200:
        msg = (data or {}).get("message") if isinstance(data, dict) else None
        if r.status_code == 429:
            raise InstagramError("Rate limited (HTTP 429) even after retries; try again later or raise --delay.")
        raise InstagramError(f"HTTP {r.status_code}: {msg or r.text[:300]}")

    if not isinstance(data, dict):
        if "login" in r.url:
            raise InstagramError("Redirected to login page: cookies are expired or invalid.")
        raise InstagramError("Non-JSON response received from Instagram.")

    if data.get("status") == "fail":
        raise InstagramError(f"Instagram error: {data.get('message') or data}")

    return data


def graphql_request(session: requests.Session, query_hash: str, variables: Dict) -> Dict:
    return get_json(
        session,
        f"{BASE_URL}/graphql/query/",
        params={"query_hash": query_hash, "variables": json.dumps(variables, separators=(",", ":"))},
    )


class Throttle:
    """Sleep between requests with a bit of jitter, to stay under rate limits."""

    def __init__(self, delay: float, sleep: Callable[[float], None] = time.sleep):
        self.delay = max(0.0, delay)
        self._sleep = sleep
        self._first = True

    def wait(self) -> None:
        if self._first:
            self._first = False
            return
        if self.delay > 0:
            self._sleep(self.delay + random.uniform(0, self.delay * 0.5))


# ---------------------------------------------------------------------------
# GraphQL backend
# ---------------------------------------------------------------------------


def parse_graphql_node(node: Dict) -> Comment:
    return Comment(
        id=node.get("id"),
        username=(node.get("owner") or {}).get("username"),
        text=node.get("text"),
        like_count=node.get("like_count", (node.get("edge_liked_by") or {}).get("count")),
        created_at=to_iso(node.get("created_at")),
    )


def fetch_graphql_replies(
    session: requests.Session,
    comment_id: str,
    first_page: Dict,
    per_page: int,
    throttle: Throttle,
    reply_hash: str,
) -> List[Comment]:
    replies = [parse_graphql_node(e.get("node", {})) for e in first_page.get("edges", [])]
    page_info = first_page.get("page_info") or {}
    while page_info.get("has_next_page") and page_info.get("end_cursor"):
        throttle.wait()
        data = graphql_request(
            session,
            reply_hash,
            {"comment_id": comment_id, "first": per_page, "after": page_info["end_cursor"]},
        )
        conn = ((data.get("data") or {}).get("comment") or {}).get("edge_threaded_comments")
        if not conn:
            break  # keep what we have rather than failing the whole run
        replies.extend(parse_graphql_node(e.get("node", {})) for e in conn.get("edges", []))
        page_info = conn.get("page_info") or {}
    return replies


def iter_graphql_comments(
    session: requests.Session,
    shortcode: str,
    per_page: int,
    include_replies: bool,
    throttle: Throttle,
) -> Iterator[Comment]:
    parent_hash = os.getenv("IG_PARENT_QUERY_HASH") or DEFAULT_PARENT_QUERY_HASH
    reply_hash = os.getenv("IG_REPLY_QUERY_HASH") or DEFAULT_REPLY_QUERY_HASH
    cursor: Optional[str] = None
    while True:
        throttle.wait()
        variables: Dict[str, Any] = {"shortcode": shortcode, "first": per_page}
        if cursor:
            variables["after"] = cursor
        data = graphql_request(session, parent_hash, variables)

        media = (data.get("data") or {}).get("shortcode_media")
        if not media:
            raise InstagramError(
                "'shortcode_media' missing (invalid URL, private content, expired cookies "
                "or outdated query hash)."
            )
        conn = media.get("edge_media_to_parent_comment")
        if conn is None:
            raise InstagramError("'edge_media_to_parent_comment' missing from GraphQL response.")

        for edge in conn.get("edges", []):
            node = edge.get("node", {})
            comment = parse_graphql_node(node)
            if include_replies:
                comment.replies = fetch_graphql_replies(
                    session, node.get("id"), node.get("edge_threaded_comments") or {},
                    per_page, throttle, reply_hash,
                )
            yield comment

        page_info = conn.get("page_info") or {}
        cursor = page_info.get("end_cursor")
        if not page_info.get("has_next_page") or not cursor:
            return


# ---------------------------------------------------------------------------
# v1 (private web API) backend
# ---------------------------------------------------------------------------


def parse_v1_comment(c: Dict) -> Comment:
    return Comment(
        id=str(c["pk"]) if c.get("pk") is not None else None,
        username=(c.get("user") or {}).get("username"),
        text=c.get("text"),
        like_count=c.get("comment_like_count"),
        created_at=to_iso(c.get("created_at")),
    )


def fetch_v1_replies(
    session: requests.Session, media_id: str, comment: Dict, throttle: Throttle
) -> List[Comment]:
    if not comment.get("child_comment_count"):
        return []
    replies: List[Comment] = []
    seen: set = set()
    max_id: Optional[str] = None
    while True:
        throttle.wait()
        params = {"max_id": max_id} if max_id else None
        data = get_json(
            session, f"{BASE_URL}/api/v1/media/{media_id}/comments/{comment['pk']}/child_comments/", params
        )
        for c in data.get("child_comments", []):
            r = parse_v1_comment(c)
            if r.id not in seen:
                seen.add(r.id)
                replies.append(r)
        max_id = data.get("next_max_child_cursor")
        if not data.get("has_more_tail_child_comments") or not max_id:
            return replies


def iter_v1_comments(
    session: requests.Session,
    shortcode: str,
    include_replies: bool,
    throttle: Throttle,
) -> Iterator[Comment]:
    media_id = shortcode_to_media_id(shortcode)
    min_id: Optional[str] = None
    while True:
        throttle.wait()
        params: Dict[str, Any] = {"can_support_threading": "true", "permalink_enabled": "false"}
        if min_id:
            params["min_id"] = min_id
        data = get_json(session, f"{BASE_URL}/api/v1/media/{media_id}/comments/", params)

        for c in data.get("comments", []):
            comment = parse_v1_comment(c)
            if include_replies:
                comment.replies = fetch_v1_replies(session, media_id, c, throttle)
            yield comment

        min_id = data.get("next_min_id")
        if not min_id:
            return


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def collect_comments(
    source: Iterator[Comment],
    results: List[Comment],
    max_comments: Optional[int] = None,
    min_likes: int = 0,
    show_progress: bool = True,
) -> None:
    """Drain `source` into `results` (mutated in place, so partial data survives errors)."""
    with tqdm(desc="Fetching comments", unit="comments", disable=not show_progress) as progress:
        for comment in source:
            if (comment.like_count or 0) < min_likes:
                continue
            results.append(comment)
            progress.update(1)
            if max_comments and len(results) >= max_comments:
                return


def fetch_comments(
    session: requests.Session,
    shortcode: str,
    api: str,
    per_page: int,
    include_replies: bool,
    throttle: Throttle,
    results: List[Comment],
    **collect_kwargs: Any,
) -> None:
    def graphql() -> Iterator[Comment]:
        return iter_graphql_comments(session, shortcode, per_page, include_replies, throttle)

    def v1() -> Iterator[Comment]:
        return iter_v1_comments(session, shortcode, include_replies, throttle)

    if api == "graphql":
        collect_comments(graphql(), results, **collect_kwargs)
    elif api == "v1":
        collect_comments(v1(), results, **collect_kwargs)
    else:  # auto
        try:
            collect_comments(graphql(), results, **collect_kwargs)
        except InstagramError as e:
            if results:
                raise  # graphql worked for a while: don't mix two sources
            print(f"[i] GraphQL backend failed ({e}); falling back to v1 API.", file=sys.stderr)
            collect_comments(v1(), results, **collect_kwargs)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def dedupe_by_key(items: List[Comment], key: str = "id") -> List[Comment]:
    seen: set = set()
    out: List[Comment] = []
    for it in items:
        k = getattr(it, key)
        if k is None or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def extract_usernames(comments: List[Comment], include_replies: bool, dedupe: bool) -> List[str]:
    """Usernames of comment authors (and reply authors if requested), sorted case-insensitively."""
    names: List[str] = []
    for c in comments:
        if c.username:
            names.append(c.username)
        if include_replies:
            names.extend(r.username for r in c.replies if r.username)
    if dedupe:
        names = list(dict.fromkeys(names))
    return sorted(names, key=str.lower)


def resolve_output(output: Optional[str], file_format: Optional[str]) -> Tuple[str, str]:
    """Reconcile --output and --file-format so the extension and the content always match."""
    if output is None:
        fmt = file_format or "json"
        return f"{DEFAULT_OUTPUT_STEM}.{fmt}", fmt
    ext = os.path.splitext(output)[1].lstrip(".").lower()
    if file_format is None:
        return output, ext if ext in FILE_FORMATS else "json"
    if ext in FILE_FORMATS and ext != file_format:
        print(
            f"[!] Warning: --file-format {file_format} does not match extension .{ext} of {output}",
            file=sys.stderr,
        )
    return output, file_format


def write_output(
    path: str,
    file_format: str,
    data_format: str,
    comments: List[Comment],
    include_replies: bool,
    dedupe: bool,
    flatten_replies: bool = False,
) -> int:
    """Write the export and return the number of records written."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    if data_format == "usernames":
        names = extract_usernames(comments, include_replies, dedupe)
        if file_format == "json":
            with open(path, "w", encoding="utf-8") as f:
                json.dump(names, f, ensure_ascii=False, indent=2)
        elif file_format == "csv":
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["username"])
                w.writerows([n] for n in names)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(f"{n}\n" for n in names)
        return len(names)

    if file_format == "json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump([c.to_dict(include_replies) for c in comments], f, ensure_ascii=False, indent=2)
        return len(comments)

    if file_format == "csv":
        rows = 0
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "parent_id", "username", "text", "like_count", "created_at", "reply_count"])
            for c in comments:
                w.writerow([c.id, "", c.username, c.text or "", c.like_count, c.created_at, len(c.replies)])
                rows += 1
                if flatten_replies:
                    for r in c.replies:
                        w.writerow([r.id, c.id, r.username, r.text or "", r.like_count, r.created_at, ""])
                        rows += 1
        return rows

    with open(path, "w", encoding="utf-8") as f:
        for c in comments:
            f.write(f"@{c.username}: {c.text} (likes={c.like_count})\n")
            if include_replies:
                for r in c.replies:
                    f.write(f"    ↳ @{r.username}: {r.text} (likes={r.like_count})\n")
    return len(comments)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EXAMPLES = (
    "Examples:\n"
    "  python instacomments.py --url https://www.instagram.com/reel/SHORT/\n"
    "  python instacomments.py --url https://www.instagram.com/p/SHORT/ --output out/usernames.csv\n"
    "  python instacomments.py --url https://www.instagram.com/reel/SHORT/ --data-format detailed \\\n"
    "      --include-replies --min-likes 5 --max-comments 200 --output out/comments.json\n"
)


def make_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Instagram comments scraper (parent comments, optional replies)",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--url", help="Instagram Reel or Post URL (https://www.instagram.com/reel/... or /p/...)")
    p.add_argument("--output", help=f"Output file path (default: {DEFAULT_OUTPUT_STEM}.<format>). "
                                    "The format is inferred from the extension when --file-format is omitted.")
    p.add_argument("--data-format", choices=["usernames", "detailed"], default="usernames", help="Data shape to export")
    p.add_argument("--file-format", choices=FILE_FORMATS, help="Output format (default: from --output extension, else json)")
    p.add_argument("--api", choices=["auto", "graphql", "v1"], default="auto",
                   help="Backend: graphql, v1, or auto (graphql then v1 fallback). Default: auto")
    p.add_argument("--per-page", type=int, default=50, help="Comments per page for graphql (default: 50)")
    p.add_argument("--max-comments", type=int, help="Stop after N parent comments (default: no limit)")
    p.add_argument("--min-likes", type=int, default=0, help="Only keep parent comments with at least N likes")
    p.add_argument("--include-replies", action="store_true", help="Fetch replies (all pages) for each parent comment")
    p.add_argument("--flatten-replies", action="store_true",
                   help="Detailed CSV only: write one row per reply, linked by parent_id")
    p.add_argument("--dedupe", action=argparse.BooleanOptionalAction, default=True,
                   help="Remove duplicates (by username in 'usernames' mode, by id in 'detailed' mode). Default: on")
    p.add_argument("--delay", type=float, default=1.5,
                   help="Seconds to wait between requests, plus up to 50%% jitter (default: 1.5)")
    p.add_argument("--retries", type=int, default=5, help="Retries on 429/5xx with exponential backoff (default: 5)")
    p.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    return p


def prompt_url() -> str:
    print(make_arg_parser().format_help())
    try:
        return input("Enter an Instagram Reel or Post URL: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(130)


def main(argv: Optional[List[str]] = None) -> int:
    args = make_arg_parser().parse_args(argv)

    url = args.url or prompt_url()
    media = extract_media(url)
    if not media:
        print("[!] Invalid URL. Example: https://www.instagram.com/reel/<short>/ or https://www.instagram.com/p/<short>/",
              file=sys.stderr)
        return 2
    media_type, shortcode = media

    try:
        creds = load_credentials()
    except InstagramError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2

    output_path, file_format = resolve_output(args.output, args.file_format)
    session = build_session(f"{BASE_URL}/{media_type}/{shortcode}/", creds, retries=max(0, args.retries))
    throttle = Throttle(args.delay)

    comments: List[Comment] = []
    exit_code = 0
    try:
        fetch_comments(
            session, shortcode, args.api, max(1, args.per_page), args.include_replies, throttle, comments,
            max_comments=args.max_comments, min_likes=max(0, args.min_likes), show_progress=not args.no_progress,
        )
    except InstagramError as e:
        print(f"[!] {e}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        exit_code = 130

    if not comments:
        print("[!] No comments fetched; nothing written.", file=sys.stderr)
        return exit_code or 1
    if exit_code:
        print(f"[i] Saving the {len(comments)} comments fetched before the stop.", file=sys.stderr)

    if args.dedupe:
        comments = dedupe_by_key(comments, "id")

    try:
        count = write_output(
            output_path, file_format, args.data_format, comments,
            include_replies=args.include_replies, dedupe=args.dedupe, flatten_replies=args.flatten_replies,
        )
    except OSError as e:
        print(f"[!] Failed to write output: {e}", file=sys.stderr)
        return 1

    print(f"[+] Saved {count} records to {output_path}" + (" (partial)" if exit_code else ""))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
