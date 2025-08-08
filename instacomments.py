import argparse
import csv
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple, Iterable

import requests
from dotenv import load_dotenv
from tqdm import tqdm


# Load environment variables from .env into os.environ
load_dotenv()

SESSIONID = os.getenv("SESSIONID")
DS_USER_ID = os.getenv("DS_USER_ID")
CSRFTOKEN = os.getenv("CSRFTOKEN")
MID = os.getenv("MID")

# GraphQL configuration
PARENT_QUERY_HASH = "97b41c52301f77ce508f55e66d17620e"


def extract_media(url: str) -> Optional[Tuple[str, str]]:
    """Return (media_type, shortcode) if URL is valid, else None.

    media_type is "reel" or "p" (post).
    """
    m = re.search(r"instagram\.com/(reel|p)/([^/?#]+)", url)
    if not m:
        return None
    return m.group(1), m.group(2)


def build_headers(referer_url: str, cookies_str: str) -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; SM-A125F) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "X-IG-App-ID": "936619743392459",
        "Referer": referer_url,
        "Cookie": cookies_str,
    }


def graphql_request(query_hash: str, variables: Dict, headers: Dict[str, str]) -> Dict:
    var_str = json.dumps(variables, separators=(",", ":"))
    url = (
        f"https://www.instagram.com/graphql/query/"
        f"?query_hash={query_hash}"
        f"&variables={requests.utils.quote(var_str)}"
    )
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        print(f"[!] HTTP {r.status_code} error for {query_hash}: {r.text[:300]}...")
        sys.exit(1)
    try:
        return r.json()
    except ValueError:
        print("[!] Non-JSON response received from Instagram.")
        sys.exit(1)


def parse_comment_node(node: Dict, include_replies: bool = False) -> Dict:
    """Extract useful data from a parent comment node.

    Returns a dict: {
        id, username, text, like_count, created_at, replies: [ {...}, ... ]
    }
    """
    owner = node.get("owner", {})
    item = {
        "id": node.get("id"),
        "username": owner.get("username"),
        "text": node.get("text"),
        "like_count": node.get("like_count"),
        "created_at": node.get("created_at"),
    }

    if include_replies:
        replies_info = node.get("edge_threaded_comments", {})
        reply_edges = replies_info.get("edges", [])
        replies = []
        for e in reply_edges:
            rnode = e.get("node", {})
            rowner = rnode.get("owner", {})
            replies.append(
                {
                    "id": rnode.get("id"),
                    "username": rowner.get("username"),
                    "text": rnode.get("text"),
                    "like_count": rnode.get("like_count"),
                    "created_at": rnode.get("created_at"),
                }
            )
        item["replies"] = replies

    return item


def fetch_parent_comments(
    shortcode: str,
    headers: Dict[str, str],
    per_page: int,
    include_replies: bool,
    max_comments: Optional[int] = None,
    min_likes: int = 0,
    show_progress: bool = True,
) -> List[Dict]:
    """Iterate pages of parent comments and return a list of dicts."""
    results: List[Dict] = []
    has_next = True
    cursor: Optional[str] = None

    progress = tqdm(desc="Fetching comments", unit="comments", disable=not show_progress)

    while has_next:
        variables = {"shortcode": shortcode, "first": per_page}
        if cursor:
            variables["after"] = cursor

        data = graphql_request(PARENT_QUERY_HASH, variables, headers)

        try:
            media = data.get("data", {}).get("shortcode_media")
            if not media:
                print("[!] 'shortcode_media' missing (invalid URL, private content or expired cookies).")
                break

            edge_info = media["edge_media_to_parent_comment"]
            edges = edge_info.get("edges", [])
            for edge in edges:
                node = edge.get("node", {})
                item = parse_comment_node(node, include_replies=include_replies)
                if (item.get("like_count") or 0) >= min_likes:
                    results.append(item)

                if max_comments and len(results) >= max_comments:
                    has_next = False
                    break
        except KeyError as e:
            print(f"[!] Error parsing comment data: {e}")
            break

        page_info = edge_info.get("page_info", {})
        has_next = bool(page_info.get("has_next_page")) and not (max_comments and len(results) >= max_comments)
        cursor = page_info.get("end_cursor")

        progress.n = len(results)
        progress.refresh()

    progress.close()
    return results


