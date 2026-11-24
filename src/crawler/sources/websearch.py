"""웹 조사 — MusicBrainz 에 없는 신보 · 피지컬 판본 · 공식 굿즈 · 공연 예매 오픈 일시를 찾습니다.

11/18 Claude 웹 검색 · 웹 페치(서버 도구) → Gemini 함수 호출 + 우리 도구 두 개 (webtools.py):
- web_search(query): Tavily 검색 (한국 · 결과 5개) → 결과 페이지를 코드가 모두 Scrapling MCP 로 열어 본문 앞부분을 돌려줌
  (11/18 실호출: 모델에게 맡기니 10개 중 1개만 열고 조각만 봄 → 사용자 지시 "싹 찾기"로 코드가 엶 · robots · 우회 끔 · 안쪽 주소 막음)
- open_page(url): 열어 둔 페이지의 더 긴 본문. **검색 결과에 나온 주소만**.
두 번 부릅니다: 1) 도구를 쓰는 조사 → 조사 글(사실마다 [출처: 주소])  2) 그 글 → 정해진 모양(JSON), 도구 없이.

지어낸 값 막기 (정확도 우선 11/17 · 규칙은 Claude 때와 같음 · 확인에 쓰는 글이 인용문 대신 출처의 실제 글):
- 출처 주소는 검색 결과에 있던 주소만. 그중 하나는 조사 글에 [출처] 로 적혀 있어야 함 (페이지는 다 열어 두므로 '열어 봄'은 기준이 아님).
- 음반 제목 · 참고 가격 · 예매 오픈 날짜와 시각은 출처의 글(제목 · 검색 본문 조각 · 페이지 본문)에 보여야 남깁니다 — 모델이 쓴 글은 근거로 쓰지 않음.
- 결과를 사용자에게 보일 때는 원 출처를 밝혀야 합니다 → 출처(evidence)를 후보에 담아 관리자가 보고 승인합니다. 본문은 저장하지 않습니다.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from ..enums import ALBUM_TYPES, GOODS_CATEGORIES, OPTION_SIZES, PHYSICAL_FORMATS
from ..keys import full_date, norm
from ..llm import LlmSkip, _obj
from ..webtools import SearchQuota

log = logging.getLogger(__name__)

MAX_SEARCHES = 5       # 조사 한 번에 검색 최대 (1번 = Tavily 1크레딧 · 무료 월 1,000 이라 아껴 씀)
PAGE_CHARS = 15000     # open_page 로 보여 줄 페이지 본문 길이 (상품 페이지 한 장은 보통 이보다 짧음)
RESULT_CHARS = 3000    # 검색 결과마다 보여 줄 본문 앞부분 (검색하면 결과 페이지를 모두 열어 둠 · 11/18 사용자 지시 "싹 찾기")
KEEP_CHARS = 200000    # 확인용으로 들고 있는 본문 길이 (저장하지 않음)
QUOTE_CHARS = 300      # 관리자에게 보일 출처 글 길이
NAME_CHARS = 500       # 한글 이름 근거로 Gemini 에 보일 검색 본문 조각 길이

SEARCH_TOOL = {"name": "web_search", "description": "웹 검색. 결과 페이지를 모두 열어 주소 · 제목 · 본문 앞부분(content) · 게시일을 돌려줍니다. "
                                                   "opened 가 false 면 열 수 없는 페이지라 content 는 검색 조각입니다.",
               "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "검색어"}},
                              "required": ["query"]}}
PAGE_TOOL = {"name": "open_page", "description": "web_search 결과에 나온 페이지의 더 긴 본문(마크다운). 다른 주소는 열 수 없습니다.",
             "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "검색 결과의 주소 그대로"}},
                            "required": ["url"]}}
TOOL_RULES = """

