from datetime import datetime, timedelta, timezone

from videobackup.retention import RemoteFile, _parse_mod_time, select_for_deletion

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


def _f(name: str, size: int, age_days: float) -> RemoteFile:
    return RemoteFile(name=name, size=size, mod_time=NOW - timedelta(days=age_days))


def test_under_cap_deletes_nothing():
    files = [_f("a", 10, 1), _f("b", 10, 2)]
    assert select_for_deletion(files, max_bytes=100, now=NOW) == []


def test_size_cap_deletes_oldest_first():
    files = [_f("new", 40, 1), _f("old", 40, 5), _f("mid", 40, 3)]
    # total 120, cap 100 -> must drop 20+ bytes -> drop oldest ("old").
    victims = select_for_deletion(files, max_bytes=100, now=NOW)
    assert [v.name for v in victims] == ["old"]


def test_size_cap_deletes_multiple_until_under():
    files = [_f("a", 50, 1), _f("b", 50, 2), _f("c", 50, 3)]
    # total 150, cap 60 -> drop oldest two (c, b) leaving a=50.
    victims = select_for_deletion(files, max_bytes=60, now=NOW)
    assert [v.name for v in victims] == ["c", "b"]


def test_age_cap_deletes_old_regardless_of_size():
    files = [_f("fresh", 10, 1), _f("stale", 10, 40)]
    victims = select_for_deletion(files, max_bytes=10_000, now=NOW, max_age_days=30)
    assert [v.name for v in victims] == ["stale"]


def test_age_and_size_combined():
    files = [_f("stale", 10, 40), _f("a", 50, 3), _f("b", 50, 2), _f("c", 50, 1)]
    # stale removed by age; remaining 150 with cap 100 -> drop oldest "a".
    victims = select_for_deletion(files, max_bytes=100, now=NOW, max_age_days=30)
    assert [v.name for v in victims] == ["stale", "a"]


def test_empty_list():
    assert select_for_deletion([], max_bytes=100, now=NOW) == []


def test_reserve_via_effective_cap_frees_headroom():
    # Pre-upload gate: reserve 30 by lowering cap 100 -> 70. total 120 must drop
    # to <=70 -> remove oldest until under: c(50)+? 120-50=70 ok -> only "c".
    files = [_f("a", 50, 1), _f("b", 20, 2), _f("c", 50, 3)]
    victims = select_for_deletion(files, max_bytes=100 - 30, now=NOW)
    assert [v.name for v in victims] == ["c"]


def test_reserve_larger_than_cap_clears_all():
    # If incoming exceeds the whole cap, effective cap floors at 0 -> delete all.
    files = [_f("a", 10, 1), _f("b", 10, 2)]
    victims = select_for_deletion(files, max_bytes=0, now=NOW)
    assert {v.name for v in victims} == {"a", "b"}


def test_parse_mod_time_nanoseconds():
    dt = _parse_mod_time("2026-07-16T12:00:00.123456789Z")
    assert dt.year == 2026 and dt.tzinfo is not None


def test_parse_mod_time_plain():
    dt = _parse_mod_time("2026-07-16T12:00:00Z")
    assert dt == NOW
