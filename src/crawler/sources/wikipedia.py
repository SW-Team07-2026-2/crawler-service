"""한국어 위키백과 - 아티스트, 앨범 소개 글의 출처 .

MusicBrainz 가 이어 둔 Wikidata 항목(url-rels) -> Wikidata 의 kowiki 문서 이름 -> 본문(평문, 표는 빠짐).
이름으로 문서를 찾지 않습니다 - 같은 제목의 다른 문서가 걸림 ('새소년' 은 1964년 어린이 잡지 문서).
한국어 문서가 없으면 None (영어 문서는 쓰지 않음 - 소개 글은 한글만, 사용자).
위키백과 글은 CC BY-SA - 그대로 옮기지 않고 사실만 새 문장으로 쓰며(llm.DESCRIBE_RULES), 출처 표시는 사이트 약관에 둡니다 (사용자 결정).
robots.txt 는 검색 로봇용으로 /w/ 를 막지만 Wikimedia API 이용 정책(API:Etiquette, User-Agent policy)이 연락처가 든 User-Agent 로
차례로 부르는 것을 허락합니다 -> http.API_POLICY.
"""
import json
import re
from datetime import timedelta
from urllib.parse import quote

WIKIDATA = "https://www.wikidata.org/w/api.php"
KOWIKI = "https://ko.wikipedia.org/w/api.php"
CACHE = timedelta(days=30)
# 소개 글에 쓰지 않는 단락 - 개인사(태어난 곳, 학교, 가족), 논란, 목록 끝 부분
SKIP_SECTIONS = re.compile(r"이전|개인|사생활|가족|학력|논란|사건|사고|병역|각주|외부 링크|같이 보기|참고|출처|주석")
_HEADING = re.compile(r"^(=+)\s*(.*?)\s*=+\s*$")


def ko_title(http, qid: str) -> str | None:
    """Wikidata 항목 -> 한국어 위키백과 문서 이름 (없으면 None)"""
    url = f"{WIKIDATA}?action=wbgetentities&ids={quote(qid)}&props=sitelinks&sitefilter=kowiki&format=json"
    entity = (json.loads(http.get("wikidata", url, max_age=CACHE)).get("entities") or {}).get(qid) or {}
    return ((entity.get("sitelinks") or {}).get("kowiki") or {}).get("title")


def ko_article(http, title: str) -> dict | None:
    """{title, url, text} - text 는 평문 본문 (== 소제목 == 줄 포함), 문서가 없거나 비었으면 None"""
    url = (f"{KOWIKI}?action=query&prop=extracts&explaintext=1&redirects=1&format=json&formatversion=2"
           f"&titles={quote(title)}")
    pages = (json.loads(http.get("wikipedia", url, max_age=CACHE)).get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing") or not (pages[0].get("extract") or "").strip():
        return None
    name = pages[0]["title"]
    return {"title": name, "url": "https://ko.wikipedia.org/wiki/" + quote(name.replace(" ", "_")), "text": pages[0]["extract"]}


def article_for(http, qid: str | None) -> dict | None:
    if not qid:
        return None
    title = ko_title(http, qid)
    return ko_article(http, title) if title else None


def readable(text: str, limit: int = 4000) -> str:
    """소개 글 출처로 넘길 본문 - 앞부분 + 쓰지 않는 단락(SKIP_SECTIONS, 그 아래 소단락까지)을 뺀 나머지, 빈 줄 정리"""
    out, skip_level = [], None
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            if skip_level is not None and level > skip_level:
                continue                                  # 뺀 단락의 소단락
            skip_level = level if SKIP_SECTIONS.search(m.group(2)) else None
            if skip_level is None:
                out.append(f"[{m.group(2)}]")
            continue
        if skip_level is None and line.strip():
            out.append(line.strip())
    # 내용 없는 소제목(표만 있던 단락)은 뺌
    lines = [line for i, line in enumerate(out)
             if not (line.startswith("[") and line.endswith("]") and (i + 1 == len(out) or out[i + 1].startswith("[")))]
    return "\n".join(lines)[:limit]


def sentences_about(text: str, titles: list[str]) -> list[str]:
    """본문에서 앨범 제목을 괄호(《》 〈〉 「」 ‘’ “” '' "")로 적은 문장 - 앨범 소개 글의 출처.
    괄호 없이 맞추면 짧은 제목(20, 집)이 다른 낱말에 걸림."""
    marks = [f"{o}{t}{c}" for t in dict.fromkeys(t.strip() for t in titles if t and t.strip())
             for o, c in (("《", "》"), ("〈", "〉"), ("「", "」"), ("‘", "’"), ("“", "”"), ("'", "'"), ('"', '"'), ("<", ">"))]
    found = []
    for line in text.splitlines():
        if _HEADING.match(line):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
            if any(m in sentence for m in marks) and sentence not in found:
                found.append(sentence)
    return found
