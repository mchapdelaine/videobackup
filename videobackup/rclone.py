"""Shared classification of rclone failures.

Several rclone failures arrive as the same exit code and differ only in their
stderr text, yet call for opposite responses: back off and retry, free space,
or stop and fetch a human. Classifying them in one place keeps the uploader and
the retention pass agreeing on what a given failure means.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# "Errors:  3 (retrying may help)" in rclone's final stats block.
_ERRORS_RE = re.compile(r"^Errors:\s+(\d+)", re.MULTILINE)

# Drive 403s that mean "slow down", as opposed to "out of space". These are
# survivable -- rclone backs off and retries -- so they never reach the exit
# code, yet they can silently discard most of a cycle's transferred bytes.
_RATE_LIMIT_MARKERS = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "quota metric",
    "quota exceeded",
)

# Drive's *other* 403: the account is full. Same status code as throttling but
# the opposite remedy -- retrying never helps, something has to be deleted.
_OUT_OF_SPACE_MARKERS = (
    "storagequotaexceeded",
    "storage quota has been exceeded",
)

# The OAuth grant is gone. Unlike everything above this is not a transient
# condition at all: no amount of retrying reauthorizes a revoked token, and a
# loop that keeps trying just burns cycles while the spool fills.
_AUTH_MARKERS = (
    "invalid_grant",
    "token has been expired or revoked",
    "couldn't fetch token",
    "failed to configure token",
    "error 401",
)


class RcloneAuthError(RuntimeError):
    """Raised when rclone fails because its OAuth grant expired or was revoked.

    Distinct from a generic failure so callers can stop retrying and say
    something actionable instead of logging the same traceback forever.
    """


class RcloneOutcome(NamedTuple):
    errors: int
    rate_limited: bool
    out_of_space: bool
    auth_expired: bool


def classify(stderr: str) -> RcloneOutcome:
    """Parse rclone's stderr into the signals worth acting on (pure).

    ``errors`` comes from the final stats block. The three flags are kept
    separate rather than collapsed into one "failed" bit because they call for
    different responses, and because two of them (throttling and a full
    account) are both 403s that would otherwise be indistinguishable.
    """
    match = _ERRORS_RE.search(stderr)
    errors = int(match.group(1)) if match else 0
    low = stderr.lower()
    return RcloneOutcome(
        errors=errors,
        rate_limited=any(m in low for m in _RATE_LIMIT_MARKERS),
        out_of_space=any(m in low for m in _OUT_OF_SPACE_MARKERS),
        auth_expired=any(m in low for m in _AUTH_MARKERS),
    )


def auth_hint(remote: str) -> str:
    """Actionable one-liner for a dead OAuth grant on ``remote``."""
    return (
        f"rclone's OAuth grant for '{remote}' has expired or been revoked; no "
        f"amount of retrying will restore it. Reauthorize with: "
        f"rclone config reconnect {remote}:   (the trailing colon is required "
        f"-- without it rclone answers 'backend doesn't support reconnect'). "
        f"If this recurs about every 7 days, the Google Cloud project's OAuth "
        f"consent screen is still in 'Testing', which expires refresh tokens "
        f"on that cycle; set it to 'In production' to stop it."
    )
