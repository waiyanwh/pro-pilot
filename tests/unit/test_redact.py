import pytest

from pro_pilot.spoke.redact import redact, redact_text


@pytest.mark.parametrize(
    ("kind", "positive", "negative"),
    [
        (
            "private_key",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIBogIBAAJCAM==\n-----END RSA PRIVATE KEY-----",
            "the private key rotation runbook is in docs/security.md",
        ),
        (
            "jwt",
            "the request included eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkZW1vIn0.sig for auth",
            "not-a-jwt-eyJ-looks-close-but-has-no-dots",
        ),
        (
            "bearer",
            "authorization: Bearer abc.def.ghi",
            "no credentials appear anywhere in this log line",
        ),
        (
            "aws_access_key",
            "AKIAABCDEFGHIJKLMNOP",
            "AKIA is a four letter prefix, not a key on its own",
        ),
        (
            "aws_secret",
            "aws_secret_access_key=abcdefghijklmnopqrstuvwxyz123456",
            "aws secret rotation is scheduled for next week",
        ),
        (
            "kv_secret",
            "password=hunter2",
            "password policy requires 12 characters",
        ),
        (
            "conn_string",
            "postgres://user:pass@example.test/db",
            "https://example.test/docs (no credentials in this URL)",
        ),
        (
            "slack_token",
            "xoxb-" + "-".join(("1" * 12, "2" * 12, "a" * 24)),
            "the slack channel is #demo-cluster-help",
        ),
        (
            "github_token",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD",
            "see github.com/example/pro-pilot for the source",
        ),
    ],
)
def test_redacts_each_pattern_with_dedicated_positive_and_negative_case(
    kind: str, positive: str, negative: str
) -> None:
    redacted, count = redact_text(positive)
    assert count >= 1
    assert f"[REDACTED:{kind}]" in redacted

    clean, clean_count = redact_text(negative)
    assert clean == negative
    assert clean_count == 0


def test_redacts_all_required_secret_patterns() -> None:
    text = (
        "jwt=eyJheader.payload.signature authorization: Bearer abc123 "
        "AKIA1234567890ABCDEF aws_secret=abcdefghijklmnopqrstuvwxyz123456 "
        "password=hunter2 postgres://user:pass@example.test/db "
        "xoxb-123456 ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ "
        "4111 1111 1111 1111"
    )
    redacted, count = redact_text(text)
    assert count == 9
    assert "eyJheader" not in redacted
    assert "hunter2" not in redacted
    assert "4111" not in redacted


def test_negative_secret_like_values_are_not_redacted() -> None:
    value = "password is not an assignment; number 4111 1111 1111 1112"
    redacted, count = redact_text(value)
    assert redacted == value
    assert count == 0


def test_luhn_pan_detection_handles_true_and_false_values() -> None:
    true_value, true_count = redact_text("card=4111111111111111")
    false_value, false_count = redact_text("card=4111111111111112")
    assert "4111111111111111" not in true_value
    assert true_count == 1
    assert false_value.endswith("4111111111111112")
    assert false_count == 0


def test_redaction_precedes_truncation_for_split_jwt() -> None:
    secret = "eyJheader.payload.signature"
    redacted, count = redact_text(f"prefix {secret} suffix")
    truncated = redacted[: len("prefix [REDACTED:jwt]")]
    assert secret not in truncated
    assert count == 1


def test_redacts_nested_result_values() -> None:
    result, count = redact({"lines": ["password=hunter2", {"message": "safe"}]})
    assert result["lines"][0] == "[REDACTED:kv_secret]"
    assert count == 1
