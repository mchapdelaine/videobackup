import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from videobackup import retention
from videobackup.retention import (
    RemoteFile,
    _delete_remote,
    _parse_mod_time,
    _rclone_delete_args,
    effective_prune_cap,
    prune_raw_spool,
    prune_spool,
    select_for_deletion,
    select_oversize,
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


# -- batched delete ------------------------------------------------------


def test_delete_args_permanent_by_default():
    args = _rclone_delete_args("gdrive:unifi-backup", "/tmp/list.txt", use_trash=False)
    assert args[:2] == ["delete", "gdrive:unifi-backup"]
    assert "--files-from" in args and "/tmp/list.txt" in args
    assert "--drive-use-trash=false" in args


def test_delete_args_trash_when_enabled():
    args = _rclone_delete_args("gdrive:x", "/tmp/l", use_trash=True)
    assert "--drive-use-trash=true" in args


def _cfg():
    return SimpleNamespace(remote_path="gdrive:unifi-backup", use_trash=False)


def test_delete_remote_batches_all_names_in_one_call(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args):
        # Capture the files-from list contents before the temp file is removed.
        path = args[args.index("--files-from") + 1]
        seen["names"] = Path(path).read_text().split()
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(retention, "_run_rclone", fake_run)
    n = _delete_remote(_cfg(), ["a.gpg", "b.gpg", "c.gpg"])
    assert n == 3  # one call, all counted
    assert seen["names"] == ["a.gpg", "b.gpg", "c.gpg"]


def test_delete_remote_returns_zero_on_failure(monkeypatch):
    monkeypatch.setattr(
        retention,
        "_run_rclone",
        lambda args: SimpleNamespace(returncode=1, stderr="boom", stdout=""),
    )
    assert _delete_remote(_cfg(), ["a.gpg"]) == 0


def test_delete_remote_noop_on_empty(monkeypatch):
    called = False

    def fake_run(args):
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(retention, "_run_rclone", fake_run)
    assert _delete_remote(_cfg(), []) == 0
    assert not called  # no rclone process for an empty victim list


# -- prune_spool (local encrypted spool ceiling) --------------------------


def _spool(tmp_path, sizes_by_age, max_spool_bytes, max_drive_bytes=10**9):
    """Build a spool dir; sizes_by_age is oldest-first. Returns a fake config."""
    enc = tmp_path / "encrypted"
    enc.mkdir()
    raw = tmp_path / "raw"
    raw.mkdir()
    for i, size in enumerate(sizes_by_age):
        p = enc / f"cam_{i:03d}.mp4.gpg"
        p.write_bytes(b"x" * size)
        os.utime(p, (1_000_000 + i, 1_000_000 + i))  # ascending mtime
    return SimpleNamespace(
        spool_encrypted=enc,
        spool_raw=raw,
        max_spool_bytes=max_spool_bytes,
        max_drive_bytes=max_drive_bytes,
    )


def test_prune_spool_drops_oldest_over_cap(tmp_path):
    cfg = _spool(tmp_path, [100, 100, 100, 100], max_spool_bytes=250)
    assert prune_spool(cfg) == 2
    left = sorted(p.name for p in cfg.spool_encrypted.glob("*.gpg"))
    assert left == ["cam_002.mp4.gpg", "cam_003.mp4.gpg"]  # newest survive


def test_prune_spool_noop_under_cap(tmp_path):
    cfg = _spool(tmp_path, [100, 100], max_spool_bytes=10_000)
    assert prune_spool(cfg) == 0
    assert len(list(cfg.spool_encrypted.glob("*.gpg"))) == 2


def test_prune_spool_disabled_by_default(tmp_path):
    # 0 must never delete local data, however far over any notional cap.
    cfg = _spool(tmp_path, [100] * 5, max_spool_bytes=0)
    assert prune_spool(cfg) == 0
    assert len(list(cfg.spool_encrypted.glob("*.gpg"))) == 5


def test_prune_spool_ignores_partial_files(tmp_path):
    cfg = _spool(tmp_path, [100, 100], max_spool_bytes=150)
    part = cfg.spool_encrypted / "cam_999.mp4.gpg.part"
    part.write_bytes(b"y" * 5_000)  # in-progress encrypt, must survive
    assert prune_spool(cfg) == 1
    assert part.exists()


# -- oversize segments (undeliverable by construction) --------------------


def test_select_oversize_picks_only_files_over_the_cap():
    files = [_f("ok", 100, 1), _f("huge", 101, 2), _f("exact", 100, 3)]
    assert [f.name for f in select_oversize(files, max_bytes=100)] == ["huge"]


def test_select_oversize_disabled_when_cap_is_zero():
    assert select_oversize([_f("huge", 10**9, 1)], max_bytes=0) == []


def test_prune_spool_drops_file_bigger_than_drive_cap(tmp_path):
    # The live failure: one 10 GiB segment against a 5 GiB cap. rclone cannot
    # split a single file, so every cycle re-attempted it and 403'd forever.
    cfg = _spool(tmp_path, [100, 100], max_spool_bytes=0, max_drive_bytes=1_000)
    huge = cfg.spool_encrypted / "cbc_20260912_080716.ts.gpg"
    huge.write_bytes(b"z" * 5_000)
    assert prune_spool(cfg) == 1
    assert not huge.exists()
    assert len(list(cfg.spool_encrypted.glob("*.gpg"))) == 2  # others untouched


def test_prune_spool_drops_oversize_even_though_it_is_newest(tmp_path):
    # Regression: the backlog pass keeps newest-first, and a still-growing
    # monster always has the newest mtime -- so it survived while good segments
    # were deleted around it. The oversize pass must run first.
    cfg = _spool(tmp_path, [100, 100], max_spool_bytes=10_000, max_drive_bytes=1_000)
    huge = cfg.spool_encrypted / "cbc_20260912_080716.ts.gpg"
    huge.write_bytes(b"z" * 5_000)
    os.utime(huge, (2_000_000, 2_000_000))  # newest of all
    assert prune_spool(cfg) == 1
    assert not huge.exists()
    assert sorted(p.name for p in cfg.spool_encrypted.glob("*.gpg")) == [
        "cam_000.mp4.gpg",
        "cam_001.mp4.gpg",
    ]


def test_prune_raw_spool_drops_oversize_only(tmp_path):
    cfg = _spool(tmp_path, [], max_spool_bytes=0, max_drive_bytes=1_000)
    huge = cfg.spool_raw / "cbc_20260912_080716.ts"
    huge.write_bytes(b"z" * 5_000)
    small = cfg.spool_raw / "front_20260912_080716.mp4"
    small.write_bytes(b"z" * 100)
    assert prune_raw_spool(cfg) == 1
    assert not huge.exists()
    assert small.exists()


def test_prune_raw_spool_never_applies_a_backlog_cap(tmp_path):
    # Raw drains via encrypt; a backlog there means encryption is failing, and
    # deleting still-deliverable footage is the wrong response to that.
    cfg = _spool(tmp_path, [], max_spool_bytes=1, max_drive_bytes=10**9)
    for i in range(5):
        (cfg.spool_raw / f"front_{i}.mp4").write_bytes(b"z" * 1_000)
    assert prune_raw_spool(cfg) == 0
    assert len(list(cfg.spool_raw.glob("*.mp4"))) == 5
