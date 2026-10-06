# Orbit remaining gaps — design

Accepted in chat 2026-10-07. Shipping approach: **incremental PRs** (one slice per PR).

## Locked sequence

1. Semantic memory
2. Sleep-wake channel
3. Mac notch pill + desktop / Accessibility

## Explicitly out of this sequence

- Round board (dropped)
- Multi-tenant auth + quotas (skipped; stay single-deployment)
- Pi / firmware / enclosure (docs-only per ARCHITECTURE.md)
- Customer acceptance trials (human gate after code; not a build slice)

## Global constraints (every PR)

- Task is the only caller of the world; Confirm for writes; spoken yes is conversation.
- uv is the only Python toolchain; Docker Linux host; no host shell; no auto-approve.
- No second voice runtime (no Pipecat). Redis stays on the talk path.
- Live BYOK e2e + HTML report must stay green before merge.
- PR + sync to Mac folder `/Users/abhinavgavireddi/Documents/ChatGPT/personal assistant/orbit`.

---

## Slice 1 — Semantic memory

### Goal

Recall by meaning as well as keyword, without replacing Redis or the existing save/recall/forget tools.

### Design

- Keep the current Redis memory list (cap 200) as the source of truth for records.
- On save (and backfill once), compute an embedding and store it beside the record in Redis (vector field or sibling keyspace). No new database.
- `recall_memory` runs keyword + vector search, merges, dedupes, and returns hits with a short human-readable “why matched” string.
- Retrieved text is data only — never trusted instructions.
- Embedding provider is BYOK/configurable; fail soft to keyword-only if embeddings are unavailable.

### Out of scope

- Per-user ownership / retention UI (tenancy skipped).
- Replacing Redis on the talk path.
- Automatic silent memory writes without the existing tool contract.

### Acceptance

- Unit/integration tests for merge ranking and “why matched”.
- Offline test with a fake embedder; optional live BYOK check when keys present.
- Existing memory tool e2e still passes.

---

## Slice 2 — Sleep-wake channel

### Goal

When a follow-up is due and no session is connected, notify the user so a card can be shown — without running world effects while asleep.

### Design

- Detect “no connected presence client” when a follow-up fires.
- Deliver a wake signal that opens a card: prefer **web push** to the face (or Mac pill when that client is installed), with **Mac OS notification** as the Mac-path delivery once the native client is the presence surface.
- Opening the notification brings Confirm / Schedule / Dismiss into view. No desktop or world effect runs from the notification alone.
- Quiet hours and coalesced missed occurrences remain as today.

### Out of scope

- Telegram / second presence bot.
- Autonomous action while asleep.
- Round board hardware.

### Acceptance

- Automated test: due follow-up with no session → wake event recorded; no effect grant issued.
- Manual: notification opens a card; Schedule/Confirm still required for activation/grant.

---

## Slice 3 — Mac notch pill + desktop / Accessibility

### Goal

Make the Mac the presence + expression surface (notch-adjacent pill) and restore desktop effects under Task Confirm, preferring Accessibility over screenshots.

### Design

- Extend the existing `native/` Mac client: a **notch-adjacent pill** shows Orbit face/expressions and session state (listening, thinking, waiting for Confirm, etc.).
- Pill hosts the same controls as the browser face for this cut: Talk (or mic), Confirm, Schedule, Dismiss — same session semantics (`device_id` on `WS /v1/voice`), not a second voice runtime.
- Browser `/face` remains available; the pill is the Mac presence client.
- Desktop execution: adapters already in tree; Prefer Accessibility trees and app APIs; screenshots limited by hop count, scope, and approval.
- Every desktop write goes through Task with Confirm (exact payload). Crash recovery re-observes; does not replay an uncertain click.
- Sleep-wake (slice 2) may target this pill/OS notification once present.

### Out of scope

- Notarized / signed distribution readiness.
- Universal multi-app autonomy.
- Round board firmware.

### Acceptance

- Pill shows at least: idle, listening, speaking, awaiting Confirm/Schedule.
- One guided live Mac trial: Accessibility (or capped screenshot) action requires Confirm and produces evidence; Stop/cancel prevents further grants.
- Face/browser path still refuses desktop when no Mac client is attached.

---

## Delivery notes

- One PR per slice in order; merge and Mac-sync before starting the next.
- After all three merge: run / schedule customer acceptance separately (held-out goals) — not part of these PRs.
