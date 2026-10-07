from orbit_common.memory import rank_memories, rank_memories_semantic


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
