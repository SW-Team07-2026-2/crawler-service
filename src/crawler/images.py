"""공연 포스터 · 소개 이미지를 Gemini 가 읽을 수 있게 — 너비를 줄이고 긴 이미지는 위에서부터 잘라 JPEG 로.

KOPIS 소개 이미지는 세로로 아주 긴 경우가 많고(예매 일정 · 출연진 · 공지가 다 들어 있음), API 는 한 변 8000px 이 넘는 이미지를 받지 않습니다.
"""
import io

from PIL import Image

WIDTH = 1000          # 이보다 넓으면 줄임 (글자가 읽히는 정도)
HEIGHT = 1500         # 한 조각 높이
MAX_SLICES = 24       # 공연 한 건에 보낼 조각 상한 (API 는 요청당 이미지 100개)


def slices(data: bytes, width: int = WIDTH, height: int = HEIGHT, limit: int = MAX_SLICES) -> list[bytes]:
    """이미지 바이트 → JPEG 조각들 (위에서부터). 이미지가 아니면 ValueError"""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError(f"이미지가 아님: {type(e).__name__}") from None
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)                                   # 움직이는 GIF 는 첫 장
    img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    out = []
    for top in range(0, img.height, height):
        if len(out) >= limit:
            break
        buf = io.BytesIO()
        img.crop((0, top, img.width, min(img.height, top + height))).save(buf, "JPEG", quality=85)
        out.append(buf.getvalue())
    return out


def size(data: bytes) -> tuple[int, int]:
    """11/26 (너비, 높이) — 이미지가 아니면 ValueError (상세 이미지 고르기: 아이콘 · 작은 썸네일 빼기)"""
    try:
        img = Image.open(io.BytesIO(data))
        return img.width, img.height
    except Exception as e:
        raise ValueError(f"이미지가 아님: {type(e).__name__}") from None
