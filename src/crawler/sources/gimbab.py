"""김밥레코즈(gimbabrecords.com) 음반 — 아티스트 이름으로 상품을 검색해 "아티스트 / 제목 (형식)" 에서 LP · CD · 카세트 판본만 씁니다.

11/18 확인: robots.txt 는 /admin · /api · /member 등만 막고 /product/ 는 허용합니다. 약관 제22조②(몰이 만든 저작물의 상업적 이용 제한)에 따라
상품명에 적힌 사실(아티스트 · 앨범 · 형식 · 예약 여부)만 쓰고 이미지 · 소개 글 · 가격은 가져오지 않습니다 (가격은 그 가게의 소매가라 우리 판매가가 아님).
"""
import html
import re
from datetime import timedelta
from urllib.parse import quote, urljoin

BASE = "https://www.gimbabrecords.com"
SEARCH = BASE + "/product/search.html?keyword={}"
_ITEM = re.compile(r'<li id="anchorBoxId_(\d+)".*?<div class="name"><a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_FORMATS = [  # 앞에 있는 것이 이김 — "(LP + CD)" 는 LP
    ("LP", re.compile(r"vinyl|(?<![a-z])\d*lp(?![a-z])", re.I)),
    ("CASSETTE", re.compile(r"cassette|카세트|(?<![a-z])tape(?![a-z])", re.I)),
    ("CD", re.compile(r"(?<![a-z])\d*cd(?![a-z])", re.I)),
]
_COLOR = re.compile(r"colou?r|컬러|투명|clear|marble|마블|splatter|스플래터|translucent", re.I)


def search(http, keyword: str) -> list[dict]:
    """검색 결과 첫 쪽의 음반 상품 [{id, url, artist_names, title, format, detail, pre_order, colored}] — 굿즈 · 형식을 모르는 상품은 뺌"""
    body = http.get("gimbab", SEARCH.format(quote(keyword)), max_age=timedelta(days=7))
    out = []
    for product_no, href, raw in _ITEM.findall(body):
        p = parse(html.unescape(re.sub(r"<[^>]+>", "", raw)).strip())
        if p:
            out.append({"id": product_no, "url": urljoin(BASE, href), **p})
    return out


def parse(text: str) -> dict | None:
    """'실리카겔 / 정규 2집 POWER ANDRE 99 (Vinyl, 2LP) *예약상품 (…)' → 이름들 · 제목 · LP · 'Vinyl, 2LP' · 예약"""
    if " / " not in text:
        return None
    artist, rest = text.split(" / ", 1)
    rest, _, note = rest.partition("*")
    rest = rest.strip()
    m = re.search(r"\(([^()]*)\)$", rest)
    if not m:
        return None
    detail = m.group(1).strip()
    fmt = next((f for f, rx in _FORMATS if rx.search(detail)), None)
    title = rest[:m.start()].strip()
    if fmt is None or not title:
        return None
    # "놀이도감 (Playbook) (놀이도감 = 김민수 of 실리카겔)" → 놀이도감 · Playbook (설명 괄호는 뺌)
    names = [re.sub(r"\([^()]*\)", "", artist).strip(), *(x.strip() for x in re.findall(r"\(([^()=]*)\)", artist))]
    return {"artist_names": [n for n in dict.fromkeys(names) if n], "title": title, "format": fmt, "detail": detail,
            "pre_order": "예약" in note, "colored": bool(_COLOR.search(detail))}
