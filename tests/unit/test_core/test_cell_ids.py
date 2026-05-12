"""Tests for `compute_cell_id` — the deterministic v2 dispatch key.

Same inputs across machines hash to the same id; the trace-store
idempotency check at the canonical sink relies on this stability.
"""

from __future__ import annotations

from eval_harness.core.models import CellDescriptor, compute_cell_id


def test_cell_id_deterministic_same_input_same_id() -> None:
    a = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"model": "claude-4-7", "temperature": 0.0},
    )
    b = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"model": "claude-4-7", "temperature": 0.0},
    )
    assert a == b
    # Shape: "<run_id>::<case_id>::<variant_name>::<12-char-hash>"
    parts = a.split("::")
    assert parts[:3] == ["r1", "c1", "v1"]
    assert len(parts[3]) == 12


def test_cell_id_key_order_does_not_matter() -> None:
    a = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"a": 1, "b": 2},
    )
    b = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"b": 2, "a": 1},
    )
    assert a == b


def test_cell_id_changes_when_config_changes() -> None:
    a = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"model": "claude-4-7"},
    )
    b = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"model": "claude-4-8"},
    )
    assert a != b


def test_cell_id_changes_when_variant_changes() -> None:
    a = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"model": "claude-4-7"},
    )
    b = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v2",
        config_slice={"model": "claude-4-7"},
    )
    assert a != b


def test_cell_id_changes_when_case_changes() -> None:
    a = compute_cell_id(
        run_id="r1", case_id="c1", variant_name="v1", config_slice={"x": 1}
    )
    b = compute_cell_id(
        run_id="r1", case_id="c2", variant_name="v1", config_slice={"x": 1}
    )
    assert a != b


def test_cell_id_changes_when_run_id_changes() -> None:
    a = compute_cell_id(
        run_id="r1", case_id="c1", variant_name="v1", config_slice={}
    )
    b = compute_cell_id(
        run_id="r2", case_id="c1", variant_name="v1", config_slice={}
    )
    assert a != b


def test_cell_descriptor_round_trips() -> None:
    desc = CellDescriptor(
        cell_id="r1::c1::v1::deadbeef0123",
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_hash="deadbeef0123",
        eval_config_dict={"systems": [{"name": "v1"}]},
        case_dict={"id": "c1", "input": {"q": "hi"}},
        workspace_kind="tempdir_snapshot",
        pool="default",
    )
    rehydrated = CellDescriptor.model_validate_json(desc.model_dump_json())
    assert rehydrated == desc


def test_cell_descriptor_optional_fields_default_to_none() -> None:
    desc = CellDescriptor(
        cell_id="x",
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_hash="h",
        eval_config_dict={},
        case_dict={},
    )
    assert desc.workspace_kind is None
    assert desc.pool is None


def test_cell_id_stable_across_unicode_payloads() -> None:
    """Non-ASCII config values must still hash deterministically — the
    JSON canonicalizer uses ensure_ascii=False so emoji + accented
    characters survive the round-trip identically."""
    a = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"prompt": "café 🚀"},
    )
    b = compute_cell_id(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_slice={"prompt": "café 🚀"},
    )
    assert a == b
