import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import instacomments as ic  # noqa: E402


# --------------------------------------------------------------------------- helpers


class FakeResponse:
    def __init__(self, payload, status=200, url="https://www.instagram.com/graphql/query/"):
        self._payload = payload
        self.status_code = status
        self.url = url
        self.text = json.dumps(payload) if payload is not None else "<html>login</html>"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Returns queued responses in order and records the calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if not self.responses:
            raise AssertionError(f"unexpected request: {url} {params}")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def gql_page(nodes, has_next=False, cursor=None):
    return FakeResponse({
        "data": {"shortcode_media": {"edge_media_to_parent_comment": {
            "edges": [{"node": n} for n in nodes],
            "page_info": {"has_next_page": has_next, "end_cursor": cursor},
        }}}
    })


def node(id_, user, likes=0, replies=None, reply_next=None):
    n = {"id": id_, "owner": {"username": user}, "text": f"text {id_}", "like_count": likes, "created_at": 0}
    if replies is not None:
        n["edge_threaded_comments"] = {
            "edges": [{"node": r} for r in replies],
            "page_info": {"has_next_page": reply_next is not None, "end_cursor": reply_next},
        }
    return n


NO_WAIT = ic.Throttle(0)


# --------------------------------------------------------------------------- URL helpers


@pytest.mark.parametrize("url,expected", [
    ("https://www.instagram.com/reel/ABC123/", ("reel", "ABC123")),
    ("https://www.instagram.com/reels/ABC123/", ("reel", "ABC123")),
    ("https://instagram.com/p/A-b_9/?igsh=xyz", ("p", "A-b_9")),
    ("https://www.instagram.com/someuser/p/XYZ/", ("p", "XYZ")),
    ("https://www.instagram.com/tv/QQ/", ("tv", "QQ")),
    ("https://www.instagram.com/someuser/", None),
    ("not a url", None),
])
def test_extract_media(url, expected):
    assert ic.extract_media(url) == expected


def test_shortcode_to_media_id():
    assert ic.shortcode_to_media_id("B") == "1"
    assert ic.shortcode_to_media_id("BA") == "64"
    # Round-trip against an independent base64 encoder
    def encode(n):
        s = ""
        while n:
            n, r = divmod(n, 64)
            s = ic.SHORTCODE_ALPHABET[r] + s
        return s
    for media_id in (2503692177931372118, 3141592653589793238, 64 ** 10 + 7):
        assert ic.shortcode_to_media_id(encode(media_id)) == str(media_id)
    # private-post shortcodes carry a suffix after 11 chars, which must be ignored
    assert ic.shortcode_to_media_id("CK-5sVMhgZW" + "Xyz") == ic.shortcode_to_media_id("CK-5sVMhgZW")
    with pytest.raises(ValueError):
        ic.shortcode_to_media_id("a!b")


def test_to_iso():
    assert ic.to_iso(0) == "1970-01-01T00:00:00+00:00"
    assert ic.to_iso(None) is None
    assert ic.to_iso("garbage") is None


# --------------------------------------------------------------------------- HTTP layer


def test_get_json_errors():
    with pytest.raises(ic.InstagramError, match="429"):
        ic.get_json(FakeSession([FakeResponse({}, status=429)]), "u")
    with pytest.raises(ic.InstagramError, match="checkpoint"):
        ic.get_json(FakeSession([FakeResponse({"message": "checkpoint_required"}, status=400)]), "u")
    with pytest.raises(ic.InstagramError, match="login"):
        ic.get_json(FakeSession([FakeResponse(None, url="https://www.instagram.com/accounts/login/")]), "u")
    with pytest.raises(ic.InstagramError, match="feedback_required"):
        ic.get_json(FakeSession([FakeResponse({"status": "fail", "message": "feedback_required"})]), "u")
    import requests
    with pytest.raises(ic.InstagramError, match="Network"):
        ic.get_json(FakeSession([requests.ConnectionError("boom")]), "u")


def test_get_json_passes_timeout():
    s = FakeSession([FakeResponse({"ok": 1})])
    ic.get_json(s, "u")
    assert s.calls[0][2] == ic.REQUEST_TIMEOUT