def to_usernames(comments: List[Dict]) -> List[str]:
    """Return unique usernames (first-seen order)."""
    seen = set()
    out: List[str] = []
    for c in comments:
        u = c.get("username")
        if u and u not in seen:
            out.append(u)
            seen.add(u)
    return out


def usernames_with_duplicates(comments: List[Dict]) -> List[str]:
    """Return usernames preserving duplicates and order."""
    out: List[str] = []
    for c in comments:
        u = c.get("username")
        if u:
            out.append(u)
    return out


def dedupe_by_key(items: Iterable[Dict], key: str) -> List[Dict]:
    """Dedupe list of dicts by a given key, preserving first occurrence."""
    seen = set()
    result: List[Dict] = []
    for it in items:
        k = it.get(key)
        if k is None or k in seen:
            continue
        seen.add(k)
        result.append(it)
    return result


def write_output(
    data_format: str,
    file_format: str,
    output_path: str,
    comments: List[Dict],
    usernames: Optional[List[str]] = None,
) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if data_format == "usernames":
        # If a usernames payload is supplied, use it directly; otherwise compute unique set
        payload = usernames if usernames is not None else to_usernames(comments)
        if file_format == "json":
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        elif file_format == "csv":
            with open(output_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["username"])
                for u in payload:
                    writer.writerow([u])
        else:  # txt
            with open(output_path, "w", encoding="utf-8") as f:
                for u in payload:
                    f.write(f"{u}\n")
        return

    # Detailed
    if file_format == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(comments, f, ensure_ascii=False, indent=2)
    elif file_format == "csv":
        # CSV for parent comments only; replies (if any) are not flattened
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "username", "text", "like_count", "created_at", "reply_count"])
            for c in comments:
                reply_count = len(c.get("replies", [])) if isinstance(c.get("replies"), list) else 0
                writer.writerow([
                    c.get("id"),
                    c.get("username"),
                    (c.get("text") or "").replace("\n", " ").strip(),
                    c.get("like_count"),
                    c.get("created_at"),
                    reply_count,
                ])
    else:  # txt
        with open(output_path, "w", encoding="utf-8") as f:
            for c in comments:
                f.write(f"@{c.get('username')}: {c.get('text')} (likes={c.get('like_count')})\n")


def validate_env() -> None:
    missing = [name for name, val in {
        "SESSIONID": SESSIONID,
        "DS_USER_ID": DS_USER_ID,
        "CSRFTOKEN": CSRFTOKEN,
        "MID": MID,
    }.items() if not val]
    if missing:
        print("[!] Missing environment variables: " + ", ".join(missing))
        print("    Add them to a .env file (see .env.example).")
        sys.exit(2)


