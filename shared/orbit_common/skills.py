"""Index installed skills. The shortlist is a window, not a second router."""
from dataclasses import dataclass, field
from pathlib import Path
import json
import re

from orbit_common.memory import terms


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    path: Path
    mtime: float
    steps: tuple = field(default_factory=tuple)


def home_skill_roots():
    home = Path.home()
    return [
        home / ".codex/skills",
        home / ".claude/skills",
        home / ".agents/skills",
        home / ".claude/plugins",
        home / "Library/Application Support/Orbit/learned-skills",
    ]


def roots_from_setting(value):
    raw = (value or "").strip()
    if not raw:
        return []
    roots = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "home":
            roots.extend(home_skill_roots())
        else:
            roots.append(Path(part))
    return roots


def _frontmatter(text):
    name, description = "", ""
    if not text.startswith("---"):
        return name, description, text
    end = text.find("\n---", 3)
    if end == -1:
        return name, description, text
    header, body = text[3:end], text[end + 4:]
    for line in header.splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip("\"'")
    return name, description, body


def _learned(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    steps = data.get("steps")
    if not isinstance(steps, list) or not data.get("description"):
        return None
    safe = []
    for step in steps:
        if not isinstance(step, dict) or step.get("type") not in {"open_app", "open_url", "ax_perform", "dictation_start"}:
            return None
        safe.append(step)
    return SkillRecord(data.get("name") or path.stem, str(data["description"])[:400], path,
                       path.stat().st_mtime, tuple(safe))


def index_skills(roots):
    found = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            record = None
            if path.name == "SKILL.md":
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
                name, description, _ = _frontmatter(text)
                record = SkillRecord(name or path.parent.name, description[:400], path, path.stat().st_mtime)
            elif path.suffix == ".json" and "learned-skills" in path.parts:
                record = _learned(path)
            if record is None:
                continue
            current = found.get(record.name)
            if current is None or record.mtime >= current.mtime:
                found[record.name] = record
    return list(found.values())


def shortlist_skills(utterance, skills, limit=8):
    words = terms(utterance)
    scored = []
    for skill in skills:
        overlap = len(words & terms(skill.name + " " + skill.description))
        if overlap:
            scored.append((overlap, skill.mtime, skill))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]


def execution_mode(record, text):
    """Writing may use the report model. A missing tool is said aloud. Everything else stays on the Mac."""
    lowered = text.lower()
    if "allowed-tools:" in lowered and "bash" in lowered and "computer" not in lowered and "accessibility" not in lowered:
        return "missing"
    if any(word in record.description.lower() for word in ("write", "draft", "essay", "prose")):
        return "writing"
    return "mac"


def shortest_section(text, goal, limit=1200):
    """Frontmatter plus the shortest section whose heading shares terms with the goal."""
    _, _, body = _frontmatter(text)
    header = ""
    marker = text.find("\n---", 3)
    if text.startswith("---") and marker != -1:
        header = text[:marker + 4]
    parts = re.split(r"\n(?=## )", body)
    words = terms(goal)
    matching = [part for part in parts if terms(part.split("\n", 1)[0]) & words]
    pool = matching or parts
    section = min(pool, key=len) if pool else ""
    return (header.strip() + "\n" + section.strip())[:limit]
