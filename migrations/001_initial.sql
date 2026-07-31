-- Autoresearch engine — initial schema.
--
-- State tables are the source of truth (D2). transition_log is append-only and
-- exists for humans debugging a campaign; nothing in the control path reads it.
-- See docs/spec/01-data-model.md and 02-state-and-history.md.

BEGIN;

-- ---------------------------------------------------------------- projects

CREATE TABLE project (
  project_id           uuid PRIMARY KEY,
  name                 text        NOT NULL UNIQUE,
  description          text,
  metric_registry      jsonb       NOT NULL,   -- doc 07: name -> {unit, direction, noise, aggregation}
  default_workflow_ref text,
  created_by           text        NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- campaigns

CREATE TABLE campaign (
  campaign_id        uuid PRIMARY KEY,
  project_id         uuid NOT NULL REFERENCES project,
  parent_campaign_id uuid REFERENCES campaign,        -- set when forked (D13)
  fork_reason        text,
  config             jsonb NOT NULL,                  -- frozen fields immutable once ACTIVE (D18)
  config_hash        text  NOT NULL,
  seed_context       jsonb,
  status             text  NOT NULL DEFAULT 'DRAFT'
                     CHECK (status IN ('DRAFT','ACTIVE','PAUSED','STOPPING','COMPLETED','ARCHIVED')),
  stop_reason        text CHECK (stop_reason IN
                       ('budget_exhausted','converged','target_reached','manual','fatal_error')),
  created_by         text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  ended_at           timestamptz
);

-- Exactly one run drives a campaign at a time. Fencing token is monotonic and is
-- checked inside transition(); a superseded run cannot write. Doc 04 §1.
CREATE TABLE campaign_lease (
  campaign_id   uuid PRIMARY KEY REFERENCES campaign,
  run_id        uuid        NOT NULL,
  fencing_token bigint      NOT NULL,
  expires_at    timestamptz NOT NULL,
  heartbeat_at  timestamptz NOT NULL
);

-- In-place config edits permitted by D18 (budget, concurrency, proposer, stopping).
CREATE TABLE campaign_amendment (
  id          bigserial PRIMARY KEY,
  campaign_id uuid NOT NULL REFERENCES campaign,
  field       text NOT NULL,
  old_value   jsonb,
  new_value   jsonb,
  actor       text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- runs

CREATE TABLE run (
  run_id          uuid PRIMARY KEY,
  campaign_id     uuid   NOT NULL REFERENCES campaign,
  fencing_token   bigint NOT NULL,
  worker_identity text   NOT NULL,
  engine_version  text   NOT NULL,
  status          text   NOT NULL DEFAULT 'ACTIVE'
                  CHECK (status IN ('ACTIVE','DRAINING','ENDED')),
  end_reason      text CHECK (end_reason IN
                    ('clean_shutdown','lease_lost','crashed','campaign_stopped')),
  started_at      timestamptz NOT NULL DEFAULT now(),
  heartbeat_at    timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz
);

-- ---------------------------------------------------------------- proposals

-- The exact brief a proposer saw. Retained so a past decision stays inspectable
-- even though surrounding state is not reconstructible (D2).
CREATE TABLE proposer_context (
  proposer_context_id uuid PRIMARY KEY,
  campaign_id         uuid  NOT NULL REFERENCES campaign,
  brief               jsonb NOT NULL,
  brief_hash          text  NOT NULL,
  model              text  NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hypothesis (
  hypothesis_id       uuid PRIMARY KEY,
  campaign_id         uuid NOT NULL REFERENCES campaign,
  created_by_run_id   uuid REFERENCES run,
  proposer_context_id uuid REFERENCES proposer_context,
  origin              text NOT NULL CHECK (origin IN
                        ('proposer','human','seed','auto_replication','auto_repair')),
  actor               text,                          -- set when origin = 'human' (D20)

  statement           text  NOT NULL,
  rationale           text  NOT NULL,
  change_spec         jsonb NOT NULL,                -- instruction to the coding agent (D5)
  structural_family   text  NOT NULL,                -- mode-collapse instrument (doc 05)
  parameters          jsonb NOT NULL DEFAULT '{}',
  predicted_effect    jsonb,
  predicted_cost      jsonb,
  derived_from        jsonb NOT NULL DEFAULT '{}',   -- lineage: experiment_ids, hypothesis_ids

  dedup_fingerprint   text  NOT NULL,
  priority            double precision NOT NULL DEFAULT 0,
  state               text  NOT NULL DEFAULT 'PROPOSED' CHECK (state IN
                        ('PROPOSED','QUEUED','CLAIMED','MATERIALIZED',
                         'REJECTED','SUPERSEDED','EXPIRED')),
  state_reason        text,
  superseded_by       uuid REFERENCES hypothesis,
  claim_run_id        uuid REFERENCES run,
  claim_expires_at    timestamptz,

  proposed_at_experiment_count int NOT NULL,          -- staleness input; a column, not a log query
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),

  UNIQUE (campaign_id, dedup_fingerprint)
);

-- ---------------------------------------------------------------- experiments

CREATE TABLE experiment (
  experiment_id        uuid PRIMARY KEY,
  campaign_id          uuid NOT NULL REFERENCES campaign,
  hypothesis_id        uuid NOT NULL REFERENCES hypothesis,
  created_by_run_id    uuid NOT NULL REFERENCES run,   -- provenance only, never ownership (D1)
  variant_label        text,
  role                 text NOT NULL DEFAULT 'primary' CHECK (role IN
                         ('primary','ablation','replication','confirmation','baseline')),

  -- the change itself (D5)
  branch               text NOT NULL,
  base_commit          text NOT NULL,
  commit_sha           text,                          -- null until `implement` completes
  diff_hash            text,                          -- exact dedup / result cache
  repair_iterations    int  NOT NULL DEFAULT 0,       -- build/test repair rounds (D26)

  workflow_version     text  NOT NULL,
  resolved_config      jsonb NOT NULL,
  resolved_config_hash text  NOT NULL,
  provenance           jsonb NOT NULL,                -- base_commit, image digest, dataset, hardware

  state                text NOT NULL DEFAULT 'CREATED' CHECK (state IN
                         ('CREATED','ADMITTED','RUNNING','AGGREGATING',
                          'SUCCEEDED','FAILED','ABORTED')),
  current_stage        text,
  outcome              text CHECK (outcome IN
                         ('success','experiment_failure','could_not_implement',
                          'infra_failure','aborted')),
  outcome_detail       text,

  metrics              jsonb,                         -- aggregated: {value, stddev, n, ci_low, ci_high}
  guardrail_violations jsonb,
  high_variance        boolean NOT NULL DEFAULT false,
  cost                 jsonb NOT NULL DEFAULT '{}',
  artifacts            jsonb NOT NULL DEFAULT '{}',   -- FS paths + integrity hashes (D16)
  analysis             text,

  -- retraction never deletes: doc 07's confirmation policy needs these visible
  invalidated_at       timestamptz,
  invalidation_reason  text,

  created_at           timestamptz NOT NULL DEFAULT now(),
  started_at           timestamptz,
  ended_at             timestamptz,
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE replicate (
  replicate_id  uuid PRIMARY KEY,
  experiment_id uuid   NOT NULL REFERENCES experiment,
  seed          bigint NOT NULL,
  state         text   NOT NULL DEFAULT 'PENDING' CHECK (state IN
                  ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')),
  outcome       text,
  metrics       jsonb,
  cost          jsonb NOT NULL DEFAULT '{}',
  started_at    timestamptz,
  ended_at      timestamptz,
  UNIQUE (experiment_id, seed)
);

-- One row per attempt. A retry INSERTs attempt+1; it never mutates the failed row.
CREATE TABLE stage_execution (
  stage_execution_id  uuid PRIMARY KEY,
  replicate_id        uuid NOT NULL REFERENCES replicate,
  stage_key           text NOT NULL,
  attempt             int  NOT NULL DEFAULT 1,
  run_id              uuid NOT NULL REFERENCES run,    -- the run that drove THIS attempt
  kind                text NOT NULL CHECK (kind IN ('local','external_job')),

  -- readable, deterministic, greppable in the job system:
  --   ar-{campaign[:8]}-{experiment[:8]}-{stage_key}-{attempt}
  idempotency_key     text NOT NULL UNIQUE,
  job_id              text,                            -- as printed by the user's launch command

  state               text NOT NULL DEFAULT 'PENDING' CHECK (state IN
                        ('PENDING','LAUNCH_INTENT','LAUNCHED','RUNNING',
                         'COMPLETED','FAILED','CANCELLED')),
  failure_class       text CHECK (failure_class IN ('infra','experiment')),
  infra_attempt_count int NOT NULL DEFAULT 0,          -- vs max_infra_reclassify (D25)

  inputs_hash         text,
  outputs             jsonb,
  cost                jsonb NOT NULL DEFAULT '{}',
  started_at          timestamptz,
  ended_at            timestamptz,
  last_polled_at      timestamptz,                     -- updated in place; polls are not history

  UNIQUE (replicate_id, stage_key, attempt)
);

-- ---------------------------------------------------------------- human input
-- These ARE read by the control path — they are inputs to the system, not a
-- record of it. Unlike transition_log. See doc 02 §5.

CREATE TABLE human_note (
  note_id     uuid PRIMARY KEY,
  campaign_id uuid NOT NULL REFERENCES campaign,
  text        text NOT NULL,                  -- appears in every subsequent brief (D14)
  actor       text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approval (
  approval_id   uuid PRIMARY KEY,
  campaign_id   uuid NOT NULL REFERENCES campaign,
  subject_type  text NOT NULL,
  subject_id    uuid NOT NULL,
  gate          text NOT NULL,
  cost_estimate jsonb,
  state         text NOT NULL DEFAULT 'REQUESTED'
                CHECK (state IN ('REQUESTED','GRANTED','DENIED')),
  actor         text,
  note          text,
  requested_at  timestamptz NOT NULL DEFAULT now(),
  decided_at    timestamptz
);

-- ---------------------------------------------------------------- history

-- Append-only. Written in the SAME TRANSACTION as every state change.
-- NOTHING IN THE CONTROL PATH READS THIS TABLE. Doc 02 §3.
CREATE TABLE transition_log (
  id          bigserial PRIMARY KEY,
  campaign_id uuid NOT NULL,
  entity_type text NOT NULL,
  entity_id   uuid NOT NULL,
  from_state  text,
  to_state    text,          -- NULL for decision records: why, not what
  reason      text NOT NULL,
  detail      jsonb,
  run_id      uuid,
  actor       text,
  occurred_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- indices

CREATE INDEX ON hypothesis      (campaign_id, state, priority DESC);
CREATE INDEX ON experiment      (campaign_id, state);
CREATE INDEX ON experiment      (campaign_id, diff_hash);
CREATE INDEX ON experiment      (campaign_id, resolved_config_hash);
CREATE INDEX ON replicate       (experiment_id, state);
CREATE INDEX ON stage_execution (state, last_polled_at)
  WHERE state IN ('LAUNCH_INTENT','LAUNCHED','RUNNING');   -- the poller's work queue
CREATE INDEX ON transition_log  (campaign_id, occurred_at);
CREATE INDEX ON transition_log  (entity_type, entity_id, occurred_at);

-- ---------------------------------------------------------------- guards

-- Terminal experiments are frozen except for retraction and analysis.
CREATE FUNCTION freeze_terminal_experiment() RETURNS trigger AS $$
BEGIN
  IF OLD.state IN ('SUCCEEDED','FAILED','ABORTED')
     AND (NEW.metrics IS DISTINCT FROM OLD.metrics
       OR NEW.provenance IS DISTINCT FROM OLD.provenance
       OR NEW.commit_sha IS DISTINCT FROM OLD.commit_sha
       OR NEW.state <> OLD.state) THEN
    RAISE EXCEPTION 'terminal experiment % is frozen', OLD.experiment_id;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER experiment_freeze BEFORE UPDATE ON experiment
  FOR EACH ROW EXECUTE FUNCTION freeze_terminal_experiment();

CREATE FUNCTION reject_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'transition_log is append-only'; END $$ LANGUAGE plpgsql;

CREATE TRIGGER transition_log_immutable BEFORE UPDATE OR DELETE ON transition_log
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();

COMMIT;
