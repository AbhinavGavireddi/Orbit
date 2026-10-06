"""One-sentence memories. Jev chooses what is stored and which episodes reach a reply."""
import re

_WORD = re.compile(r"[a-z0-9']+")
_PREFERENCE = re.compile(r"\b(?:i|i'm|i am|my|prefer|always|usually)\b", re.IGNORECASE)
_FORGET = re.compile(r"\bforget that\b", re.IGNORECASE)


def terms(text):
    return set(_WORD.findall(str(text).lower()))


def candidate_sentence(text):
    if not isinstance(text, str):
        return None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    sentence = re.split(r"(?<=[.!?])\s", cleaned, maxsplit=1)[0]
    return sentence[:240]


def wants_forget(text):
    return bool(_FORGET.search(text or ""))


def memory_kind(text):
    return "preference" if _PREFERENCE.search(text or "") else "episode"


def standing_preferences(memories, limit=5):
    prefs = [item for item in memories if item.get("kind") == "preference"]
    prefs.sort(key=lambda item: item.get("created_at", 0), reverse=True)
    return prefs[:limit]


def rank_memories(utterance, memories, limit=5):
    """Episodes that share terms with this sentence, closest and newest first."""
    words = terms(utterance)
    scored = []
    for memory in memories:
        if memory.get("kind") == "preference":
            continue
        overlap = len(terms(memory.get("text", "")) & words)
        if overlap:
            scored.append((overlap, memory.get("created_at", 0), memory))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]


def selected_episodes(choice, ranked):
    if choice == "one":
        return ranked[:1]
    if choice == "few":
        return ranked[:3]
    return []


def memory_instructions(choice, standing, episodes):
    lines = [
        f"Memory selection: {choice}. Use a memory only when it bears on this sentence. "
        "Do not recite the store. The current sentence outranks both."
    ]
    lines.extend("Standing preference: " + item["text"] for item in standing)
    lines.extend("Related memory: " + item["text"] for item in episodes)
    return "\n".join(lines)


def rank_memories_semantic(utterance, memories, query_vec, limit=5):
    """Merge keyword overlap with vector similarity; attach match_reason."""
    from orbit_common.embeddings import cosine
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
