"""Campaign lease. Doc 04 §1.

Exactly one run drives a campaign at a time. A run that loses the lease must
stop writing immediately; the fencing token makes that true regardless of what
the run believes about itself.
"""
from __future__ import annotations

import datetime
import socket
import os

TTL_SECONDS = 60
RENEW_EVERY = TTL_SECONDS / 3
# Stop launching new work once we are this far past the last successful renewal.
# Deliberately half the TTL: authority must be surrendered before it expires,
# not after, or two processes can briefly both believe they hold it.
UNSAFE_AFTER = TTL_SECONDS / 2


def worker_identity() -> str:
    return f"{socket.gethostname()}/{os.getpid()}"


def acquire(cur, campaign_id: str, run_id: str, ttl: int = TTL_SECONDS) -> int | None:
    """Take the lease if it is free or expired. Returns the fencing token, or None."""
    cur.execute(
        """INSERT INTO campaign_lease AS l
                (campaign_id, run_id, fencing_token, expires_at, heartbeat_at)
           VALUES (%s, %s, 1, now() + make_interval(secs => %s), now())
           ON CONFLICT (campaign_id) DO UPDATE
              SET run_id        = EXCLUDED.run_id,
                  fencing_token = l.fencing_token + 1,
                  expires_at    = EXCLUDED.expires_at,
                  heartbeat_at  = now()
            WHERE l.expires_at < now()
        RETURNING fencing_token, (l.run_id IS DISTINCT FROM %s) AS seized""",
        (campaign_id, run_id, ttl, run_id),
    )
    row = cur.fetchone()
    return row["fencing_token"] if row else None


def renew(cur, campaign_id: str, run_id: str, token: int, ttl: int = TTL_SECONDS) -> bool:
    cur.execute(
        """UPDATE campaign_lease
              SET expires_at = now() + make_interval(secs => %s), heartbeat_at = now()
            WHERE campaign_id = %s AND run_id = %s AND fencing_token = %s""",
        (ttl, campaign_id, run_id, token),
    )
    return cur.rowcount == 1


def release(cur, campaign_id: str, run_id: str, token: int) -> None:
    """Expire the lease so the next run can take it immediately."""
    cur.execute(
        """UPDATE campaign_lease SET expires_at = now() - interval '1 second'
            WHERE campaign_id = %s AND run_id = %s AND fencing_token = %s""",
        (campaign_id, run_id, token),
    )


def holder(cur, campaign_id: str) -> dict | None:
    cur.execute("SELECT * FROM campaign_lease WHERE campaign_id = %s", (campaign_id,))
    return cur.fetchone()


class Lease:
    """Tracks local authority between renewals."""

    def __init__(self, campaign_id: str, run_id: str, token: int):
        self.campaign_id = campaign_id
        self.run_id = run_id
        self.token = token
        self.last_renewed = datetime.datetime.now(datetime.timezone.utc)
        self.lost = False

    def maybe_renew(self, cur) -> bool:
        age = (datetime.datetime.now(datetime.timezone.utc) - self.last_renewed).total_seconds()
        if age < RENEW_EVERY:
            return True
        if renew(cur, self.campaign_id, self.run_id, self.token):
            self.last_renewed = datetime.datetime.now(datetime.timezone.utc)
            return True
        self.lost = True
        return False

    @property
    def safe_to_launch(self) -> bool:
        """False once we cannot prove we still hold the lease."""
        if self.lost:
            return False
        age = (datetime.datetime.now(datetime.timezone.utc) - self.last_renewed).total_seconds()
        return age < UNSAFE_AFTER
