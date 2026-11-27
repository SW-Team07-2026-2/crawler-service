# syntax=docker/dockerfile:1
# 크롤러 - KT VM compose 에서 Studio 옆에 돕니다 (manifests-kt README "크롤러")
# 빌드: .github/workflows/ci.yml (main 머지 -> Docker Hub <사용자>/indig-crawler:<커밋 SHA>, amd64 -> manifests-kt compose.yml 태그 커밋)
# 1차 소스는 모두 공개 API 라 브라우저(scrapling install)는 넣지 않습니다 - HTTP Fetcher 만. 브라우저가 필요한 사이트를 붙일 때 추가.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Seoul PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .
COPY seeds ./seeds
USER app
# 시크릿 파일: /run/secrets/crawler/{crawler.shared-secret, db-password, gemini-api-key, tavily-api-key, kopis-service-key, discogs-token, slack-webhook-url}
# 웹 조사의 페이지 열기는 scrapling-mcp(stdio, 이 컨테이너 안 자식 프로세스, 포트 없음)의 HTTP 요청만 씁니다 - 브라우저 없음
CMD ["crawler", "serve"]
