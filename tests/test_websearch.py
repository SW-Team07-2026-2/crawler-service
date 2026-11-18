import json

import pytest

from crawler.http import Blocked
from crawler.llm import LlmSkip
from crawler.sources.websearch import GOODS_SCHEMA, MAX_SEARCHES, RELEASES_SCHEMA, TOOL_RULES, WebSearch
from crawler.webtools import SearchQuota


class ScriptedLlm:
    """모델 대신 — research 는 script 의 도구 호출을 차례로 실행하고 마지막 글을 돌려줌 · extract 는 정해 둔 JSON"""

    def __init__(self, script, extracted=None):
        self.script, self.extracted = script, extracted or {}
        self.tool_results, self.extracts, self.systems = [], [], []

    def research(self, system, content, tools):
        run = {d["name"]: fn for d, fn in tools}
        self.systems.append(system)
        for name, args in self.script[:-1]:
            self.tool_results.append(run[name](**args))
        return self.script[-1]

    def extract(self, system, content, schema):
        self.extracts.append((json.loads(content), schema))
        return self.extracted


class FakeTavily:
    def __init__(self, hits=None, error=None):
        self.hits, self.error, self.queries, self.searches = hits or {}, error, [], 0

    def search(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        self.searches += 1
        return self.hits.get(query, [])


class FakePages:
    def __init__(self, pages=None, error=None):
        self.pages, self.error, self.opened, self.fetches = pages or {}, error, [], 0

    def read(self, url):
        self.opened.append(url)
        if self.error:
            raise self.error
        self.fetches += 1
        return url, self.pages[url]

    def close(self):
        pass


def hit(url, title, content, date=None):
    return {"url": url, "title": title, "content": content, "published_date": date}


LABEL = "https://label.example/notice/1"
SHOP = "https://shop.example/lp"
OTHER = "https://blog.example/rumor"
ARTIST = {"name": "Silica Gel", "aliases": ["실리카겔"], "label": "Magic Strawberry Sound", "known_albums": []}
HITS = {"실리카겔 신보": [hit(LABEL, "실리카겔 정규 2집 POWER ANDRE 99 발매", "정규 2집 POWER ANDRE 99 를 11월 4일 발매합니다", "2026-11-01"),
                       hit(SHOP, "POWER ANDRE 99 2LP 예약", "POWER ANDRE 99 2LP 예약 판매 45,000원"),
                       hit(OTHER, "소문", "미니 앨범도 낸다는 말이 있습니다")]}


def test_releases_keep_only_items_whose_source_was_searched_and_shows_the_title():
    extracted = {"releases": [
        {"title": "POWER ANDRE 99", "type": "ALBUM", "released_at": "2026-11-04", "source_urls": [LABEL],
         "formats": [{"format": "LP", "name": "2LP", "released_at": "", "pre_order": True, "vinyl_color": "UNKNOWN",
                      "reference_price": 45000, "source_urls": [SHOP]},
                     {"format": "CD", "name": "CD", "released_at": "", "pre_order": False, "vinyl_color": "UNKNOWN",
                      "reference_price": -1, "source_urls": ["https://made.up/cd"]}]},            # 검색 결과에 없는 주소
        {"title": "새 미니 앨범", "type": "EP", "released_at": "", "source_urls": [OTHER], "formats": []},   # 조사 글에 [출처] 로 안 적힘 · 안 엶
        {"title": "Kyo181", "type": "ALBUM", "released_at": "2020-01-01", "source_urls": [LABEL], "formats": []},  # 출처에 제목이 없음
    ]}
    llm = ScriptedLlm([("web_search", {"query": "실리카겔 신보"}),
                       f"정규 2집이 11월 4일 나왔습니다 [출처: {LABEL}] LP 는 예약 판매 중 [출처: {SHOP}] 미니 앨범 소문도 있음"], extracted)
    search = FakeTavily(HITS)
    found = WebSearch(llm, search, FakePages()).releases(ARTIST)

    assert [r["title"] for r in found] == ["POWER ANDRE 99"]
    r = found[0]
    assert r["type"] == "ALBUM" and r["released_at"] == "2026-11-04"
    assert r["evidence"] == [{"url": LABEL, "title": "실리카겔 정규 2집 POWER ANDRE 99 발매", "quote": "정규 2집 POWER ANDRE 99 를 11월 4일 발매합니다"}]
    assert [(f["format"], f["pre_order"], f["reference_price"]) for f in r["formats"]] == [("LP", True, 45000)]

    assert llm.systems[0].endswith(TOOL_RULES)
    assert llm.tool_results[0] == {"results": [{**h, "opened": False} for h in HITS["실리카겔 신보"]]}   # 못 연 페이지는 조각 그대로
    assert search.searches == 1
    body, schema = llm.extracts[0]
    assert schema == RELEASES_SCHEMA and "[출처: https://label.example/notice/1]" in body["research"]
    assert {s["url"]: (s["cited"], s["opened"]) for s in body["sources"]} == {LABEL: (True, False), SHOP: (True, False), OTHER: (False, False)}
    assert all("snippet" not in s and "text" not in s for s in body["sources"])   # 본문은 두 번째 호출에 다시 넣지 않음


def test_open_page_only_for_search_results_and_page_text_checks_title_and_price():
    store = "https://store.example/products/7"
    other = "https://store.example/products/8"
    page = "실리카겔 공식 스토어\nPOWER ANDRE 99 2LP (투명 컬러)\n가격 52,000원\n12월 발송 예정"
    other_page = "3집 Ballad of You CD 예약 · 함께 본 상품: POWER ANDRE 99"            # 본문에만 예전 앨범 이름
    extracted = {"releases": [
        {"title": "POWER ANDRE 99", "type": "ALBUM", "released_at": "", "source_urls": [store],
         "formats": [{"format": "LP", "name": "2LP 투명 컬러", "released_at": "", "pre_order": True, "vinyl_color": "COLOR",
                      "reference_price": 52000, "source_urls": [store]},
                     {"format": "CASSETTE", "name": "카세트", "released_at": "", "pre_order": False, "vinyl_color": "UNKNOWN",
                      "reference_price": 19000, "source_urls": [store]},     # 출처에 없는 가격 → 참고 가격 비움
                     {"format": "CD", "name": "CD", "released_at": "2026-11-24", "pre_order": True, "vinyl_color": "UNKNOWN",
                      "reference_price": -1, "source_urls": [other]}]},      # 다른 앨범 상품 페이지 → 판본으로 안 붙임
        {"title": "Kyo181", "type": "ALBUM", "released_at": "", "source_urls": [store], "formats": []},   # 본문에 없는 제목
    ]}
    llm = ScriptedLlm([("web_search", {"query": "q"}), ("open_page", {"url": store}), ("open_page", {"url": other}),
                       ("open_page", {"url": "http://studio:8085/internal/crawl/concerts"}),     # 검색 결과에 없는 주소
                       f"2LP 투명 컬러반을 예약 판매합니다 [출처: {store}] [출처: {other}]"], extracted)
    pages = FakePages({store: page, other: other_page})
    hits = {"q": [hit(store, "공식 스토어 - POWER ANDRE 99", "실리카겔 굿즈와 음반"), hit(other, "실리카겔 3집 Ballad of You CD", "예약 판매")]}
    [r] = WebSearch(llm, FakeTavily(hits), pages).releases(ARTIST)
    assert r["title"] == "POWER ANDRE 99"
    assert r["evidence"] == [{"url": store, "title": "공식 스토어 - POWER ANDRE 99", "quote": "실리카겔 굿즈와 음반"}]
    assert [(f["format"], f["vinyl_color"], f["reference_price"]) for f in r["formats"]] == [("LP", "COLOR", 52000), ("CASSETTE", "UNKNOWN", None)]
    assert pages.opened == [store, other]
    assert llm.tool_results[1] == {"url": store, "content": page}
    assert "web_search 결과에 나온 주소만" in llm.tool_results[3]["error"]
    assert llm.extracts[0][0]["sources"][0]["opened"] is True


def test_tool_errors_go_back_to_the_model_and_use_limits():
    llm = ScriptedLlm([("web_search", {"query": "q"}), ("open_page", {"url": LABEL}), "없음"])
    WebSearch(llm, FakeTavily({"q": [hit(LABEL, "t", "c")]}), FakePages(error=Blocked(LABEL))).goods(ARTIST)
    assert "robots.txt" in llm.tool_results[1]["error"]

    llm = ScriptedLlm([("web_search", {"query": "q"}), "없음"])
    WebSearch(llm, FakeTavily(error=RuntimeError("Tavily 500")), FakePages()).goods(ARTIST)
    assert "검색 실패" in llm.tool_results[0]["error"] and llm.extracts == []     # 결과가 없으면 정리 호출도 안 함

    llm = ScriptedLlm([*[("web_search", {"query": f"q{i}"}) for i in range(MAX_SEARCHES + 1)], "없음"])
    search = FakeTavily()
    WebSearch(llm, search, FakePages()).goods(ARTIST)
    assert len(search.queries) == MAX_SEARCHES and "다 썼습니다" in llm.tool_results[-1]["error"]


def test_search_credits_running_out_stops_the_research():
    llm = ScriptedLlm([("web_search", {"query": "q"}), "없음"])
    tavily = FakeTavily(error=SearchQuota("Tavily 432"))
    ws = WebSearch(llm, tavily, FakePages())
    with pytest.raises(LlmSkip, match="search_quota"):
        ws.releases(ARTIST)
    assert not ws.exhausted
    tavily.exhausted = True                                        # 진짜 Tavily 는 한도 · 432 에서 스스로 표시
    assert ws.exhausted                                            # → 파이프라인이 남은 조사를 다음 실행으로


def test_goods_purchase_url_only_when_it_was_a_search_result():
    store = "https://store.example/silica-tee"
    extracted = {"goods": [
        {"name": "로고 티셔츠 블랙", "category": "TSHIRT", "sizes": ["S", "M", "L", "M"], "reference_price": 35000,
         "purchase_url": store, "source_urls": [store]},
        {"name": "키링", "category": "KEYRING", "sizes": [], "reference_price": -1,
         "purchase_url": "https://made.up/keyring", "source_urls": [store]},
        {"name": "출처 없는 모자", "category": "HAT", "sizes": [], "reference_price": -1, "purchase_url": "", "source_urls": []},
    ]}
    llm = ScriptedLlm([("web_search", {"query": "q"}), f"로고 티셔츠를 팝니다 [출처: {store}]"], extracted)
    goods = WebSearch(llm, FakeTavily({"q": [hit(store, "실리카겔 로고 티셔츠", "실리카겔 로고 티셔츠 블랙 S M L 35,000원")]}),
                      FakePages()).goods(ARTIST)
    assert [(g["name"], g["sizes"], g["purchase_url"], g["reference_price"]) for g in goods] == [
        ("로고 티셔츠 블랙", ["S", "M", "L"], store, 35000), ("키링", [], None, None)]
    assert llm.extracts[0][1] == GOODS_SCHEMA


CONCERT = {"title": "실리카겔 단독 공연", "artistNames": ["실리카겔"], "venueName": "무신사 개러지", "startsAt": "2026-12-12T18:00:00+09:00"}
NOTICE = "https://label.example/notice/9"


def ticket(found_at, snippet, title="실리카겔 단독 공연 티켓 오픈 안내", url=NOTICE):
    llm = ScriptedLlm([("web_search", {"query": "q"}), f"일반 예매가 열립니다 [출처: {url}]"],
                      {"booking_open_at": found_at, "source_urls": [url]})
    return WebSearch(llm, FakeTavily({"q": [hit(url, title, snippet)]}), FakePages()).ticket_open(CONCERT)


def test_ticket_open_needs_the_date_and_time_in_the_source():
    assert ticket("2026-11-20T20:00", "일반 예매 11월 20일(금) 오후 8시 오픈") == (
        "2026-11-20T20:00:00+09:00",
        [{"url": NOTICE, "title": "실리카겔 단독 공연 티켓 오픈 안내", "quote": "일반 예매 11월 20일(금) 오후 8시 오픈"}])
    assert ticket("2026-11-20T20:00", "TICKET OPEN 2026.11.20 20:00")[0] == "2026-11-20T20:00:00+09:00"
    long = "가" * 400 + " 일반 예매 11월 20일(금) 오후 8시 오픈 " + "나" * 400                     # 날짜가 뒤쪽에 있는 긴 글
    quote = ticket("2026-11-20T20:00", long)[1][0]["quote"]
    assert "11월 20일(금) 오후 8시" in quote and len(quote) <= 320                  # 앞부분이 아니라 날짜가 적힌 곳을 인용
    assert ticket("2026-11-21T20:00", "일반 예매 11월 20일(금) 오후 8시 오픈") is None      # 출처와 날짜가 다름
    assert ticket("2026-11-20T19:00", "일반 예매 11월 20일(금) 오후 8시 오픈") is None      # 시각이 다름
    assert ticket("2026-11-20", "일반 예매 11월 20일 오픈") is None                          # 시각 모름
    assert ticket("2026-12-20T20:00", "12월 20일 20:00 오픈") is None                       # 공연보다 뒤
    assert ticket("2026-11-20T20:00", "11월 20일 20:00 오픈", title="다른 팀 콘서트 티켓 오픈") is None   # 이 공연 이야기가 아님


def test_search_opens_every_result_page_once_and_needs_a_citation():
    store, blog = "https://store.example/p/1", "https://blog.example/rumor"
    page = "# POWER ANDRE 99 2LP\n" + "상품 설명 " * 2000 + "\n가격 52,000원"          # 가격이 본문 뒤쪽에 있는 긴 페이지
    extracted = {"releases": [
        {"title": "POWER ANDRE 99", "type": "ALBUM", "released_at": "", "source_urls": [store], "formats": []},
        {"title": "새 미니 앨범", "type": "EP", "released_at": "", "source_urls": [blog], "formats": []}]}  # 열린 페이지지만 [출처] 없음
    llm = ScriptedLlm([("web_search", {"query": "a"}), ("web_search", {"query": "b"}), ("open_page", {"url": store}),
                       f"POWER ANDRE 99 2LP 판매 중 [출처: {store}] 미니 앨범 소문"], extracted)
    pages = FakePages({store: page, blog: "새 미니 앨범을 낸다는 소문"})
    hits = {"a": [hit(store, "스토어", "조각"), hit(blog, "블로그", "조각")], "b": [hit(store, "스토어", "조각")]}
    found = WebSearch(llm, FakeTavily(hits), pages).releases(ARTIST)
    first = llm.tool_results[0]["results"]
    assert [(r["opened"], r["content"]) for r in first] == [(True, page[:3000]), (True, "새 미니 앨범을 낸다는 소문")]   # 조각 대신 본문
    assert pages.opened == [store, blog]                                            # 두 번 나와도 한 번만 엶
    assert llm.tool_results[2] == {"url": store, "content": page[:15000]}            # 더 긴 본문은 open_page 로
    assert [r["title"] for r in found] == ["POWER ANDRE 99"]                        # 블로그는 [출처] 로 안 적힘 → 버림
