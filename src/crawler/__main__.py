"""crawler serve                — 새벽 03:30 · 점심 12:30 (KST) 일정으로 계속 실행 (컨테이너 기본 명령)
crawler run dawn|lunch [--dry-run]  — 한 번만 실행. --dry-run 은 Studio 로 보내지 않고 크롤러 DB 에만 남김
실행이 끝나거나 멈추면 Slack 으로 알립니다 (notify.py · dry-run 은 알리지 않음).
"""
import argparse
import logging
import time

from . import config, notify, runner
from . import db as dbm
from .http import Http, redact
from .llm import Llm
from .pipeline import Pipeline
from .sources.websearch import WebSearch
from .studio import Studio
from .webtools import PageReader, Tavily

log = logging.getLogger("crawler")


def build(settings, dry_run: bool):
    sessions = dbm.connect(settings.database_url)
    http = Http(sessions, settings.user_agent)
    llm = Llm(settings.gemini_api_key, settings.model) if settings.gemini_api_key else None
    if llm is None:
        log.warning("gemini-api-key 없음 — 장르 · 공연 정리 · 웹 검색(신보 · 피지컬 · 굿즈)은 건너뜁니다")
    searcher = None
    if llm is not None and settings.tavily_api_key:
        searcher = WebSearch(llm, Tavily(settings.tavily_api_key, budget=runner.search_budget(sessions)),
                             PageReader(http, settings.user_agent))
    elif llm is not None:
        log.warning("tavily-api-key 없음 — 웹 검색(신보 · 피지컬 · 굿즈 · 예매 오픈)은 건너뜁니다")
    studio = None if dry_run else Studio(settings.studio_url, settings.crawler_secret)
    pipeline = Pipeline(sessions, http, settings, llm=llm, studio=studio, searcher=searcher)
    return sessions, pipeline


def once(kind: str, dry_run: bool = False):
    settings = config.load()
    sessions, pipeline = build(settings, dry_run)
    started = time.monotonic()
    try:
        result = runner.run(kind, sessions, pipeline, runner.read_seeds(settings.seed_file))
    finally:
        if pipeline.searcher is not None:
            pipeline.searcher.close()           # Scrapling MCP 서버(자식 프로세스) 끄기
    if not dry_run:                             # 실행이 끝나면 Slack 으로 (dry-run 은 알리지 않음)
        notify.send(settings.slack_webhook_url, notify.message(kind, result, (time.monotonic() - started) / 60))
    return result


def serve():
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BlockingScheduler(timezone="Asia/Seoul")
    # 정각에 몰리지 않게 몇 분씩 흩음 (MusicBrainz 이용 정책) · 한 번에 하나만 · 놓친 실행은 1시간 안이면 바로
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}
    # 전체 실행(dawn)은 20:00 — 11/25 사용자 결정: 03:30 은 미국 업무 시간(KST 23:00~10:00)이라 Gemini 무료 등급이 503 "high demand"
    # (11/19 · 11/24 · 11/25 새벽) · 점심 12:30 은 그대로 (이름 dawn 은 기록 · 명령과 맞추려고 그대로 둠)
    sched.add_job(lambda: _safe("dawn"), CronTrigger(hour=20, minute=0, jitter=1200, timezone="Asia/Seoul"), id="dawn", **common)
    sched.add_job(lambda: _safe("lunch"), CronTrigger(hour=12, minute=30, jitter=600, timezone="Asia/Seoul"), id="lunch", **common)
    log.info("crawler_scheduler_started jobs=dawn 20:00 · lunch 12:30 KST")
    sched.start()


def _safe(kind):
    try:
        once(kind)
    except Exception as e:  # 설정 · DB 문제 — 기록하고 다음 일정에 다시
        log.exception("crawl_%s_crashed", kind)
        notify.send(config.load().slack_webhook_url,
                    f"크롤러 {notify.KINDS.get(kind, kind)} 실행 중단 — {redact(f'{type(e).__name__}: {e}')[:500]}")


class _Redact(logging.Filter):
    """어느 라이브러리가 찍든 주소 안의 키(KOPIS service= 등)를 가림 — Scrapling 은 요청 주소를 그대로 찍음"""

    def filter(self, record):
        record.msg, record.args = redact(record.getMessage()), None
        return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(_Redact())
    scrapling_log = logging.getLogger("scrapling")             # Scrapling 은 자기 출력 창구를 따로 붙임 → 로거에도 필터
    scrapling_log.addFilter(_Redact())
    scrapling_log.setLevel(logging.WARNING)                    # 요청마다 찍는 줄은 끔
    p = argparse.ArgumentParser(prog="crawler")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    r = sub.add_parser("run")
    r.add_argument("kind", choices=["dawn", "lunch"])
    r.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.cmd == "serve":
        serve()
    else:
        print(once(args.kind, args.dry_run))


if __name__ == "__main__":
    main()
