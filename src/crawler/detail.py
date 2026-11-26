"""11/26 굿즈 · 음반 판본 상세 설명 — 공식 판매 페이지(아티스트 · 레이블)의 글 · 상세 이미지에서.

- 판매처 · 마켓 · SNS 는 열지 않습니다 (SKIP_HOSTS): 김밥레코즈는 약관 제22조(10.6 · 같은 음반 판매처), 다른 판매처 · 플랫폼은 약관 미확인,
  SNS 는 로그인 없이 못 읽음. Discogs 는 판 정보만 CC0 라 discogs.description 으로 따로 (이미지는 쓰지 않음).
- 글은 Gemini 가 사실만 다시 씀 (llm.PRODUCT_SYSTEM) · 상세 이미지는 주소만 넘기고 Studio 가 승인할 때 우리 버킷으로 옮김.
"""
import re
from urllib.parse import urljoin, urlsplit

SKIP_HOSTS = (
    "gimbabrecords.com", "gimbabrecords2.com", "discogs.com",
    "instagram.com", "x.com", "twitter.com", "facebook.com", "youtube.com", "youtu.be", "tiktok.com", "threads.net",
    "kream.co.kr", "musinsa.com", "29cm.co.kr", "coupang.com", "yes24.com", "aladin.co.kr", "kyobobook.co.kr",
    "interpark.com", "11st.co.kr", "gmarket.co.kr", "auction.co.kr", "ssg.com", "lotteon.com", "ebay.com", "amazon.com",
    "melon.com", "bugs.co.kr", "genie.co.kr", "spotify.com", "apple.com",
)
MAX_TEXT = 8000          # Gemini 에 넣는 페이지 글
MAX_DESCRIPTION = 1000   # Catalog 칸 (goods.description · physical_album.description)
_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def skipped(url: str | None) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return not host or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def page_for(data: dict, evidence: list[dict]) -> str | None:
    """상세를 읽을 페이지 — 판매 페이지 주소 → 출처 주소 순으로, 판매처 · SNS 가 아닌 첫 주소"""
    for url in [data.get("purchase_url"), *[e.get("url") for e in evidence or []]]:
        if isinstance(url, str) and url.startswith(("http://", "https://")) and not skipped(url):
            return url
    return None


def image_urls(markdown: str, base: str) -> list[str]:
    """본문 마크다운의 이미지 주소 (나온 순서 · 같은 주소 한 번 · data: 빼기)"""
    out = []
    for src in _IMAGE.findall(markdown or ""):
        url = urljoin(base, src)
        if url.startswith(("http://", "https://")) and url not in out:
            out.append(url)
    return out


def page_text(markdown: str) -> str:
    """이미지 · 링크 주소를 뺀 글 (Gemini 입력)"""
    text = _IMAGE.sub(" ", markdown or "")
    text = _LINK.sub(r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_TEXT]


def markdown(result: dict, source_url: str) -> str | None:
    """Gemini 결과 → 상세 설명 (소개 · 상품 정보 · 관리 · 주의 · 출처). 사실 줄이 하나도 없으면 None (관리자가 씀)"""
    def rows(key, limit):
        vals = [" ".join(str(v).split()) for v in result.get(key) or [] if isinstance(v, str)]
        return [v for v in dict.fromkeys(vals) if v][:limit]

    summary = " ".join(str(result.get("summary") or "").split())
    facts, care = rows("facts", 12), rows("care", 5)
    if not facts:
        return None
    lines = [summary, ""] if summary else []
    lines += ["## 상품 정보", *[f"- {f}" for f in facts], ""]
    if care:
        lines += ["## 관리 · 주의", *[f"- {c}" for c in care], ""]
    tail = f"정보 출처: {urlsplit(source_url).hostname}"
    while len("\n".join(lines + [tail])) > MAX_DESCRIPTION and len(lines) > 3:
        lines.pop(-2 if lines[-1] == "" else -1)       # 끝 줄부터 덜어냄
    text = "\n".join(lines + [tail])
    return text if len(text) <= MAX_DESCRIPTION else None
