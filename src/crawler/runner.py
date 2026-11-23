"""실행 한 번 (새벽 · 점심) — crawl_run 으로 겹침을 막고 건수 · 토큰 · 오류를 남깁니다."""
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import db as dbm
from .keys import norm
from .pipeline import Pipeline
from .webtools import MONTHLY_SEARCHES, RUN_BUDGET, SEARCH_RESERVE

log = logging.getLogger(__name__)
STALE = timedelta(hours=6)          # 이보다 오래된 RUNNING 은 죽은 실행으로 봄
MAX_NEW_ARTISTS = 150               # 공연 출연진에서 찾은 새 이름은 한 번에 이만큼만 (MusicBrainz 초당 1건 · 11/23 50 → 150)
MISS_RETRY = timedelta(days=30)     # 못 찾은 이름은 이만큼 지나야 다시 찾음 (MusicBrainz 검색 캐시와 같은 30일)
KST = ZoneInfo("Asia/Seoul")


def run(kind: str, sessions, pipeline: Pipeline, seeds: list[str]) -> dict:
    """kind: dawn(전체) · lunch(공연만). 이미 돌고 있으면 건너뜀."""
    with sessions() as s:
        busy = s.scalar(select(dbm.CrawlRun).where(dbm.CrawlRun.status == "RUNNING",
                                                   dbm.CrawlRun.started_at > dbm.now() - STALE))
        if busy is not None:
            log.warning("crawl_run_skipped kind=%s busy_run=%s", kind, busy.id)
            return {"skipped": busy.id}
        row = dbm.CrawlRun(kind=kind, status="RUNNING", started_at=dbm.now())
        s.add(row)
        s.commit()
        run_id = row.id

    errors: list[str] = []

    def stage(name, fn):
        """단계 하나 — 실패해도 다음 단계는 계속 (11/19 KOPIS 목록 400 하나로 새벽 실행 전체가 멈췄음). 실패한 단계는 다음 실행에 다시"""
        try:
            return fn()
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
            log.exception("crawl_stage_failed kind=%s run=%s stage=%s", kind, run_id, name)
            return None

    found = stage("공연", pipeline.concerts) or []
    concerts_failed = bool(errors)
    if kind == "dawn":
        found += stage("인디스트릿", pipeline.indistreet) or []   # 인디스트릿 최근 발매 — 아티스트 이름은 새 아티스트 찾기에도
        def artists():
            new = _new_names(sessions, found)
            pipeline.stats["artist_new_names"] = len(new)   # 0 이면 더 찾을 새 이름이 없음 (연달아 돌릴 때 멈추는 기준)
            pipeline.artists(seeds + new)
        stage("아티스트", artists)
        stage("다시 보기", pipeline.refresh_known)                # 시드 · 공연에 없는, 전에 찾은 아티스트의 신보
        if concerts_failed:   # 한 번 더 — 11/22 KOPIS 차단이 몇 분 이어져 공연만 실패 · 아티스트 단계(1~3시간) 뒤엔 풀려 있을 때가 많음
            first = errors.pop(0)
            pipeline.stats["concert_stage_retry"] += 1
            if stage("공연 다시", pipeline.concerts) is None:
                errors.insert(0, first)
    stage("전송", pipeline.send)
    status = "FAILED" if errors else "DONE"
    error = " | ".join(errors)[:2000] or None

    llm = pipeline.llm
    with sessions() as s:
        row = s.get(dbm.CrawlRun, run_id)
        row.status, row.error, row.ended_at = status, error, dbm.now()
        if pipeline.searcher is not None and getattr(pipeline.searcher, "searches", 0):
            pipeline.stats["web_search_requests"] = pipeline.searcher.searches   # Tavily 크레딧 (무료 월 1,000)
        if pipeline.searcher is not None and getattr(pipeline.searcher, "fetches", 0):
            pipeline.stats["page_fetches"] = pipeline.searcher.fetches           # Scrapling MCP 로 연 페이지
        row.stats_json = json.dumps(dict(pipeline.stats), ensure_ascii=False, sort_keys=True)
        row.input_tokens = llm.input_tokens if llm else 0
        row.output_tokens = llm.output_tokens if llm else 0
        s.commit()
    log.info("crawl_run_done kind=%s run=%s status=%s stats=%s", kind, run_id, status, row.stats_json)
    return {"run": run_id, "status": status, "stats": dict(pipeline.stats), "error": error,
            "input_tokens": row.input_tokens, "output_tokens": row.output_tokens}


def _new_names(sessions, names: list[str]) -> list[str]:
    """공연 출연진 중 아직 대표 키에 없고, 30일 안에 못 찾은 적도 없는 이름"""
    out = []
    with sessions() as s:
        missed = set(s.scalars(select(dbm.ArtistMiss.name_norm).where(dbm.ArtistMiss.checked_at > dbm.now() - MISS_RETRY)))
        for n in dict.fromkeys(names):
            if dbm.lookup_artist_key(s, n) is None and norm(n)[:200] not in missed:
                out.append(n)
            if len(out) >= MAX_NEW_ARTISTS:
                break
    return out


def search_budget(sessions, now: datetime | None = None) -> int:
    """이번 실행에 쓸 검색 크레딧 — 이번 달 남은 크레딧(예비 몫 빼고)을 남은 날수로 나눈 것이 하루 몫 (한국 시간).
    오늘 앞선 실행이 쓴 만큼 빼고, 실행당 RUN_BUDGET 까지. 덜 쓴 날이 있으면 남은 날의 몫이 늘어납니다."""
    day = (now or datetime.now(KST)).replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    days_left = ((month + timedelta(days=32)).replace(day=1) - day).days     # 오늘 포함
    used_month, used_today = _searches_since(sessions, month), _searches_since(sessions, day)
    daily = (MONTHLY_SEARCHES - SEARCH_RESERVE - (used_month - used_today)) // days_left
    return max(0, min(RUN_BUDGET, daily - used_today))


def _searches_since(sessions, when: datetime) -> int:
    since = when.astimezone(timezone.utc).replace(tzinfo=None)              # crawl_run 시각은 UTC
    with sessions() as s:
        rows = s.scalars(select(dbm.CrawlRun.stats_json).where(dbm.CrawlRun.started_at >= since)).all()
    return sum(json.loads(r).get("web_search_requests", 0) for r in rows if r)


def read_seeds(path) -> list[str]:
    """한 줄에 아티스트 이름 하나 · # 은 주석"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]
