"""이름 정규화, 해시."""
import hashlib
import json
import re
import unicodedata
from datetime import date

_DROP = re.compile(r"[\s\W_]+", re.UNICODE)
HANGUL = re.compile(r"[가-힣]")
LATIN = re.compile(r"[A-Za-z]")


def norm(name: str) -> str:
    """같은 이름 비교용 - 전각, 대소문자, 공백, 기호, 앞의 the 를 무시합니다 ("The Black Skirts" == "black skirts")."""
    s = unicodedata.normalize("NFKC", name or "").lower().strip()
    s = re.sub(r"^the\s+", "", s)
    return _DROP.sub("", s)


def full_date(value: str | None) -> str | None:
    """YYYY-MM-DD 로 읽히는 날짜만 - YYYY, YYYY-MM, 2022-00-00 같은 것은 비움 (Catalog 는 날짜 전체가 필요)"""
    try:
        return date.fromisoformat(value).isoformat() if value and len(value) == 10 else None
    except ValueError:
        return None


def content_hash(value) -> str:
    """정렬한 JSON 의 SHA-256 - Studio 의 source_hash (같으면 UNCHANGED)."""
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
