import json
from pathlib import Path
import hashlib

from orbit_common.evaluation import promotion_report as evaluate_promotion

from orbit_common.routing import CAPABILITIES

DATA = Path(__file__).parents[1] / "evals"


def voice_manifest():
    return [{**f, "pcm_sha256": hashlib.sha256(f["id"].encode()).hexdigest()}
            for f in json.loads((DATA / "voice_english.json").read_text())["fixtures"]]


def promotion_report(routing, baseline, candidate):
    return evaluate_promotion(routing, baseline, candidate, routing_manifest=fixture_results(), voice_manifest=voice_manifest())


def fixture_results():
    return [{**row, "available": True, "capability": None if row["expected"] == "defer" else row["expected"],
             "eligible": row["expected"] != "defer", "model": "jev-1.13.0", "elapsed_ms": 100}
            for row in (json.loads(line) for line in (DATA / "routing_english.jsonl").read_text().splitlines())]


def measurements(configuration, action_ms=1000):
    return [{"fixture_id": f["id"], "configuration": configuration, "measurement": "native_end_to_end",
             "capability": f["expected_capability"], "pcm_sha256": f["pcm_sha256"],
             "jev_mode": "off" if configuration == "baseline" else "active", "jev_model": "jev-1.13.0",
             "settings": {"realtime_model": "gpt-realtime-2.1-mini", "transcription_model": "gpt-4o-transcribe",
                          "agent_model": "gpt-5.6-sol", "vad_eagerness": "medium", "native_build": "test-build",
                          "audio_format": "pcm16_mono_24000"}, "correct": True, "premature_cutoff": False,
             "meaningful_reply_verified": True, "speech_end_to_first_audible_ms": 600,
             "speech_end_to_first_verified_action_ms": action_ms if f["expected_capability"] else None}
            for f in voice_manifest()]


def test_fixture_manifest_is_balanced_unique_and_split_before_evaluation():
    rows = fixture_results()
    assert len(rows) == len({r["id"] for r in rows}) == len({r["utterance"] for r in rows}) == 100
    for split in ("calibration", "holdout"):
        selected = [r for r in rows if r["split"] == split]
        assert len(selected) == 50
        assert {r["expected"] for r in selected} == {*CAPABILITIES, "defer"}
    assert len(measurements("baseline")) == 30


def test_provider_timing_cannot_promote_native_latency_claims():
    rows = measurements("jev", 600)
    for row in rows:
        row["measurement"] = "provider_component"
    result = promotion_report(fixture_results(), measurements("baseline"), rows)
    assert result["enabled_capabilities"] == []
    assert any("native" in reason.lower() for reason in result["blocked_reasons"])


def test_complete_correct_native_measurements_can_promote_each_capability():
    result = promotion_report(fixture_results(), measurements("baseline"), measurements("jev", 600))
    assert set(result["enabled_capabilities"]) == set(CAPABILITIES)
    assert result["blocked_reasons"] == []


def test_false_execution_or_stale_model_blocks_promotion():
    for kind in ("wrong", "model"):
        rows = fixture_results()
        if kind == "wrong":
            negative = next(row for row in rows if row["split"] == "holdout" and row["expected"] == "defer")
            negative.update(capability="open_notes", eligible=True)
        else:
            rows[0]["model"] = "different-version"
        result = promotion_report(rows, measurements("baseline"), measurements("jev", 600))
        assert result["enabled_capabilities"] == []


def test_missing_or_non_improving_benchmarks_keep_shadow():
    for measured in ([], measurements("jev", 900), measurements("jev", 1200)):
        assert not promotion_report(fixture_results(), measurements("baseline"), measured)["enabled_capabilities"]


def test_cutoffs_and_faster_but_incorrect_actions_do_not_count_as_wins():
    for field, value in [("correct", False), ("premature_cutoff", True), ("meaningful_reply_verified", False)]:
        rows = measurements("jev", 100)
        rows[0][field] = value
        assert not promotion_report(fixture_results(), measurements("baseline"), rows)["enabled_capabilities"]


def test_wrong_configuration_and_foreign_fixtures_cannot_promote():
    wrong_config = promotion_report(fixture_results(), measurements("baseline"), measurements("baseline", 600))
    assert wrong_config["recommended_mode"] == "shadow"
    baseline, candidate = measurements("baseline"), measurements("jev", 600)
    for rows in (baseline, candidate):
        for row in rows:
            row["fixture_id"] = "foreign-" + row["fixture_id"]
    assert promotion_report(fixture_results(), baseline, candidate)["recommended_mode"] == "shadow"


def test_missing_hash_mismatched_settings_and_relabelled_evidence_fail_closed():
    for mutation in ("hash", "settings", "routing_label", "voice_label"):
        routing, baseline, candidate = fixture_results(), measurements("baseline"), measurements("jev", 600)
        if mutation == "hash":
            candidate[0].pop("pcm_sha256")
        elif mutation == "settings":
            candidate[0]["settings"]["vad_eagerness"] = "high"
        elif mutation == "routing_label":
            routing[0]["expected"] = "defer"
        else:
            candidate[0]["capability"] = "open_chrome"
        assert promotion_report(routing, baseline, candidate)["recommended_mode"] == "shadow"
    assert evaluate_promotion(fixture_results(), measurements("baseline"), measurements("jev", 600))["recommended_mode"] == "shadow"