def test_throttle_skips_first_wait_then_sleeps():
    slept = []
    t = ic.Throttle(2, sleep=slept.append)
    t.wait()
    t.wait()
    assert len(slept) == 1 and 2 <= slept[0] <= 3


def test_build_session_sets_cookies_and_retries():
    creds = {"SESSIONID": "s", "DS_USER_ID": "1", "CSRFTOKEN": "c", "MID": "m"}
    s = ic.build_session("https://www.instagram.com/p/X/", creds, retries=3)
    assert s.cookies["sessionid"] == "s"
    assert s.headers["X-CSRFToken"] == "c"
    retry = s.get_adapter("https://www.instagram.com").max_retries
    assert retry.total == 3 and 429 in retry.status_forcelist


# --------------------------------------------------------------------------- GraphQL backend


def test_graphql_pagination_and_replies():
    session = FakeSession([
        gql_page([node("1", "alice", replies=[node("1a", "bob")], reply_next="rc")], has_next=True, cursor="c1"),
        FakeResponse({"data": {"comment": {"edge_threaded_comments": {
            "edges": [{"node": node("1b", "carol")}], "page_info": {"has_next_page": False}}}}}),
        gql_page([node("2", "dave")]),
    ])
    out = list(ic.iter_graphql_comments(session, "X", 50, True, NO_WAIT))
    assert [c.username for c in out] == ["alice", "dave"]
    assert [r.username for r in out[0].replies] == ["bob", "carol"]
    assert out[0].created_at == "1970-01-01T00:00:00+00:00"
    assert json.loads(session.calls[2][1]["variables"])["after"] == "c1"


def test_graphql_missing_media_raises():
    with pytest.raises(ic.InstagramError, match="shortcode_media"):
        list(ic.iter_graphql_comments(FakeSession([FakeResponse({"data": {}})]), "X", 50, False, NO_WAIT))


# --------------------------------------------------------------------------- v1 backend


def test_v1_pagination_and_replies():
    session = FakeSession([
        FakeResponse({"comments": [
            {"pk": 1, "user": {"username": "alice"}, "text": "hi", "comment_like_count": 3,
             "created_at": 0, "child_comment_count": 2},
        ], "next_min_id": "m1"}),
        FakeResponse({"child_comments": [{"pk": 11, "user": {"username": "bob"}}],
                      "has_more_tail_child_comments": True, "next_max_child_cursor": "k"}),
        FakeResponse({"child_comments": [{"pk": 12, "user": {"username": "carol"}}],
                      "has_more_tail_child_comments": False}),
        FakeResponse({"comments": [{"pk": 2, "user": {"username": "dave"}}]}),
    ])
    out = list(ic.iter_v1_comments(session, "B", True, NO_WAIT))
    assert [c.username for c in out] == ["alice", "dave"]
    assert out[0].id == "1" and out[0].like_count == 3
    assert [r.username for r in out[0].replies] == ["bob", "carol"]
    assert "/media/1/comments/" in session.calls[0][0]
    assert session.calls[3][1]["min_id"] == "m1"


# --------------------------------------------------------------------------- orchestration


def test_collect_filters_and_limits():
    src = iter([ic.Comment(str(i), f"u{i}", "", i, None) for i in range(10)])
    res = []
    ic.collect_comments(src, res, max_comments=3, min_likes=5, show_progress=False)
    assert [c.like_count for c in res] == [5, 6, 7]


def test_collect_keeps_partial_results_on_error():
    def src():
        yield ic.Comment("1", "a", "", 0, None)
        raise ic.InstagramError("boom")

    res = []
    with pytest.raises(ic.InstagramError):
        ic.collect_comments(src(), res, show_progress=False)
    assert len(res) == 1


def test_auto_falls_back_to_v1_when_graphql_fails_immediately():
    session = FakeSession([
        FakeResponse({"data": {}}),  # graphql: no shortcode_media
        FakeResponse({"comments": [{"pk": 5, "user": {"username": "zed"}}]}),
    ])
    res = []
    ic.fetch_comments(session, "B", "auto", 50, False, NO_WAIT, res, show_progress=False)
    assert [c.username for c in res] == ["zed"]


