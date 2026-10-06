"""Evidence gates for the prototype; component timing cannot establish device latency."""
import math
import re
from statistics import median

from .routing import CAPABILITIES


def duration_summary(values):
    values = list(values)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("Durations must be finite nonnegative numbers")
    return {"n": len(values), "median_ms": median(values) if values else None,
            "p95_ms": sorted(values)[math.ceil(len(values) * .95) - 1] if values else None}


def routing_summary(rows):
    result = {}
    for split in ("calibration", "holdout"):
        selected = [r for r in rows if r["split"] == split]
        available = [r for r in selected if r.get("available") is True]
        correct = [r for r in available if (r.get("capability") or "defer") == r["expected"]]
        executions = [r for r in selected if r.get("eligible") is True]
        wrong = [r["id"] for r in executions if r.get("capability") != r["expected"]]
        result[split] = {"n": len(selected), "available": len(available), "correct_choices": len(correct),
            "choice_accuracy_including_outages": len(correct) / len(selected) if selected else 0,
            "eligible": len(executions), "false_execution_ids": wrong,
            "eligible_by_capability": {cap: sum(r.get("capability") == cap and r["expected"] == cap for r in executions)
                                       for cap in CAPABILITIES},
            "provider_elapsed": duration_summary(r["elapsed_ms"] for r in available)}
    return result


def evidence_identity_errors(routing, baseline, candidate, routing_manifest, voice_manifest, model):
    reasons = []
    routing_expected = {r["id"]: r for r in routing_manifest or []}
    voice_expected = {r["id"]: r for r in voice_manifest or []}
    if len(routing_expected) != 100 or len(voice_expected) != 30:
        return ["Require the fixed routing and audio manifests, including recorded PCM hashes."]
    if {r["id"] for r in routing} != set(routing_expected) or any(
            any(row.get(key) != routing_expected.get(row["id"], {}).get(key)
                for key in ("split", "expected", "utterance")) for row in routing):
        reasons.append("Routing evidence differs from the fixed labeled manifest.")
    settings_keys = {"realtime_model", "transcription_model", "agent_model", "vad_eagerness", "native_build", "audio_format"}
    reference_settings = baseline[0].get("settings", {}) if baseline else {}
    before = {r["fixture_id"]: r for r in baseline}
    for name, mode, rows in (("baseline", "off", baseline), ("jev", "active", candidate)):
        if {r["fixture_id"] for r in rows} != set(voice_expected):
            reasons.append("Audio evidence differs from the fixed 30-fixture manifest.")
        for row in rows:
            expected = voice_expected.get(row["fixture_id"], {})
            digest = row.get("pcm_sha256", "")
            settings = row.get("settings", {})
            if row.get("configuration") != name or row.get("jev_mode") != mode or row.get("jev_model") != model:
                reasons.append("Require explicit baseline-off and pinned Jev-active configurations.")
            if (not isinstance(settings, dict) or set(settings) != settings_keys
                    or any(not isinstance(v, str) or not v for v in settings.values())
                    or settings != reference_settings):
                reasons.append("Require identical recorded non-Jev settings and native build across trials.")
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or digest != expected.get("pcm_sha256")
                    or row.get("capability") != expected.get("expected_capability")
                    or (name == "jev" and digest != before.get(row["fixture_id"], {}).get("pcm_sha256"))):
                reasons.append("Require the fixed capability labels and matching recorded PCM hashes.")
    return list(dict.fromkeys(reasons))


def promotion_report(routing, baseline, candidate, model="jev-1.13.0", *, routing_manifest=None, voice_manifest=None):
    summary = routing_summary(routing)
    reasons = evidence_identity_errors(routing, baseline, candidate, routing_manifest, voice_manifest, model)
    if (len(routing) != 100 or len({r["id"] for r in routing}) != 100
            or any(summary[s]["n"] != 50 for s in summary)):
        reasons.append("Require all 100 unique routing examples with fixed 50/50 splits.")
    if any(r.get("model") != model for r in routing):
        reasons.append("Routing evidence must use the pinned Jev model.")
    if any(summary[s]["false_execution_ids"] for s in summary):
        reasons.append("False execution in routing evaluation; retain shadow mode.")
    if (len(baseline) != 30 or len(candidate) != 30
            or len({r["fixture_id"] for r in baseline}) != 30
            or len({r["fixture_id"] for r in candidate}) != 30
            or {r["fixture_id"] for r in baseline} != {r["fixture_id"] for r in candidate}):
        reasons.append("Require the same 30 unique audio fixtures in baseline and Jev runs.")
    if any(r.get("measurement") != "native_end_to_end" for r in baseline + candidate):
        reasons.append("Require native end-to-end evidence; provider timing cannot prove audible or action latency.")
    if any(r.get("correct") is not True or r.get("premature_cutoff") is not False
           or r.get("meaningful_reply_verified") is not True for r in baseline + candidate):
        reasons.append("Every measured trial needs verified correctness, meaningful speech and no premature cutoff.")
    comparisons = {}
    if not reasons:
        try:
            baseline_by_id = {r["fixture_id"]: r for r in baseline}
            if any(r.get("capability") != baseline_by_id[r["fixture_id"]].get("capability") for r in candidate):
                raise ValueError("Fixture capability labels differ between configurations")
            before_voice = duration_summary(r["speech_end_to_first_audible_ms"] for r in baseline)
            after_voice = duration_summary(r["speech_end_to_first_audible_ms"] for r in candidate)
            if after_voice["p95_ms"] > before_voice["p95_ms"]:
                reasons.append("Spoken response p95 regressed.")
            for cap in CAPABILITIES:
                before = duration_summary(r["speech_end_to_first_verified_action_ms"] for r in baseline if r.get("capability") == cap)
                after = duration_summary(r["speech_end_to_first_verified_action_ms"] for r in candidate if r.get("capability") == cap)
                qualified = (before["n"] >= 3 and after["n"] == before["n"]
                             and summary["holdout"]["eligible_by_capability"][cap] >= 3
                             and before["median_ms"] > 0 and after["median_ms"] <= .8 * before["median_ms"]
                             and after["p95_ms"] <= before["p95_ms"])
                comparisons[cap] = {"baseline": before, "jev": after, "qualified": qualified}
        except (KeyError, TypeError, ValueError) as error:
            reasons.append("Missing or invalid measurement evidence: " + str(error))
    enabled = [cap for cap, report in comparisons.items() if report["qualified"]] if not reasons else []
    if not reasons and not enabled:
        reasons.append("No capability meets the 20% median improvement, p95 and held-out coverage gates.")
    return {"model": model, "recommended_mode": "active" if enabled else "shadow",
            "enabled_capabilities": enabled, "blocked_reasons": reasons, "routing": summary,
            "comparisons": comparisons, "applies_configuration": False}
