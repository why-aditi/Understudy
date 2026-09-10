"""Overlay resolution: what a tenant diff may change, and when it stops being trusted.

The interesting cases are the refusals. An overlay that applies cleanly is easy; an overlay
that quietly applies to nothing, or that is still trusted after the base moved underneath it,
is the failure this design exists to prevent.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cua.replay.overlay import (
    CAPABILITY_SCOPE,
    OverlayError,
    apply_overrides,
    for_tenant,
    load_overlay,
    overlay_path,
    resolve,
    set_path,
)
from cua.schema.models import (
    AppRef,
    Capability,
    CapabilityOverlay,
    Condition,
    ControlDescriptor,
    EntryPoint,
    Locator,
    Outcome,
    Override,
    Provenance,
    Recovery,
    Step,
)


def control(role: str, name: str) -> ControlDescriptor:
    return ControlDescriptor(
        role=role,
        name=name,
        candidates=[
            Locator(
                strategy="role_name",
                params={"role": role, "name": name, "match": "exact"},
                stability_score=0.85,
                verified_unique_at_record=True,
            )
        ],
    )


def base(version: str = "1.0.0") -> Capability:
    """A two-step capability recorded against tenant-a, approved and reviewed."""
    return Capability(
        id="member.search",
        name="Search for a member",
        description="Look a member up by id.",
        version=version,
        app=AppRef(vendor_product="meridian-core", tenant_id="tenant-a"),
        entry=EntryPoint(url="http://127.0.0.1:8099/tenant-a/"),
        parameters=[],
        outputs=[],
        steps=[
            Step(
                id="enter-id",
                intent="type the member id",
                action="type",
                target=control("textbox", "Member ID"),
            ),
            Step(
                id="submit",
                intent="submit the search",
                action="click",
                target=control("link", "Search"),
            ),
        ],
        outcomes=[],
        provenance=Provenance(
            discovered_at=datetime(2026, 9, 10, tzinfo=UTC),
            model="test",
            discovery_run_id="run-1",
            state="approved",
            approved_by="aditi",
            outcomes_reviewed=True,
        ),
    )


def overlay(**kwargs: object) -> CapabilityOverlay:
    defaults: dict[str, object] = {
        "base_capability_id": base().id,
        "base_version": "1.0.0",
        "tenant_id": "tenant-b",
        "verified_against": "1.0.0",
        "overrides": [
            Override(
                step_id="enter-id",
                field_path="target.candidates.0.params.name",
                value="Account Holder ID",
            )
        ],
    }
    return CapabilityOverlay.model_validate({**defaults, **kwargs})


# ---- the path applier ---------------------------------------------------------------------


def test_a_nested_path_with_a_list_index_is_replaced() -> None:
    data = {"target": {"candidates": [{"params": {"name": "Member ID"}}]}}

    set_path(data, "target.candidates.0.params.name", "Account Holder ID", where="w")

    assert data["target"]["candidates"][0]["params"]["name"] == "Account Holder ID"


def test_a_path_that_matches_nothing_is_an_error_not_a_no_op() -> None:
    """The failure this prevents: an overlay that looks maintained and changes nothing."""
    data = {"target": {"candidates": [{"params": {"name": "Member ID"}}]}}

    with pytest.raises(OverlayError, match="no field 'nmae'"):
        set_path(data, "target.candidates.0.params.nmae", "x", where="w")


def test_a_list_index_past_the_end_is_an_error() -> None:
    data: dict[str, object] = {"candidates": [{"params": {}}]}

    with pytest.raises(OverlayError, match="out of range"):
        set_path(data, "candidates.4.params", "x", where="w")


def test_a_non_numeric_list_index_says_so() -> None:
    with pytest.raises(OverlayError, match="not a list index"):
        set_path({"c": [{}]}, "c.first", "x", where="w")


def test_descending_into_a_scalar_is_an_error() -> None:
    with pytest.raises(OverlayError, match="cannot descend"):
        set_path({"name": "Member ID"}, "name.inner", "x", where="w")


def test_an_empty_path_is_rejected() -> None:
    with pytest.raises(OverlayError, match="not a usable field path"):
        set_path({"a": 1}, "", "x", where="w")


def test_an_override_naming_an_unknown_step_is_an_error() -> None:
    data = base().model_dump(mode="json")
    override = Override(step_id="no-such-step", field_path="intent", value="x")

    with pytest.raises(OverlayError, match="no step 'no-such-step'"):
        apply_overrides(data, [override])


# ---- resolution -----------------------------------------------------------------------------


def test_an_override_reaches_the_resolved_capability() -> None:
    resolved = resolve(base(), overlay())

    step = resolved.capability.steps[0]
    assert step.target is not None
    assert step.target.candidates[0].params["name"] == "Account Holder ID"


def test_the_base_is_not_mutated_by_resolution() -> None:
    """Resolution has to be safe to run twice, and against the in-memory base a caller holds."""
    original = base()

    resolve(original, overlay())

    assert original.steps[0].target is not None
    assert original.steps[0].target.candidates[0].params["name"] == "Member ID"


def test_the_resolved_capability_carries_the_tenant_and_names_its_base() -> None:
    resolved = resolve(base(), overlay())

    assert resolved.capability.app.tenant_id == "tenant-b"
    assert resolved.capability.app.base_capability_id == base().id


def test_the_capability_scope_reaches_fields_outside_any_step() -> None:
    """The entry url is the one that matters: a tenant lives at a different address."""
    entry = Override(
        step_id=CAPABILITY_SCOPE,
        field_path="entry.url",
        value="http://127.0.0.1:8099/tenant-b/",
    )

    resolved = resolve(base(), overlay(overrides=[entry]))

    assert resolved.capability.entry.url == "http://127.0.0.1:8099/tenant-b/"


def test_added_outcomes_are_appended_not_replaced() -> None:
    extra = Outcome(
        name="tenant_b_maintenance",
        kind="recoverable",
        detect=Condition(kind="text_present", params={"text": "Scheduled downtime"}),
        message_template="Northgate is in a maintenance window.",
        recovery=Recovery(action="wait_and_retry", max_attempts=2),
    )
    before = len(base().outcomes)

    resolved = resolve(base(), overlay(added_outcomes=[extra]))

    assert len(resolved.capability.outcomes) == before + 1
    assert resolved.capability.outcomes[-1].name == "tenant_b_maintenance"


def test_an_overlay_for_a_different_capability_is_refused() -> None:
    with pytest.raises(OverlayError, match="but the base is"):
        resolve(base(), overlay(base_capability_id="something.else"))


def test_an_override_that_produces_an_invalid_capability_fails_at_resolution() -> None:
    """Validate is a real step, not a formality: a bad value must not reach replay."""
    broken = Override(step_id="enter-id", field_path="action", value="teleport")

    with pytest.raises(OverlayError, match="invalid capability"):
        resolve(base(), overlay(overrides=[broken]))


# ---- the staleness rule -----------------------------------------------------------------------


def test_a_current_overlay_needs_no_review() -> None:
    resolved = resolve(base("1.0.0"), overlay(verified_against="1.0.0"))

    assert resolved.needs_review is False
    assert resolved.capability.provenance.state == "approved"
    assert resolved.capability.provenance.replayable_unattended is True


def test_a_base_that_moved_past_verified_against_demotes_the_result_to_draft() -> None:
    """The refusal is delegated to the gate that already exists, rather than a second one."""
    resolved = resolve(base("1.1.0"), overlay(verified_against="1.0.0"))

    assert resolved.needs_review is True
    assert resolved.capability.provenance.state == "draft"
    assert resolved.capability.provenance.replayable_unattended is False


def test_the_staleness_reason_names_both_versions() -> None:
    resolved = resolve(base("2.0.0"), overlay(verified_against="1.0.0"))

    assert resolved.reason is not None
    assert "2.0.0" in resolved.reason
    assert "1.0.0" in resolved.reason


def test_a_stale_overlay_still_resolves_so_it_can_be_run_attended() -> None:
    """Refusing outright would leave no way to check whether the overlay still works."""
    resolved = resolve(base("1.1.0"), overlay(verified_against="1.0.0"))

    step = resolved.capability.steps[0]
    assert step.target is not None
    assert step.target.candidates[0].params["name"] == "Account Holder ID"


# ---- on disk --------------------------------------------------------------------------------


def test_an_overlay_round_trips_through_its_file(tmp_path: Path) -> None:
    written = overlay()
    path = overlay_path(written.base_capability_id, "tenant-b", tmp_path)
    path.write_text(written.model_dump_json(indent=2), encoding="utf-8")

    assert load_overlay(written.base_capability_id, "tenant-b", tmp_path) == written


def test_a_missing_overlay_names_the_path_it_looked_in(tmp_path: Path) -> None:
    with pytest.raises(OverlayError, match="no overlay for"):
        load_overlay("member.search", "tenant-z", tmp_path)


def test_a_malformed_overlay_is_not_silently_skipped(tmp_path: Path) -> None:
    path = overlay_path("member.search", "tenant-b", tmp_path)
    path.write_text(json.dumps({"tenant_id": "tenant-b"}), encoding="utf-8")

    with pytest.raises(OverlayError, match="not a valid overlay"):
        load_overlay("member.search", "tenant-b", tmp_path)


def test_asking_for_the_tenant_it_was_recorded_against_needs_no_overlay(tmp_path: Path) -> None:
    """Reuse is the default. The base is already tenant-a's capability."""
    original = base()

    assert original.app.tenant_id is not None
    resolved = for_tenant(original, original.app.tenant_id, tmp_path)

    assert resolved.capability is original
    assert resolved.needs_review is False


