"""The harness must generate failures on demand, and tenant-b must be config, not a fork."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.harness.app import MEMBERS, TENANTS, app

HARNESS = Path(__file__).resolve().parents[1] / "apps" / "harness"

client = TestClient(app, follow_redirects=False)


def body(path: str) -> str:
    response = client.get(path)
    assert response.status_code in (200, 403), f"{path} -> {response.status_code}"
    text: str = response.text
    return text


# ---- tenant-b is the same app under a different config --------------------------------


def test_no_tenant_specific_source_file_exists() -> None:
    """A tenant that needed its own module or template would be a fork, not a variant."""
    named = [
        p.name
        for p in HARNESS.rglob("*")
        if p.is_file() and re.search(r"tenant[-_]?[ab]", p.name, re.IGNORECASE)
    ]
    assert named == [], f"tenant-specific files exist: {named}"


def test_both_tenants_declare_exactly_the_same_settings() -> None:
    """A tenant is a row in one table; a missing key would mean a special case somewhere."""
    keys = [set(settings) for settings in TENANTS.values()]
    assert all(k == keys[0] for k in keys)


@pytest.mark.parametrize("tenant", sorted(TENANTS))
def test_every_tenant_serves_the_whole_flow_on_the_same_routes(tenant: str) -> None:
    member = "12345"
    ref = MEMBERS[member]["accounts"][0]["ref"]
    assert "form" in body(f"/{tenant}/")
    assert "Okonkwo-Bright" in body(f"/{tenant}/members?q={member}")
    assert "<frameset" in body(f"/{tenant}/members/{member}")
    assert "Wilhelmina" in body(f"/{tenant}/members/{member}/summary")
    assert ref in body(f"/{tenant}/members/{member}/accounts")
    assert "4,182.55" in body(f"/{tenant}/members/{member}/accounts/{ref}")
    assert "ENQ-" in body(f"/{tenant}/members/{member}/accounts/{ref}/confirm")


def test_the_renamed_labels_actually_differ() -> None:
    a, b = body("/tenant-a/"), body("/tenant-b/")
    assert "Member ID" in a and "Member ID" not in b
    assert "Account Holder ID" in b and "Account Holder ID" not in a


def test_the_balance_label_is_renamed_on_the_sub_account() -> None:
    a = body("/tenant-a/members/12345/accounts/SAV-88120")
    b = body("/tenant-b/members/12345/accounts/SAV-88120")
    assert "Savings Balance" in a and "Deposit Balance" not in a
    assert "Deposit Balance" in b and "Savings Balance" not in b
    assert "4,182.55" in a and "4,182.55" in b  # same value, different label


def test_the_version_string_is_bumped_in_the_footer() -> None:
    assert "version 4.2.1" in body("/tenant-a/")
    assert "version 5.0.3" in body("/tenant-b/")


def test_one_table_column_is_reordered() -> None:
    """Same data, columns swapped: an overlay has to survive this."""
    a = body("/tenant-a/members/12345/accounts")
    b = body("/tenant-b/members/12345/accounts")
    assert a.index("Reference") < a.index("Type")
    assert b.index("Type") < b.index("Reference")


def test_branding_differs() -> None:
    assert "Meridian Core Servicing" in body("/tenant-a/")
    assert "Northgate Servicing Suite" in body("/tenant-b/")


# ---- the failure flags -----------------------------------------------------------------


def test_not_found_is_a_result_page_not_an_error() -> None:
    response = client.get("/tenant-a/members?q=12345&fail=not_found")
    assert response.status_code == 200
    assert "No member matches" in response.text


def test_permission_denied_is_a_panel_with_a_403() -> None:
    response = client.get("/tenant-a/members/12345?fail=permission")
    assert response.status_code == 403
    assert "does not permit" in response.text


def test_timeout_redirects_out_of_the_flow() -> None:
    response = client.get("/tenant-a/members/12345?fail=timeout")
    assert response.status_code == 302
    assert response.headers["location"] == "/tenant-a/session-expired"
    assert "Sign in again" in body("/tenant-a/session-expired")


def test_modal_fires_on_the_next_action_and_can_be_acknowledged() -> None:
    armed = body("/tenant-a/members/12345/accounts/SAV-88120?fail=modal")
    assert 'role="dialog"' in armed and "Acknowledge" in armed
    assert "4,182.55" not in armed  # the screen behind it is not reachable yet

    through = body("/tenant-a/members/12345/accounts/SAV-88120?fail=modal&ack=1")
    assert "4,182.55" in through


def test_a_flag_rides_along_the_flow() -> None:
    """Arming a failure on one screen must reach an action several clicks later."""
    assert "fail=modal" in body("/tenant-a/members?q=12345&fail=modal")


# ---- deliberately hostile markup --------------------------------------------------------


def test_element_ids_are_regenerated_on_every_render() -> None:
    first = set(re.findall(r'id="(e[0-9a-f]{8})"', body("/tenant-a/")))
    second = set(re.findall(r'id="(e[0-9a-f]{8})"', body("/tenant-a/")))
    assert first and not (first & second)


def test_there_are_no_test_ids_anywhere() -> None:
    for path in ("/tenant-a/", "/tenant-a/members?q=12345", "/tenant-a/members/12345/accounts"):
        assert not re.search(r"data-(testid|test|cy|qa)", body(path))


def test_the_search_submit_is_an_anchor_with_onclick() -> None:
    page = body("/tenant-a/")
    assert "<button" not in page
    assert 'onclick="document.forms' in page


def test_the_detail_screen_is_a_real_frameset() -> None:
    page = body("/tenant-a/members/12345")
    assert "<frameset" in page and "<frame " in page and "noframes" in page


# ---- the verification screen -------------------------------------------------------------


def test_the_verification_screen_offers_a_code_field() -> None:
    body = client.get("/tenant-a/members/12345/verify").text

    assert "Verification code" in body
    assert 'name="code"' in body
    assert "onclick" in body, "submit is an <a>, like the rest of the harness"


def test_the_right_code_verifies_and_a_wrong_one_does_not() -> None:
    right = client.get("/tenant-a/members/12345/verified?code=QX7-4412").text
    wrong = client.get("/tenant-a/members/12345/verified?code=nope").text

    assert "Identity verified" in right
    assert "Code not recognised" in wrong


def test_the_code_is_shaped_so_only_a_declared_value_can_redact_it() -> None:
    """The shape-based backstop must not match it, or the demo proves the wrong half.

    `redact` with no declared values has to leave these untouched; only declaring them
    should blank them. Otherwise the evidence would show the pattern matcher working and
    say nothing about per-invocation sensitive parameters.
    """
    from apps.harness.app import MEMBERS
    from cua.policy.redaction import redact

    for member in MEMBERS.values():
        code = str(member["code"])
        assert redact(code) == code, f"{code!r} matches a shape pattern"
        assert redact(code, {"code": code}) == "[REDACTED:code]"
