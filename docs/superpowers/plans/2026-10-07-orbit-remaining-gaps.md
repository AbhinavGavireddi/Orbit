# Orbit Remaining Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship semantic memory, sleep-wake notifications, and Mac notch-pill presence/desktop under Task Confirm — in three incremental PRs.

**Architecture:** Keep Redis + Task-as-only-grant. Slice 1 adds embeddings beside existing memory records. Slice 2 emits a wake when follow-ups are due with no connected session. Slice 3 upgrades the existing native `FacePanel` (already notch-adjacent) with expression states and re-validates Accessibility desktop grants.

**Tech Stack:** Python 3 + uv, Redis, FastAPI/websockets, optional OpenAI embeddings (BYOK), Swift/AppKit native client, Playwright Chromium unchanged.

**Spec:** `docs/superpowers/specs/2026-10-07-orbit-remaining-gaps-design.md`

## Global Constraints

- Task is the only world caller; Confirm for writes; spoken yes is conversation.
- uv-only Python; Docker Linux host; no host shell; no auto-approve; no second voice runtime.
- Redis stays on the talk path; round board and multi-tenant are out of this plan.
- One PR per task below; merge + Mac sync before starting the next.
- Live BYOK e2e / HTML report must stay green (or keyword-fallback documented) before merge.
- Never print `.env` or secrets.

## File map

| Slice | Create | Modify | Test |
|---|---|---|---|
| 1 Semantic memory | `shared/orbit_common/embeddings.py` | `shared/orbit_common/memory.py`, `services/task/orbit_task/store.py`, `services/voice/orbit_voice/app.py` (recall path), `.env.example`, `docs/PLUGINS.md` or `docs/DEVELOPMENT.md` note | `tests/test_memory_semantic.py`, extend memory e2e if present |
| 2 Sleep-wake | `shared/orbit_common/wake.py`, `services/task/orbit_task/wake_delivery.py` | `services/task/orbit_task/followups.py`, `services/task/orbit_task/app.py`, face JS if any service worker, native notification bridge | `tests/test_sleep_wake.py` |
| 3 Mac pill + desktop | expression helpers under `native/Sources/OrbitApp/` as needed | `native/Sources/OrbitApp/Entry.swift`, `OrbitModel.swift`, DeviceBridge accessibility path docs in README | native unit if present + guided checklist in plan |

---

### Task 1: Semantic memory

**Files:**
- Create: `shared/orbit_common/embeddings.py`
- Modify: `shared/orbit_common/memory.py`, `services/task/orbit_task/store.py` (memory save/list ~447–476), recall callers in `services/voice/orbit_voice/app.py`
- Modify: `.env.example` (`ORBIT_EMBEDDING_MODEL`, optional key already present)
- Test: `tests/test_memory_semantic.py`

**Interfaces:**
- Consumes: Redis keys `orbit:memory:order`, `orbit:memory:{id}` (JSON records with `id`, `text`, `kind`, `created_at`)
- Produces:
  - `async def embed_texts(texts: list[str]) -> list[list[float] | None]`
  - `def rank_memories_semantic(utterance, memories, query_vec, limit=5) -> list[dict]` each hit may include `match_reason: str`
  - Store persists optional `embedding: list[float]` on the record; backfill best-effort on list/save

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_semantic.py
from shared.orbit_common.memory import rank_memories, rank_memories_semantic

def test_semantic_merge_prefers_vector_when_terms_miss():
    memories = [
        {"id": "1", "text": "User likes oat milk in coffee", "kind": "episode",
         "created_at": 1, "embedding": [1.0, 0.0]},
        {"id": "2", "text": "Bought new shoes yesterday", "kind": "episode",
         "created_at": 2, "embedding": [0.0, 1.0]},
    ]
    # utterance shares no lexical terms with memory 1 but vector aligns
    hits = rank_memories_semantic("dairy alternative for espresso", memories, [0.99, 0.01], limit=2)
    assert hits[0]["id"] == "1"
    assert "match_reason" in hits[0]
    assert "oat" in hits[0]["match_reason"].lower() or "similar" in hits[0]["match_reason"].lower()

def test_keyword_still_works_without_embeddings():
    memories = [{"id": "1", "text": "flight to delhi monday", "kind": "episode", "created_at": 1}]
    assert rank_memories("delhi flight", memories)[0]["id"] == "1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_memory_semantic.py -v` (from repo root)
Expected: FAIL (`rank_memories_semantic` not defined)

- [ ] **Step 3: Implement embeddings helper + ranking**

