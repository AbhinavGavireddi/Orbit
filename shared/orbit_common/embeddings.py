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
