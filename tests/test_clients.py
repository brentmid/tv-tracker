"""Offline tests for tvtracker/tvmaze.py and tvtracker/tmdb.py.

All network is replaced by fixture-fed fake fetches; the rate limiter gets
a fake clock/sleep so nothing actually waits.
"""

import json
from pathlib import Path

import pytest

from tvtracker import tmdb, tvmaze

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(rel: str):
    return json.loads((FIXTURES / rel).read_text())


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make_client(responses, clock=None):
    """TVMazeClient with a fake fetch that pops canned (status, data) pairs
    per URL and a private, never-blocking-for-real limiter."""
    clock = clock or FakeClock()
    calls = []

    def fetch(url):
        calls.append(url)
        key = next((k for k in responses if k in url), None)
        assert key is not None, f"unexpected URL fetched: {url}"
        queue = responses[key]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    limiter = tvmaze.TokenBucket(clock=clock.clock, sleep=clock.sleep)
    client = tvmaze.TVMazeClient(fetch=fetch, limiter=limiter, sleep=clock.sleep)
    return client, calls, clock


# ---------------------------------------------------------------------------
# TVmaze client
# ---------------------------------------------------------------------------

def test_search_shows_parses_fixture():
    client, calls, _ = make_client(
        {"/search/shows?q=game%20of%20thrones": [(200, fixture("tvmaze/search_shows.json"))]}
    )
    results = client.search_shows("game of thrones")
    assert len(results) == 2
    assert results[0]["show"]["name"] == "Game of Thrones"
    assert "q=game%20of%20thrones" in calls[0]


def test_show_with_episodes_and_field_mapping():
    client, _, _ = make_client(
        {"/shows/82?embed=episodes": [(200, fixture("tvmaze/show_82_episodes.json"))]}
    )
    show = client.show_with_episodes(82)
    fields = tvmaze.show_fields(show)
    assert fields == {
        "tvmaze_id": 82,
        "name": "Game of Thrones",
        "tvmaze_status": "Ended",
        "runtime_min": 61,
        "image_url": "https://static.tvmaze.com/uploads/images/medium_portrait/498/1245274.jpg",
        "premiered": "2011-04-17",
    }
    episodes = tvmaze.embedded_episodes(show)
    assert len(episodes) == 5
    mapped = [tvmaze.episode_fields(e) for e in episodes]
    assert mapped[3] is None  # special with number: null is unkeyable -> skipped
    assert mapped[0]["tvmaze_episode_id"] == 4952
    assert mapped[0]["airdate"] == "2011-04-17"
    assert mapped[4]["airdate"] is None  # "" normalized to None


def test_lookup_by_thetvdb_found_and_missing():
    show = fixture("tvmaze/show_82_episodes.json")
    client, _, _ = make_client({
        "thetvdb=121361": [(200, show)],
        "thetvdb=999999999": [(404, None)],
    })
    assert client.lookup_by_thetvdb(121361)["id"] == 82
    assert client.lookup_by_thetvdb(999999999) is None


def test_429_sleeps_and_retries_once():
    ok = fixture("tvmaze/search_shows.json")
    clock = FakeClock()
    client, calls, clock = make_client(
        {"/search/shows": [(429, None), (200, ok)]}, clock=clock
    )
    results = client.search_shows("x")
    assert len(results) == 2
    assert len(calls) == 2                       # retried exactly once
    assert tvmaze.RATE_LIMIT_PERIOD in clock.slept  # backed off before retry


def test_repeated_429_raises():
    client, calls, _ = make_client({"/search/shows": [(429, None), (429, None)]})
    with pytest.raises(tvmaze.TVMazeError):
        client.search_shows("x")
    assert len(calls) == 2  # one retry, then give up — never a hot loop


def test_non_200_raises():
    client, _, _ = make_client({"/shows/82": [(500, None)]})
    with pytest.raises(tvmaze.TVMazeError):
        client.show_with_episodes(82)


def test_token_bucket_blocks_after_burst():
    clock = FakeClock()
    bucket = tvmaze.TokenBucket(capacity=20, period=10.0,
                                clock=clock.clock, sleep=clock.sleep)
    for _ in range(20):
        bucket.take()
    assert not clock.slept          # full burst allowed
    bucket.take()                   # 21st call must wait for a refill
    assert clock.slept and clock.slept[0] == pytest.approx(0.5)


def test_token_bucket_refills_over_time():
    clock = FakeClock()
    bucket = tvmaze.TokenBucket(capacity=20, period=10.0,
                                clock=clock.clock, sleep=clock.sleep)
    for _ in range(20):
        bucket.take()
    clock.now += 10.0               # full period passes -> full bucket
    for _ in range(20):
        bucket.take()
    assert not clock.slept


# ---------------------------------------------------------------------------
# TMDB client
# ---------------------------------------------------------------------------

def make_tmdb(responses):
    calls = []

    def fetch(url):
        calls.append(url)
        key = next((k for k in responses if k in url), None)
        assert key is not None, f"unexpected URL fetched: {url}"
        return responses[key]

    return tmdb.TMDBClient(api_key="testkey", fetch=fetch), calls


def test_tmdb_search_movies_and_field_mapping():
    client, calls = make_tmdb(
        {"/search/movie": (200, fixture("tmdb/search_movie.json"))}
    )
    results = client.search_movies("inception", year=2010)
    assert [m["id"] for m in results] == [27205, 64956, 12345]
    assert "api_key=testkey" in calls[0]
    assert "query=inception" in calls[0]
    assert "primary_release_year=2010" in calls[0]

    fields = tmdb.movie_fields(results[0])
    assert fields == {
        "tmdb_id": 27205,
        "title": "Inception",
        "year": 2010,
        "runtime_min": None,  # search results carry no runtime
        "poster_url": "https://image.tmdb.org/t/p/w342/oYuLEt3zVCKq57qu2F8dT7NIa6f.jpg",
    }
    assert tmdb.movie_fields(results[2])["year"] is None  # empty release_date


def test_tmdb_movie_detail_has_runtime():
    client, _ = make_tmdb({"/movie/27205": (200, fixture("tmdb/movie_27205.json"))})
    fields = tmdb.movie_fields(client.movie(27205))
    assert fields["runtime_min"] == 148


def test_tmdb_error_and_404():
    client, _ = make_tmdb({"/movie/1": (500, None), "/movie/2": (404, None)})
    with pytest.raises(tmdb.TMDBError):
        client.movie(1)
    assert client.movie(2) is None


def test_tmdb_key_loading(tmp_path, monkeypatch):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    keyfile = tmp_path / "tmdb_api_key"
    # no env, no file -> helpful error
    with pytest.raises(tmdb.TMDBKeyMissing, match="themoviedb.org"):
        tmdb.load_api_key(key_file=keyfile)
    # file wins when present (trailing newline stripped)
    keyfile.write_text("abc123\n")
    assert tmdb.load_api_key(key_file=keyfile) == "abc123"
    # env beats file
    monkeypatch.setenv("TMDB_API_KEY", "envkey")
    assert tmdb.load_api_key(key_file=keyfile) == "envkey"
