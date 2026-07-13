"""Unit tests for the Memory_Tier taxonomy mapping."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import cast

import pytest

from molt.models.tiers import MEMORY_TIERS, TIER_NAMES, WORKING_TIER, MemoryTierSpec

EXPECTED_ORDER: tuple[str, ...] = (
    "episodic",
    "attribution",
    "procedural_semantic",
    "provenance",
    "action",
    "working",
)


def test_all_six_tiers_present_in_declared_order() -> None:
    assert TIER_NAMES == EXPECTED_ORDER
    assert tuple(MEMORY_TIERS) == EXPECTED_ORDER
    assert len(MEMORY_TIERS) == 6


def test_every_key_matches_its_own_spec_name() -> None:
    for name, spec in MEMORY_TIERS.items():
        assert spec.name == name


def test_every_tier_carries_all_four_columns() -> None:
    for spec in MEMORY_TIERS.values():
        assert spec.tables
        assert all(table and table.islower() for table in spec.tables)
        assert spec.mutability.strip() == spec.mutability
        assert spec.capability.strip() == spec.capability
        assert spec.mutability
        assert spec.capability


def test_table_assignment_per_tier() -> None:
    assert MEMORY_TIERS["episodic"].tables == ("ledger",)
    assert MEMORY_TIERS["attribution"].tables == ("client_binding",)
    assert MEMORY_TIERS["procedural_semantic"].tables == (
        "derived_artifact",
        "procedure_retrieval",
        "procedure_outcome",
        "procedure_confidence_change",
    )
    assert MEMORY_TIERS["provenance"].tables == ("lineage_edge", "ledger", "ledger_checkpoint")
    assert MEMORY_TIERS["action"].tables == (
        "erasure_lease",
        "erasure_request",
        "erasure_run",
        "erasure_candidate",
        "residue_candidate",
        "disposition",
        "run_session",
        "backup_record",
        "erasure_certificate",
    )
    assert MEMORY_TIERS[WORKING_TIER].tables == ("working_memory",)


def test_working_tier_is_the_disposable_one() -> None:
    assert WORKING_TIER in MEMORY_TIERS
    assert MEMORY_TIERS[WORKING_TIER].mutability.startswith("Disposable.")
    assert "Row-Level TTL" in MEMORY_TIERS[WORKING_TIER].capability


def test_mapping_refuses_mutation_at_runtime() -> None:
    # The declared types already refuse each of these statically; the casts are
    # what let the runtime refusal be asserted as well.
    spec = MEMORY_TIERS["episodic"]
    mutable_mapping = cast("MutableMapping[str, MemoryTierSpec]", MEMORY_TIERS)
    with pytest.raises(TypeError):
        mutable_mapping["extra"] = spec
    with pytest.raises(TypeError):
        del mutable_mapping["episodic"]
    mutable_tables = cast("list[str]", spec.tables)
    with pytest.raises(TypeError):
        mutable_tables[0] = "other"
    with pytest.raises(AttributeError):
        spec.__setattr__("mutability", "other")


def test_spec_is_frozen_and_slotted() -> None:
    spec = MemoryTierSpec(
        name="probe",
        tables=("t",),
        table_note=None,
        mutability="Immutable.",
        capability="capability",
    )
    assert not hasattr(spec, "__dict__")
    with pytest.raises(AttributeError):
        spec.__setattr__("name", "renamed")
