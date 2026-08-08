"""The contract between the side that writes a certificate's queries and the side that admits them.

A certificate carries a fixed set of verification queries as text. The builder
writes that text into the payload; the verifier admits a query only when the text
it reads matches the counterpart it documents, token for token, and rejects the
query as unknown otherwise. The two literals therefore live in different modules
and must say the same thing.

Nothing else in the suite checks the pair. The tests that exercise the verifier
build their payloads from the verifier's own documented text, so they agree with
themselves whatever the builder emits, and the tests that exercise the builder
never present the result to a verifier. A drift in either literal -- a placeholder
in the driver's form rather than the document's, a renamed column, a query added on
one side only -- passes both suites and fails every real verification. These
assertions are the seam.
"""

from __future__ import annotations

from typing import Final

import pytest

from molt.attest.builder import VERIFICATION_TEMPLATES, VerificationTemplate
from molt.attest.verifier import QUERY_TEMPLATES

# The document's positional placeholder. A certificate is evidence an outside
# auditor may run, so its text carries this form rather than the driver's `%s`.
DOCUMENT_PLACEHOLDER: Final[str] = "$1"
DRIVER_PLACEHOLDER: Final[str] = "%s"


def _collapsed(sql: str) -> str:
    """A statement's text with its whitespace collapsed, as the verifier compares it."""
    return " ".join(sql.split())


def test_both_sides_name_the_same_query_set() -> None:
    """Neither side admits a query the other has never heard of.

    A query the builder emits and the verifier does not document is rejected as
    unknown on every certificate. A query the verifier documents and the builder
    never emits is a check that silently never runs.
    """
    written = {template.name for template in VERIFICATION_TEMPLATES}
    admitted = set(QUERY_TEMPLATES)
    assert written == admitted, "the written query set and the admitted query set are one set"


@pytest.mark.parametrize("template", VERIFICATION_TEMPLATES, ids=lambda t: t.name)
def test_written_text_matches_documented_text(template: VerificationTemplate) -> None:
    """The text a certificate carries is the text the verifier admits.

    Compared the way the verifier compares it, on tokens rather than on layout,
    because the same claim wrapped across two lines is the same claim.
    """
    name = template.name
    sql = template.sql
    documented = QUERY_TEMPLATES[name].documented_sql
    assert _collapsed(sql) == _collapsed(documented), (
        f"the certificate text for {name} is the text the verifier documents"
    )


@pytest.mark.parametrize("template", VERIFICATION_TEMPLATES, ids=lambda t: t.name)
def test_written_text_uses_the_documents_placeholder(template: VerificationTemplate) -> None:
    """A certificate's query text is written for a reader, not for the driver.

    The driver's placeholder appearing here would mean the payload had been written
    from an executable literal, which is the confusion this pair of modules exists
    to keep apart.
    """
    sql = template.sql
    assert DOCUMENT_PLACEHOLDER in sql, "the tenant travels as a documented positional parameter"
    assert DRIVER_PLACEHOLDER not in sql, "the driver's placeholder does not reach a document"


@pytest.mark.parametrize("template", VERIFICATION_TEMPLATES, ids=lambda t: t.name)
def test_parameter_count_matches_the_documented_text(template: VerificationTemplate) -> None:
    """The verifier's arity for a query is the arity the documented text shows.

    The verifier rejects a certificate whose parameter list is the wrong length, so
    an arity recorded here that the text does not show would reject every
    certificate the builder writes correctly.
    """
    name = template.name
    documented = QUERY_TEMPLATES[name]
    shown = documented.documented_sql.count(DOCUMENT_PLACEHOLDER)
    assert shown == documented.parameter_count, (
        f"the documented text for {name} shows exactly the parameters the verifier binds"
    )


@pytest.mark.parametrize("template", VERIFICATION_TEMPLATES, ids=lambda t: t.name)
def test_executed_statement_binds_what_the_document_declares(
    template: VerificationTemplate,
) -> None:
    """The statement actually run binds as many values as the document declares.

    The executed literal is the verifier's own and carries the driver's
    placeholders. It is a different string from the documented text by design; what
    must hold across the two is the number of values bound, since the verifier
    passes the certificate's parameter list straight into it.
    """
    name = template.name
    documented = QUERY_TEMPLATES[name]
    assert documented.statement.count(DRIVER_PLACEHOLDER) == documented.parameter_count, (
        f"the executed statement for {name} binds the declared number of values"
    )
