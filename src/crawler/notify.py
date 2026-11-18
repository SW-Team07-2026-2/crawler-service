"""실행이 끝나면 Slack 으로 알림 (11/18) — Incoming Webhook 주소는 시크릿 파일 slack-webhook-url (Grafana 장애 알림과 같은 웹훅).
주소가 없으면 보내지 않고, 보내기가 실패해도 실행 결과에는 영향을 주지 않습니다 (기록만)."""
import json
import logging
import urllib.request

log = logging.getLogger(__name__)
KINDS = {"dawn": "새벽", "lunch": "점심"}
TYPES = (("artist", "아티스트"), ("album", "앨범"), ("concert", "공연"))


def message(kind: str, result: dict, minutes: float) -> str:
    """runner.run 결과 → Slack 글"""
    name = f"크롤러 {KINDS.get(kind, kind)} 실행"
    if "skipped" in result:
        return f"{name} 건너뜀 — 앞선 실행 #{result['skipped']} 이 아직 돌고 있습니다"
    stats = result["stats"]
    lines = [f"{name} {'끝' if result['status'] == 'DONE' else '실패'} · #{result['run']} · {minutes:.0f}분"]
    if result.get("error"):
        lines.append(f"오류: {result['error'][:500]}")
    sent = [f"{label} {sum(v for k, v in stats.items() if k.startswith(f'send_{t}_') and k != f'send_{t}_failed')}"
            + (f" (거절 {stats[f'send_{t}_failed']})" if stats.get(f"send_{t}_failed") else "")
            for t, label in TYPES]
    lines.append("Studio 로 보냄: " + " · ".join(sent))
    errors = sum(v for k, v in stats.items() if k.endswith("_error"))
    if errors:
        lines.append(f"항목 오류 {errors}건 (아래 *_error)")
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