def make_arg_parser() -> argparse.ArgumentParser:
    epilog = (
        "Examples:\n"
        "  python instacomments.py --url https://www.instagram.com/reel/SHORT/\n"
        "  python instacomments.py --url https://www.instagram.com/p/SHORT/ --data-format usernames --file-format csv --output out/usernames.csv\n"
        "  python instacomments.py --url https://www.instagram.com/reel/SHORT/ --data-format detailed --include-replies --min-likes 5 --max-comments 200\n"
    )
    parser = argparse.ArgumentParser(
        description="Instagram comments scraper (parent comments, optional replies)",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", help="Instagram Reel or Post URL (https://www.instagram.com/reel/... or /p/...)")
    parser.add_argument("--output", default="listComments.json", help="Output file path (default: listComments.json)")
    parser.add_argument("--data-format", choices=["usernames", "detailed"], default="usernames", help="Data shape to export")
    parser.add_argument("--file-format", choices=["json", "csv", "txt"], default="json", help="Output file format")
    parser.add_argument("--per-page", type=int, default=50, help="Comments per page (default: 50)")
    parser.add_argument("--max-comments", type=int, default=None, help="Stop after N parent comments (default: no limit)")
    parser.add_argument("--min-likes", type=int, default=0, help="Filter: minimum likes for a parent comment")
    parser.add_argument("--include-replies", action="store_true", help="Include available replies for each parent comment")
    # Toggle deduplication of duplicates
    try:
        # Python 3.9+: BooleanOptionalAction allows --dedupe / --no-dedupe
        action_bool = argparse.BooleanOptionalAction
    except AttributeError:  # pragma: no cover
        action_bool = None  # type: ignore
    if action_bool:
        parser.add_argument(
            "--dedupe",
            action=action_bool,  # type: ignore[arg-type]
            default=True,
            help=(
                "Enable/disable duplicate removal (by username in 'usernames' mode, "
                "by id in 'detailed' mode). Default: enabled."
            ),
        )
    else:
        # Fallback for very old Python: only provide positive flag
        parser.add_argument("--dedupe", action="store_true", help="Remove duplicates in the output")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    return parser


def main() -> None:
        parser = make_arg_parser()
        # Show a nice guide when no arguments are provided
        if len(sys.argv) == 1:
                print("""
============================================================
 InstaComments — Instagram Comment Scraper
============================================================
Quick start:
    python instacomments.py --url https://www.instagram.com/reel/SHORT/

Popular options:
    --data-format usernames|detailed   Data shape to export (default: usernames)
    --file-format json|csv|txt         Output format (default: json)
    --output PATH                      Output file path (default: listComments.json)
    --per-page N                       Comments per page (default: 50)
    --max-comments N                   Stop after N parent comments
    --min-likes N                      Only include comments with at least N likes
    --include-replies                  Include replies for each parent comment
    --dedupe/--no-dedupe              Toggle duplicate removal (default: dedupe)
    --no-progress                      Disable progress bar

Examples:
    python instacomments.py --url https://www.instagram.com/reel/SHORT/
    python instacomments.py --url https://www.instagram.com/p/SHORT/ --data-format usernames --file-format csv --output out/usernames.csv
    python instacomments.py --url https://www.instagram.com/reel/SHORT/ --data-format detailed --include-replies --min-likes 5 --max-comments 200

Tip: run with --help to see the full help and descriptions.
============================================================
""")

        args = parser.parse_args()

        url = args.url
        if not url:
            # Interactive fallback after showing the guide
            print("Enter an Instagram Reel or Post URL (or run with --help for examples):")
            url = input().strip()

        media = extract_media(url)
        if not media:
            print("[!] Invalid URL. Example: https://www.instagram.com/reel/<short>/ or https://www.instagram.com/p/<short>/")
            sys.exit(2)

        media_type, shortcode = media

        validate_env()

        cookies_str = f"sessionid={SESSIONID}; ds_user_id={DS_USER_ID}; csrftoken={CSRFTOKEN}; mid={MID};"
        referer_url = f"https://www.instagram.com/{media_type}/{shortcode}/"
        headers = build_headers(referer_url, cookies_str)

        comments = fetch_parent_comments(
            shortcode=shortcode,
            headers=headers,
            per_page=max(1, args.per_page),
            include_replies=args.include_replies,
            max_comments=args.max_comments,
            min_likes=max(0, args.min_likes),
            show_progress=not args.no_progress,
        )

        # Apply deduplication depending on data format
        if args.data_format == "detailed":
            # Dedupe parent comments by id when enabled
            if getattr(args, "dedupe", True):
                comments = dedupe_by_key(comments, key="id")

        # Prepare usernames payload (with or without dedupe)
        usernames_payload: Optional[List[str]] = None
        if args.data_format == "usernames":
            if getattr(args, "dedupe", True):
                usernames_payload = to_usernames(comments)
            else:
                usernames_payload = usernames_with_duplicates(comments)
            # Sort alphabetically for a stable output (keeps duplicates if present)
            usernames_payload.sort(key=lambda s: s.lower())

        try:
            write_output(
                data_format=args.data_format,
                file_format=args.file_format,
                output_path=args.output,
                comments=comments,
                usernames=usernames_payload,
            )
        except OSError as e:
            print(f"[!] Failed to write output: {e}")
            sys.exit(1)

        count = (
            len(usernames_payload) if args.data_format == "usernames" and usernames_payload is not None else len(comments)
        )
        print(f"[+] Saved {count} records to {args.output}")


if __name__ == "__main__":
    main()