도구: web_search(검색어) 는 결과 페이지를 열어 본문 앞부분까지 돌려줍니다. 앞부분에 없는 내용(긴 상품 설명 · 공지 아래쪽)은 open_page(주소) 로 더 긴 본문을 봅니다.
검색은 최대 5번입니다. 검색어는 아티스트 이름(한글 · 영문)과 찾는 것을 함께 넣어 한 번에 여러 사실이 나오게 합니다.
조사가 끝나면 찾은 사실을 글로 정리합니다. 사실마다 근거가 된 페이지 주소를 [출처: 주소] 로 붙이고, 검색 결과 · 연 페이지에 없는 내용은 쓰지 않습니다."""

_URLS = {"type": "array", "items": {"type": "string"}}

RELEASES_SCHEMA = _obj({"releases": {"type": "array", "items": _obj({
    "title": {"type": "string"},
    "type": {"type": "string", "enum": [*ALBUM_TYPES, "UNKNOWN"]},
    "released_at": {"type": "string", "description": "YYYY-MM-DD · 모르면 빈 문자열"},
    "source_urls": _URLS,
    "formats": {"type": "array", "items": _obj({
        "format": {"type": "string", "enum": PHYSICAL_FORMATS},
        "name": {"type": "string", "description": "판 이름 (예: 2LP 한정반) · 모르면 빈 문자열"},
        "released_at": {"type": "string", "description": "발매 · 발송일 YYYY-MM-DD · 모르면 빈 문자열"},
        "pre_order": {"type": "boolean"},
        "vinyl_color": {"type": "string", "enum": ["BLACK", "COLOR", "UNKNOWN"]},
        "reference_price": {"type": "integer", "description": "원 · 모르면 -1"},
        "source_urls": _URLS,
    })},
})}})

GOODS_SCHEMA = _obj({"goods": {"type": "array", "items": _obj({
    "name": {"type": "string", "description": "상품 이름 (색이 있으면 이름에)"},
    "category": {"type": "string", "enum": GOODS_CATEGORIES},
    "sizes": {"type": "array", "items": {"type": "string", "enum": OPTION_SIZES}},
    "reference_price": {"type": "integer", "description": "원 · 모르면 -1"},
    "purchase_url": {"type": "string", "description": "판매 페이지 주소 · 모르면 빈 문자열"},
    "source_urls": _URLS,
})}})

TICKET_SCHEMA = _obj({
    "booking_open_at": {"type": "string", "description": "일반 예매 시작 · YYYY-MM-DDTHH:MM (한국 시간) · 모르면 빈 문자열"},
    "source_urls": _URLS,
})

TICKET_SYSTEM = """한국 공연 한 건의 일반 예매(티켓) 오픈 일시를 웹에서 찾습니다. 결과는 관리자가 출처를 보고 확인한 뒤 등록됩니다.
- 제목 · 아티스트 · 공연장 · 공연 날짜가 맞는 바로 그 공연의 공지만 씁니다. 같은 아티스트의 다른 공연 · 다른 도시 · 다른 해 공연은 뺍니다.
- 예매처 공지 · 기획사나 아티스트의 공식 SNS · 홈페이지 · 기사처럼 오픈 일시가 직접 적힌 곳에서 확인하고 출처를 밝힙니다.
- 팬클럽 · 카드사 · 멤버십 선예매가 아니라 일반 예매가 열리는 날짜와 시각입니다. 시각까지 확인하지 못하면 모른다고 씁니다.
- 검색 결과만으로 확실하지 않으면 그 공지 페이지를 open_page 로 열어 확인합니다. 짐작하지 않습니다."""

RELEASES_SYSTEM = """한국 인디 아티스트 한 팀의 최근 발매를 웹에서 찾습니다. 결과는 관리자가 출처를 보고 확인한 뒤 서비스에 들어갑니다.
찾을 것:
1. 오늘 기준 지난 1년 안에 나왔거나 발매일이 발표된 앨범 · EP · 싱글 (알려진 앨범 목록에 없는 것 위주)
2. 이 아티스트 음반(예전 앨범 포함)의 LP · CD · 카세트 발매 · 재발매 · 예약 판매

