# Open Questions

Resolved decisions are in [README.md](README.md). Everything below blocks or shapes a section of
the spec. Ordered by how much rework the answer prevents.

## Blocking — these change the architecture

**Q2. What is an experiment, mechanically?**
(a) An agent writes and runs code; (b) the engine sweeps parameters over a fixed user-supplied
workflow; (c) both. This is the largest scope fork in the project. (a) needs sandboxing, diff
review, and protected-path enforcement; (b) is much closer to conventional HPO and needs almost
none of it. The spec currently assumes (c) with (b) as the safe subset.
→ *Affects: 06, 07, safety model, v1 scope.*

**Q4. Proposer strategy — pure LLM, or LLM + classical optimizer?**
Recommendation in `05-outer-loop.md`: design the hypothesis schema for hybrid now (proposer emits
a parameterized family plus a search space; a sampler picks points). Pure LLM becomes the case
with zero free parameters. Retrofitting means rewriting the hypothesis schema.
→ *Affects: 01 (hypothesis schema), 05.*

**Q8. Typical experiment duration — minutes, hours, or days?**
Sub-hour makes controller-crash cost negligible and the re-attach machinery close to optional.
Multi-day makes checkpointing, early-kill, and spot-preemption handling the dominant concerns.
→ *Affects: 04, 06, cost model, poll intervals.*

**Q9. Target concurrency per campaign — 1, ~10, or hundreds?**
Under ~10, a single controller process per campaign is fine. Hundreds means the controller must
shard stage polling and the proposer must handle a wide unresolved frontier.
→ *Affects: 04 (control loop), 05 (in-flight context), deployment topology.*

## Shaping — these change scope or defaults

**Q1. First 2–3 real campaigns.** "v4 latency" and "pretraining quality" have very different
inner loops. Is v1 single-domain (ML training/inference) or general?

**Q3. How are inner-loop stages authored?** User Python handlers, container images, or references
to an existing job platform? Determines the executor set in `06-inner-loop.md`.

**Q5. Proposer context policy.** Confirm the assembled-brief approach in `05-outer-loop.md`
(summary + top-k + recent-k + negatives + in-flight) and set k. Also: is the research summary an
LLM-maintained artifact or a structured/deterministic rollup?

**Q6. Cross-campaign visibility.** You specified within-campaign visibility. Within a *project*,
may a campaign see prior campaigns' results? Is campaign isolation a scientific control or just
organizational grouping? Affects the result cache too — cross-campaign cache hits are a large
efficiency win but only sound if provenance matches exactly.

**Q7. Human-authored hypotheses mid-campaign.** Assumed yes in the spec (`origin: human`,
`HumanNoteAdded`). Confirm — and confirm whether human notes should be able to *steer* the
proposer, not just add ideas.

**Q10. Scheduling.** Does this engine own scheduling and resource allocation, or sit on top of an
existing scheduler (K8s, Slurm, internal)? Determines whether quota/preemption logic is in scope.

**Q11. Ledger storage details.** Postgres is decided (D2). Which instance, expected retention, and
where do artifacts live (S3/GCS/internal blob store)?

**Q12. Scale.** Experiments per campaign, campaigns per project, total retention horizon. Drives
partitioning of `ledger_event` and whether projections can stay synchronous.

**Q13. Campaign config edits.** Spec asserts immutable-with-fork, with budget increases and
concurrency changes as the only in-place exceptions. Confirm this is acceptable operationally —
it is the assumption that makes results comparable.

**Q14. Campaign deliverable.** A winning config, a diff/PR, a report, or a promoted artifact? If a
PR, the engine needs write access to a repo and that changes the safety model considerably.

**Q15. Users and tenancy.** Single user, a team, or a product? Determines auth, quotas,
multi-tenancy, and whether the registry needs access control.

**Q16. Interface for v1.** CLI, SDK, service + API, or UI? A durable engine with no observability
surface is unoperatable — at minimum a campaign inspector is needed early.

**Q17. Stack and existing frameworks.** Language preference; anything you want to use or avoid
(Temporal, Prefect, Ray, Optuna, MLflow, W&B). Notably, Optuna/Ax could supply the sampler for the
hybrid proposer in Q4, and W&B/MLflow could supply artifact tracking rather than building it.

**Q18. Multi-objective.** Deferred in `07-objectives-and-validity.md` in favour of
constrained-scalar. Confirm no project needs a true Pareto frontier in v1.
