"""인디스트릿(indistreet.com) 최근 발매 앨범 - 페이지에 들어 있는 목록 데이터(발매일 순 최근 40개)에서 사실만 씁니다.

확인:
- robots.txt 는 일반 수집기(*)에 /albums 를 허용하고 /api/ 는 막습니다 -> 페이지만 봅니다. 쪽 넘김(?page=)은 페이지가 무시하고
  다음 쪽은 막힌 /api/ 라 최근 40개만 봅니다 (한 달 5개 안팎이라 하루 한 번이면 새 발매는 놓치지 않음).
- AI 수집 로봇(ClaudeBot, GPTBot 등)은 사이트 전체를 막아 두었습니다 -> 이 데이터는 AI 모델(Gemini)에 넣지 않습니다 (전처리 없이 씀).
- 약관상 글, 이미지 저작권은 올린 회원에게 있습니다 -> 소개 글, 표지는 가져오지 않고 제목, 종류, 발매일, 아티스트 이름만.
"""
import json
import re
from datetime import timedelta

from ..keys import full_date

URL = "https://indistreet.com/albums"
TYPES = {"FULL_ALBUM": "ALBUM", "EP": "EP", "SINGLE": "SINGLE"}
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def latest(http) -> list[dict]:
    """[{id, title, titles, type, released_at, url, artists: [[이름들], ...]}] - 아티스트 이름은 한글, 영어, 표시 이름 순"""
    out = []
    for a in _albums(http.get("indistreet", URL, max_age=timedelta(hours=12))):
        type_, title = TYPES.get(a.get("type")), (a.get("defaultName") or "").strip()
        if not type_ or not title or a.get("isRemoved"):
            continue
        artists = [list(dict.fromkeys(n.strip() for n in (x.get("nameKo"), x.get("nameEn"), x.get("defaultName")) if n and n.strip()))
                   for x in a.get("artists") or [] if not x.get("isRemoved")]
        out.append({"id": a["documentId"], "title": title,
                    "titles": list(dict.fromkeys(t.strip() for t in (title, a.get("nameKo"), a.get("nameEn")) if t and t.strip())),
                    "type": type_, "released_at": full_date(a.get("releaseDate")),
                    "url": f"{URL}/{a['slug']}", "artists": [n for n in artists if n]})
    return out


def _albums(body: str) -> list[dict]:
    """Next.js 가 페이지에 넣은 데이터 조각을 이어 붙여 initialData 를 읽음"""
    flight = "".join(json.loads(c) for c in _PUSH.findall(body))
    start = flight.find('{"initialData"')
    if start < 0:
        raise ValueError("인디스트릿 앨범 목록 데이터가 없습니다 — 페이지 모양이 바뀌었는지 확인")
    data, _ = json.JSONDecoder().raw_decode(flight, start)
    return data["initialData"]["data"]
