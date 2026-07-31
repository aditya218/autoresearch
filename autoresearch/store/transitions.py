"""The single chokepoint for state changes. Doc 02 §2.

Every transition is:
  1. a fencing check   — a superseded run cannot write
  2. a compare-and-swap on the state column, with a row-count check
  3. a transition_log insert, in the SAME transaction

Nothing else in the codebase may UPDATE a state column.

Deviation from the spec, noted deliberately: doc 02 puts the fencing check
inside a plpgsql function so application code cannot forget it. Here it lives
in Python. The property is preserved by making this the only writer and by
testing it directly (see tests/test_fencing.py) rather than by making it
structurally impossible.
"""
from __future__ import annotations

import json
from typing import Any

from ..domain import states


def _fence_ok(cur, campaign_id: str, fencing_token: int | None) -> bool:
    if fencing_token is None:          # system/CLI writes are unfenced
        return True
    cur.execute(
        "SELECT 1 FROM campaign_lease WHERE campaign_id = %s AND fencing_token = %s",
        (campaign_id, fencing_token),
    )
    return cur.fetchone() is not None


def log(
    cur,
    campaign_id: str,
    entity_type: str,
    entity_id: str,
    reason: str,
    detail: dict[str, Any] | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    run_id: str | None = None,
    actor: str | None = None,
) -> None:
    """Write a transition_log row.

    Called with to_state=None for decision records — the 'why' that the state
    tables do not capture (doc 02 §4).
    """
    cur.execute(
        """INSERT INTO transition_log
             (campaign_id, entity_type, entity_id, from_state, to_state,
              reason, detail, run_id, actor)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (campaign_id, entity_type, entity_id, from_state, to_state,
         reason, json.dumps(detail or {}), run_id, actor),
    )


def transition(
    cur,
    campaign_id: str,
    entity_type: str,
    entity_id: str,
    frm: str,
    to: str,
    reason: str,
    detail: dict[str, Any] | None = None,
    run_id: str | None = None,
    fencing_token: int | None = None,
    actor: str | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Move an entity from `frm` to `to`. Returns False if someone else moved it.

    `extra` sets additional columns in the same UPDATE, so a state change and
    the data that goes with it (metrics, job_id, outcome) commit together.
    """
    states.check(entity_type, frm, to)

    if not _fence_ok(cur, campaign_id, fencing_token):
        raise states.StaleFence(
            f"run {run_id} token {fencing_token} is no longer the lease holder"
        )

    table, pk, state_col = states.TABLES[entity_type]
    sets = [f"{state_col} = %s"]
    params: list[Any] = [to]
    for col, val in (extra or {}).items():
        sets.append(f"{col} = %s")
        params.append(json.dumps(val) if isinstance(val, (dict, list)) else val)
    if entity_type in ("experiment", "hypothesis"):
        sets.append("updated_at = now()")
    params += [entity_id, frm]

    cur.execute(
        f"UPDATE {table} SET {', '.join(sets)} WHERE {pk} = %s AND {state_col} = %s",
        params,
    )
    if cur.rowcount == 0:
        return False        # lost the CAS; caller re-reads and decides

    log(cur, campaign_id, entity_type, entity_id, reason, detail,
        from_state=frm, to_state=to, run_id=run_id, actor=actor)
    return True
