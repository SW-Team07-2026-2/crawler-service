"""Discogs API — 피지컬 앨범(LP · CD · 카세트)의 판본 · 색.

토큰 파일(discogs-token)이 있을 때만 씁니다. 11/18 확인 (약관 페이지는 자동 접근 403 — 검색 요약 기준, 토큰 받을 때 원문 확인):
음반 정보(CC0)는 상업적으로 써도 되고, 장터 가격 · 회원 데이터(Restricted Data)는 상업적으로 쓸 수 없으며, 쓴 데이터 옆에 출처 표시가 필요합니다.
그래서 판본 · 형식 · 색 · 발매일만 가져오고 가격 · 이미지는 쓰지 않습니다.
아티스트는 MusicBrainz 가 이어 둔 Discogs id 로만 찾습니다 — 이름 검색 결과는 판마다 아티스트 id 를 확인해 다른 아티스트 것을 버립니다.
"""
import json
import re
from datetime import timedelta
from urllib.parse import quote

from ..keys import full_date, norm

API = "https://api.discogs.com"
FORMATS = {"vinyl": "LP", "cd": "CD", "cassette": "CASSETTE"}


def physical_by_title(http, token: str, artist_id: str) -> dict[str, list[dict]]:
    """앨범 제목(정규화) → 그 앨범의 피지컬 판본들. 한국 발매 · 그 Discogs 아티스트가 주 아티스트인 판만."""
    headers = {"Authorization": f"Discogs token={token}"}

    def get(path, days):
        return json.loads(http.get("discogs", f"{API}/{path}", headers=headers, max_age=timedelta(days=days)))

    name = get(f"artists/{artist_id}", 30)["name"]
    results = get(f"database/search?artist={quote(name)}&type=release&country=South%20Korea&per_page=100", 7).get("results", [])
    by_title: dict[str, list[dict]] = {}
    for r in results:
        fmt = next((FORMATS[f.lower()] for f in r.get("format", []) if f.lower() in FORMATS), None)
        if fmt is None:
            continue
        rel = get(f"releases/{r['id']}", 30)
        if not any(str(a.get("id")) == str(artist_id) for a in rel.get("artists", [])):
            continue                                            # 이름만 비슷한 다른 아티스트
        item = _physical(rel, fmt)
        for title in {rel.get("title", ""), *rel.get("title", "").split(" = ")}:   # "떠나지마 = Thirsty" 같은 병기
            if norm(title):
                by_title.setdefault(norm(title), []).append(item)
    return by_title


def _physical(rel: dict, fmt: str) -> dict:
    f = next((x for x in rel.get("formats", []) if FORMATS.get(x.get("name", "").lower()) == fmt), {})
    words = [*f.get("descriptions", []), *([f["text"]] if f.get("text") else [])]
    color_text = f.get("text") or ""
    colored = fmt == "LP" and bool(color_text) and "black" not in color_text.lower()
    text = description(rel, f, fmt)   # 11/26 판 상세 설명 (Discogs 음반 정보 · CC0 · 출처 표시) — 없으면 칸을 넣지 않음 (null = Catalog 가 그대로 둠)
    return {
        **({"description": text} if text else {}),
        "external_id": f"discogs-{rel['id']}",
        "format": fmt,
        "name": (f"{fmt} · " + " · ".join(words))[:255] if words else fmt,
        "released_at": full_date(rel.get("released")),
        # 판매가 · 재고는 관리자가 넣는 값 (11/18) → null 로 보냄 (Catalog 는 null 이면 저장된 값을 그대로 둠)
        "options": [{"name": (color_text or "기본")[:100], "vinyl_color": ("COLOR" if colored else "BLACK") if fmt == "LP" else None,
                     "size": None, "price": None, "qty": None}],
    }


# 11/26 판 상세 설명 — Discogs 음반 정보(CC0)만으로 만듦 (Gemini 안 씀 · 이미지는 CC0 가 아니라 안 씀). Catalog 칸이 1,000자라 그 안에서
WORDS = {"Album": "정규", "Mini-Album": "미니 앨범", "EP": "EP", "Single": "싱글", "Compilation": "컴필레이션", "Limited Edition": "한정판",
         "Numbered": "넘버링", "Reissue": "재발매", "Repress": "재프레스", "Remastered": "리마스터", "Deluxe Edition": "디럭스 에디션",
         "Special Edition": "스페셜 에디션", "Gatefold": "게이트폴드", "180 Gram": "180g", "Picture Disc": "픽처 디스크", "Promo": "프로모",
         "Stereo": "스테레오", "Mono": "모노", "Club Edition": "클럽 에디션", "Unofficial Release": "비공식 발매", "Box Set": "박스 세트",
         '12"': "12인치", '10"': "10인치", '7"': "7인치"}
SAME = {"LP", "CD", "Cassette", "Vinyl"}
COUNTRY = {"South Korea": "한국", "Japan": "일본", "US": "미국", "UK": "영국", "Europe": "유럽", "Taiwan": "대만"}
MAX_DESCRIPTION = 1000
UNITS = {"LP": "장", "CD": "장", "CASSETTE": "개"}


def description(rel: dict, f: dict, fmt: str) -> str | None:
    """판 정보(형식 · 색 · 레이블 · 발매) + 수록곡 (면 표시) 마크다운. 쓸 값이 없으면 None"""
    qty = int(f.get("qty") or 1) if str(f.get("qty") or "1").isdigit() else 1
    kinds = [WORDS.get(w, w) for w in f.get("descriptions", []) if w not in SAME]
    info = [f"형식: {' · '.join([f'{fmt} {qty}{UNITS[fmt]}' if qty > 1 else fmt, *kinds])}"]
    if f.get("text"):
        info.append(f"{'색' if fmt == 'LP' else '비고'}: {' '.join(f['text'].split())}")
    labels = [" ".join(x for x in (lb.get("name"), f"({lb['catno']})" if lb.get("catno") and lb["catno"] != "none" else None) if x)
              for lb in rel.get("labels", []) if lb.get("name")]
    if labels:
        info.append("레이블: " + " · ".join(dict.fromkeys(labels)))
    released = re.sub(r"(-00)+$", "", rel.get("released") or "")      # Discogs 는 모르는 월 · 일을 00 으로
    when = " · ".join(x for x in (released, COUNTRY.get(rel.get("country"), rel.get("country"))) if x)
    if when:
        info.append(f"발매: {when}")
    tracks = [" ".join(x for x in (t.get("position"), " ".join((t.get("title") or "").split())) if x)
              for t in rel.get("tracklist", []) if t.get("type_", "track") == "track" and t.get("title")]
    if len(info) == 1 and not tracks:
        return None                                  # 형식뿐 — 판 이름에 이미 있음
    tail = ["", f"정보 출처: Discogs ({rel['uri']})" if rel.get("uri") else "정보 출처: Discogs"]
    head = ["## 판 정보", *[f"- {x}" for x in info]]
    lines = list(head)
    if tracks:
        lines += ["", "## 수록곡"]
        for i, t in enumerate(tracks):
            rest = len(tracks) - i
            more = [f"- 외 {rest}곡"] if rest else []
            if len("\n".join(lines + [f"- {t}"] + more + tail)) > MAX_DESCRIPTION - 20:
                lines.append(f"- 외 {rest}곡")
                break
            lines.append(f"- {t}")
    text = "\n".join(lines + tail)
    return text if len(text) <= MAX_DESCRIPTION else "\n".join(head + tail)[:MAX_DESCRIPTION]
