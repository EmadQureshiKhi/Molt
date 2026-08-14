"""The run detail, redaction comparison, certificate, and live verification views.

Credential-free and database-free. The store is a stand-in answering each statement
of the two view modules with canned rows, and the application is driven through the
Lambda adapter, so the route table, the authentication middleware, and the templates
are all exercised on the deployed path.

The load-bearing assertion of this module is negative: the redaction comparison view
sends only the statements its module declares, none of which projects a body column
from anything but the row as it stands *after* the rewrite, so the view cannot render
a pre-redaction body it has no way to obtain.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeVar, cast

from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import build_app
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.console.routes import certificates, runs
from molt.store.capability import CapabilityRecord

T = TypeVar("T")

CREDENTIAL: Final[str] = "an-operator-credential"
SESSION_KEY: Final[str] = "a-session-signing-key"
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

RUN_ID: Final[str] = "11111111-1111-4111-8111-111111111111"
ARTIFACT_ID: Final[str] = "22222222-2222-4222-8222-222222222222"
RETAINED_ID: Final[str] = "33333333-3333-4333-8333-333333333333"
CHECKPOINT_ID: Final[str] = "44444444-4444-4444-8444-444444444444"

PRE_DIGEST: Final[str] = "a" * 64
POST_DIGEST: Final[str] = "b" * 64
SURVIVING_BODY: Final[str] = "the text that other tenants contributed and that survived"

_RUN_ROW: Final[tuple[object, ...]] = (
    RUN_ID,
    "acme",
    "an-operator",
    False,
    "completed",
    "done",
    NOW,
    NOW,
    0.2,
    0.45,
    0,
    7,
    3,
    NOW,
    NOW,
)

_REDACTED_ROW: Final[tuple[object, ...]] = (
    ARTIFACT_ID,
    "derived_artifact",
    "surgical_redaction",
    "blended_artifact_rewritten",
    "explicit_sweep",
    PRE_DIGEST,
    POST_DIGEST,
    ["acme", "globex"],
    ["globex"],
    4,
    9,
    NOW,
)

_RETAINED_ROW: Final[tuple[object, ...]] = (
    RETAINED_ID,
    "event",
    "retained",
    "binding_already_absent",
    "explicit_sweep",
    PRE_DIGEST,
    None,
    ["globex"],
    ["globex"],
    None,
    None,
    NOW,
)

_BODY_ROW: Final[tuple[object, ...]] = (SURVIVING_BODY, POST_DIGEST, 4, NOW)

_PAYLOAD: Final[Mapping[str, object]] = {
    "ownership": {
        "owner": "an-erasure-worker",
        "fencing_generation": 3,
        "idempotency_key": "a-request-key",
    },
    "ledger_checkpoint": {
        "checkpoint_id": CHECKPOINT_ID,
        "signed_digest": "c" * 64,
    },
    "dispositions": [
        {
            "artifact_id": ARTIFACT_ID,
            "disposition": "surgical_redaction",
            "reason": "blended_artifact_rewritten",
            "first_attributed_at": "an-earlier-instant",
            "first_attribution_method": "capture_hook",
        }
    ],
}

_CERTIFICATE_ROW: Final[tuple[object, ...]] = (
    "55555555-5555-4555-8555-555555555555",
    _PAYLOAD,
    "d" * 64,
    b"a-signature",
    "a-key-identifier",
    "an-algorithm",
    "a-bucket",
    "certificates/acme/a-run.json",
    "a-version",
    "stored",
    None,
    NOW,
)


class StubCursor:
    """A cursor answering each declared statement with canned rows."""

    def __init__(self, answers: Mapping[str, Sequence[tuple[object, ...]]]) -> None:
        self.answers = answers
        self.statements: list[str] = []
        self._rows: list[tuple[object, ...]] = []

    def execute(self, statement: str, parameters: object = None) -> None:
        assert parameters is not None
        self.statements.append(statement)
        self._rows = list(self.answers.get(statement, ()))

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows, self._rows = self._rows, []
        return rows


class StubStore:
    """The one call a view makes on a store, plus the health route's two."""

    role = "reader"

    def __init__(self, answers: Mapping[str, Sequence[tuple[object, ...]]]) -> None:
        self.cursor = StubCursor(answers)

    def read(self, body: Callable[[StubCursor], T]) -> T:
        return body(self.cursor)

    def known_capabilities(self) -> CapabilityRecord:
        return CapabilityRecord()


