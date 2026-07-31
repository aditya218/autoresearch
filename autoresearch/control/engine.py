"""The control loop, recovery, and admission. Doc 04 §4–5.

Invariants this file exists to hold:

  * Every state change goes through store.transitions.transition().
  * The intent row is committed BEFORE any external side effect.
  * Work already in flight is advanced before any new work is created.
  * The controller holds no in-memory state that recovery cannot rebuild.
"""
from __future__ import annotations

import json
import os
import time
import traceback

from ..domain import states
from ..executors.base import idem_key
from ..executors.command import CommandExecutor
from ..executors.local import LocalExecutor
from ..store import transitions as tr
from ..store.db import new_id, tx
from ..workflow import spec as wfspec
from . import lease as lease_mod


class Engine:
    def __init__(self, conn, campaign_id: str, artifact_root: str, tick_seconds: float = 2.0):
        self.conn = conn
        self.campaign_id = campaign_id
        self.artifact_root = artifact_root
        self.tick_seconds = tick_seconds
        self.run_id: str | None = None
        self.lease: lease_mod.Lease | None = None
        self.campaign: dict = {}
        self.workflow: wfspec.Workflow | None = None
        self.stop_requested = False

    # ------------------------------------------------------------------ setup

    def _load_campaign(self, cur) -> None:
        cur.execute("SELECT * FROM campaign WHERE campaign_id = %s", (self.campaign_id,))
        row = cur.fetchone()
        if not row:
            raise SystemExit(f"no such campaign {self.campaign_id}")
        self.campaign = dict(row)
        self.workflow = wfspec.build(self.campaign["config"]["workflow"])

    def start(self) -> bool:
        """Acquire the lease and register the run. False means someone else holds it."""
        self.run_id = new_id()
        with tx(self.conn) as cur:
            self._load_campaign(cur)
            token = lease_mod.acquire(cur, self.campaign_id, self.run_id)
            if token is None:
                held = lease_mod.holder(cur, self.campaign_id)
                print(f"lease held by run {held['run_id']} (token {held['fencing_token']})")
                return False
            self.lease = lease_mod.Lease(self.campaign_id, self.run_id, token)
            cur.execute(
                """INSERT INTO run (run_id, campaign_id, fencing_token,
                                    worker_identity, engine_version)
                   VALUES (%s,%s,%s,%s,%s)""",
                (self.run_id, self.campaign_id, token,
                 lease_mod.worker_identity(), "prototype-0.1"),
            )
            tr.log(cur, self.campaign_id, "run", self.run_id, "run_started",
                   {"token": token, "worker": lease_mod.worker_identity()},
                   to_state="ACTIVE", run_id=self.run_id)
        return True

    # --------------------------------------------------------------- recovery

    def recover(self) -> None:
        """Reconcile everything left behind by a previous run. Doc 04 §4.

        Pure state-reading plus external reconciliation. Anything a run needs
        but cannot rebuild here is a bug.
        """
        with tx(self.conn) as cur:
            # 1. Release hypothesis claims whose lease lapsed — requeue, never drop.
            cur.execute(
                """SELECT hypothesis_id FROM hypothesis
                    WHERE campaign_id = %s AND state = 'CLAIMED'
                      AND (claim_expires_at IS NULL OR claim_expires_at < now())""",
                (self.campaign_id,),
            )
            for row in cur.fetchall():
                tr.transition(cur, self.campaign_id, "hypothesis", row["hypothesis_id"],
                              "CLAIMED", "QUEUED", "claim_expired",
                              run_id=self.run_id, fencing_token=self.lease.token,
                              extra={"claim_run_id": None, "claim_expires_at": None})

        # 2. Reconcile non-terminal stages.
        for stage_row in self._stages_in_states(states.STAGE_NON_TERMINAL):
            self._recover_stage(stage_row)

        # 3. Experiments whose replicates are all terminal but which never aggregated.
        self._finalize_experiments()

    def _recover_stage(self, s: dict) -> None:
        ctx = self._stage_context(s)
        stage = self.workflow.stages[s["stage_key"]]

        if stage.kind == "local":
            # Cheap by lint rule, so just redo it: fail this attempt, retry next.
            with tx(self.conn) as cur:
                if s["state"] != "PENDING":
                    tr.transition(cur, self.campaign_id, "stage_execution",
                                  s["stage_execution_id"], s["state"], "FAILED",
                                  "recovered_local_incomplete",
                                  {"note": "controller restarted mid-stage; re-executing"},
                                  run_id=self.run_id, fencing_token=self.lease.token,
                                  extra={"failure_class": "infra"})
            return

        ex = CommandExecutor(stage, ctx["artifact_dir"], ctx["env"])

        if s["state"] == "LAUNCH_INTENT":
            # The one ambiguous state: a job may or may not be running.
            job_id = ex.recover(s["idempotency_key"])
            with tx(self.conn) as cur:
                if job_id:
                    tr.transition(cur, self.campaign_id, "stage_execution",
                                  s["stage_execution_id"], "LAUNCH_INTENT", "LAUNCHED",
                                  "reattached", {"job_id": job_id, "via": "receipt_or_find"},
                                  run_id=self.run_id, fencing_token=self.lease.token,
                                  extra={"job_id": job_id})
                else:
                    # No job exists. Fail this attempt; the retry path relaunches
                    # with attempt+1 rather than reusing an ambiguous key.
                    tr.transition(cur, self.campaign_id, "stage_execution",
                                  s["stage_execution_id"], "LAUNCH_INTENT", "FAILED",
                                  "launch_never_landed", {"tier": self.workflow.recovery_tier()},
                                  run_id=self.run_id, fencing_token=self.lease.token,
                                  extra={"failure_class": "infra"})
            return

        # LAUNCHED / RUNNING: the job_id is recorded; just resume polling.
        if s["job_id"]:
            with tx(self.conn) as cur:
                tr.log(cur, self.campaign_id, "stage_execution", s["stage_execution_id"],
                       "resumed_polling", {"job_id": s["job_id"]}, run_id=self.run_id)

    # ------------------------------------------------------------------- loop

    def run_forever(self, max_ticks: int | None = None) -> None:
        ticks = 0
        while not self.stop_requested:
            if max_ticks is not None and ticks >= max_ticks:
                return
            ticks += 1
            try:
                if not self.tick():
                    return
            except states.StaleFence:
                print("lease lost; another run is authoritative — exiting")
                self._end_run("lease_lost")
                return
            except Exception:
                traceback.print_exc()
                with tx(self.conn) as cur:
                    tr.log(cur, self.campaign_id, "run", self.run_id, "tick_error",
                           {"traceback": traceback.format_exc()[-2000:]}, run_id=self.run_id)
            time.sleep(self.tick_seconds)

    def tick(self) -> bool:
        """One iteration. Returns False when the campaign is finished."""
        with tx(self.conn) as cur:
            if not self.lease.maybe_renew(cur):
                raise states.StaleFence("renewal failed")
            self._load_campaign(cur)

        status = self.campaign["status"]
        if status in ("COMPLETED", "ARCHIVED"):
            self._end_run("campaign_stopped")
            return False

        # Advance work that is already in flight BEFORE creating any more of it.
        # A controller that proposes first accumulates work it never advances.
        self._poll_external_stages()
        self._advance_ready_stages()
        self._finalize_experiments()

        if status == "STOPPING":
            if self._in_flight_count() == 0:
                self._complete_campaign("manual")
                return False
            return True        # drain: in-flight jobs run to completion (D24)

        if status == "PAUSED":
            return True        # pause gates admission, not execution

        reason = self._stopping_reason()
        if reason:
            self._begin_stopping(reason)
            return True

        self._admit_experiments()
        return True

    # ------------------------------------------------------- stage advancement

    def _poll_external_stages(self) -> None:
        for s in self._stages_in_states({"LAUNCHED", "RUNNING"}):
            stage = self.workflow.stages[s["stage_key"]]
            if stage.kind != "external_job" or not s["job_id"]:
                continue
            ctx = self._stage_context(s)
            ex = CommandExecutor(stage, ctx["artifact_dir"], ctx["env"])
            st = ex.poll(s["job_id"])

            with tx(self.conn) as cur:
                # Polls are not history; only transitions are.
                cur.execute(
                    "UPDATE stage_execution SET last_polled_at = now() WHERE stage_execution_id = %s",
                    (s["stage_execution_id"],),
                )
                if st.state == "RUNNING":
                    if s["state"] == "LAUNCHED":
                        tr.transition(cur, self.campaign_id, "stage_execution",
                                      s["stage_execution_id"], "LAUNCHED", "RUNNING",
                                      "job_running", {"status": st.detail},
                                      run_id=self.run_id, fencing_token=self.lease.token)
                elif st.state == "COMPLETED":
                    tr.transition(cur, self.campaign_id, "stage_execution",
                                  s["stage_execution_id"], s["state"], "COMPLETED",
                                  "job_succeeded", {"status": st.detail},
                                  run_id=self.run_id, fencing_token=self.lease.token,
                                  extra={"outputs": self._collect_outputs(stage, ctx)})
                else:
                    self._fail_stage(cur, s, st, ex)

    def _fail_stage(self, cur, s: dict, st, ex) -> None:
        """Classify, then either retry as infra or surface as a result. D25."""
        cls = st.failure_class
        infra_count = s["infra_attempt_count"]
        stage = self.workflow.stages[s["stage_key"]]

        if cls is None:
            # Ambiguous. The ceiling is what stops a genuinely broken change
            # from retrying forever, and it applies regardless of any verdict.
            cls = "infra" if infra_count < stage.max_infra_retries else "experiment"

        tr.transition(cur, self.campaign_id, "stage_execution", s["stage_execution_id"],
                      s["state"], "FAILED", "job_failed",
                      {"status": st.detail, "class": cls, "infra_attempts": infra_count},
                      run_id=self.run_id, fencing_token=self.lease.token,
                      extra={"failure_class": cls})

    def _advance_ready_stages(self) -> None:
        """Start stages whose dependencies are satisfied, and handle retries."""
        cur_rows = self._active_replicates()
        for rep in cur_rows:
            done, failed, live, attempts = self._stage_status(rep["replicate_id"])
            if live:
                continue                      # something already in flight

            # A failed stage either retries (infra, under the ceiling) or ends the replicate.
            if failed:
                stage_key, info = failed
                stage = self.workflow.stages[stage_key]
                if (info["failure_class"] == "infra"
                        and info["infra_attempt_count"] < stage.max_infra_retries):
                    self._start_stage(rep, stage_key,
                                      attempt=attempts[stage_key] + 1,
                                      infra_count=info["infra_attempt_count"] + 1)
                else:
                    self._finish_replicate(rep, "FAILED", info["failure_class"] or "experiment")
                continue

            nxt = self._next_stage(done)
            if nxt is None:
                self._finish_replicate(rep, "COMPLETED", None)
            else:
                self._start_stage(rep, nxt, attempt=attempts.get(nxt, 0) + 1, infra_count=0)

    def _next_stage(self, done: set[str]) -> str | None:
        for key in self.workflow.order:
            if key in done:
                continue
            if all(dep in done for dep in self.workflow.stages[key].needs):
                return key
        return None

    def _start_stage(self, rep: dict, stage_key: str, attempt: int, infra_count: int) -> None:
        stage = self.workflow.stages[stage_key]
        if not self.lease.safe_to_launch:
            return          # surrender authority before the TTL, not after

        se_id = new_id()
        key = idem_key(self.campaign_id, rep["experiment_id"], stage_key, attempt)
        artifact_dir = self._artifact_dir(rep, stage_key, attempt)
        os.makedirs(artifact_dir, exist_ok=True)

        # ---- INTENT FIRST, COMMITTED, before any side effect ----
        with tx(self.conn) as cur:
            cur.execute(
                """INSERT INTO stage_execution
                     (stage_execution_id, replicate_id, stage_key, attempt, run_id,
                      kind, idempotency_key, state, infra_attempt_count, started_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'PENDING',%s, now())""",
                (se_id, rep["replicate_id"], stage_key, attempt, self.run_id,
                 stage.kind, key, infra_count),
            )
            tr.transition(cur, self.campaign_id, "stage_execution", se_id,
                          "PENDING", "LAUNCH_INTENT", "launch_intent",
                          {"stage": stage_key, "attempt": attempt, "key": key},
                          run_id=self.run_id, fencing_token=self.lease.token)

        s = {"stage_execution_id": se_id, "replicate_id": rep["replicate_id"],
             "stage_key": stage_key, "attempt": attempt, "idempotency_key": key}
        ctx = self._stage_context({**rep, **s})

        if stage.kind == "local":
            ex = LocalExecutor(stage, artifact_dir, ctx["env"])
            with tx(self.conn) as cur:
                tr.transition(cur, self.campaign_id, "stage_execution", se_id,
                              "LAUNCH_INTENT", "RUNNING", "local_started", {},
                              run_id=self.run_id, fencing_token=self.lease.token)
            st = ex.run()
            with tx(self.conn) as cur:
                if st.state == "COMPLETED":
                    tr.transition(cur, self.campaign_id, "stage_execution", se_id,
                                  "RUNNING", "COMPLETED", "local_completed", {},
                                  run_id=self.run_id, fencing_token=self.lease.token,
                                  extra={"outputs": self._collect_outputs(stage, ctx)})
                else:
                    tr.transition(cur, self.campaign_id, "stage_execution", se_id,
                                  "RUNNING", "FAILED", "local_failed",
                                  {"detail": st.detail[-500:], "class": st.failure_class},
                                  run_id=self.run_id, fencing_token=self.lease.token,
                                  extra={"failure_class": st.failure_class})
            return

        # external job: launch, then record. The window between is what the
        # receipt file and `find` exist to resolve.
        ex = CommandExecutor(stage, artifact_dir, ctx["env"])
        try:
            job_id = ex.launch(key)
        except Exception as exc:
            with tx(self.conn) as cur:
                tr.transition(cur, self.campaign_id, "stage_execution", se_id,
                              "LAUNCH_INTENT", "FAILED", "launch_error",
                              {"error": str(exc)[-500:]},
                              run_id=self.run_id, fencing_token=self.lease.token,
                              extra={"failure_class": "infra"})
            return

        with tx(self.conn) as cur:
            tr.transition(cur, self.campaign_id, "stage_execution", se_id,
                          "LAUNCH_INTENT", "LAUNCHED", "job_launched", {"job_id": job_id},
                          run_id=self.run_id, fencing_token=self.lease.token,
                          extra={"job_id": job_id})

    # ------------------------------------------------------------- experiments

    def _finish_replicate(self, rep: dict, state: str, failure_class: str | None) -> None:
        metrics = self._read_metrics(rep)
        with tx(self.conn) as cur:
            tr.transition(cur, self.campaign_id, "replicate", rep["replicate_id"],
                          rep["state"], state, "replicate_finished",
                          {"failure_class": failure_class},
                          run_id=self.run_id, fencing_token=self.lease.token,
                          extra=({"outcome": failure_class or "success", "metrics": metrics}
                                 if metrics else {"outcome": failure_class or "success"}))

    def _finalize_experiments(self) -> None:
        """Aggregate an experiment once all its replicates are terminal."""
        with tx(self.conn) as cur:
            cur.execute(
                """SELECT e.* FROM experiment e
                    WHERE e.campaign_id = %s AND e.state IN ('RUNNING','AGGREGATING')
                      AND NOT EXISTS (SELECT 1 FROM replicate r
                                       WHERE r.experiment_id = e.experiment_id
                                         AND r.state IN ('PENDING','RUNNING'))""",
                (self.campaign_id,),
            )
            experiments = cur.fetchall()

        for e in experiments:
            with tx(self.conn) as cur:
                cur.execute("SELECT * FROM replicate WHERE experiment_id = %s",
                            (e["experiment_id"],))
                reps = cur.fetchall()
                if e["state"] == "RUNNING":
                    if not tr.transition(cur, self.campaign_id, "experiment",
                                         e["experiment_id"], "RUNNING", "AGGREGATING",
                                         "replicates_terminal", {"n": len(reps)},
                                         run_id=self.run_id, fencing_token=self.lease.token):
                        continue

                ok = [r for r in reps if r["state"] == "COMPLETED"]
                if ok:
                    metrics = self._aggregate([r["metrics"] for r in ok if r["metrics"]])
                    tr.transition(cur, self.campaign_id, "experiment", e["experiment_id"],
                                  "AGGREGATING", "SUCCEEDED", "experiment_succeeded",
                                  {"metrics": metrics},
                                  run_id=self.run_id, fencing_token=self.lease.token,
                                  extra={"metrics": metrics, "outcome": "success",
                                         "current_stage": None})
                else:
                    cls = next((r["outcome"] for r in reps if r["outcome"]), "experiment_failure")
                    outcome = "infra_failure" if cls == "infra" else "experiment_failure"
                    tr.transition(cur, self.campaign_id, "experiment", e["experiment_id"],
                                  "AGGREGATING", "FAILED", "experiment_failed",
                                  {"class": cls},
                                  run_id=self.run_id, fencing_token=self.lease.token,
                                  extra={"outcome": outcome, "current_stage": None})

    # --------------------------------------------------------------- admission

    def _admit_experiments(self) -> None:
        budget = self.campaign["config"].get("budget", {})
        max_concurrent = budget.get("max_concurrent_experiments", 2)
        max_experiments = budget.get("max_experiments", 20)

        while self._in_flight_count() < max_concurrent:
            if self._experiment_count() >= max_experiments:
                return
            if not self._materialize_one():
                return

    def _materialize_one(self) -> bool:
        """Claim a queued hypothesis and turn it into an experiment."""
        with tx(self.conn) as cur:
            cur.execute(
                """SELECT * FROM hypothesis
                    WHERE campaign_id = %s AND state = 'QUEUED'
                    ORDER BY priority DESC, created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1""",
                (self.campaign_id,),
            )
            h = cur.fetchone()
            if not h:
                return False

            if not tr.transition(cur, self.campaign_id, "hypothesis", h["hypothesis_id"],
                                 "QUEUED", "CLAIMED", "claimed",
                                 run_id=self.run_id, fencing_token=self.lease.token,
                                 extra={"claim_run_id": self.run_id,
                                        "claim_expires_at": None}):
                return False

            exp_id = new_id()
            pins = self.campaign["config"].get("provenance_pins", {})
            resolved = {"parameters": h["parameters"], "change_spec": h["change_spec"]}
            cur.execute(
                """INSERT INTO experiment
                     (experiment_id, campaign_id, hypothesis_id, created_by_run_id, role,
                      branch, base_commit, workflow_version, resolved_config,
                      resolved_config_hash, provenance, state)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'CREATED')""",
                (exp_id, self.campaign_id, h["hypothesis_id"], self.run_id,
                 h["parameters"].get("role", "primary"),
                 f"autoresearch/{self.campaign_id}/{exp_id}",
                 pins.get("base_commit", "unknown"),
                 self.workflow.workflow_version,
                 json.dumps(resolved),
                 str(abs(hash(json.dumps(resolved, sort_keys=True)))),
                 json.dumps(pins)),
            )
            tr.log(cur, self.campaign_id, "experiment", exp_id, "experiment_created",
                   {"hypothesis": h["hypothesis_id"], "statement": h["statement"]},
                   to_state="CREATED", run_id=self.run_id)

            tr.transition(cur, self.campaign_id, "hypothesis", h["hypothesis_id"],
                          "CLAIMED", "MATERIALIZED", "materialized",
                          {"experiment_id": exp_id},
                          run_id=self.run_id, fencing_token=self.lease.token)

            tr.transition(cur, self.campaign_id, "experiment", exp_id,
                          "CREATED", "ADMITTED", "admitted",
                          {"slot": True}, run_id=self.run_id,
                          fencing_token=self.lease.token)

            rep_id = new_id()
            seed = self.campaign["config"].get("replication", {}).get("base_seed", 1000)
            cur.execute(
                """INSERT INTO replicate (replicate_id, experiment_id, seed, state, started_at)
                   VALUES (%s,%s,%s,'PENDING', now())""",
                (rep_id, exp_id, seed),
            )
            tr.transition(cur, self.campaign_id, "experiment", exp_id,
                          "ADMITTED", "RUNNING", "started",
                          run_id=self.run_id, fencing_token=self.lease.token)
            tr.transition(cur, self.campaign_id, "replicate", rep_id,
                          "PENDING", "RUNNING", "replicate_started",
                          run_id=self.run_id, fencing_token=self.lease.token)
        return True

    # ---------------------------------------------------------------- stopping

    def _stopping_reason(self) -> str | None:
        budget = self.campaign["config"].get("budget", {})
        with tx(self.conn) as cur:
            cur.execute(
                """SELECT count(*) FILTER (WHERE state IN ('SUCCEEDED','FAILED','ABORTED')) AS done,
                          count(*) AS total
                     FROM experiment WHERE campaign_id = %s""",
                (self.campaign_id,),
            )
            counts = cur.fetchone()
            cur.execute(
                "SELECT count(*) AS n FROM hypothesis WHERE campaign_id = %s AND state = 'QUEUED'",
                (self.campaign_id,),
            )
            queued = cur.fetchone()["n"]

        if counts["total"] >= budget.get("max_experiments", 20) and counts["done"] == counts["total"]:
            return "budget_exhausted"
        # The prototype has no proposer, so an empty queue with nothing running
        # is the end of the campaign rather than a cue to think of more ideas.
        if queued == 0 and self._in_flight_count() == 0 and counts["total"] > 0:
            return "converged"
        return None

    def _begin_stopping(self, reason: str) -> None:
        with tx(self.conn) as cur:
            tr.transition(cur, self.campaign_id, "campaign", self.campaign_id,
                          self.campaign["status"], "STOPPING", "stopping", {"reason": reason},
                          run_id=self.run_id, fencing_token=self.lease.token,
                          extra={"stop_reason": reason})

    def _complete_campaign(self, fallback: str) -> None:
        with tx(self.conn) as cur:
            cur.execute("SELECT stop_reason FROM campaign WHERE campaign_id = %s",
                        (self.campaign_id,))
            reason = cur.fetchone()["stop_reason"] or fallback
            tr.transition(cur, self.campaign_id, "campaign", self.campaign_id,
                          "STOPPING", "COMPLETED", "completed", {"reason": reason},
                          run_id=self.run_id, fencing_token=self.lease.token,
                          extra={"stop_reason": reason})
        self._end_run("campaign_stopped")

    def _end_run(self, reason: str) -> None:
        with tx(self.conn) as cur:
            tr.transition(cur, self.campaign_id, "run", self.run_id, "ACTIVE", "ENDED",
                          "run_ended", {"reason": reason}, run_id=self.run_id,
                          extra={"end_reason": reason})
            if self.lease and not self.lease.lost:
                lease_mod.release(cur, self.campaign_id, self.run_id, self.lease.token)

    # ----------------------------------------------------------------- helpers

    def _stages_in_states(self, wanted: set[str]) -> list[dict]:
        with tx(self.conn) as cur:
            cur.execute(
                """SELECT se.*, r.experiment_id, r.replicate_id
                     FROM stage_execution se
                     JOIN replicate r USING (replicate_id)
                     JOIN experiment e USING (experiment_id)
                    WHERE e.campaign_id = %s AND se.state = ANY(%s)
                    ORDER BY se.started_at""",
                (self.campaign_id, list(wanted)),
            )
            return [dict(r) for r in cur.fetchall()]

    def _active_replicates(self) -> list[dict]:
        with tx(self.conn) as cur:
            cur.execute(
                """SELECT r.*, e.experiment_id FROM replicate r
                     JOIN experiment e USING (experiment_id)
                    WHERE e.campaign_id = %s AND r.state = 'RUNNING'""",
                (self.campaign_id,),
            )
            return [dict(x) for x in cur.fetchall()]

    def _stage_status(self, replicate_id: str):
        """(completed keys, unresolved failure, live count, latest attempt per key)

        The failure reported is the LATEST attempt of the stage, not the first.
        Reporting the first makes infra_attempt_count look permanently zero and
        the retry loop never terminates.
        """
        with tx(self.conn) as cur:
            cur.execute(
                "SELECT * FROM stage_execution WHERE replicate_id = %s ORDER BY attempt",
                (replicate_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]

        latest: dict[str, dict] = {}
        done, attempts, live = set(), {}, 0
        for r in rows:
            key = r["stage_key"]
            attempts[key] = max(attempts.get(key, 0), r["attempt"])
            if key not in latest or r["attempt"] >= latest[key]["attempt"]:
                latest[key] = r
            if r["state"] == "COMPLETED":
                done.add(key)
            elif r["state"] in states.STAGE_NON_TERMINAL:
                live += 1

        failed = None
        for key in self.workflow.order:
            r = latest.get(key)
            if r and r["state"] == "FAILED" and key not in done:
                failed = (key, r)
                break
        return done, failed, live, attempts

    def _in_flight_count(self) -> int:
        with tx(self.conn) as cur:
            cur.execute(
                """SELECT count(*) AS n FROM experiment
                    WHERE campaign_id = %s AND state IN ('ADMITTED','RUNNING','AGGREGATING')""",
                (self.campaign_id,),
            )
            return cur.fetchone()["n"]

    def _experiment_count(self) -> int:
        with tx(self.conn) as cur:
            cur.execute("SELECT count(*) AS n FROM experiment WHERE campaign_id = %s",
                        (self.campaign_id,))
            return cur.fetchone()["n"]

    def _artifact_dir(self, rep: dict, stage_key: str, attempt: int) -> str:
        return os.path.join(
            self.artifact_root, self.campaign["project_id"], self.campaign_id,
            "experiments", rep["experiment_id"], "replicates", rep["replicate_id"],
            "stages", stage_key, str(attempt),
        )

    def _replicate_dir(self, rep: dict) -> str:
        return os.path.join(
            self.artifact_root, self.campaign["project_id"], self.campaign_id,
            "experiments", rep["experiment_id"], "replicates", rep["replicate_id"],
        )

    def _stage_context(self, s: dict) -> dict:
        artifact_dir = self._artifact_dir(s, s["stage_key"], s.get("attempt", 1))
        return {
            "artifact_dir": artifact_dir,
            "env": {
                "AUTORESEARCH_CAMPAIGN": self.campaign_id,
                "AUTORESEARCH_EXPERIMENT": s.get("experiment_id", ""),
                "AUTORESEARCH_REPLICATE": s.get("replicate_id", ""),
                "AUTORESEARCH_STAGE": s["stage_key"],
                "AUTORESEARCH_SEED": str(s.get("seed", 0)),
            },
        }

    def _collect_outputs(self, stage, ctx: dict) -> dict:
        out = {}
        for name, tmpl in stage.outputs.items():
            path = tmpl.replace("{{ artifact_dir }}", ctx["artifact_dir"]) \
                       .replace("{{artifact_dir}}", ctx["artifact_dir"])
            out[name] = path
        return out

    def _read_metrics(self, rep: dict) -> dict | None:
        """Find the metrics file written by the metrics-producing stage."""
        with tx(self.conn) as cur:
            cur.execute(
                """SELECT outputs FROM stage_execution
                    WHERE replicate_id = %s AND state = 'COMPLETED'
                      AND outputs ? 'metrics' ORDER BY attempt DESC LIMIT 1""",
                (rep["replicate_id"],),
            )
            row = cur.fetchone()
        if not row or not row["outputs"].get("metrics"):
            return None
        path = row["outputs"]["metrics"]
        if not os.path.exists(path):
            return None
        try:
            return json.load(open(path))
        except Exception:
            return None

    @staticmethod
    def _aggregate(metric_dicts: list[dict]) -> dict:
        """n=1 in the prototype, but shaped so replication drops in unchanged."""
        agg: dict[str, dict] = {}
        keys = {k for d in metric_dicts for k in d}
        for k in keys:
            vals = [float(d[k]) for d in metric_dicts if k in d]
            if not vals:
                continue
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1) if len(vals) > 1 else 0.0
            agg[k] = {"value": mean, "stddev": var ** 0.5, "n": len(vals)}
        return agg