지킬 것:
- 이 아티스트의 것만 찾습니다. 이름이 같은 다른 아티스트, 피처링 참여, 컴필레이션 수록은 뺍니다. 별칭 · 레이블 · 알려진 앨범으로 같은 팀인지 확인합니다.
- 레이블 · 유통사 공지, 아티스트 공식 SNS · 홈페이지, 음원 사이트 앨범 페이지, 음반 가게 상품 페이지처럼 발매 사실이 직접 적힌 곳에서 확인합니다. 짐작 · 소문 · 팬 글은 쓰지 않습니다.
- 검색 결과만으로 세부 값(발매일 · 형식 · 판 이름 · 색 · 가격)이 확실하지 않으면 그 공지 · 상품 페이지를 open_page 로 열어 확인합니다.
- 사실마다 출처를 밝힙니다. 확인하지 못한 값(정확한 날짜 · 형식 · 색 · 가격)은 모른다고 씁니다.
- 음반마다 제목(발매 표기 그대로), 종류(정규 · EP/미니 · 싱글), 발매일, 피지컬 판본(형식 · 판 이름 · 발매 또는 예약 발송일 · 색 · 가격)을 적습니다."""

GOODS_SYSTEM = """한국 인디 아티스트 한 팀의 공식 굿즈(MD)를 웹에서 찾습니다. 결과는 관리자가 출처를 보고 확인한 뒤 제휴 판매 후보가 됩니다.
지킬 것:
- 아티스트 · 소속 레이블의 공식 스토어나 공식 공지(SNS · 홈페이지)에 올라온, 지금 팔고 있거나 판매가 발표된 상품만 찾습니다.
- 되팔기 · 중고 · 팬 제작 · 구매 대행은 뺍니다. 음반(LP · CD · 카세트) · 공연 티켓 · 디지털 상품은 굿즈가 아닙니다.
- 이름이 같은 다른 아티스트의 상품은 뺍니다. 별칭 · 레이블로 같은 팀인지 확인합니다.
- 색이 여러 개면 색마다 따로 적습니다(상품 이름에 색). 사이즈가 있으면 사이즈를 모두 적습니다.
- 찾은 판매 페이지는 open_page 로 열어 상품 이름 · 색 · 사이즈 · 가격을 페이지에서 확인합니다.
- 상품마다 판매 페이지 주소와 가격(원)을 출처와 함께 적습니다. 확인하지 못한 값은 모른다고 씁니다."""

EXTRACT_SYSTEM = """웹 조사 글을 정해진 모양으로 옮깁니다. 조사 글에 출처와 함께 적힌 사실만 옮깁니다.
- source_urls 에는 그 사실을 적은 출처 주소를 sources 목록에 있는 그대로 넣습니다. 출처가 없는 항목은 넣지 않습니다.
- 모르는 값은 모름 값으로 둡니다 (문자열은 빈 문자열, 가격은 -1, 종류는 UNKNOWN, LP 색은 UNKNOWN).
- 날짜는 YYYY-MM-DD 로 확실할 때만 넣습니다. 예매 오픈 일시는 날짜와 시각이 모두 확실할 때만 YYYY-MM-DDTHH:MM 으로 넣습니다.
- 굿즈 분류는 목록에서 고르고, 맞는 것이 없으면 ETC 입니다. 사이즈가 따로 없는 상품은 빈 배열입니다."""


class WebSearch:
    def __init__(self, llm, search, pages):
        """search: webtools.Tavily · pages: webtools.PageReader"""
        self.llm = llm
        self.search = search
        self.pages = pages

    @property
    def searches(self) -> int:
        return self.search.searches

    @property
    def fetches(self) -> int:
        return self.pages.fetches

    @property
    def exhausted(self) -> bool:
        """이번 실행에 더 조사할 수 없음 (검색 한도 · Gemini 하루 한도) — 남은 것은 다음 실행에"""
        return bool(getattr(self.search, "exhausted", False) or getattr(self.llm, "exhausted", False))

    def close(self):
        self.pages.close()

    def mentions(self, name: str) -> list[str]:
        """11/24 한글 활동명 근거 — '{이름} 가수 앨범' 검색 1번(1크레딧)의 제목 · 본문 조각. 페이지는 열지 않음.
        (11/24 시험: Royal 44 → 로얄44 · Skinny Brown → 스키니브라운 · palecistus → 페일시스터스)"""
        return [f"{r['title']} | {r['content'][:NAME_CHARS]}" for r in self.search.search(f"{name} 가수 앨범")]

    def releases(self, artist: dict) -> list[dict]:
        """[{title, type(없으면 None), released_at, evidence, formats: [{format, name, released_at, pre_order, vinyl_color, reference_price, evidence}]}]"""
        data, results, cited = self._ask(RELEASES_SYSTEM, artist, RELEASES_SCHEMA)
        out = []
        for r in data.get("releases", []):
            title = r["title"].strip()
            evidence = _evidence(r["source_urls"], results, cited)
            if not title or not evidence or not _mentions(title, evidence, results, cited):
                continue
            formats = []
            for f in r["formats"]:
                fe = _evidence(f["source_urls"], results, cited)
                if not fe or not _names(title, fe, results):
                    continue
                formats.append({"format": f["format"], "name": f["name"].strip(), "released_at": full_date(f["released_at"]),
                                "pre_order": bool(f["pre_order"]), "vinyl_color": f["vinyl_color"],
                                "reference_price": _price(f["reference_price"], fe, results, cited), "evidence": fe})
            out.append({"title": title, "type": r["type"] if r["type"] in ALBUM_TYPES else None,
                        "released_at": full_date(r["released_at"]), "evidence": evidence, "formats": formats})
        return out

    def goods(self, artist: dict) -> list[dict]:
        """[{name, category, sizes, reference_price, purchase_url, evidence}] — 판매 페이지 주소는 검색 결과에 있던 주소일 때만"""
        data, results, cited = self._ask(GOODS_SYSTEM, artist, GOODS_SCHEMA)
        out = []
        for g in data.get("goods", []):
            evidence = _evidence(g["source_urls"], results, cited)
            if not g["name"].strip() or not evidence:
                continue
            out.append({"name": g["name"].strip(), "category": g["category"] if g["category"] in GOODS_CATEGORIES else "ETC",
                        "sizes": [s for s in dict.fromkeys(g["sizes"]) if s in OPTION_SIZES],
                        "reference_price": _price(g["reference_price"], evidence, results, cited),
                        "purchase_url": g["purchase_url"] if g["purchase_url"] in results else None, "evidence": evidence})
        return out

    def ticket_open(self, concert: dict) -> tuple[str, list[dict]] | None:
        """일반 예매 오픈 일시 (KOPIS 에 없을 때) → (ISO 8601 +09:00, 출처). concert: {title, artistNames, venueName, startsAt}.
        출처 글에 그 날짜 · 시각과 이 공연(제목이나 아티스트 이름)이 보이고, 공연 시작보다 앞일 때만 받습니다."""
        data, results, cited = self._ask(TICKET_SYSTEM, concert, TICKET_SCHEMA, subject="concert")
        when = _kst(data.get("booking_open_at"))
        evidence = _evidence(data.get("source_urls") or [], results, cited)
        if when is None or not evidence:
            return None
        starts = _kst(concert.get("startsAt"))
        if starts is not None and not starts - timedelta(days=366) < when < starts:
            return None
        if not _shows_when(when, evidence, results, cited):
            return None
        if not any(_mentions(n, evidence, results, cited) for n in [concert.get("title") or "", *concert.get("artistNames", [])]):
            return None
        evidence = [{**e, "quote": _around(results[e["url"]], _day_re(when)) or e["quote"]} for e in evidence]
        return when.isoformat(), evidence

    def _ask(self, system: str, target: dict, schema: dict, subject: str = "artist"):
        """1) 도구를 쓰는 조사  2) 조사 글 → JSON. → (JSON, 결과 {주소: 제목 · 날짜 · 본문 조각 · 연 본문}, 조사 글에 [출처] 로 적힌 주소)"""
        question = json.dumps({"today": date.today().isoformat(), subject: target}, ensure_ascii=False)
        results: dict[str, dict] = {}
        text = self.llm.research(system + TOOL_RULES, question, self._tools(results))
        if not results:
            return {}, results, set()
        cited = {u for u in results if u in text}
        sources = [{"url": u, "title": r["title"], "page_age": r["page_age"], "cited": u in cited, "opened": r["text"] is not None}
                   for u, r in results.items()]
        data = self.llm.extract(EXTRACT_SYSTEM, json.dumps({subject: target, "research": text, "sources": sources},
                                                           ensure_ascii=False), schema)
        return data, results, cited

    def _tools(self, results: dict) -> list:
        """이번 조사 한 번의 도구 — 찾은 것은 results 에 쌓음. 오류는 모델에게 돌려주고, 검색 크레딧 소진만 조사를 멈춤."""
        used = {"web_search": 0}

        def web_search(query: str) -> dict:
            if used["web_search"] >= MAX_SEARCHES:
                return {"error": "이번 조사의 검색 횟수를 다 썼습니다. 지금까지 찾은 것으로 정리하세요."}
            used["web_search"] += 1
            try:
                hits = self.search.search(query)
            except SearchQuota:
                raise LlmSkip("search_quota") from None
            except Exception as e:
                return {"error": f"검색 실패 ({type(e).__name__})"}
            out = []
            for h in hits:
                r = results.setdefault(h["url"], {"title": h["title"], "page_age": h["published_date"], "snippet": h["content"],
                                                  "text": None, "tried": False})
                if not r["tried"]:                      # 조각만 보지 않게 결과 페이지는 코드가 다 엶 (크레딧 안 씀)
                    r["tried"] = True
                    r["text"] = self._read(h["url"])
                out.append({**h, "content": (r["text"] or h["content"])[:RESULT_CHARS], "opened": r["text"] is not None})
            return {"results": out}

        def open_page(url: str) -> dict:
            if url not in results:
                return {"error": "web_search 결과에 나온 주소만 열 수 있습니다."}
            if results[url]["text"] is None:
                return {"error": "열 수 없는 페이지입니다 (robots.txt · 오류). 검색 조각만 쓸 수 있습니다."}
            return {"url": url, "content": results[url]["text"][:PAGE_CHARS]}

        return [(SEARCH_TOOL, web_search), (PAGE_TOOL, open_page)]

    def _read(self, url: str) -> str | None:
        """검색 결과 페이지 본문 — robots.txt 가 막았거나 못 열면 None (그 결과는 검색 조각만 씀)"""
        try:
            return self.pages.read(url)[1][:KEEP_CHARS]
        except Exception as e:
            log.info("web_page_skipped url=%s reason=%s", url[:200], type(e).__name__)
            return None


def _evidence(urls: list[str], results: dict, cited: set) -> list[dict]:
    """검색 결과에 있던 주소만 · 그중 하나는 조사 글에 [출처] 로 적혀 있어야 함 (아니면 빈 목록 = 버림)"""
    ok = [u for u in dict.fromkeys(urls) if u in results]
    if not any(u in cited for u in ok):
        return []
    return [{"url": u, "title": results[u]["title"], "quote": _quote(results[u])} for u in ok]


def _quote(r: dict) -> str | None:
    """관리자에게 보일 출처 글 — 검색 본문 조각 앞부분 (모델이 쓴 글이 아니라 출처의 글)"""
    text = " ".join((r.get("snippet") or "").split())
    return text[:QUOTE_CHARS] or None


def _source_text(evidence: list[dict], results: dict) -> str:
    """출처에서 보이는 글 전부 (페이지 제목 · 검색 본문 조각 · 연 페이지 본문)"""
    parts = []
    for e in evidence:
        r = results[e["url"]]
        parts += [r["title"] or "", r.get("snippet") or "", r["text"] or ""]
    return " ".join(parts)


def _seen(evidence: list[dict], results: dict, cited: set) -> str:
    return norm(_source_text(evidence, results))


def _names(title: str, evidence: list[dict], results: dict) -> bool:
    """판본용 — 앨범 제목이 그 출처의 페이지 제목이나 그 주소의 검색 본문 조각에 있어야 함 (페이지 본문 어딘가는 안 됨).
    11/18 실호출: '3집 Ballad of You' 상품 페이지 본문에 예전 앨범 이름이 같이 있어 그 CD 가 예전 앨범 판본으로 붙음 → 막음"""
    t = norm(title)
    return bool(t) and any(t in norm(f"{results[e['url']]['title'] or ''} {results[e['url']].get('snippet') or ''}") for e in evidence)


def _mentions(title: str, evidence: list[dict], results: dict, cited: set) -> bool:
    """제목이 출처의 페이지 제목 · 본문 조각 · 연 본문에 보여야 함 — 다른 음반 이야기를 이 제목으로 옮긴 것을 막음"""
    return bool(norm(title)) and norm(title) in _seen(evidence, results, cited)


def _price(price: int, evidence: list[dict], results: dict, cited: dict) -> int | None:
    """참고 가격 — 출처에 그 숫자가 보일 때만 ('35,000원' → 35000)"""
    return price if price > 0 and str(price) in _seen(evidence, results, cited) else None


KST = timezone(timedelta(hours=9))


def _kst(value) -> datetime | None:
    """ISO 8601 → 한국 시간 (시간대가 없으면 한국 시간으로 봄). 날짜만 있으면 None (시각까지 필요)"""
    if not isinstance(value, str) or len(value) < 16:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t.replace(tzinfo=KST) if t.tzinfo is None else t.astimezone(KST)


def _day_re(when: datetime) -> str:
    return rf"(?<!\d)0?{when.month}\s*(?:월|\.|/)\s*0?{when.day}(?!\d)"


def _around(r: dict, pattern: str) -> str | None:
    """출처 글에서 pattern 이 처음 보이는 곳 앞뒤 — 관리자가 날짜가 적힌 문장을 바로 보게 (11/18 실호출: 앞부분만으론 안 보였음)"""
    half = QUOTE_CHARS // 2
    for text in (r.get("snippet"), r.get("text")):
        text = " ".join((text or "").split())
        m = re.search(pattern, text)
        if m:
            return text[max(0, m.start() - half):m.end() + half]
    return None


def _shows_when(when: datetime, evidence: list[dict], results: dict, cited: set) -> bool:
    """출처 글에 그 날짜(11월 20일 · 11.20 · 11/20)와 시각(20:00 · 20시 · 오후 8시 · 8PM)이 보여야 함 — 다른 날짜를 옮긴 것을 막음"""
    text = " ".join(_source_text(evidence, results).split())
    h, mi = when.hour, when.minute
    h12 = h % 12 or 12
    day = re.search(_day_re(when), text)
    times = [rf"(?<!\d)0?{h}\s*:\s*{mi:02d}(?!\d)", rf"(?<!\d)0?{h}\s*시"]
    if h >= 12:
        times += [rf"오후\s*0?{h12}\s*시", rf"(?<!\d){h12}\s*(?::\s*{mi:02d}\s*)?(?:PM|pm|P\.M\.|p\.m\.)"]
    else:
        times += [rf"오전\s*0?{h12}\s*시", rf"(?<!\d){h12}\s*(?::\s*{mi:02d}\s*)?(?:AM|am|A\.M\.|a\.m\.)"]
    return bool(day) and any(re.search(t, text) for t in times)
