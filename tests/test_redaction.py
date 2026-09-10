"""Nothing sensitive reaches disk. This filter is the backstop, not the primary control."""

import logging
from pathlib import Path

from cua.policy.redaction import RedactionFilter, redact

REPO = Path(__file__).resolve().parents[1]


def _record(
    msg: str, *, args: tuple[object, ...] | None = None, **extra: object
) -> logging.LogRecord:
    """A LogRecord carrying `extra` fields, the way logging builds one."""
    record = logging.LogRecord("cua.test", logging.INFO, __file__, 1, msg, args, None)
    record.__dict__.update(extra)
    return record


def test_declared_values_are_replaced_by_their_parameter_name() -> None:
    text = "looked up member ssn=123-45-6789 for account 9876543210"
    assert redact(text, {"member_ssn": "123-45-6789"}) == (
        "looked up member ssn=[REDACTED:member_ssn] for account [REDACTED:account]"
    )


def test_ssn_shaped_strings_are_caught_without_being_declared() -> None:
    assert redact("subject 123-45-6789 filed") == "subject [REDACTED:ssn] filed"


def test_card_shaped_strings_are_caught_with_or_without_separators() -> None:
    assert redact("paid with 4111 1111 1111 1111") == "paid with [REDACTED:card]"
    assert redact("paid with 4111111111111111") == "paid with [REDACTED:card]"


def test_account_shaped_strings_are_caught() -> None:
    assert redact("account 000123456789 closed") == "account [REDACTED:account] closed"


def test_a_short_member_id_survives() -> None:
    """Redacting every number would make the evidence useless."""
    assert redact("member 12345 has 3 accounts") == "member 12345 has 3 accounts"


def test_a_declared_value_that_is_too_short_is_ignored() -> None:
    assert redact("status 1", {"code": "1"}) == "status 1"


def test_the_filter_scrubs_the_message_the_args_and_the_extras() -> None:
    log_filter = RedactionFilter()
    log_filter.declare("member_ssn", "123-45-6789")
    record = _record(
        "looked up %s",
        args=("123-45-6789",),
        target_name="Account 9876543210",
        payload={"ssn": "123-45-6789", "ok": True},
    )

    assert log_filter.filter(record) is True
    assert record.args == ("[REDACTED:member_ssn]",)
    assert record.__dict__["target_name"] == "Account [REDACTED:account]"
    assert record.__dict__["payload"] == {"ssn": "[REDACTED:member_ssn]", "ok": True}
    assert "123-45-6789" not in record.getMessage()


def test_forget_drops_declared_values_when_a_run_ends() -> None:
    log_filter = RedactionFilter({"pin": "998877"})
    log_filter.forget()
    assert redact("pin 998877", {}) == "pin 998877"
    record = _record("pin 998877")
    log_filter.filter(record)
    assert record.msg == "pin 998877"


def test_reserved_record_attributes_are_left_alone() -> None:
    """Scrubbing levelname or pathname would corrupt the record itself."""
    log_filter = RedactionFilter({"path": __file__})
    record = _record("hello")
    log_filter.filter(record)
    assert record.pathname == __file__
    assert record.levelname == "INFO"


# ---- the committed capability that exercises this end to end -----------------------------


def test_the_verify_capability_declares_its_code_sensitive_and_carries_no_example() -> None:
    """The artifact this project ships to prove redaction does something.

    `member.verify` is the only capability with a sensitive parameter, which is what makes
    the filter reachable by an evidence run rather than only by these unit tests.
    """
    from cua.replay.engine import load_capability

    capability = load_capability("member.verify", REPO / "capabilities")
    code = next(p for p in capability.parameters if p.name == "code")

    assert code.sensitive is True
    assert code.example is None, "an example of a real code is a real code"


def test_the_verify_capability_references_its_code_rather_than_storing_it() -> None:
    """A sensitive value reaches a step as a reference. The artifact never holds the value."""
    from cua.replay.engine import load_capability
    from cua.schema.models import ParamRef

    capability = load_capability("member.verify", REPO / "capabilities")
    typed = next(s for s in capability.steps if s.action == "type")

    assert isinstance(typed.value, ParamRef)
    assert typed.value.param == "code"
    assert "QX7-4412" not in capability.model_dump_json(), "no code literal anywhere"


def test_a_declared_value_is_scrubbed_out_of_a_url_it_leaked_into() -> None:
    """The verify screen puts the code in a query string on purpose.

    A secret that never reaches a logged field would demonstrate nothing; this one lands in
    the observation url, so the filter has to catch it there.
    """
    leaked = "http://127.0.0.1:8099/tenant-a/members/12345/verified?code=QX7-4412"

    assert redact(leaked, {"code": "QX7-4412"}).endswith("?code=[REDACTED:code]")


# ---- the two paths that used to escape the filter entirely ---------------------------------


def test_a_declared_value_inside_a_pydantic_model_is_scrubbed() -> None:
    """A model reaches a log record whole, and every string inside it went straight through.

    `logger.event("replay_end", result=result)` carries a whole ReplayResult, and
    `FailureDetail.observed` is where a url with a secret in its query string lands. Walking
    only dicts and lists missed all of it.
    """
    from cua.schema.models import FailureDetail

    record = logging.LogRecord("cua", logging.INFO, "x", 1, "m", None, None)
    record.detail = FailureDetail(
        step_id="submit",
        expected="the result screen",
        observed="checkpoint not met at http://host/verified?code=QX7-4412",
    )

    RedactionFilter({"code": "QX7-4412"}).filter(record)

    assert "QX7-4412" not in record.detail.observed  # type: ignore[attr-defined]
    assert "[REDACTED:code]" in record.detail.observed  # type: ignore[attr-defined]


def test_a_text_artifact_is_scrubbed_on_its_way_to_disk(tmp_path: Path) -> None:
    """Failure evidence is written as bytes and never passes through a log record.

    An `Observation.url` can carry a secret in its query string, and the success path writes
    neither file - so this only ever leaked when a capability with a sensitive parameter
    failed, which is precisely when the evidence matters.
    """
    from cua.evidence.logger import RunLogger

    with RunLogger("redaction-artifact", root=tmp_path) as log:
        log.declare_sensitive("code", "QX7-4412")
        snapshot = log.save_artifact(
            "failure-submit.ax.json",
            b'{"url": "http://host/verified?code=QX7-4412", "tree": null}',
        )

    written = snapshot.read_text(encoding="utf-8")
    assert "QX7-4412" not in written
    assert "[REDACTED:code]" in written


def test_a_screenshot_is_written_untouched(tmp_path: Path) -> None:
    """Pixels cannot be scrubbed by string replacement, which is why they default to off."""
    from cua.evidence.logger import RunLogger

    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    with RunLogger("redaction-png", root=tmp_path) as log:
        log.declare_sensitive("code", "QX7-4412")
        path = log.save_artifact("step-01.png", png)

    assert path.read_bytes() == png
