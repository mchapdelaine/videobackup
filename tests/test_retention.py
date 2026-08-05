from datetime import datetime, timedelta, timezone

from videobackup.retention import (
    RemoteFile,
    _parse_mod_time,
    effective_prune_cap,
    select_for_deletion,
)

GiB = 2**30

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


# -- effective_prune_cap (quota-aware budget) ----------------------------


def test_cap_folder_only_when_quota_guard_off():
    # min_free_bytes=0 -> account ignored, just max_drive_bytes - reserve.
    cap = effective_prune_cap(
        max_drive_bytes=5 * GiB,
        reserve_bytes=1 * GiB,
        folder_total=5 * GiB,
        account_free=100 * GiB,  # ignored
        min_free_bytes=0,
    )
    assert cap == 4 * GiB


def test_cap_folder_only_when_about_unavailable():
    # about failed (None) -> degrade to folder cap even with guard on.
    cap = effective_prune_cap(
        max_drive_bytes=5 * GiB,
        reserve_bytes=0,
        folder_total=5 * GiB,
        account_free=None,
        min_free_bytes=2 * GiB,
    )
    assert cap == 5 * GiB


def test_cap_untightened_when_free_already_ample():
    # Plenty free -> deficit <= 0, folder cap wins.
    cap = effective_prune_cap(
        max_drive_bytes=5 * GiB,
        reserve_bytes=0,
        folder_total=5 * GiB,
        account_free=10 * GiB,
        min_free_bytes=2 * GiB,
    )
    assert cap == 5 * GiB


def test_cap_tightened_to_keep_account_free():
    # Folder 5 GiB, only 1 GiB free, want 2 GiB free after uploading 0.
    # deficit = 2 - (1 - 0) = 1 GiB -> cap = min(5, 5-1) = 4 GiB.
    cap = effective_prune_cap(
        max_drive_bytes=5 * GiB,
        reserve_bytes=0,
        folder_total=5 * GiB,
        account_free=1 * GiB,
        min_free_bytes=2 * GiB,
    )
    assert cap == 4 * GiB


def test_cap_accounts_for_reserve_in_deficit():
    # Uploading 1 GiB while wanting 2 GiB free, 2 GiB free now, folder 5 GiB.
    # deficit = 2 - (2 - 1) = 1 GiB -> account_cap = 5-1 = 4 GiB.
    # folder cap = 5 - 1(reserve) = 4 GiB. min(4,4)=4.
    cap = effective_prune_cap(
        max_drive_bytes=5 * GiB,
        reserve_bytes=1 * GiB,
        folder_total=5 * GiB,
        account_free=2 * GiB,
        min_free_bytes=2 * GiB,
    )
    assert cap == 4 * GiB


def test_cap_floors_at_zero_when_other_usage_dominates():
    # Account so full that even emptying the folder can't reach min_free.
    cap = effective_prune_cap(
        max_drive_bytes=5 * GiB,
        reserve_bytes=0,
        folder_total=1 * GiB,
        account_free=0,
        min_free_bytes=4 * GiB,
    )
    assert cap == 0
