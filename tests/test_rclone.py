"""Tests for rclone stderr classification (pure).

Several of these failures share an exit code and a 403 status and differ only
in wording, so the point of every test here is that two things that look alike
are told apart.
"""

from videobackup.rclone import auth_hint, classify

_CLEAN = """
Transferred:   	    1.234 GiB / 1.234 GiB, 100%, 12.000 MiB/s, ETA -
Transferred:           40 / 40, 100%
Elapsed time:      1m30.0s
"""

_THROTTLED = """
ERROR : seg.gpg: Failed to copy: googleapi: Error 403: Quota exceeded for
 quota metric 'Queries' and limit 'Requests per minute', rateLimitExceeded

Transferred:   	  512.000 MiB / 1.234 GiB, 40%, 3.000 MiB/s, ETA 4m
Errors:                 7 (retrying may help)
Transferred:           12 / 40, 30%
Elapsed time:      2m0.0s
"""

# Verbatim from a live failure: Drive's "account is full" 403.
_FULL = """
ERROR : cbc_20260912_080716.ts.gpg: Failed to copy: googleapi: Error 403: The \
user's Drive storage quota has been exceeded., storageQuotaExceeded
ERROR : Attempt 1/3 failed with 1 errors and: googleapi: Error 403: The user's \
Drive storage quota has been exceeded., storageQuotaExceeded
Transferred:              0 B / 0 B, -, 0 B/s, ETA -
Errors:                 1 (retrying may help)
Elapsed time:         2.5s
"""

# Verbatim from a live failure: the OAuth grant expired.
_EXPIRED = (
    'Failed to create file system for "gdrive_oauth:unifi-backup": couldn\'t '
    "find root directory ID: Get "
    '"https://www.googleapis.com/drive/v3/files/root?alt=json&fields=id": '
    "couldn't fetch token - maybe it has expired? - refresh with \"rclone "
    'config reconnect gdrive_oauth:": oauth2: "invalid_grant" "Token has been '
    'expired or revoked."'
)


def test_clean_run():
    assert classify(_CLEAN) == (0, False, False, False)


def test_empty_stderr():
    assert classify("") == (0, False, False, False)


def test_counts_errors_and_flags_throttling():
    outcome = classify(_THROTTLED)
    assert outcome.errors == 7
    assert outcome.rate_limited is True
    assert outcome.out_of_space is False


def test_detects_throttling_without_error_line():
    # The dangerous case: rclone retries past the 403s, so the stats block has
    # no Errors line and the exit code is 0 -- but bytes were still discarded.
    outcome = classify("NOTICE: low level retry 1/10: rateLimitExceeded\n" + _CLEAN)
    assert outcome.errors == 0
    assert outcome.rate_limited is True


def test_flags_out_of_space():
    outcome = classify(_FULL)
    assert outcome.errors == 1
    assert outcome.out_of_space is True


def test_does_not_mistake_a_full_account_for_throttling():
    # Both are 403s. Reporting "out of space" as throttling would print the
    # client_id advice, which is irrelevant, and imply retrying fixes it.
    assert classify(_FULL).rate_limited is False


def test_does_not_mistake_throttling_for_a_full_account():
    assert classify(_THROTTLED).out_of_space is False


def test_ignores_unrelated_403_free_text():
    assert classify("Transferred: 1 / 1, 100%\n") == (0, False, False, False)


# -- expired / revoked OAuth grant ---------------------------------------


def test_flags_an_expired_grant():
    assert classify(_EXPIRED).auth_expired is True


def test_expired_grant_is_not_reported_as_quota_trouble():
    # It is neither a 403 nor fixable by waiting; conflating it would send the
    # reader after storage or rate limits that are both fine.
    outcome = classify(_EXPIRED)
    assert outcome.rate_limited is False
    assert outcome.out_of_space is False


def test_bare_invalid_grant_is_enough():
    assert classify('oauth2: "invalid_grant"').auth_expired is True


def test_401_is_treated_as_an_auth_failure():
    assert classify("googleapi: Error 401: Invalid Credentials").auth_expired is True


def test_healthy_output_is_not_an_auth_failure():
    for text in (_CLEAN, _THROTTLED, _FULL, ""):
        assert classify(text).auth_expired is False


def test_auth_hint_names_the_remote_and_the_exact_command():
    hint = auth_hint("gdrive_oauth")
    # The trailing colon is the whole reason the obvious command fails.
    assert "rclone config reconnect gdrive_oauth:" in hint


def test_auth_hint_explains_the_seven_day_cycle():
    # A grant that dies weekly means the consent screen is still in Testing;
    # without this the reader just reconnects and waits to be broken again.
    hint = auth_hint("gdrive_oauth")
    assert "Testing" in hint and "7 days" in hint
