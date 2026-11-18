"""설정 — 값은 환경 변수, 비밀 값은 시크릿 폴더의 파일 (KT: /opt/indig/secrets/crawler → 컨테이너 /run/secrets/crawler)."""
import os
from dataclasses import dataclass
from pathlib import Path


def _secret(name: str) -> str | None:
    path = Path(os.environ.get("CRAWLER_SECRETS_DIR", "/run/secrets/crawler")) / name
    return path.read_text().strip() if path.is_file() else None


@dataclass(frozen=True)
class Settings:
    studio_url: str
    database_url: str
    crawler_secret: str | None      # crawler.shared-secret — Studio 토큰 서명 (internal.shared-secret 과 다른 값)
    gemini_api_key: str | None      # 장르 · 공연 정리 · 웹 조사 (11/18 Anthropic → Gemini 무료 등급)
    tavily_api_key: str | None      # 웹 검색 (Tavily · 무료 월 1,000 크레딧)
    kopis_key: str | None
    discogs_token: str | None
    model: str
    seed_file: Path
    user_agent: str
    concert_days: int               # 오늘부터 며칠 뒤 공연까지 볼지
    slack_webhook_url: str | None = None   # 실행이 끝나면 알림 (notify.py) · 없으면 보내지 않음


def load() -> Settings:
    db_password = _secret("db-password") or ""
    return Settings(
        studio_url=os.environ.get("STUDIO_URL", "http://studio:8085").rstrip("/"),
        database_url=os.environ.get("DATABASE_URL", f"mysql+pymysql://crawler:{db_password}@mysql:3306/crawler?charset=utf8mb4"),
        crawler_secret=_secret("crawler.shared-secret"),
        gemini_api_key=_secret("gemini-api-key") or os.environ.get("GEMINI_API_KEY"),
        tavily_api_key=_secret("tavily-api-key") or os.environ.get("TAVILY_API_KEY"),
        kopis_key=_secret("kopis-service-key"),
        discogs_token=_secret("discogs-token"),
        model=os.environ.get("CRAWLER_MODEL", "gemini-3.5-flash-lite"),
        seed_file=Path(os.environ.get("SEED_FILE", "seeds/artists.txt")),
        # MusicBrainz 는 앱 이름 · 연락처가 든 User-Agent 를 요구합니다 (개인 메일 대신 서비스 주소)
        user_agent=os.environ.get("CRAWLER_USER_AGENT", "indig-crawler/0.1 ( https://indig.co.kr )"),
        concert_days=int(os.environ.get("CONCERT_DAYS", "90")),
        slack_webhook_url=_secret("slack-webhook-url"),
    )
