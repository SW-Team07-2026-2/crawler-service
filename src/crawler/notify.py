"""실행이 끝나면 Slack 으로 알림 (11/18) — Incoming Webhook 주소는 시크릿 파일 slack-webhook-url (Grafana 장애 알림과 같은 웹훅).
주소가 없으면 보내지 않고, 보내기가 실패해도 실행 결과에는 영향을 주지 않습니다 (기록만)."""
import json
import logging
import re
import urllib.error
import urllib.request

from .http import redact
from .llm import LlmSkip
from .studio import StudioRejected
from .webtools import SearchQuota

log = logging.getLogger(__name__)
KINDS = {"dawn": "새벽", "lunch": "점심"}
TYPES = (("artist", "아티스트"), ("album", "앨범"), ("concert", "공연"))
# 11/24 항목 오류가 난 곳 (Pipeline._fail 의 what) — 알림에 이름과 함께
PLACES = {
    "reviewed_fetch": "Studio 처리 목록", "concert_fetch": "KOPIS 공연 상세", "concert_facility": "KOPIS 공연장",
    "concert_image": "공연 포스터 · 소개 이미지", "concert_cast": "출연진 → 팀 (MusicBrainz)", "concert_ticket": "예매 오픈 검색",
    "indistreet": "인디스트릿", "artist": "아티스트 찾기 (MusicBrainz)", "artist_refresh": "아티스트 다시 보기 (MusicBrainz)",
    "discogs": "Discogs 피지컬", "gimbab": "김밥레코즈", "web_search": "웹 조사 (신보 · 굿즈)", "artist_name_search": "한글 이름 검색",
}
SAMPLES = 3   # 오류 종류마다 예시 수


def error_code(e: BaseException) -> str:
    """알림에 보일 짧은 오류 코드 — HTTP 상태 · Studio 코드 · Gemini 사유 · 시간 초과 · 예외 이름"""
    text = str(e)
    if isinstance(e, StudioRejected):
        code = re.search(r'"code"\s*:\s*"([A-Z0-9_]+)"', text)
        return f"Studio {e.status}" + (f" {code.group(1)}" if code else "")
    if isinstance(e, LlmSkip):
        return f"Gemini {text}"
    if isinstance(e, SearchQuota):
        return "검색 한도"
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    status = re.search(r"→ (\d{3})\b", text)                  # http.py · webtools 의 "GET … → 404"
    if status:
        return f"HTTP {status.group(1)}"
    if isinstance(e, TimeoutError) or "timed out" in text.lower():
        return "시간 초과"
    if isinstance(e, urllib.error.URLError):
        return f"연결 실패 ({type(e.reason).__name__})"
    return type(e).__name__


def _safe(text: str, limit: int) -> str:
    """키는 가리고(redact) Slack 이 꺾쇠를 링크로 읽지 않게"""
    text = redact(" ".join(str(text).split()))[:limit]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def failure_lines(failures: dict) -> list[str]:
    """{what: {count, codes: {코드: 수}, samples: [(항목, 내용)]}} → 많은 순으로 한 줄씩 + 예시"""
    out = []
    for what, f in sorted(failures.items(), key=lambda kv: -kv[1]["count"]):
        codes = " · ".join(f"{c} ×{n}" for c, n in sorted(f["codes"].items(), key=lambda kv: -kv[1]))
        out.append(f"• {PLACES.get(what, what)} ({what}) {f['count']}건 — {_safe(codes, 200)}")
        out += [f"    예) {_safe(item, 120)}: {_safe(text, 200)}" for item, text in f["samples"][:SAMPLES]]
    return out


def message(kind: str, result: dict, minutes: float) -> str:
    """runner.run 결과 → Slack 글"""
    name = f"크롤러 {KINDS.get(kind, kind)} 실행"
    if "skipped" in result:
        return f"{name} 건너뜀 — 앞선 실행 #{result['skipped']} 이 아직 돌고 있습니다"
    stats = result["stats"]
    lines = [f"{name} {'끝' if result['status'] == 'DONE' else '실패'} · #{result['run']} · {minutes:.0f}분"]
    if result.get("stage_errors"):                      # 11/24 단계마다 한 줄 (코드 · 어느 단계)
        lines.append(f"단계 실패 {len(result['stage_errors'])}건")
        lines += [f"• {_safe(e, 400)}" for e in result["stage_errors"]]
    elif result.get("error"):
        lines.append(f"오류: {result['error'][:500]}")
    sent = [f"{label} {sum(v for k, v in stats.items() if k.startswith(f'send_{t}_') and k != f'send_{t}_failed')}"
            + (f" (거절 {stats[f'send_{t}_failed']})" if stats.get(f"send_{t}_failed") else "")
            for t, label in TYPES]
    lines.append("Studio 로 보냄: " + " · ".join(sent))
    errors = sum(v for k, v in stats.items() if k.endswith("_error"))
    if errors:
        lines.append(f"항목 오류 {errors}건 (어디서 · 코드 · 예시)" if result.get("failures") else f"항목 오류 {errors}건 (아래 *_error)")
        lines += failure_lines(result.get("failures") or {})
    skipped = {k: v for k, v in stats.items() if "_llm_" in k}   # Gemini 한도 · 503 등으로 다음 실행에 넘긴 것
    if skipped:
        lines.append("Gemini 로 못 한 것 (다음 실행에): " + " · ".join(f"{k} {v}" for k, v in sorted(skipped.items())))
    lines.append(f"Gemini 토큰 입력 {result.get('input_tokens', 0):,} · 출력 {result.get('output_tokens', 0):,}")
    if stats:
        lines.append("```" + "\n".join(f"{k} {v}" for k, v in sorted(stats.items())) + "```")
    return "\n".join(lines)


def send(url: str | None, text: str, post=None):
    if not url:
        return
    try:
        (post or _post)(url, {"text": text})
    except Exception as e:  # 알림 실패로 실행을 실패로 만들지 않음 · 주소(비밀)는 찍지 않음
        log.warning("slack_notify_failed error=%s", type(e).__name__)


def _post(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as res:
        res.read()