def test_auto_does_not_fall_back_after_partial_graphql():
    session = FakeSession([gql_page([node("1", "a")], has_next=True, cursor="c"), FakeResponse({}, status=500)])
    res = []
    with pytest.raises(ic.InstagramError):
        ic.fetch_comments(session, "B", "auto", 50, False, NO_WAIT, res, show_progress=False)
    assert len(res) == 1


# --------------------------------------------------------------------------- output


@pytest.mark.parametrize("output,fmt,expected", [
    (None, None, ("listComments.json", "json")),
    (None, "csv", ("listComments.csv", "csv")),
    ("out/a.txt", None, ("out/a.txt", "txt")),
    ("out/a.dat", None, ("out/a.dat", "json")),
    ("out/a.csv", "csv", ("out/a.csv", "csv")),
])
def test_resolve_output(output, fmt, expected):
    assert ic.resolve_output(output, fmt) == expected


def sample():
    return [
        ic.Comment("1", "Bob", "hello\nworld", 2, "t", replies=[ic.Comment("1a", "alice", "re", 0, "t")]),
        ic.Comment("2", "alice", "é", 1, "t"),
        ic.Comment("3", "bob2", "x", 0, "t"),
    ]


def test_usernames_include_reply_authors_and_dedupe():
    assert ic.extract_usernames(sample(), include_replies=True, dedupe=True) == ["alice", "Bob", "bob2"]
    assert ic.extract_usernames(sample(), include_replies=True, dedupe=False) == ["alice", "alice", "Bob", "bob2"]
    assert ic.extract_usernames(sample(), include_replies=False, dedupe=True) == ["alice", "Bob", "bob2"]


def test_write_detailed_json(tmp_path):
    p = tmp_path / "o.json"
    n = ic.write_output(str(p), "json", "detailed", sample(), include_replies=True, dedupe=True)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert n == 3 and data[0]["replies"][0] == {
        "id": "1a", "username": "alice", "text": "re", "like_count": 0, "created_at": "t"}
    ic.write_output(str(p), "json", "detailed", sample(), include_replies=False, dedupe=True)
    assert "replies" not in json.loads(p.read_text(encoding="utf-8"))[0]


def test_write_detailed_csv_flattened_with_bom(tmp_path):
    p = tmp_path / "sub" / "o.csv"
    n = ic.write_output(str(p), "csv", "detailed", sample(), include_replies=True, dedupe=True, flatten_replies=True)
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    assert n == 4
    assert rows[0][:2] == ["id", "parent_id"]
    assert rows[2][:3] == ["1a", "1", "alice"]
    # multiline text is preserved inside a quoted CSV field
    with open(p, encoding="utf-8-sig", newline="") as f:
        assert list(csv.reader(f))[1][3] == "hello\nworld"


def test_write_usernames_txt(tmp_path):
    p = tmp_path / "u.txt"
    assert ic.write_output(str(p), "txt", "usernames", sample(), include_replies=False, dedupe=True) == 3
    assert p.read_text(encoding="utf-8").splitlines() == ["alice", "Bob", "bob2"]


# --------------------------------------------------------------------------- CLI


def test_main_invalid_url():
    assert ic.main(["--url", "https://example.com/x"]) == 2


def test_main_missing_env(monkeypatch):
    for k in ic.REQUIRED_ENV:
        monkeypatch.setenv(k, "")
    monkeypatch.setattr(ic, "load_dotenv", lambda: None)
    assert ic.main(["--url", "https://www.instagram.com/p/B/"]) == 2


def test_main_saves_partial_results(monkeypatch, tmp_path):
    for k in ic.REQUIRED_ENV:
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(ic, "load_dotenv", lambda: None)
    session = FakeSession([gql_page([node("1", "a"), node("2", "b")], has_next=True, cursor="c"),
                           FakeResponse({}, status=500)])
    monkeypatch.setattr(ic, "build_session", lambda *a, **k: session)
    out = tmp_path / "r.json"
    code = ic.main(["--url", "https://www.instagram.com/p/B/", "--output", str(out),
                    "--delay", "0", "--no-progress", "--api", "graphql"])
    assert code == 1
    assert json.loads(out.read_text()) == ["a", "b"]
