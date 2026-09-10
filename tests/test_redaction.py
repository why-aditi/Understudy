"""Nothing sensitive reaches disk. This filter is the backstop, not the primary control."""

import logging

from cua.policy.redaction import RedactionFilter, redact


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
