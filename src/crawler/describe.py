"""소개 글 검사 - Gemini 가 쓴 문장(llm.BIO_SYSTEM, ALBUM_SYSTEM)을 출처와 대조해 거른 뒤 이어 붙입니다.

문장마다: 근거 출처 번호가 있고, 숫자, 괄호 속 제목이 그 출처 글에 있고, 개인정보 낱말, 본명이 없어야 남습니다.
남은 문장이 모자라면 None - 후보에 null 로 가서 관리자가 승인할 때 씁니다 (사용자: 찾아서 안 나오면 빈칸).
"""
import re

from .keys import HANGUL, norm

# 개인정보, 사건 - 출처에 있어도 소개 글에는 쓰지 않음 (한로로 문서 첫 줄에 생년월일, 본명, 태어난 곳, 학교)
PRIVATE = re.compile("|".join([
    r"본명", r"태어", r"출생", r"생년", r"\d+\s*년생", r"\d+\s*일생", r"\d+\s*살", r"만\s*\d+\s*세",
    r"학교", r"대학", r"입학", r"졸업", r"재학", r"가족", r"부모", r"아버지", r"어머니", r"남편", r"아내",
    r"결혼", r"이혼", r"열애", r"연애", r"병역", r"입대", r"전역", r"군 복무", r"사망", r"별세", r"투병",
    r"논란", r"소송", r"구속", r"혐의", r"폭행",
]))
TITLES = re.compile(r"[《〈「‘“'\"<]([^》〉」’”'\">]{1,80})[》〉」’”'\">]")
NUMBERS = re.compile(r"\d+")
_LEGAL = re.compile(r"본명\s*[:：]?\s*(?:은|는)?\s*([가-힣]{2,5}|[A-Za-z][A-Za-z .'-]{1,40}[A-Za-z])")
MAX_SENTENCE = 160


def legal_names(texts) -> set[str]:
    """출처 글의 '본명: 한지수' 같은 값 - 소개 글에 이 이름이 나오면 그 문장을 뺌"""
    return {m.group(1).strip() for t in texts for m in _LEGAL.finditer(t or "")}


def checked(raw: list[dict], sources: list[dict], *, max_sentences: int, max_chars: int, min_sentences: int,
            banned_names=(), facts_no: int | None = None) -> tuple[str | None, set[int]]:
    """-> (이어 붙인 글 또는 None, 쓴 출처 번호). sources: [{no, text, ...}].
    facts_no: 사실 값만 담은 출처(앨범의 MusicBrainz 값) 번호 - 그 출처만 쓴 글은 None (틀만 채운 글은 쓰지 않음, 사용자)"""
    by_no = {s["no"]: s for s in sources}
    banned = {n for n in banned_names if n and len(n) >= 2}
    kept: list[tuple[str, int]] = []
    for s in raw:
        text = " ".join(str(s.get("text") or "").split())
        src = by_no.get(s.get("source"))
        if not text or src is None or not HANGUL.search(text) or len(text) > MAX_SENTENCE:
            continue
        if PRIVATE.search(text) or any(n in text for n in banned):
            continue
        body = src["text"]
        if any(n not in body for n in NUMBERS.findall(text)):
            continue                                    # 출처에 없는 숫자(연도, 인원, 곡 수) - 지어냈을 수 있음
        if any(norm(t) and norm(t) not in norm(body) for t in TITLES.findall(text)):
            continue                                    # 출처에 없는 앨범, 곡 제목
        if text not in {k for k, _ in kept}:
            kept.append((text, src["no"]))
    out: list[tuple[str, int]] = []
    for text, no in kept[:max_sentences]:
        if len(" ".join([t for t, _ in out] + [text])) > max_chars:
            break
        out.append((text, no))
    used = {no for _, no in out}
    if len(out) < min_sentences or (facts_no is not None and not used - {facts_no}):
        return None, set()
    return " ".join(t for t, _ in out), used
