"""크롤러 DB (KT MySQL 스키마 crawler · 로컬 · 테스트는 SQLite). 테이블은 처음 뜰 때 create_all 로 만듭니다."""
import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import (BigInteger, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .keys import norm

LongText = Text().with_variant(MEDIUMTEXT(), "mysql")
Id = BigInteger().with_variant(Integer(), "sqlite")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class CrawlRun(Base):
    """실행 1번 = 1행. status RUNNING 이 남아 있으면 다음 실행은 건너뜁니다 (겹침 방지)."""
    __tablename__ = "crawl_run"
    id: Mapped[int] = mapped_column(Id, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))                 # dawn · lunch · once
    status: Mapped[str] = mapped_column(String(16))               # RUNNING · DONE · FAILED
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    stats_json: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class RawPage(Base):
    """가져온 원문 — 같은 주소를 max_age 안에 다시 부르지 않게(캐시), 바뀌었는지(content_hash) 보는 데 씁니다."""
    __tablename__ = "raw_page"
    __table_args__ = (UniqueConstraint("url_hash"),)
    id: Mapped[int] = mapped_column(Id, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(1024))
    url_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    content_hash: Mapped[str] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(LongText)


class Item(Base):
    """전처리한 항목 하나 — Studio 로 보낼 모양 그대로 (payload_json). 열쇠는 (type, key) = Studio 의 external_id.
    상태: READY(보낼 것) → SENT · FAILED(Studio 가 거절 · 사유 error) · SKIPPED(인디 공연이 아님 등 · 보내지 않음)
    · HOLD(장르를 아직 못 정한 아티스트 — 정하면 READY)"""
    __tablename__ = "item"
    __table_args__ = (UniqueConstraint("type", "key"),)
    id: Mapped[int] = mapped_column(Id, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(16))                 # ARTIST · ALBUM · CONCERT
    key: Mapped[str] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(LongText)
    input_hash: Mapped[str] = mapped_column(String(64))           # 전처리에 넣은 원본의 해시 — 같으면 Gemini 를 다시 부르지 않음
    source_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))               # READY · SENT · FAILED · SKIPPED
    sent_hash: Mapped[str | None] = mapped_column(String(64))     # 마지막으로 Studio 가 받은 해시 — 같으면 다시 안 보냄
    studio_result: Mapped[str | None] = mapped_column(String(16)) # CREATED · UPDATED · UNCHANGED · REVIEWED
    error: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str | None] = mapped_column(String(1024))      # 원래 사이트 · 주소 (관리자 확인용 · Studio 로는 안 감)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ArtistKey(Base):
    """대표 아티스트 — 여러 사이트에서 온 같은 아티스트를 하나로 (Studio 로는 source=crawler · external_id=key)."""
    __tablename__ = "artist_key"
    id: Mapped[int] = mapped_column(Id, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))


class ArtistLink(Base):
    """사이트별 id · 이름 → 대표 키. site 예: musicbrainz(id=mbid) · name(id=정규화한 이름 · 별칭 포함)"""
    __tablename__ = "artist_link"
    __table_args__ = (UniqueConstraint("site", "site_id"),)
    id: Mapped[int] = mapped_column(Id, primary_key=True, autoincrement=True)
    site: Mapped[str] = mapped_column(String(32))
    site_id: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(64))


class AlbumLink(Base):
    """앨범 대표 키 — 웹 검색 · 인디스트릿에서 먼저 찾은 앨범을 나중에 MusicBrainz 에 생긴 같은 앨범과 하나로 (Studio 후보가 둘이 되지 않게).
    site: musicbrainz(id=release group id) · title(id=아티스트 키:종류:정규화한 제목)"""
    __tablename__ = "album_link"
    __table_args__ = (UniqueConstraint("site", "site_id"),)
    id: Mapped[int] = mapped_column(Id, primary_key=True, autoincrement=True)
    site: Mapped[str] = mapped_column(String(32))
    site_id: Mapped[str] = mapped_column(String(255))
    key: Mapped[str] = mapped_column(String(64))


class Found(Base):
    """MusicBrainz 밖(웹 검색 · 인디스트릿 · 김밥레코즈)에서 찾은 아티스트의 신보(RELEASE) · 피지컬 판본(PHYSICAL) · 굿즈(GOODS).
    한 번 찾은 것은 남겨 둡니다 — 다음 검색에 안 나와도 지우지 않고, 값은 처음 찾은 것을 두고 빈 값만 채웁니다 (후보가 매번 흔들리지 않게).
    evidence_json = 출처 [{site, url, title, quote}] — 관리자 확인용으로 후보에 함께 갑니다."""
    __tablename__ = "found"
    __table_args__ = (UniqueConstraint("artist_key", "kind", "match_key"),)
    id: Mapped[int] = mapped_column(Id, primary_key=True, autoincrement=True)
    artist_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))                 # RELEASE · PHYSICAL · GOODS
    match_key: Mapped[str] = mapped_column(String(255))           # 같은 것인지 보는 값 (정규화한 제목 · 상품 번호 등)
    site: Mapped[str] = mapped_column(String(32))                 # 처음 찾은 곳: websearch · indistreet · gimbab
    data_json: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)


