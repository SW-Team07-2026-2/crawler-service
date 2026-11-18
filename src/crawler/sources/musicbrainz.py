"""MusicBrainz API — 아티스트 · 앨범(release group) · 트랙 · 레이블.

쓰는 것은 CC0 인 핵심 데이터(이름 · 별칭 · 앨범 · 트랙 · 날짜 · 레이블)뿐입니다.
태그 · 장르 연결은 보조 데이터(CC BY-NC-SA — 비상업)라 가져오지 않습니다 (doc/MusicBrainz_Database).
"""
import json
from datetime import timedelta
from urllib.parse import quote

from ..keys import full_date, norm

API = "https://musicbrainz.org/ws/2"
TYPES = {"Album": "ALBUM", "EP": "EP", "Single": "SINGLE"}


def _get(http, path: str, days: int) -> dict:
    return json.loads(http.get("musicbrainz", f"{API}/{path}", max_age=timedelta(days=days)))


def find_artist(http, name: str) -> dict | None:
    """이름으로 찾기 — 점수 95 이상이고 이름(또는 별칭)이 정규화해서 같고 한국 아티스트인 것만. 애매하면 None (억지로 고르지 않음).
    띄어쓰기가 다르면 검색이 못 찾습니다 (브로콜리너마저 ↔ 브로콜리 너마저) — 시드에는 MusicBrainz 표기나 영문 이름을 씁니다."""
    safe = name.replace('"', " ")
    q = quote(f'artist:"{safe}" OR alias:"{safe}"')         # 한글 이름이 별칭에만 있는 경우가 많음 (SE SO NEON · 새소년)
    found = _get(http, f"artist?query={q}&fmt=json&limit=5", days=30).get("artists", [])
    target = norm(name)
    for a in found:
        names = {norm(a.get("name", "")), norm(a.get("sort-name", ""))} | {norm(x.get("name", "")) for x in a.get("aliases", [])}
        if a.get("score", 0) < 95 or target not in names:
            continue
        # 나라가 비어 있고 도시만 있는 경우가 있음 (Say Sue Me → Busan) — 그때는 지역을 따라 올라가 봄
        korean = a.get("country") == "KR" or (a.get("country") is None and _in_korea(http, a.get("area") or a.get("begin-area")))
        if korean:
            return artist(http, a["id"])
    return None


def bands_of(http, person: str) -> list[str]:
    """사람 이름 → 그 사람이 멤버인 밴드 · 팀 이름 (MusicBrainz "member of band"). 공연 출연진이 멤버 실명일 때 팀 이름 힌트로 씁니다.
    이름(또는 별칭)이 정확히 같고 점수 95 이상인 한국 사람만 · 같은 이름이 여럿이면 모두의 팀을 돌려줍니다 (Gemini 가 포스터로 확인)."""
    safe = person.replace('"', " ")
    q = quote(f'(artist:"{safe}" OR alias:"{safe}") AND type:person')
    found = _get(http, f"artist?query={q}&fmt=json&limit=5", days=30).get("artists", [])
    target = norm(person)
    bands: set[str] = set()
    for a in found:
        names = {norm(a.get("name", "")), norm(a.get("sort-name", ""))} | {norm(x.get("name", "")) for x in a.get("aliases", [])}
        if a.get("score", 0) < 95 or target not in names:
            continue
        if not (a.get("country") == "KR" or (a.get("country") is None and _in_korea(http, a.get("area") or a.get("begin-area")))):
            continue
        rels = _get(http, f"artist/{a['id']}?inc=artist-rels&fmt=json", days=30).get("relations", [])
        bands |= {r["artist"]["name"] for r in rels
                  if r.get("type") == "member of band" and r.get("direction") == "forward" and r.get("artist", {}).get("name")}
    return sorted(bands)


def _in_korea(http, area: dict | None, depth: int = 0) -> bool:
    """지역(도시 · 구)이 한국인지 — ISO 코드(KR · KR-26)를 보거나 "part of" 로 위 지역을 따라 올라감 (최대 4단계)"""
    if not area or not area.get("id"):
        return False
    a = _get(http, f"area/{area['id']}?inc=area-rels&fmt=json", days=180)
    if "KR" in (a.get("iso-3166-1-codes") or []) or any(c.startswith("KR-") for c in a.get("iso-3166-2-codes") or []):
        return True
    parent = next((r["area"] for r in a.get("relations", [])
                   if r.get("type") == "part of" and r.get("direction") == "backward" and r.get("area")), None)
    return depth < 4 and _in_korea(http, parent, depth + 1)


def artist(http, mbid: str) -> dict:
    """discogs: MusicBrainz 편집자가 이어 둔 Discogs 아티스트 id (하나일 때만 — 이름으로 찾으면 동명이인이 섞임)"""
    a = _get(http, f"artist/{mbid}?inc=aliases+url-rels&fmt=json", days=7)
    links = {r["url"]["resource"].rstrip("/").rsplit("/", 1)[-1] for r in a.get("relations", [])
             if r.get("type") == "discogs" and "/artist/" in r.get("url", {}).get("resource", "")}
    return {"mbid": a["id"], "name": a["name"], "aliases": sorted({x["name"] for x in a.get("aliases", [])} - {a["name"]}),
            "discogs": links.pop() if len(links) == 1 else None}


def release_groups(http, mbid: str) -> list[dict]:
    """정규 · EP · 싱글 (편집 · 라이브 · OST 같은 2차 유형이 붙은 것은 뺌) — 발매일 순. 100개가 넘으면 다음 쪽까지."""
    found, offset = [], 0
    while True:
        data = _get(http, f"release-group?artist={mbid}&type=album|ep|single&fmt=json&limit=100&offset={offset}", days=7)
        page = data.get("release-groups", [])
        found += page
        offset += len(page)
        if not page or offset >= data.get("release-group-count", 0):
            break
    out = []
    for g in found:
        if g.get("primary-type") in TYPES and not g.get("secondary-types"):
            out.append({"rgid": g["id"], "title": g["title"], "type": TYPES[g["primary-type"]],
                        "released_at": full_date(g.get("first-release-date"))})
    return sorted(out, key=lambda g: g["released_at"] or "9999")


def first_release(http, rgid: str, days: int = 90) -> dict:
    """그 앨범의 가장 이른 공식 발매본 — 트랙 · 레이블. 갓 나온 앨범은 트랙 · 레이블이 나중에 채워지는 일이 많아 짧게(days) 다시 봅니다."""
    data = _get(http, f"release?release-group={rgid}&status=official&inc=recordings+labels&fmt=json&limit=25", days=days)
    releases = sorted(data.get("releases", []), key=lambda r: r.get("date") or "9999")
    if not releases:
        return {"tracks": [], "label": None}
    r = releases[0]
    tracks, no = [], 0
    for medium in r.get("media", []):
        for t in medium.get("tracks", []):
            no += 1
            tracks.append({"track_no": no, "title": t["title"][:200]})
    labels = [li["label"]["name"] for li in r.get("label-info", []) if li.get("label")]
    return {"tracks": tracks[:100], "label": labels[0] if labels else None}