```python
# shared/orbit_common/embeddings.py
"""Optional BYOK embeddings. Fail soft to None when unset or provider errors."""
import os
from typing import Optional

async def embed_texts(texts: list[str]) -> list[Optional[list[float]]]:
    if not texts:
        return []
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ORBIT_OPENAI_API_KEY")
    model = os.environ.get("ORBIT_EMBEDDING_MODEL", "text-embedding-3-small")
    if not api_key:
        return [None] * len(texts)
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        resp = await client.embeddings.create(model=model, input=texts)
        by_index = {item.index: item.embedding for item in resp.data}
        return [by_index.get(i) for i in range(len(texts))]
    except Exception:
        return [None] * len(texts)

def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
```

Extend `memory.py`:

```python
def rank_memories_semantic(utterance, memories, query_vec, limit=5):
    """Merge keyword overlap with vector similarity; attach match_reason."""
    from shared.orbit_common.embeddings import cosine
    lexical = {m["id"]: m for m in rank_memories(utterance, memories, limit=limit * 2)}
    scored = []
    for memory in memories:
        if memory.get("kind") == "preference":
            continue
        lex = len(terms(memory.get("text", "")) & terms(utterance))
        sim = cosine(query_vec, memory.get("embedding")) if query_vec else 0.0
        if lex == 0 and sim < 0.75:
            continue
        reason_parts = []
        if lex:
            reason_parts.append(f"shared {lex} term(s)")
        if sim >= 0.75:
            reason_parts.append(f"similar meaning ({sim:.2f})")
        item = dict(memory)
        item["match_reason"] = "; ".join(reason_parts) or "related"
        scored.append((lex + sim * 2, memory.get("created_at", 0), item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in scored[:limit]]
```

- [ ] **Step 4: Wire store save/list to persist embeddings**

In `store.py` memory save path after building `record`:
- call `embed_texts([record["text"]])` and set `record["embedding"]` when non-None
- on list/recall path used by voice, if records lack embeddings, optionally backfill one batch (cap e.g. 20) without blocking talk forever — prefer save-time embed + lazy backfill on explicit recall tool

In voice `recall` / memory instruction builder: embed the utterance; call `rank_memories_semantic`; include `match_reason` only in tool/debug payload, not as trusted instructions (keep `memory_instructions` wording: memories are data).

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_memory_semantic.py tests/test_voice_first.py -q --tb=short`
Expected: PASS (adjust suite names if memory tests live elsewhere)

- [ ] **Step 6: Document env**

Add to `.env.example`:
```
# Optional; recall falls back to keyword-only when unset
ORBIT_EMBEDDING_MODEL=text-embedding-3-small
```

- [ ] **Step 7: Commit + PR**

```bash
git checkout -b codex/orbit-semantic-memory
git add shared/orbit_common/embeddings.py shared/orbit_common/memory.py services/task/orbit_task/store.py services/voice/orbit_voice/app.py tests/test_memory_semantic.py .env.example
git commit -m "feat: semantic memory recall with Redis-side embeddings"
# open PR, wait CI, merge, sync Mac main
```

---

### Task 2: Sleep-wake channel

**Files:**
- Create: `shared/orbit_common/wake.py`, `services/task/orbit_task/wake_delivery.py`
- Modify: `services/task/orbit_task/followups.py`, `services/task/orbit_task/app.py` (due publish loop ~191)
- Modify: native `OrbitModel` / `Entry` for `UNUserNotification` (or reuse existing alert path); optional face service worker later
- Test: `tests/test_sleep_wake.py`

**Interfaces:**
- Consumes: `followups.due`, session connected set from task WS
- Produces: `WakeEvent(device_id, delivery_id, channel, created_at)` recorded in Redis; push/OS notification; **never** calls effects/Confirm grant

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sleep_wake.py
import pytest
from shared.orbit_common.wake import should_wake, WakeDecision

def test_wake_when_due_and_no_session():
    d = should_wake(due_count=1, connected_sessions=0, quiet_hours=False)
    assert d == WakeDecision.NOTIFY

def test_no_wake_when_session_connected():
    assert should_wake(1, connected_sessions=1, quiet_hours=False) == WakeDecision.SKIP_CONNECTED

def test_no_wake_in_quiet_hours():
    assert should_wake(1, 0, quiet_hours=True) == WakeDecision.SKIP_QUIET

def test_wake_does_not_imply_grant():
    # documentation-level: wake module must not import effects/page/mcp_door
    import shared.orbit_common.wake as wake
    src = open(wake.__file__).read()
    assert "effects" not in src and "mcp_door" not in src and "page" not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sleep_wake.py -v`
Expected: FAIL (module missing)

- [ ] **Step 3: Implement wake decision + Redis record**

