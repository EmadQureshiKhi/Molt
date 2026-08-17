"""The served specification is the tracked document, and it carries no memory content.

`GET /spec` is public, so what it returns is what an anonymous caller on the public
internet receives. Two things therefore have to hold. The served body has to be the
tracked document byte for byte, because a document that is edited on the way out is a
second document nothing holds honest. And the document has to describe shapes only: no
stored identifier, no recalled text, no vector, no example lifted from a corpus. A
specification that carried one row would be a memory leak with a schema attached.

The absence path is asserted too. A deployment whose document is missing answers 503
and names no path, because the path is a deployment fact rather than a caller's
concern.

Credential-free and database-free. The console is built directly with a credential of
this module's own and a stand-in store whose call count is asserted to stay at zero,
which is how the claim that this route reads no table is checked rather than trusted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest

from molt.config.secrets import Credential, CredentialSource
from molt.console import auth
from molt.console.app import SPECIFICATION_MEDIA_TYPE, build_app
from molt.console.deps import Console, ConsoleSettings
from molt.console.lambda_adapter import LambdaResponse, invoke
from molt.store.capability import CapabilityRecord

pytestmark: Final[pytest.MarkDecorator] = pytest.mark.spec

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DOCUMENT_PATH: Final[Path] = REPOSITORY_ROOT / "docs" / "interface.json"

CREDENTIAL: Final[str] = "a-specification-suite-credential"
SESSION_KEY: Final[str] = "a-specification-suite-session-key"
# A fixed instant derived from a count of seconds, so this module carries no reading
# of a calendar or of a clock.
NOW: Final[datetime] = datetime.fromtimestamp(1_000_000_000, tz=UTC)

# Keys that would only appear in a body carrying memory content. The document names
# field names, so these are searched for as JSON *values* rather than as words.
CONTENT_VALUE_KEYS: Final[tuple[str, ...]] = (
    "text_body",
    "payload",
    "excerpt",
    "embedding",
    "vector",
    "prompt",
    "completion",
)

# A stored identifier's shape. The document describes the format by name and must
# never carry an instance of it.
IDENTIFIER_SHAPE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


class CountingStore:
    """A store that answers nothing and counts every call made to it."""

    role = "reader"

    def __init__(self) -> None:
        self.calls = 0

    def read(self, body: object) -> object:  # noqa: ARG002
        self.calls += 1
        return None

    def known_capabilities(self) -> CapabilityRecord:
        self.calls += 1
        return CapabilityRecord()


def _console(store: CountingStore, *, spec: Path | None = None) -> Console:
    settings = ConsoleSettings(
        host="127.0.0.1",
        port=8080,
        demo_mode=False,
        interface_spec_path=DOCUMENT_PATH if spec is None else spec,
        template_directory=REPOSITORY_ROOT / "web" / "templates",
        static_directory=REPOSITORY_ROOT / "web" / "static",
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


def _serve(store: CountingStore, *, spec: Path | None = None) -> LambdaResponse:
    """One anonymous request for the specification, through the deployed path."""
    app = build_app(_console(store, spec=spec))
    event: dict[str, object] = {
        "version": "2.0",
        "rawPath": "/spec",
        "rawQueryString": "",
        "headers": {"accept": "application/json"},
        "cookies": [],
        "requestContext": {"http": {"method": "GET", "path": "/spec"}},
        "body": "",
        "isBase64Encoded": False,
    }
    return invoke(cast(Any, app), event)


def _body(answer: LambdaResponse) -> str:
    assert answer.get("isBase64Encoded") is False, "a JSON document is served as text"
    return cast(str, answer["body"])


# -- the served body -------------------------------------------------------


def test_the_served_body_is_the_tracked_document() -> None:
    store = CountingStore()
    answer = _serve(store)
    assert answer["statusCode"] == 200
    assert _body(answer) == DOCUMENT_PATH.read_text(encoding="utf-8")


def test_the_served_body_carries_the_documented_content_type() -> None:
    headers = cast(Mapping[str, str], _serve(CountingStore())["headers"])
    assert headers["content-type"].startswith(SPECIFICATION_MEDIA_TYPE)


def test_serving_the_specification_reads_no_table() -> None:
    store = CountingStore()
    _serve(store)
    assert store.calls == 0, "the specification route opened a call against the store"


def test_an_absent_document_is_an_unavailability_naming_no_path(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    answer = _serve(CountingStore(), spec=missing)
    assert answer["statusCode"] == 503
    body = cast(str, answer["body"])
    assert missing.name not in body
    assert str(tmp_path) not in body


def test_the_served_body_parses_as_the_same_document() -> None:
    served = json.loads(_body(_serve(CountingStore())))
    tracked = json.loads(DOCUMENT_PATH.read_text(encoding="utf-8"))
    assert served == tracked


# -- what the body may not carry -------------------------------------------


def _values(node: object) -> list[str]:
    """Every string value anywhere in the document, keys excluded."""
    found: list[str] = []
    if isinstance(node, dict):
        for value in cast(Mapping[str, Any], node).values():
            found.extend(_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_values(item))
    elif isinstance(node, str):
        found.append(node)
    return found


def test_the_document_carries_no_stored_identifier() -> None:
    text = DOCUMENT_PATH.read_text(encoding="utf-8")
    assert IDENTIFIER_SHAPE.search(text) is None, "the document carries an identifier instance"


def _example_keys(node: object) -> list[str]:
    """Every declared example key anywhere in the document, by position not by spelling.

    The check is structural rather than a substring scan of the rendered document. A
    scan cannot tell an `example` *block* from the word appearing in a description,
    and one of this document's own descriptions states that it carries no example
    drawn from a corpus, so a scan would be tripped by the sentence explaining the
    policy it is enforcing.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("example", "examples"):
                found.append(str(key))
            found.extend(_example_keys(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_example_keys(item))
    return found


def test_the_document_carries_no_example_drawn_from_a_corpus() -> None:
    parsed = json.loads(DOCUMENT_PATH.read_text(encoding="utf-8"))
    assert _example_keys(parsed) == [], "the document declares an example block"


def test_no_content_key_appears_as_a_value() -> None:
    """A field *name* is a shape; a field name quoted as a value would be a row."""
    served = json.loads(_body(_serve(CountingStore())))
    for value in _values(served):
        for key in CONTENT_VALUE_KEYS:
            assert value != key, f"the document carries {key!r} as a value"


def test_the_document_names_no_numeric_row_count_as_a_value() -> None:
    """Counts are described as integers; a literal count would be a reading of a table."""
    served = json.loads(_body(_serve(CountingStore())))
    schemas = cast(Mapping[str, Any], served["components"]["schemas"])
    for name, schema in schemas.items():
        assert "default" not in cast(Mapping[str, Any], schema), name
        assert "const" not in cast(Mapping[str, Any], schema), name
