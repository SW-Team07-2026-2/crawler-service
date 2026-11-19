"""실행 한 번 (새벽 · 점심) — crawl_run 으로 겹침을 막고 건수 · 토큰 · 오류를 남깁니다."""
import json
import logging
from datetime import timedelta

from sqlalchemy import select

from . import db as dbm
from .pipeline import Pipeline

log = logging.getLogger(__name__)
STALE = timedelta(hours=6)          # 이보다 오래된 RUNNING 은 죽은 실행으로 봄
MAX_NEW_ARTISTS = 50                # 공연 출연진에서 찾은 새 이름은 한 번에 이만큼만 (MusicBrainz 초당 1건)


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
    if kind == "dawn":
        found += stage("인디스트릿", pipeline.indistreet) or []   # 인디스트릿 최근 발매 — 아티스트 이름은 새 아티스트 찾기에도
        stage("아티스트", lambda: pipeline.artists(seeds + _new_names(sessions, found)))
        stage("다시 보기", pipeline.refresh_known)                # 시드 · 공연에 없는, 전에 찾은 아티스트의 신보
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
    """공연 출연진 중 아직 대표 키에 없는 이름"""
    out = []
    with sessions() as s:
        for n in dict.fromkeys(names):
            if dbm.lookup_artist_key(s, n) is None:
                out.append(n)
            if len(out) >= MAX_NEW_ARTISTS:
                break
    return out


def read_seeds(path) -> list[str]:
    """한 줄에 아티스트 이름 하나 · # 은 주석"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]