```python
# shared/orbit_common/wake.py
from enum import Enum

class WakeDecision(str, Enum):
    NOTIFY = "notify"
    SKIP_CONNECTED = "skip_connected"
    SKIP_QUIET = "skip_quiet"
    SKIP_EMPTY = "skip_empty"

def should_wake(due_count: int, connected_sessions: int, quiet_hours: bool) -> WakeDecision:
    if due_count <= 0:
        return WakeDecision.SKIP_EMPTY
    if quiet_hours:
        return WakeDecision.SKIP_QUIET
    if connected_sessions > 0:
        return WakeDecision.SKIP_CONNECTED
    return WakeDecision.NOTIFY
```

`wake_delivery.py`: persist `orbit:wake:{device_id}:{delivery_id}` with TTL; publish `followup.wake` on the device stream **and** enqueue native push payload. Opening notification must only bring UI; Schedule/Confirm unchanged.

- [ ] **Step 4: Hook follow-up due loop**

In `app.py` where `followups.due` / `publish` runs: count active voice/task sessions for that `device_id`. If `should_wake` → `NOTIFY`, call wake delivery once per `delivery_id` (idempotent via Redis SET NX).

- [ ] **Step 5: Native notification**

In Mac app: request notification permission; on `followup.wake` or APNs/local schedule when backend marks wake, show notification whose click focuses `FacePanel` and surfaces the due follow-up card. Do not auto-Schedule.

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_sleep_wake.py tests/test_followups.py -q --tb=short`
Expected: PASS

- [ ] **Step 7: Commit + PR**

```bash
git checkout -b codex/orbit-sleep-wake
git add shared/orbit_common/wake.py services/task/orbit_task/wake_delivery.py services/task/orbit_task/followups.py services/task/orbit_task/app.py native/Sources/OrbitApp tests/test_sleep_wake.py
git commit -m "feat: sleep-wake notify for due follow-ups without grants"
```

---

### Task 3: Mac notch pill expressions + desktop Accessibility

**Files:**
- Modify: `native/Sources/OrbitApp/Entry.swift` (`FacePanel` already centers under notch ~line 131)
- Modify: `native/Sources/OrbitApp/OrbitModel.swift` (status / confirmations / follow-ups)
- Modify: `DeviceBridge.swift` only if Accessibility hop/scope guards need tightening
- Docs: README production status — note pill is Mac presence; desktop requires this client
- Test: extend `native/Tests` if present; otherwise checklist below + existing Swift checks via `native/check.sh`

**Interfaces:**
- Consumes: existing WS session, Confirm/Schedule/Dismiss, DeviceBridge Accessibility
- Produces: expression states on pill: `idle`, `listening`, `speaking`, `thinking`, `needsConfirm`, `needsSchedule`

- [ ] **Step 1: Define expression enum + failing UI test or model test**

```swift
// native/Sources/OrbitApp/FaceExpression.swift
enum FaceExpression: String {
    case idle, listening, speaking, thinking, needsConfirm, needsSchedule
}
```

Add a small unit test (or OrbitCore test) that maps model flags → expression with precedence: needsConfirm > needsSchedule > speaking > listening > thinking > idle.

- [ ] **Step 2: Run native check to see current baseline**

Run: `cd native && ./check.sh`
Expected: note current pass/fail; do not claim green without output.

- [ ] **Step 3: Wire expression into FacePanel chrome**

In `Entry.swift` face chrome (the 52pt bar): replace/augment static visuals with expression-driven face (color/eye/mouth or SF Symbol set). Keep Confirm/Schedule/Dismiss cards as today.

In `OrbitModel`: compute `var expression: FaceExpression` from mic/playback/confirmation/follow-up state.

- [ ] **Step 4: Desktop grant path sanity**

Verify DeviceBridge still requires Accessibility; ensure task payloads for desktop actions still need Confirm (no auto-approve). Add regression test on backend if desktop action type exists in contracts; otherwise document guided trial.

- [ ] **Step 5: Guided live checklist (do not skip)**

1. Launch Mac Orbit with local Docker stack.
2. Pill visible under notch; cycles listening/speaking on Talk.
3. Trigger a Confirm desktop/Accessibility action → pill shows `needsConfirm` → approve → evidence returns.
4. Stop/cancel → no further grants.
5. Browser `/face` alone still refuses desktop.

- [ ] **Step 6: Commit + PR**

```bash
git checkout -b codex/orbit-mac-pill-desktop
git add native/Sources/OrbitApp README.md
git commit -m "feat: Mac notch pill expressions and desktop presence path"
```

---

## Self-review

1. **Spec coverage:** Slice 1/2/3 of the design each map to Tasks 1–3; board and tenancy correctly absent.
2. **Placeholders:** none intentional; thresholds (`0.75` cosine) are explicit — tune only with a test change.
3. **Types:** `WakeDecision`, `rank_memories_semantic`, `FaceExpression` names consistent across steps.

## After all three merge

Customer acceptance (held-out goals) remains a human gate — schedule with Orbit QA; not part of these PRs.
