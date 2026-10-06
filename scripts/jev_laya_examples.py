"""Ask Orbit's Jev questions to a local Laya model.

The decision service posts these questions to Jev at api.typesafe.ai.
This file builds the same questions and asks Laya instead.
It does not call Jev, and it does not read a key.

Laya is an open decision model. ``laya.load`` downloads
``convaiinnovations/laya`` from Hugging Face on the first run.
A decision model returns a label and a probability. It does not write a reply.
A high score is advice. The score does not grant an action.

Ollama 0.35 speaks the same ``/v1/systemone`` body for Nimble and Tev1.
It does not host this Laya checkpoint. ``ollama pull hf.co/convaiinnovations/laya``
cannot load it, because Laya is an encoder with decision heads, not a chat model.

Run from the repository root:

    uv run --with laya python scripts/jev_laya_examples.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "shared"), str(ROOT / "services/decision")]

from orbit_common.routing import (  # noqa: E402
    ACTING,
    CONFIDENCE_MIN,
    PROBABILITY_MIN,
    SPEAKING,
    imperative,
)
from orbit_decision.app import question_for  # noqa: E402

JEV_MODEL = "jev-1.13.0"
LAYA_REPO = "convaiinnovations/laya"


def jev_request(utterance, name="intent", options=None):
    """Build the JSON body the decision service posts to Jev."""
    state = {"utterance": utterance, "language": "en", "question": name}
    if options is not None:
        state["options"] = options
    question_name, question = question_for(state)
    return {
        "model": JEV_MODEL,
        "state": {"utterance": utterance, "language": "en"},
        "questions": {question_name: question},
    }


def orbit_would_accept(utterance, name, answer):
    """Apply Orbit's score gates. Acceptance here is still not a grant."""
    if answer.get("type") != "choice":
        return False
    choice = answer["choice"]
    probability = answer["probabilities"][choice]
    if answer["confidence"] < CONFIDENCE_MIN or probability < PROBABILITY_MIN:
        return False
    if name == "intent":
        if choice in SPEAKING:
            return True
        if choice in ACTING:
            return imperative(utterance)
        return False
    return choice in {"low", "high"}


def show(agent, title, utterance, name="intent", options=None):
    request = jev_request(utterance, name, options)
    question_name = next(iter(request["questions"]))
    result = agent.predict(request["state"], request["questions"])
    answer = result["answers"][question_name]
    choice = answer["choice"]
    probability = answer["probabilities"][choice]
    accepted = orbit_would_accept(utterance, name, answer)
    print(title)
    print("  Jev model field: " + request["model"])
    print("  question: " + question_name)
    print("  Laya choice: " + choice)
    print(f"  confidence: {answer['confidence']:.3f}  probability: {probability:.3f}")
    print("  Orbit score gate: " + ("pass" if accepted else "fail"))
    print("  grant: no")
    print()


def main():
    try:
        import laya
    except ImportError:
        print("Install Laya, then run this file again:")
        print("  uv run --with laya python scripts/jev_laya_examples.py")
        return 1

    print("Loading " + LAYA_REPO + " from Hugging Face.")
    print("The first run downloads the weights. Later runs use the local cache.")
    agent = laya.load(LAYA_REPO)
    print()

    show(agent, "1. A chat question. Jev intent. No task.", "What is two plus two?")
    show(agent, "2. A desktop phrase. Jev intent. This face still has no desktop.", "Open Notes")
    show(
        agent,
        "3. A negated phrase. Jev intent. Negation must not act.",
        "Do not open Chrome",
    )

    risk_text = json.dumps(
        {
            "goal": "Email the report to the client",
            "action": {"type": "send_mail", "to": "client@example.com"},
        },
        sort_keys=True,
    )
    show(
        agent,
        "4. A proposed send. Jev risk. The task service asks this before policy.",
        risk_text,
        name="risk",
    )
    show(
        agent,
        "5. A memory sentence. Jev choice. Voice asks store, skip, or delete.",
        "My dentist is on Thursday.",
        name="remember",
        options=["store", "skip", "delete"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