def _console(store: StubStore) -> Console:
    root = Path(__file__).resolve().parents[2]
    settings = ConsoleSettings(
        host="127.0.0.1",
        port=8080,
        demo_mode=False,
        interface_spec_path=root / "docs" / "interface.json",
        template_directory=root / "web" / "templates",
        static_directory=root / "web" / "static",
    )
    return Console(
        settings=settings,
        store=cast(Any, store),
        credential=Credential(
            auth.credential_record(CREDENTIAL, iterations=2),
            source_name="test",
            source=CredentialSource.ENVIRONMENT,
        ),
        session_key=Credential(
            SESSION_KEY, source_name="test", source=CredentialSource.ENVIRONMENT
        ),
        clock=lambda: NOW,
    )


def _serve(
    method: str,
    path: str,
    answers: Mapping[str, Sequence[tuple[object, ...]]],
) -> tuple[LambdaResponse, StubStore]:
    store = StubStore(answers)
    app = build_app(_console(store))
    session, cookie = auth.issue(SESSION_KEY, now=NOW)
    event = {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {
            "accept": "text/html",
            "x-csrf-token": session.csrf_token,
        },
        "cookies": [f"{auth.COOKIE_NAME}={cookie}"],
        "requestContext": {"http": {"method": method, "path": path}},
        "body": "",
        "isBase64Encoded": False,
    }
    return (invoke(cast(Any, app), event), store)


_RUN_ANSWERS: Final[dict[str, Sequence[tuple[object, ...]]]] = {
    runs.RUN_QUERY: (_RUN_ROW,),
    runs.DISPOSITIONS_QUERY: (_REDACTED_ROW, _RETAINED_ROW),
    runs.REDACTION_QUERY: (_REDACTED_ROW,),
    runs.POST_REDACTION_BODY_QUERY: (_BODY_ROW,),
}

_CERTIFICATE_ANSWERS: Final[dict[str, Sequence[tuple[object, ...]]]] = {
    certificates.CERTIFICATE_QUERY: (_CERTIFICATE_ROW,)
}


# -- the run detail view ---------------------------------------------------


def test_the_run_detail_view_renders_every_disposition_with_its_evidence() -> None:
    answer, _ = _serve("GET", f"/erase/{RUN_ID}", _RUN_ANSWERS)
    assert answer["statusCode"] == 200
    body = cast(str, answer["body"])
    assert "acme" in body
    assert ARTIFACT_ID in body
    assert RETAINED_ID in body
    assert "surgical_redaction" in body
    assert PRE_DIGEST in body
    assert POST_DIGEST in body
    assert "<caption>" in body
    assert 'scope="col"' in body


def test_an_absent_segment_count_renders_as_inapplicable_rather_than_zero() -> None:
    answer, _ = _serve("GET", f"/erase/{RUN_ID}", _RUN_ANSWERS)
    body = cast(str, answer["body"])
    assert "not applicable: no rewrite" in body


def test_a_run_the_table_holds_no_row_for_is_a_refusal() -> None:
    answer, _ = _serve("GET", f"/erase/{RUN_ID}", {runs.RUN_QUERY: ()})
    assert answer["statusCode"] == 404


# -- the redaction comparison view ----------------------------------------


