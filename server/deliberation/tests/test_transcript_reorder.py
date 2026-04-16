"""Tests for _reorder_transcript_for_bias_mitigation.

Permutes Claude/GPT-4 order in the transcript view sent to the synthesizer
to mitigate primacy bias. Original transcript stays unchanged; only the
display copy is reordered. The parity comes from SHA-256 of deliberation_id.
"""
import hashlib
import json

from server.deliberation.synthesizer import _reorder_transcript_for_bias_mitigation


def _make_transcript() -> dict:
    return {
        "phase1": {
            "claude": {"findings": ["c1", "c2"]},
            "gpt4":   {"findings": ["g1", "g2"]},
        },
        "phase2_rounds": [
            {
                "round": 1,
                "claude_critique_of_gpt4": {"items": ["crit_c"]},
                "gpt4_critique_of_claude": {"items": ["crit_g"]},
                "claude_revised": {"findings": ["c1_rev"]},
                "gpt4_revised":   {"findings": ["g1_rev"]},
            },
            {
                "round": 2,
                "claude_critique_of_gpt4": {"items": ["crit2_c"]},
                "gpt4_critique_of_claude": {"items": ["crit2_g"]},
                "claude_revised": {"findings": ["c2_rev"]},
                "gpt4_revised":   {"findings": ["g2_rev"]},
            },
        ],
    }


def _even_hash_id() -> str:
    # Try string representations of integers until we find an even-hash id
    for n in range(1000):
        cand = f"deliberation-{n}"
        if hashlib.sha256(cand.encode()).digest()[0] % 2 == 0:
            return cand
    raise AssertionError("unreachable")


def _odd_hash_id() -> str:
    for n in range(1000):
        cand = f"deliberation-{n}"
        if hashlib.sha256(cand.encode()).digest()[0] % 2 == 1:
            return cand
    raise AssertionError("unreachable")


def test_even_hash_keeps_claude_first():
    t = _make_transcript()
    out = _reorder_transcript_for_bias_mitigation(t, _even_hash_id())
    keys_p1 = list(out["phase1"].keys())
    assert keys_p1.index("claude") < keys_p1.index("gpt4")


def test_odd_hash_puts_gpt4_first():
    t = _make_transcript()
    out = _reorder_transcript_for_bias_mitigation(t, _odd_hash_id())
    keys_p1 = list(out["phase1"].keys())
    assert keys_p1.index("gpt4") < keys_p1.index("claude")


def test_odd_hash_reorders_round_critiques_and_revisions():
    t = _make_transcript()
    out = _reorder_transcript_for_bias_mitigation(t, _odd_hash_id())
    for rnd in out["phase2_rounds"]:
        keys = list(rnd.keys())
        assert keys.index("gpt4_critique_of_claude") < keys.index("claude_critique_of_gpt4")
        assert keys.index("gpt4_revised") < keys.index("claude_revised")


def test_no_data_loss_on_reorder():
    t = _make_transcript()
    out = _reorder_transcript_for_bias_mitigation(t, _odd_hash_id())
    # Values are preserved — only order changes
    assert out["phase1"]["claude"] == t["phase1"]["claude"]
    assert out["phase1"]["gpt4"] == t["phase1"]["gpt4"]
    for orig, reordered in zip(t["phase2_rounds"], out["phase2_rounds"]):
        for k, v in orig.items():
            assert reordered[k] == v


def test_original_transcript_not_mutated():
    t = _make_transcript()
    original_keys = list(t["phase1"].keys())
    _reorder_transcript_for_bias_mitigation(t, _odd_hash_id())
    assert list(t["phase1"].keys()) == original_keys


def test_deterministic_for_same_id():
    t = _make_transcript()
    out1 = _reorder_transcript_for_bias_mitigation(t, "same-id")
    out2 = _reorder_transcript_for_bias_mitigation(t, "same-id")
    assert json.dumps(out1) == json.dumps(out2)


def test_empty_transcript_does_not_crash():
    # Degenerate case — real deliberations always produce phase1+phase2_rounds.
    # We just require the function to return a dict without raising.
    out = _reorder_transcript_for_bias_mitigation({}, "x")
    assert isinstance(out, dict)


def test_non_dict_transcript_passthrough():
    # Defensive: non-dict input returns unchanged
    assert _reorder_transcript_for_bias_mitigation(None, "x") is None
    assert _reorder_transcript_for_bias_mitigation("not a dict", "x") == "not a dict"


def test_round_number_stays_first_in_round_dict():
    t = _make_transcript()
    out = _reorder_transcript_for_bias_mitigation(t, _odd_hash_id())
    for rnd in out["phase2_rounds"]:
        assert list(rnd.keys())[0] == "round"
