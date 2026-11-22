import json
from urllib.parse import quote

import pytest

from crawler.http import Http
from crawler.sources import discogs, gimbab, indistreet, kopis, musicbrainz

from conftest import UA, FakeWeb, make_http

KOPIS_DETAIL = """<?xml version="1.0" encoding="UTF-8"?><dbs><db>
<mt20id>PF100</mt20id><prfnm>실리카겔 단독 공연</prfnm><prfpdfrom>2026.12.12</prfpdfrom><prfpdto>2026.12.13</prfpdto>
<fcltynm>무신사 개러지 (공연장)</fcltynm><mt10id>FC100</mt10id><mt13id>FC100-01</mt13id><prfcast>실리카겔</prfcast>
<pcseguidance>전석 55,000원</pcseguidance><prfage>만 12세 이상</prfage><dtguidance>토요일(18:00), 일요일(17:00)</dtguidance>
<entrpsnmH>(주)파스텔뮤직</entrpsnmH><entrpsnmA>(주)파스텔뮤직</entrpsnmA>
<poster>http://www.kopis.or.kr/upload/pfmPoster/PF_PF100.gif</poster>
<styurls><styurl>http://www.kopis.or.kr/upload/pfmIntroImage/PF_PF100_1.jpg</styurl><styurl>http://elsewhere.example/2.jpg</styurl></styurls>
<relates><relate><relatenm>예매처</relatenm><relateurl>https://ticket.example/1</relateurl></relate></relates>
</db></dbs>"""


def test_kopis_parse_and_error():
    row = kopis.parse(KOPIS_DETAIL)[0]
    assert row["prfnm"] == "실리카겔 단독 공연"
    assert row["styurls"] == ["http://www.kopis.or.kr/upload/pfmIntroImage/PF_PF100_1.jpg", "http://elsewhere.example/2.jpg"]
    assert row["relates"] == [{"relatenm": "예매처", "relateurl": "https://ticket.example/1"}]
    with pytest.raises(RuntimeError, match="02"):
        kopis.parse("<dbs><db><returncode>02</returncode><errmsg>SERVICE KEY IS NOT REGISTERED</errmsg></db></dbs>")