def test_the_comparison_renders_both_digests_both_binding_sets_and_both_counts() -> None:
    answer, _ = _serve("GET", f"/erase/{RUN_ID}/redactions/{ARTIFACT_ID}", _RUN_ANSWERS)
    assert answer["statusCode"] == 200
    body = cast(str, answer["body"])
    assert PRE_DIGEST in body
    assert POST_DIGEST in body
    assert "globex" in body
    assert "4" in body
    assert "9" in body
    assert SURVIVING_BODY in body


def test_the_comparison_states_that_the_original_text_was_not_retained() -> None:
    answer, _ = _serve("GET", f"/erase/{RUN_ID}/redactions/{ARTIFACT_ID}", _RUN_ANSWERS)
    body = cast(str, answer["body"])
    assert "The original text was not retained" in body
    assert "not retained, by design" in body


def test_the_comparison_sends_only_its_declared_statements_and_reads_no_prior_body() -> None:
    _, store = _serve("GET", f"/erase/{RUN_ID}/redactions/{ARTIFACT_ID}", _RUN_ANSWERS)
    sent = store.cursor.statements
    assert sent == [runs.REDACTION_QUERY, runs.POST_REDACTION_BODY_QUERY]
    # The disposition projection carries digests and counts and no body column, and
    # the only body read is of the row as it stands now, which is the post-redaction
    # body. Neither statement can name a moment in the past, so no historical read
    # can resurrect the destroyed text.
    assert "body" not in runs.REDACTION_QUERY
    assert "AS OF SYSTEM TIME" not in runs.POST_REDACTION_BODY_QUERY
    for statement in sent:
        assert "AS OF SYSTEM TIME" not in statement
        assert "%s" in statement


def test_a_disposition_that_performed_no_rewrite_says_so() -> None:
    answers = dict(_RUN_ANSWERS) | {runs.REDACTION_QUERY: (_RETAINED_ROW,)}
    answer, _ = _serve("GET", f"/erase/{RUN_ID}/redactions/{RETAINED_ID}", answers)
    assert answer["statusCode"] == 200
    body = cast(str, answer["body"])
    assert "rather than a surgical" in body


# -- the certificate display ----------------------------------------------


def test_the_certificate_display_shows_the_generation_checkpoint_and_attribution() -> None:
    answer, _ = _serve("GET", f"/certificates/{RUN_ID}", _CERTIFICATE_ANSWERS)
    assert answer["statusCode"] == 200
    body = cast(str, answer["body"])
    assert "Ownership generation the finalising owner held" in body
    assert CHECKPOINT_ID in body
    assert "First attribution method" in body
    assert "capture_hook" in body


def test_a_run_holding_no_certificate_is_a_refusal() -> None:
    answer, _ = _serve("GET", f"/certificates/{RUN_ID}", {certificates.CERTIFICATE_QUERY: ()})
    assert answer["statusCode"] == 404


def test_the_display_offers_a_labelled_verification_control() -> None:
    answer, _ = _serve("GET", f"/certificates/{RUN_ID}", _CERTIFICATE_ANSWERS)
    body = cast(str, answer["body"])
    assert "Verify this certificate now" in body
    assert f'action="/certificates/{RUN_ID}/verify"' in body


# -- live verification with no key service --------------------------------


def test_an_absent_key_service_is_an_operational_report_and_not_a_failed_outcome() -> None:
    answer, _ = _serve("POST", f"/certificates/{RUN_ID}/verify", _CERTIFICATE_ANSWERS)
    assert answer["statusCode"] == 503
    body = cast(str, answer["body"])
    assert certificates.KEY_SERVICE_COMPONENT in body
    assert "not a verification failure" in body
    assert "Outcome: <strong>failed" not in body


def test_the_unattempted_block_makes_no_claim_about_the_certificate() -> None:
    block = certificates._unattempted_block(certificates.KEY_SERVICE_COMPONENT)
    assert block["attempted"] is False
    assert block["outcome"] == certificates.OUTCOME_UNATTEMPTED
    assert block["verified"] is None
