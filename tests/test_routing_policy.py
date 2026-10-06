import pytest


@pytest.mark.parametrize(("text", "expected"), [
    ("Orbit, please open Notes.", "start_dictation"),
    ("Show me Apple Notes", "open_notes"),
    ("Could you kindly launch Google Chrome for me?", "open_chrome"),
    ("Open the Spotify website", "open_spotify"),
    ("Open Notes and write the text I dictate", "start_dictation"),
    ("Start dictation in Notes", "start_dictation"),
    ("Don't open Notes", None),
    ('Say "open Notes"', None),
    ("Open Notes and research batteries", None),
    ("Can Chrome open Spotify?", None),
    ("When I arrive, open Notes", None),
    ("Please open it", None),
    ("Play Get Lucky on Spotify", None),
    ("Open the browser and play Spotify", None),
    ("Open Calendar", None),
    ("Chrome kholo", None),
])
def test_only_explicit_bounded_english_commands_are_fast_candidates(text, expected):
    from orbit_common.routing import explicit_capability
    assert explicit_capability(text) == expected


@pytest.mark.parametrize(("text", "allowed"), [
    ("Open Calendar", True),
    ("Open the browser and play Spotify", True),
    ("Please turn the volume down", True),
    ("Don't open Calendar", False),
    ('He said "Open Calendar"', False),
    ("When I arrive, open Calendar", False),
    ("What is on my calendar?", False),
    ("", False),
])
def test_general_computer_requests_are_imperatives_not_a_command_catalog(text, allowed):
    from orbit_common.routing import imperative
    assert imperative(text) is allowed


def test_confidence_gate_is_conservative_and_capability_promotion_is_explicit():
    from orbit_common.routing import RouteDecision
    strong = RouteDecision(available=True, capability="open_notes", confidence=.98, probability=.97,
                           direct=True, elapsed_ms=80, model="jev-1.13.0")
    assert strong.eligible
    assert strong.model_copy(update={"confidence": .8}).eligible
    assert not strong.model_copy(update={"confidence": .79}).eligible
    assert not strong.model_copy(update={"probability": .94}).eligible
    assert not strong.model_copy(update={"direct": False}).eligible
    assert not strong.model_copy(update={"available": False}).eligible
