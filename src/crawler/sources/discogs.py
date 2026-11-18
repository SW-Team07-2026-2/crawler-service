"""Discogs API — 피지컬 앨범(LP · CD · 카세트)의 판본 · 색.

토큰 파일(discogs-token)이 있을 때만 씁니다. 11/18 확인 (약관 페이지는 자동 접근 403 — 검색 요약 기준, 토큰 받을 때 원문 확인):
음반 정보(CC0)는 상업적으로 써도 되고, 장터 가격 · 회원 데이터(Restricted Data)는 상업적으로 쓸 수 없으며, 쓴 데이터 옆에 출처 표시가 필요합니다.
그래서 판본 · 형식 · 색 · 발매일만 가져오고 가격 · 이미지는 쓰지 않습니다.
아티스트는 MusicBrainz 가 이어 둔 Discogs id 로만 찾습니다 — 이름 검색 결과는 판마다 아티스트 id 를 확인해 다른 아티스트 것을 버립니다.
"""
import json
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
    return {
        "external_id": f"discogs-{rel['id']}",
        "format": fmt,
        "name": (f"{fmt} · " + " · ".join(words))[:255] if words else fmt,
        "released_at": full_date(rel.get("released")),
        # 판매가 · 재고는 관리자가 넣는 값 (11/18) → null 로 보냄 (Catalog 는 null 이면 저장된 값을 그대로 둠)
        "options": [{"name": (color_text or "기본")[:100], "vinyl_color": ("COLOR" if colored else "BLACK") if fmt == "LP" else None,
                     "size": None, "price": None, "qty": None}],
    }