def connect(url: str) -> sessionmaker[Session]:
    engine = create_engine(url, pool_pre_ping=True, future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def artist_key_for(db: Session, name: str, links: dict[str, str], aliases: list[str] = ()) -> str:
    """사이트 id 가 이미 연결돼 있으면 그 키 → 아니면 이름(별칭 포함)이 같은 키 → 없으면 새 키. 받은 id · 이름은 모두 연결해 둡니다."""
    found = None
    for site, site_id in links.items():
        found = found or db.scalar(select(ArtistLink.key).where(ArtistLink.site == site, ArtistLink.site_id == site_id))
    names = {norm(n) for n in [name, *aliases] if norm(n)}
    for n in names:
        found = found or db.scalar(select(ArtistLink.key).where(ArtistLink.site == "name", ArtistLink.site_id == n))
    if found is None:
        found = "a-" + uuid.uuid4().hex[:20]
        db.add(ArtistKey(key=found, name=name))
    for site, site_id in [*links.items(), *(("name", n) for n in names)]:
        if db.scalar(select(ArtistLink.id).where(ArtistLink.site == site, ArtistLink.site_id == site_id)) is None:
            db.add(ArtistLink(site=site, site_id=site_id, key=found))
    db.flush()
    return found


def lookup_artist_key(db: Session, name: str) -> str | None:
    return db.scalar(select(ArtistLink.key).where(ArtistLink.site == "name", ArtistLink.site_id == norm(name)))


def album_key_for(db: Session, artist_key: str, type_: str, title: str, rgid: str | None = None) -> str:
    """MusicBrainz 앨범(rgid 있음): 연결된 키 → 같은 아티스트 · 종류 · 제목으로 먼저 찾아 둔 키(아직 다른 MusicBrainz 앨범과 안 이어진 것) → b-{rgid}.
    MusicBrainz 밖 앨범(rgid 없음): 같은 아티스트 · 종류 · 제목의 키 → w-{해시}. 받은 rgid · 제목은 연결해 둡니다."""
    title_id = f"{artist_key}:{type_}:{norm(title)}"[:255]
    by_title = db.scalar(select(AlbumLink.key).where(AlbumLink.site == "title", AlbumLink.site_id == title_id))
    if rgid:
        key = db.scalar(select(AlbumLink.key).where(AlbumLink.site == "musicbrainz", AlbumLink.site_id == rgid))
        if key is None:
            taken = by_title is not None and db.scalar(select(AlbumLink.id).where(
                AlbumLink.site == "musicbrainz", AlbumLink.key == by_title)) is not None
            key = by_title if by_title is not None and not taken else f"b-{rgid}"   # 같은 제목의 싱글 · 앨범이 MusicBrainz 에 둘이면 따로
            db.add(AlbumLink(site="musicbrainz", site_id=rgid, key=key))
    else:
        key = by_title or "w-" + hashlib.sha256(title_id.encode()).hexdigest()[:20]
    if by_title is None:
        db.add(AlbumLink(site="title", site_id=title_id, key=key))
    db.flush()
    return key


def remember(db: Session, artist_key: str, kind: str, match_key: str, site: str, data: dict, evidence: list[dict]) -> bool:
    """찾은 것 쌓기 — 새것이거나 빈 값이 채워졌으면 True"""
    match_key = match_key[:255]
    row = db.scalar(select(Found).where(Found.artist_key == artist_key, Found.kind == kind, Found.match_key == match_key))
    if row is None:
        db.add(Found(artist_key=artist_key, kind=kind, match_key=match_key, site=site, first_seen=now(), last_seen=now(),
                     data_json=json.dumps(data, ensure_ascii=False), evidence_json=json.dumps(evidence, ensure_ascii=False)))
        db.flush()
        return True
    row.last_seen = now()
    old = json.loads(row.data_json)
    filled = {k: v for k, v in data.items() if old.get(k) in (None, "", []) and v not in (None, "", [])}
    if not filled:
        return False
    old_evidence = json.loads(row.evidence_json)
    urls = {e["url"] for e in old_evidence}
    row.data_json = json.dumps({**old, **filled}, ensure_ascii=False)
    row.evidence_json = json.dumps(old_evidence + [e for e in evidence if e["url"] not in urls], ensure_ascii=False)
    return True