def test_asking_for_another_tenant_loads_its_overlay(tmp_path: Path) -> None:
    written = overlay()
    path = overlay_path(written.base_capability_id, "tenant-b", tmp_path)
    path.write_text(written.model_dump_json(indent=2), encoding="utf-8")

    resolved = for_tenant(base(), "tenant-b", tmp_path)

    assert resolved.capability.app.tenant_id == "tenant-b"


# ---- the shipped overlay ----------------------------------------------------------------------


def test_the_committed_tenant_b_overlay_resolves_against_the_committed_base() -> None:
    """The evidence run's overlay, checked in CI rather than only on my machine."""
    from cua.replay.engine import load_capability

    base_capability = load_capability("member.search")
    resolved = for_tenant(base_capability, "tenant-b")

    assert resolved.needs_review is False, "the committed overlay is verified against this base"
    assert resolved.capability.entry.url.endswith("/tenant-b/")

    enter_id = next(step for step in resolved.capability.steps if step.id == "enter-id")
    assert enter_id.target is not None
    assert enter_id.target.candidates[0].params["name"] == "Account Holder ID"

    # Nothing tenant-a-specific may survive into the tenant-b artifact.
    serialised = resolved.capability.model_dump_json()
    assert "Member ID" not in serialised
    assert "tenant-a" not in serialised