def mb_web():
    web = FakeWeb([("/robots.txt", 200, "")])
    web.add("artist?query=", {"artists": [
        {"id": "jp", "name": "Silica Gel", "score": 100, "country": "JP", "aliases": [{"name": "실리카겔"}]},
        {"id": "kr", "name": "Silica Gel", "score": 100, "country": "KR", "aliases": [{"name": "실리카겔"}]},
    ]})
    web.add("artist/kr?", {"id": "kr", "name": "Silica Gel", "aliases": [{"name": "실리카겔"}, {"name": "Silica Gel"}],
                           "relations": [{"type": "discogs", "url": {"resource": "https://www.discogs.com/artist/6588484"}},
                                         {"type": "youtube", "url": {"resource": "https://www.youtube.com/@silicagel"}}]})
    web.add("release-group?artist=kr", {"release-groups": [
        {"id": "rg2", "title": "POWER ANDRE 99", "primary-type": "Album", "secondary-types": [], "first-release-date": "2024-06-01"},
        {"id": "rg1", "title": "Kyo181", "primary-type": "EP", "first-release-date": "2022-01-14"},
        {"id": "rg3", "title": "Live", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": "2023-01-01"},
        {"id": "rg4", "title": "Demo", "primary-type": "Single", "first-release-date": "2015"},
    ]})
    web.add("release?release-group=rg1", {"releases": [
        {"date": "2022-03-01", "media": [{"tracks": [{"title": "late"}]}], "label-info": []},
        {"date": "2022-01-14", "media": [{"tracks": [{"title": "A"}, {"title": "B"}]}, {"tracks": [{"title": "C"}]}],
         "label-info": [{"label": {"name": "Magic Strawberry Sound"}}]},
    ]})
    return web


def test_musicbrainz_picks_korean_exact_match_and_filters_groups(sessions):
    http = make_http(sessions, mb_web())
    a = musicbrainz.find_artist(http, "실리카겔")
    assert a == {"mbid": "kr", "name": "Silica Gel", "aliases": ["실리카겔"], "discogs": "6588484",
                 "gender": None, "composition": None}                 # 종류 · 멤버 기록이 없으면 모름
    assert musicbrainz.find_artist(http, "실리카") is None          # 이름이 정확히 같지 않으면 만들지 않음

    groups = musicbrainz.release_groups(http, "kr")
    assert [(g["rgid"], g["type"], g["released_at"]) for g in groups] == [
        ("rg1", "EP", "2022-01-14"), ("rg2", "ALBUM", "2024-06-01"), ("rg4", "SINGLE", None)]   # 라이브 제외 · 연도만이면 비움

    rel = musicbrainz.first_release(http, "rg1")
    assert rel == {"tracks": [{"track_no": 1, "title": "A"}, {"track_no": 2, "title": "B"}, {"track_no": 3, "title": "C"}],
                   "label": "Magic Strawberry Sound"}


def _member(mid, ended=False):
    return {"type": "member of band", "direction": "backward", "ended": ended, "artist": {"id": mid, "name": mid}}


@pytest.mark.parametrize("artist, people, expected", [
    ({"type": "Person", "gender": "Female"}, {}, ("FEMALE", "SOLO")),
    ({"type": "Person", "gender": "Non-binary"}, {}, (None, "SOLO")),             # Catalog 에 없는 성별은 비움
    ({"type": "Group", "relations": [_member("m1"), _member("m2")]}, {"m1": "Male", "m2": "Male"}, ("MALE", "GROUP")),
    ({"type": "Group", "relations": [_member("m1"), _member("m2"), _member("m3")]},
     {"m1": "Male", "m2": "Female"}, ("MIXED", "GROUP")),                        # 한 명을 몰라도 남녀가 섞였으면 혼성
    ({"type": "Group", "relations": [_member("m1"), _member("m2")]}, {"m1": "Male"}, (None, "GROUP")),
    ({"type": "Group", "relations": [_member("m1"), _member("m2", ended=True), _member("m3", ended=True)]},
     {"m1": "Female"}, ("FEMALE", "SOLO")),                                      # 팀 이름이어도 한 명만 남음
    ({"type": "Group", "relations": [_member("m1"), _member("m1", ended=True), _member("m2")]},
     {"m1": "Male", "m2": "Male"}, ("MALE", "GROUP")),                           # 나갔다 돌아온 멤버는 지금 멤버
    ({"type": "Group", "relations": [_member("m1")]}, {"m1": "Male"}, (None, None)),   # 멤버를 한 명만 적어 둔 팀일 수 있음
    ({"type": "Group", "relations": [_member("m1", ended=True)]}, {}, (None, None)),
    ({"type": "Character"}, {}, (None, None)),
    ({}, {}, (None, None)),
])
def test_musicbrainz_lineup_gives_gender_and_composition_only_when_sure(sessions, artist, people, expected):
    web = FakeWeb([("/robots.txt", 200, "")])
    for mid in ("m1", "m2", "m3"):
        web.add(f"artist/{mid}?fmt=json", {"id": mid, "type": "Person", **({"gender": people[mid]} if mid in people else {})})
    assert musicbrainz.lineup(make_http(sessions, web), artist) == expected


def test_musicbrainz_city_only_artist_is_checked_through_area(sessions):
    web = FakeWeb([("/robots.txt", 200, "")])
    web.add("artist?query=", {"artists": [
        {"id": "osaka-band", "name": "Say Sue Me", "score": 100, "area": {"id": "osaka", "name": "Osaka"},
         "aliases": [{"name": "세이수미"}]},
        {"id": "busan-band", "name": "Say Sue Me", "score": 100, "area": {"id": "busan", "name": "Busan"},
         "aliases": [{"name": "세이수미"}]},
    ]})
    web.add("area/osaka?", {"name": "Osaka", "relations": [
        {"type": "part of", "direction": "backward", "area": {"id": "japan", "name": "Japan"}}]})
    web.add("area/japan?", {"name": "Japan", "iso-3166-1-codes": ["JP"], "relations": []})
    web.add("area/busan?", {"name": "Busan", "iso-3166-2-codes": ["KR-26"], "relations": []})
    web.add("artist/busan-band?", {"id": "busan-band", "name": "Say Sue Me", "aliases": [{"name": "세이수미"}]})
    a = musicbrainz.find_artist(make_http(sessions, web), "세이수미")
    assert a["mbid"] == "busan-band"                               # 오사카(일본) 쪽은 버리고 부산 쪽


def test_musicbrainz_release_groups_follow_pages(sessions):
    web = FakeWeb([("/robots.txt", 200, "")])
    web.add("release-group?artist=big", {"release-group-count": 2, "release-groups": [
        {"id": "p1", "title": "One", "primary-type": "Single", "first-release-date": "2020-01-01"}]})
    web.add("release-group?artist=big&type=album|ep|single&fmt=json&limit=100&offset=1", {"release-group-count": 2, "release-groups": [
        {"id": "p2", "title": "Two", "primary-type": "Album", "first-release-date": "2026-10-01"}]})
    assert [g["rgid"] for g in musicbrainz.release_groups(make_http(sessions, web), "big")] == ["p1", "p2"]


def test_discogs_uses_linked_artist_id_and_drops_other_artists(sessions):
    web = FakeWeb([("/robots.txt", 200, "")])
    web.add("api.discogs.com/artists/9", {"id": 9, "name": "검정치마"})
    web.add("database/search?artist=", {"results": [
        {"id": 1, "format": ["Vinyl", "LP"]}, {"id": 2, "format": ["CD"]}, {"id": 3, "format": ["File"]}]})
    web.add("releases/1", {"id": 1, "title": "떠나지마 = Thirsty", "artists": [{"id": 9}], "released": "2019-02-12",
                           "formats": [{"name": "Vinyl", "descriptions": ["LP"], "text": "Black"}]})
    web.add("releases/2", {"id": 2, "title": "Thirsty", "artists": [{"id": 77}], "formats": [{"name": "CD"}]})   # 이름만 비슷한 다른 팀
    out = discogs.physical_by_title(make_http(sessions, web), "TOKEN", "9")
    assert {"떠나지마", "thirsty"} <= set(out) and len(out) == 3           # 병기 제목은 전체 · 앞 · 뒤 모두로 맞춤
    assert [p["external_id"] for p in out["thirsty"]] == ["discogs-1"]
    assert out["thirsty"][0]["options"][0]["vinyl_color"] == "BLACK"
    assert not any("releases/3" in c for c in web.calls)                   # 피지컬이 아닌 판은 부르지 않음


def test_discogs_physical_options():
    lp = discogs._physical({"id": 9, "released": "2024-06-01",
                            "formats": [{"name": "Vinyl", "descriptions": ["LP", "Album"], "text": "Clear"}]}, "LP")
    assert lp["external_id"] == "discogs-9" and lp["format"] == "LP" and lp["released_at"] == "2024-06-01"
    assert lp["options"] == [{"name": "Clear", "vinyl_color": "COLOR", "size": None, "price": None, "qty": None}]
    cd = discogs._physical({"id": 10, "released": "2024", "formats": [{"name": "CD"}]}, "CD")
    assert cd["name"] == "CD" and cd["released_at"] is None
    assert cd["options"][0]["vinyl_color"] is None


def indistreet_page(albums, chunks=2):
    """Next.js 가 넣는 모양 — 데이터 문자열을 여러 push 로 나눠 담음"""
    flight = 'a5:["$","$La6",null,' + json.dumps({"initialData": {"data": albums, "meta": {}}}, ensure_ascii=False) + "]\n"
    size = len(flight) // chunks + 1
    parts = [flight[i:i + size] for i in range(0, len(flight), size)]
    return "<html>" + "".join(f"<script>self.__next_f.push([1,{json.dumps(p, ensure_ascii=False)}])</script>" for p in parts)


def ind_album(doc, title, type_="SINGLE", date="2026-11-11", artist=("리플리카", "RIPLEICA"), **over):
    return {"documentId": doc, "defaultName": title, "nameKo": title, "nameEn": None, "type": type_, "releaseDate": date,
            "slug": f"x-{doc}", "isRemoved": False, "description": "소개 글", "coverImages": [{"url": "https://cdn/x.png"}],
            "artists": [{"nameKo": artist[0], "nameEn": artist[1], "defaultName": f"{artist[0]} {artist[1]}", "isRemoved": False}],
            **over}


def test_indistreet_reads_page_data_and_keeps_only_facts(sessions):
    w = FakeWeb([("/robots.txt", 200, "User-agent: *\nDisallow: /api/\n\nUser-agent: ClaudeBot\nDisallow: /\n")])
    w.add("indistreet.com/albums", indistreet_page([
        ind_album("d1", "흔한 클리셰처럼"),
        ind_album("d2", "PORTRAIT", type_="FULL_ALBUM", date="2026-10-16"),
        ind_album("d3", "지운 앨범", isRemoved=True),
        ind_album("d4", "모르는 종류", type_="COMPILATION"),
    ]))
    got = indistreet.latest(make_http(sessions, w))
    assert [(a["title"], a["type"], a["released_at"]) for a in got] == [("흔한 클리셰처럼", "SINGLE", "2026-11-11"), ("PORTRAIT", "ALBUM", "2026-10-16")]
    assert got[0]["url"] == "https://indistreet.com/albums/x-d1"
    assert got[0]["artists"] == [["리플리카", "RIPLEICA", "리플리카 RIPLEICA"]]
    assert "description" not in got[0] and "cover" not in json.dumps(got[0])        # 소개 글 · 표지는 가져오지 않음


def test_indistreet_changed_page_fails_loudly(sessions):
    w = FakeWeb([("/robots.txt", 200, ""), ("indistreet.com/albums", 200, "<html>no data</html>")])
    with pytest.raises(ValueError):
        indistreet.latest(make_http(sessions, w))


GIMBAB_LIST = """<ul>
<li id="anchorBoxId_25260" class="xans-record-"><div class="thumbnail"><img alt="품절"></div>
 <div class="description"><div class="name"><a href="/product/x/25260/category/42/display/1/"><span>실리카겔 / 정규 2집 POWER ANDRE 99 (Vinyl, 2LP) *예약상품 (11월 26일 이후 배송 예정)</span></a></div>
 <span>45,000원</span></div></li>
<li id="anchorBoxId_13902" class="xans-record-"><div class="description"><div class="name"><a href="/product/y/13902/"><span>놀이도감 (Playbook) (놀이도감 = 김민수 of 실리카겔) / Playbook (Cassette+Download)</span></a></div></div></li>
<li id="anchorBoxId_9" class="xans-record-"><div class="description"><div class="name"><a href="/product/z/9/"><span>실리카겔 / 로고 티셔츠 (블랙)</span></a></div></div></li>
</ul>"""


def test_gimbab_search_keeps_physical_facts_only(sessions):
    w = FakeWeb([("/robots.txt", 200, "User-agent: *\nDisallow: /admin\nDisallow: /api\n")])
    w.add("product/search.html?keyword=" + quote("실리카겔"), GIMBAB_LIST)
    got = gimbab.search(make_http(sessions, w), "실리카겔")
    assert [(p["id"], p["artist_names"], p["title"], p["format"], p["pre_order"]) for p in got] == [
        ("25260", ["실리카겔"], "정규 2집 POWER ANDRE 99", "LP", True),
        ("13902", ["놀이도감", "Playbook"], "Playbook", "CASSETTE", False)]          # 티셔츠(형식 없음)는 뺌
    assert got[0]["url"] == "https://www.gimbabrecords.com/product/x/25260/category/42/display/1/"
    assert "price" not in got[0]                                                       # 그 가게 소매가는 가져오지 않음


@pytest.mark.parametrize("text, fmt, detail, colored", [
    ("검정치마 / 201 (재발매 CD)", "CD", "재발매 CD", False),
    ("A / B (2CD + Booklet)", "CD", "2CD + Booklet", False),
    ("A / X (LP, Clear Vinyl)", "LP", "LP, Clear Vinyl", True),
    ("A / X (카세트테이프)", "CASSETTE", "카세트테이프", False),
    ("A / Help (티셔츠)", None, None, None),
    ("제목만 있는 상품 (CD)", None, None, None),
])
def test_gimbab_parse(text, fmt, detail, colored):
    p = gimbab.parse(text)
    assert (p and (p["format"], p["detail"], p["colored"])) == ((fmt, detail, colored) if fmt else None)


def test_robots_crawl_delay_is_respected(sessions):
    waits = []
    w = FakeWeb([("/robots.txt", 200, "User-agent: *\nCrawl-delay: 10\n"), ("/a", 200, "ok"), ("/b", 200, "ok")])
    http = Http(sessions, UA, fetch=w, sleep=waits.append)
    http.get("x", "https://slow.example/a")
    http.get("x", "https://slow.example/b")
    assert waits and waits[0] > 9
