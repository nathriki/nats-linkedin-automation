# TODOS

## Pipeline

### Cross-post to other platforms (Twitter/X, Facebook)

**What:** Add output channels beyond LinkedIn once the pipeline is proven — cross-post the same drafted/approved content to other platforms.

**Why:** The NATS architecture already supports adding output channels without a rewrite (that's the whole reason Approach B was chosen over a single-script pipeline).

**Context:** Not needed for v1. Each platform has its own ToS/rate-limit/auth quirks — real work, not free. Revisit after LinkedIn cadence is stable (see `docs/designs/ph-corporate-worker-linkedin-pipeline.md`).

**Effort:** M
**Priority:** P3
**Depends on:** LinkedIn pipeline running reliably first.

### Upgrade secrets storage to a real secrets manager

**What:** Move from gitignored `.env` env vars (LinkedIn OAuth token, Telegram bot token, LLM API key) to a real secrets manager (Vault, 1Password CLI, or similar).

**Why:** Env vars are fine for one operator, but if this pipeline ever gets a second maintainer or moves to a shared server, plaintext `.env` files become a real liability — no rotation, no audit trail, real risk of an accidental commit.

**Context:** No current justification for the infra overhead on a solo pipeline (see `docs/designs/ph-corporate-worker-linkedin-pipeline.md`, Constraints). Revisit if a second person gets involved or this moves off your own machine — that's the upgrade trigger, not a calendar date.

**Effort:** S
**Priority:** P4
**Depends on:** None — purely a future trigger condition.
