"""Small, fixed capabilities shared across routing and automation transports."""
from dataclasses import dataclass
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CapabilityName = Literal[
    "open_notes", "open_chrome", "open_spotify", "start_dictation",
    "computer", "research", "skill", "answer", "clarify",
]
RouteSource = Literal["jev", "realtime"]
SPEAKING = frozenset({"answer", "clarify"})
ACTING = frozenset({"computer", "research", "skill", "open_notes", "open_chrome", "open_spotify", "start_dictation"})
CONFIDENCE_MIN = 0.8
PROBABILITY_MIN = 0.95


@dataclass(frozen=True)
class Capability:
    kind: str
    goal: str
    bundle_id: str
    url: str | None = None


CAPABILITIES = {
    "open_notes": Capability("automation", "Open Notes", "com.apple.Notes"),
    "open_chrome": Capability("automation", "Open Chrome", "com.google.Chrome"),
    "open_spotify": Capability("automation", "Open Spotify website", "com.google.Chrome", "https://open.spotify.com/"),
    "start_dictation": Capability("dictation", "Start Notes dictation", "com.apple.Notes"),
}


class RouteDecision(BaseModel):
    model_config = ConfigDict(strict=True)
    available: bool = False
    capability: CapabilityName | None = None
    confidence: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    probability: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    direct: bool = False
    elapsed_ms: float = Field(default=0, ge=0, allow_inf_nan=False)
    model: str = ""
    error: str | None = None
    mode: str = "shadow"

    @property
    def eligible(self):
        return (self.available and self.capability is not None and self.direct
                and self.confidence >= CONFIDENCE_MIN and self.probability >= PROBABILITY_MIN)


_PREFIX = r"(?:(?:hey )?orbit[, ]+)?(?:(?:can|could|would|will) you )?(?:(?:please|kindly) )?"
_SUFFIX = r"(?: (?:for me|please|now))?[.!?]*"
_COMMANDS = {
    "open_notes": r"show (?:me )?(?:apple )?notes(?: app)?",
    "open_chrome": r"(?:open|launch|bring up) (?:google )?chrome(?: browser)?",
    "open_spotify": r"(?:open|launch|bring up) (?:the )?spotify(?: website| web player| web)?(?: in chrome)?",
    "start_dictation": (r"(?:start (?:a new )?(?:notes dictation|dictation(?: in (?:apple )?notes)?)"
                         r"|open (?:apple )?notes and (?:write (?:the )?text i dictate|start dictation)"
                         r"|(?:open|launch|bring up) (?:apple )?notes(?: app)?)"),
}
_NEGATION = re.compile(r"\b(?:don'?t|do not|never|unless)\b")
_CONDITIONAL = re.compile(r"^(?:(?:hey )?orbit[, ]+)?(?:if|when)\b")
_PATTERNS = {name: re.compile(_PREFIX + command + _SUFFIX) for name, command in _COMMANDS.items()}


def explicit_capability(text: str) -> str | None:
    """Conservative exact-command gate; unsupported phrasing stays with Realtime.

    This is an execution boundary, not a general-purpose intent classifier. It
    deliberately refuses quoted, conditional, negated and compound requests.
    """
    normalized = " ".join(text.lower().split())
    if len(normalized) > 240:
        return None
    for capability, pattern in _PATTERNS.items():
        if pattern.fullmatch(normalized):
            return capability
    return None


def imperative(text: str) -> bool:
    """A current request to act. Questions, quotations, negations and conditions do not qualify."""
    if not isinstance(text, str):
        return False
    normalized = " ".join(text.lower().split())
    if not normalized or len(normalized) > 240:
        return False
    if any(mark in text for mark in ("?", '"', "“", "”")):
        return False
    if _NEGATION.search(normalized) or _CONDITIONAL.search(normalized):
        return False
    return True
