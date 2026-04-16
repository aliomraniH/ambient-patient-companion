"""Phase 3 — Transcript reorder before synthesis tests."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from server.deliberation.synthesizer import _reorder_transcript_for_bias_mitigation


def _make_transcript() -> dict:
    return {
        "phase1": {
            "claude": {"findings": ["claude finding 1"]},
            "gpt4":   {"findings": ["gpt4 finding 1"]},
        },
        "phase2_rounds": [
            {
                "round": 1,
                "claude_critique_of_gpt4":   {"items": ["c_crit"]},
                "gpt4_critique_of_claude":   {"items": ["g_crit"]},
                "claude_revised":            {"revised": ["c_rev"]},
                "gpt4_revised":              {"revised": ["g_rev"]},
            }
        ],
    }


def _first_byte_parity(deliberation_id: str) -> int:
    return hashlib.sha256(deliberation_id.encode()).digest()[0] % 2


def _find_even_id() -> str:
    for i in range(1000):
        did = f"delib-{i:04d}"
        if _first_byte_parity(did) == 0:
            return did
    raise RuntimeError("No even-hash ID found in range")


def _find_odd_id() -> str:
    for i in range(1000):
        did = f"delib-{i:04d}"
        if _first_byte_parity(did) == 1:
            return did
    raise RuntimeError("No odd-hash ID found in range")


EVEN_ID = _find_even_id()
ODD_ID  = _find_odd_id()


def test_even_hash_claude_first():
    """Even-hash ID keeps the original (claude-first) ordering."""
    t = _make_transcript()
    result = _reorder_transcript_for_bias_mitigation(t, EVEN_ID)
    keys = list(result["phase1"].keys())
    assert keys[0] == "claude", f"Expected claude first, got {keys}"


def test_odd_hash_gpt4_first():
    """Odd-hash ID swaps to gpt4-first ordering in phase1."""
    t = _make_transcript()
    result = _reorder_transcript_for_bias_mitigation(t, ODD_ID)
    keys = list(result["phase1"].keys())
    assert keys[0] == "gpt4", f"Expected gpt4 first, got {keys}"


def test_odd_hash_phase2_rounds_swapped():
    """Odd-hash ID also swaps the per-round keys in phase2_rounds."""
    t = _make_transcript()
    result = _reorder_transcript_for_bias_mitigation(t, ODD_ID)
    rnd = result["phase2_rounds"][0]
    rnd_keys = list(rnd.keys())
    # After swap, gpt4_critique_of_claude should appear before claude_critique_of_gpt4
    assert rnd_keys.index("gpt4_critique_of_claude") < rnd_keys.index("claude_critique_of_gpt4"), (
        f"gpt4_critique_of_claude should come first; got order: {rnd_keys}"
    )


def test_no_data_loss():
    """Values must be identical after reordering — only key order changes."""
    t = _make_transcript()
    result = _reorder_transcript_for_bias_mitigation(t, ODD_ID)
    # phase1 values
    assert result["phase1"]["claude"] == t["phase1"]["claude"]
    assert result["phase1"]["gpt4"]   == t["phase1"]["gpt4"]
    # phase2_rounds values
    orig_rnd   = t["phase2_rounds"][0]
    result_rnd = result["phase2_rounds"][0]
    assert result_rnd["claude_critique_of_gpt4"] == orig_rnd["claude_critique_of_gpt4"]
    assert result_rnd["gpt4_critique_of_claude"] == orig_rnd["gpt4_critique_of_claude"]
    assert result_rnd["claude_revised"]          == orig_rnd["claude_revised"]
    assert result_rnd["gpt4_revised"]            == orig_rnd["gpt4_revised"]


def test_deterministic():
    """Same deliberation ID always produces the same ordering."""
    t1 = _make_transcript()
    t2 = _make_transcript()
    r1 = _reorder_transcript_for_bias_mitigation(t1, ODD_ID)
    r2 = _reorder_transcript_for_bias_mitigation(t2, ODD_ID)
    assert list(r1["phase1"].keys()) == list(r2["phase1"].keys())


def test_original_not_mutated():
    """The original transcript dict is never mutated."""
    t = _make_transcript()
    orig_phase1_keys = list(t["phase1"].keys())
    _reorder_transcript_for_bias_mitigation(t, ODD_ID)
    assert list(t["phase1"].keys()) == orig_phase1_keys, "Original transcript was mutated"
