"""Database access. Raw SQL, no ORM (D22)."""
from __future__ import annotations

import contextlib
import os
import uuid

import psycopg2
import psycopg2.extras


def dsn() -> str:
    return os.environ.get(
        "AUTORESEARCH_DSN", "host=/var/run/postgresql user=postgres dbname=autoresearch"
    )


def connect():
    conn = psycopg2.connect(dsn())
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


@contextlib.contextmanager
def tx(conn):
    """One transaction. Commits on success, rolls back on any exception.

    Everything that must be atomic — notably a state change and its
    transition_log row — goes through here.
    """
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    except Exception:
        raise


def new_id() -> str:
    return str(uuid.uuid4())


def short(entity_id: str, n: int = 8) -> str:
    return entity_id.replace("-", "")[:n]
