# Findings — overnight testing, 2026-08-27

Corner cases found by running campaigns rather than reading code. **Nothing
here has been changed**; each entry is written so we can decide together.

Two of these only appeared with a *real* agent driving the engine — the
scripted stand-ins used in tests do the same trivial thing every time, so
they could never have surfaced them.

Ordered by how much they'd hurt in a real campaign.

---

## 1. Autonomous campaigns cannot ideate (crash) — blocking

**What happens.** A live campaign with `--harness sdk --ideate` runs the
baseline, then dies as `stalled_ideation` after three ideation attempts,
with `RuntimeWarning: coroutine 'with_timeout' was never awaited`. Only the
baseline trial ever runs.

**Why.** `ClaudeSDKHarness.invoke()` ends in `asyncio.run(...)`, which
cannot be called from inside a running event loop. Phases get away with it
because the engine invokes them through `asyncio.to_thread` (engine.py:115)
— a worker thread has no running loop. Ideation is called directly on the
loop thread (loop.py:97), so `asyncio.run` raises, the broad `except
Exception` in `top_up_backlog` counts it as an ideation failure, and three
of those trip the stall guard.

Confirmed in isolation: `asyncio.run()` inside a running loop →
`RuntimeError: asyncio.run() cannot be called from a running event loop`.

**Two candidate fixes** — I did not pick one:

- *In the loop:* `await asyncio.to_thread(self.ideator, ...)`, matching how
  phases are invoked. Makes the loop consistent; requires `top_up_backlog`
  to become async.
- *In the harness:* detect a running loop and use a thread internally.
  Fixes every future caller at once, but hides an async/sync mismatch
  rather than making it explicit.

**Also worth deciding:** `top_up_backlog` catching bare `Exception` turned a
programming error into "the ideator is flaky". A crash from *our* code and a
badly-behaved ideator probably deserve different treatment.

---

## 2. The baseline trial is not a baseline — measurement integrity

**What happens.** T000 ran the *full* workflow, including the agentic
`implement` phase. Given no idea, the agent improved the model anyway — it
implemented input standardisation — and the "baseline" scored **0.620**
instead of the true baseline **1.373**.

Its own note: *"Implemented input standardisation in model.py. `prepare(train_rows)`
now computes per-feature mean and std…"*

**Why it matters.** §4 and §8 say T000 is "the workflow on unmodified
baseline code" and that every later trial is reported as a delta against it.
If the agent silently improves the baseline, every subsequent delta is
measured against a moving reference, and the campaign's headline claim
("we improved on baseline by X") is wrong — in the direction of
*understating* progress, which is at least the safe direction.

It also wastes the strongest signal: with a contaminated baseline, nobody
can see that standardisation was the discovery.

**Options:** have the baseline skip agentic phases entirely; or add a phase
flag (`skip_for_baseline: true`); or run only deterministic phases for T000.
The first is simplest but assumes an agentic phase never does setup work the
baseline needs.

**Note:** the agent behaved *well* here — it read the data, found the real
problem, and fitted statistics on the training split only (no leakage). The
skills work. It just did excellent work in the one place we didn't want any.

---

## 3. Branching silently degrades when a VCS is configured

**What happens.** Trial branching works via directory copy: a child trial's
workspace inherits its parent's code (verified — a lineage file accumulated
`first` then `second`). With `--vcs hg` or `--vcs git`, it does not.

**Why.** `admit()` passes `base_dir=` but never `base_node=`
(loop.py:173), and `commit_workspace()` / `release_workspace()` **are never
called anywhere**. So no trial's code state is ever committed,
`trial.base_node` is always `None`, and `workspace_acquire(None, …)`
resolves to `tip`. Every trial branches from the repo tip regardless of its
parent.

**Consequences:**

- The "tree of ideas with concrete code lineage" (§6.4) is real only in the
  copy path, and silently degrades in the *recommended* configuration.
- Workspaces are never released, so a pooled hg adapter never reuses its
  pool and git worktrees accumulate for the life of the campaign.

**The design question underneath:** when should a trial's code be committed?
After `implement`? At trial end? Only on success? That's a decision, not a
bug fix, which is why I left it.

---

## 4. A cheap gate cannot catch a slow-burning bad idea — expected, worth knowing

On the new task, the "knobs only" idea (32 hidden, lr 0.05, 300 epochs)
*passes* the 5-epoch smoke gate at 1.81, then finishes at **8.81** — far
worse than baseline. The gate is doing its job (it catches divergence), but
an idea that looks fine early and degrades with training gets through.

Not a bug — a real property of escalation. Worth stating in the docs so
nobody expects the gate to be a filter for *quality* rather than for
*catastrophe*.

---

## 5. No size limit on what a phase can put in the ledger — minor

A phase can write a 5 MB `notes` string and the engine records it verbatim,
bloating the log (verified: a single event produced a 5 MB ledger). Agent
notes are truncated to 500 characters in `agentic.py`, but a *script* that
dumps a log into `notes` is unbounded.

The ledger is meant to stay small and replayable; artifacts have their own
directory for exactly this. A cap (with truncation recorded honestly) would
be cheap.

---

## What held up well

Worth recording, so we don't re-litigate what's already solid:

- **Concurrency and ledger consistency.** 25 trials, 8 concurrent: no
  duplicate trial ids, sequence numbers contiguous, replay-from-scratch
  identical to live state, budget respected exactly, no idea consumed twice.
- **Replay at scale.** 10,001 events / 2,000 trials replays in **0.01 s**
  (1.5 MB log). The "no snapshots needed" decision (§12) is comfortably
  justified.
- **Hostile content.** Newlines, quotes, backslashes, emoji, RTL overrides
  and tabs in a `notes` field round-trip exactly, and the line count still
  equals the event count — the JSONL format holds.
- **Resume with an exhausted budget.** An in-flight trial finishes after a
  crash even when nothing new can be admitted, reattaching to its job rather
  than relaunching (now a test).
- **The skills produce good work.** The agent found the task's actual
  insight unprompted, implemented it correctly without leaking the
  validation split, and wrote an analysis that compared train and val
  properly.
