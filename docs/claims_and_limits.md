# Claims & Limits

This project lives or dies by claim discipline. This document states exactly what the
LP-Flow Monitor may and may not be said to do. It is deliberately conservative — stating
the boundaries is the point, not a disclaimer bolted on at the end.

## Authorized positioning

A bounded **LP-flow observability primitive** for selected Uniswap V3 pools.
It is **not** a stress-prediction system, a production alerting system, or a validated
commercial product.

## What the primitive measures

1. It uses **direct Uniswap V3 pool Mint/Burn events only.**
2. It reconstructs daily **gross LP inflow, gross LP outflow, and net LP-flow (USD)** for
   bounded windows.
3. From that series it derives a monitoring status — `QUIET` / `RESOLVED` / `PENDING` /
   `FLAGGED` — describing whether a large, sustained, non-replenished LP-outflow signature
   was *observed* in the window.

## What it does NOT claim

- stress prediction, early-warning performance, or stress probability;
- precision, recall, F1, false-positive rate, or signal-to-noise ratio;
- causal attribution of LP-flow patterns (it reports *what* moved, never *why*);
- event discrimination — it does **not** separate a catalogued stress event from a quiet
  window (on the development set, event windows flag at 21% vs admitted controls at 17%);
- representative incremental value versus TVL, volume, or fees;
- operational superiority versus other data platforms;
- all-Uniswap, cross-protocol, or cross-chain generalization;
- production readiness, scalability, or monetization;
- active-liquidity inference, LP intent, turnover ratios, or invented TVL.

## What it may honestly be used for

- reconstructing auditable LP-flow primitives for selected pools;
- separating LP movements from swap activity at the measurement level;
- supporting a bounded, human-in-the-loop triage workflow;
- defining monitoring hypotheses for future prospective validation;
- demonstrating versioned, explicitly-bounded, claim-disciplined research practice.

## Validation status

**Descriptive only, on a development set. No prospective signal validation has been
achieved.** Event-adjacent windows have FP=0 by construction; control windows were selected
retrospectively, so the development-set figures are **retrospective calibration, not
independent validation.** See [`validation_summary.md`](validation_summary.md). Those figures
were produced by the prior detector version; v3's net-replenishment change means a v3
re-characterization is pending (the qualitative "no clean separation" finding is expected to hold).

## Provenance rule

The detector is frozen and identified by a **pinned-manifest digest** (`manifest.py` + external
`MANIFEST.sha256`): the whole artifact (detector, runner, SQL, schema, dependency lock) is
integrity-checked, fail-closed — though **integrity-checked against a pinned manifest, not
tamper-proof** (a signed release/tag would be needed for that). Any change to the method is a new,
separately-versioned artifact (v2 is retained unmodified as history) — frozen results are never
silently rewritten to look stronger.
