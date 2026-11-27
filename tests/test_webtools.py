import pytest

from crawler.http import Blocked
from crawler.webtools import PageReader, SearchQuota, Tavily

from conftest import UA, FakeWeb, make_http


def test_tavily_basic_search_in_korea_and_result_shape():
    sent = []
    t = Tavily("tvly-x", post=lambda body: sent.append(body) or (200, {"results": [
        {"url": "https://a", "title": "A", "content": "조각", "score": 0.9, "published_date": "2026-11-01"},
        {"title": "주소 없음"},
        {"url": "https://open.spotify.com/artist/x", "title": "S", "content": "c"},          # Spotify 는 안 씀
        {"url": "https://m2.indistreet.com/albums/1", "title": "I", "content": "c"}]}))
    assert t.search("실리카겔 신보") == [{"url": "https://a", "title": "A", "content": "조각", "published_date": "2026-11-01"},
                                     {"url": "https://m2.indistreet.com/albums/1", "title": "I", "content": "c", "published_date": None}]
    [body] = sent
    assert {k: v for k, v in body.items() if k != "exclude_domains"} == {
        "query": "실리카겔 신보", "search_depth": "basic", "max_results": 5, "country": "south korea",
        "include_published_date": True}                                 # basic = 1크레딧 (auto_parameters 는 2크레딧이라 안 씀)
    assert body["exclude_domains"] == ["spotify.com", "open.spotify.com"]                  # Spotify 만 뺌 (결정)
    assert t.searches == 1


@pytest.mark.parametrize("status,error", [(432, SearchQuota), (433, SearchQuota), (500, RuntimeError), (401, RuntimeError)])
def test_tavily_errors(status, error):
    t = Tavily("tvly-x", post=lambda body: (status, {"detail": {"error": "x"}}))
    with pytest.raises(error):
        t.search("q")
    assert t.searches == 0



def test_tavily_stops_at_the_run_budget():
    t = Tavily("tvly-x", post=lambda body: (200, {"results": []}), budget=2)
    t.search("a")
    t.search("b")
    with pytest.raises(SearchQuota):
        t.search("c")
    assert t.searches == 2 and t.exhausted

def public(host, _):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def reader(sessions, web, call, resolve=public):
    return PageReader(make_http(sessions, web), UA, call=call, resolve=resolve)


def test_page_reader_calls_mcp_without_evasion_and_counts(sessions):
    calls = []
    r = reader(sessions, FakeWeb(), lambda name, args: calls.append((name, args)) or
               {"status": 200, "content": ["# 제목", "본문"], "url": "https://shop.example/p/1"})
    assert r.read("https://shop.example/p/1") == ("https://shop.example/p/1", "# 제목\n\n본문")
    [(name, args)] = calls
    assert name == "make_request"
    assert args["impersonate"] is None and args["stealthy_headers"] is False              # 우회 끔
    assert args["headers"] == {"User-Agent": UA} and args["follow_redirects"] == "safe"   # 사설 IP 로 가는 리다이렉트 금지
    assert r.fetches == 1


def test_page_reader_obeys_robots_and_blocks_inner_addresses(sessions):
    web = FakeWeb()
    web.add("shop.example/robots.txt", "User-agent: *\nDisallow: /private\n")
    called = []
    r = reader(sessions, web, lambda name, args: called.append(args) or {"status": 200, "content": [], "url": args["url"]})
    with pytest.raises(Blocked):
        r.read("https://shop.example/private/1")                                          # robots.txt 가 막음

    inner = reader(sessions, web, lambda *a: called.append(a), resolve=lambda host, _: [(2, 1, 6, "", ("10.0.3.7", 0))])
    with pytest.raises(Blocked):
        inner.read("http://studio:8085/internal/crawl/concerts")                          # 같은 망 안쪽 주소
    for url in ("file:///etc/passwd", "ftp://shop.example/x"):
        with pytest.raises(Blocked):
            r.read(url)
    assert called == []                                                                   # MCP 까지 가지 않음


def test_page_reader_checks_where_a_redirect_landed(sessions):
    web = FakeWeb()
    web.add("other.example/robots.txt", "User-agent: *\nDisallow: /\n")
    r = reader(sessions, web, lambda name, args: {"status": 200, "content": ["x"], "url": "https://other.example/landing"})
    with pytest.raises(Blocked):
        r.read("https://shop.example/go")
    bad = reader(sessions, FakeWeb(), lambda name, args: {"status": 404, "content": [], "url": args["url"]})
    with pytest.raises(RuntimeError):
        bad.read("https://shop.example/gone")
    assert r.fetches == bad.fetches == 0
