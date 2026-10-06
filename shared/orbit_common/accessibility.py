"""Rank Accessibility controls locally. Jev picks one; this module does not."""
import re

_WORD = re.compile(r"[a-z0-9]+")


def rank_controls(goal, controls, limit=8):
    words = set(_WORD.findall(str(goal).lower()))
    scored = []
    for index, control in enumerate(controls or []):
        if not isinstance(control, dict):
            continue
        blob = " ".join(str(control.get(key, "")) for key in ("role", "title", "description"))
        overlap = len(words & set(_WORD.findall(blob.lower())))
        if overlap and (control.get("title") or control.get("description")):
            scored.append((overlap, -index, control))
    scored.sort(reverse=True)
    ranked = []
    for index, (_, _, control) in enumerate(scored[:limit]):
        item = {
            "id": str(control.get("id") or index),
            "role": str(control.get("role") or ""),
            "title": str(control.get("title") or ""),
            "description": str(control.get("description") or ""),
        }
        if isinstance(control.get("ax_ref"), dict):
            item["ax_ref"] = control["ax_ref"]
        ranked.append(item)
    return ranked
