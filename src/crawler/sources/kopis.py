"""KOPIS 공연예술통합전산망 오픈 API - 대중음악(CCCD) 공연 목록, 상세. 서비스 키는 kopis.or.kr 에서 발급 (출처 표시).

필드 이름은 KOPIS 오픈 API 안내 기준이고, 키가 없어 에는 실제 응답으로 확인하지 못했습니다 - 키를 받은 뒤
`crawler run lunch --dry-run` 으로 첫 응답을 보고 맞춥니다. 상세는 통째로(dict) Gemini 에 넘기므로 이름이 조금 달라도 동작합니다.
"""
import xml.etree.ElementTree as ET
from datetime import date, timedelta

API = "https://www.kopis.or.kr/openApi/restful"
POP_MUSIC = "CCCD"


def list_ids(http, key: str, start: date, end: date, max_pages: int = 10) -> list[str]:
    ids: list[str] = []
    for page in range(1, max_pages + 1):
        url = (f"{API}/pblprfr?service={key}&stdate={start:%Y%m%d}&eddate={end:%Y%m%d}"
               f"&cpage={page}&rows=100&shcate={POP_MUSIC}")
        rows = parse(http.get("kopis", url, max_age=timedelta(hours=6)))
        ids += [r["mt20id"] for r in rows if r.get("mt20id")]
        if len(rows) < 100:
            break
    return ids


def detail(http, key: str, mt20id: str) -> dict:
    rows = parse(http.get("kopis", f"{API}/pblprfr/{mt20id}?service={key}", max_age=timedelta(hours=6)))
    return rows[0] if rows else {}


def facility(http, key: str, mt10id: str) -> dict:
    """공연시설 상세 - 주소(adres), 전체 좌석(seatscale), 공연장(mt13s: [{mt13id, prfplcnm, seatscale}]). 시설 정보는 잘 안 바뀌어 7일 캐시"""
    if not mt10id:
        return {}
    rows = parse(http.get("kopis", f"{API}/prfplc/{mt10id}?service={key}", max_age=timedelta(days=7)))
    return rows[0] if rows else {}


def hall(fac: dict, mt13id: str | None) -> dict:
    """그 공연이 열리는 공연장(관) - 이름, 좌석. 못 찾으면 빈 dict"""
    for h in fac.get("mt13s") or []:
        if isinstance(h, dict) and mt13id and h.get("mt13id") == mt13id:
            return h
    return {}


def parse(xml_text: str) -> list[dict]:
    """<dbs><db>...</db></dbs> -> [{태그: 글자}], 하위 목록(styurls, relates)은 리스트로. 오류 응답(returncode)은 예외."""
    root = ET.fromstring(xml_text)
    out = []
    for db in root.findall("db"):
        row = {}
        for child in db:
            if len(child):
                row[child.tag] = [_flat(c) for c in child]
            else:
                row[child.tag] = (child.text or "").strip()
        if "returncode" in row and row.get("returncode") not in ("00", ""):
            raise RuntimeError(f"KOPIS {row.get('returncode')} {row.get('errmsg')}")
        out.append(row)
    return out


def _flat(el):
    if len(el):
        return {c.tag: (c.text or "").strip() for c in el}
    return (el.text or "").strip()


def window(days: int, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    return today, today + timedelta(days=days)
