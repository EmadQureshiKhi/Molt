"""Generators shared by the redaction properties.

This module holds `payloads()`, the generator both redaction properties draw
from: recursive JSON documents whose keys mix neutral names with
sensitive-shaped names and whose string values mix ordinary text, one shape per
recognised secret class, already-replaced spans, and concatenations of those.

Two decisions in here are load-bearing.

**Every secret shape is assembled from its alphabet at generation time.** No
span of any recognised shape is written as a literal anywhere below. A literal
plausible credential in a tracked file is indistinguishable, to anything
scanning the tree, from a real leak, so each shape is stated as its character
classes and its fixed lengths and built inside a strategy. The armour lines of
the private-key shape are held as separate fragments for the same reason: the
opening fragment, the closing fragment, and the shared tail never appear as one
span in this source, so this file holds no armoured block.

**Already-replaced spans are generated deliberately.** The placeholder belongs
to no alphabet a value shape admits, and the branches that keep part of a span
guard against it explicitly. A generator that only ever produced fresh secrets
would never reach those guards, and a payload that has already been through
redaction is the ordinary case on a re-processing path, so the placeholder is
drawn as a value in its own right and as a segment inside longer text.

Depth is a parameter rather than a constant. The default of six levels sits far
below the recursion bound, so an example exercises replacement rather than
truncation; passing a depth above the bound is what a property about the depth
cut wants instead.
"""

from __future__ import annotations

import string
from typing import Final

from hypothesis import strategies as st

from molt.models.event import JsonObject, JsonValue
from molt.redact import REDACTION_PLACEHOLDER
from molt.redact.patterns import BUILTIN_SENSITIVE_NAMES

__all__ = [
    "DEFAULT_PAYLOAD_DEPTH",
    "access_key_identifiers",
    "bearer_credentials",
    "connection_string_credentials",
    "credential_assignments",
    "json_values",
    "neutral_strings",
    "payload_keys",
    "payloads",
    "private_key_blocks",
    "redacted_spans",
    "secret_shaped_strings",
    "strings",
]

# The nesting the default payload reaches, counting the payload mapping itself
# as the first level. Six is what the redaction properties use, and it is well
# inside the recursion bound so no example is truncated.
DEFAULT_PAYLOAD_DEPTH: Final[int] = 6

# --------------------------------------------------------------------------
# Alphabets
# --------------------------------------------------------------------------

_UPPER_ALNUM: Final[str] = string.ascii_uppercase + string.digits
_BASE64: Final[str] = string.ascii_letters + string.digits + "/+="
_UNRESERVED: Final[str] = string.ascii_letters + string.digits + "-._~+/"
_AUTHORITY_NAME: Final[str] = string.ascii_letters + string.digits + "-_."
_AUTHORITY_CREDENTIAL: Final[str] = string.ascii_letters + string.digits + "-_.!$%*"

# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

# Ordinary field names, including several deliberate near misses: a name that
# merely contains a sensitive word inside a longer word is not a credential, and
# whole-word matching is what keeps those values intact. Those near misses are
# here so an example can distinguish the two.
_NEUTRAL_KEY_NAMES: Final[tuple[str, ...]] = (
    "path",
    "command",
    "exit_code",
    "note",
    "author",
    "authoring_tool",
    "tokenizer",
    "passwords_policy",
    "secretariat",
    "duration_ms",
    "lines",
    "payload",
    "kind",
    "nested",
)

# Decorations applied to a sensitive word. Each of these leaves the word intact
# as a separated word, in medial capitals, or behind a punctuation run, which is
# the whole point: the same name written five ways is one name.
_KEY_PREFIXES: Final[tuple[str, ...]] = ("", "db_", "client", "x-", "APP_", "primary.")
_KEY_SUFFIXES: Final[tuple[str, ...]] = ("", "_value", "Field", "_2", "-header")


def _decorate(prefix: str, word: str, suffix: str) -> str:
    """Join a decoration around a sensitive word without losing the word."""
    return f"{prefix}{word}{suffix}"


def payload_keys() -> st.SearchStrategy[str]:
    """Draw a mapping key, neutral or sensitive-shaped."""
    sensitive = st.builds(
        _decorate,
        st.sampled_from(_KEY_PREFIXES),
        st.sampled_from(sorted(BUILTIN_SENSITIVE_NAMES)),
        st.sampled_from(_KEY_SUFFIXES),
    )
    return st.one_of(st.sampled_from(_NEUTRAL_KEY_NAMES), sensitive)


# --------------------------------------------------------------------------
# Class 1: access key identifier
# --------------------------------------------------------------------------

# The four-letter resource-type prefixes the recogniser admits. A prefix alone
# is no credential shape; the sixteen further characters that make it one are
# drawn from the uppercase alphanumeric alphabet at generation time.
_ACCESS_KEY_PREFIXES: Final[tuple[str, ...]] = (
    "ABIA",
    "ACCA",
    "AGPA",
    "AIDA",
    "AIPA",
    "AKIA",
    "ANPA",
    "ANVA",
    "APKA",
    "AROA",
    "ASCA",
    "ASIA",
)


def _run(alphabet: str, length: int) -> st.SearchStrategy[str]:
    """Draw a run of exactly the given length from the given alphabet."""
    return st.text(alphabet=alphabet, min_size=length, max_size=length)


def _joined(parts: tuple[str, ...]) -> str:
    """Concatenate drawn fragments into one span."""
    return "".join(parts)


def access_key_identifiers() -> st.SearchStrategy[str]:
    """Draw a prefix followed by sixteen uppercase alphanumeric characters."""
    return st.tuples(st.sampled_from(_ACCESS_KEY_PREFIXES), _run(_UPPER_ALNUM, 16)).map(_joined)


# --------------------------------------------------------------------------
# Class 2: long credential in assignment context
# --------------------------------------------------------------------------

# The spellings of the provider's secret-key name the recogniser admits, with
# the separator absent, an underscore, a hyphen, or a blank, in either case.
_CREDENTIAL_NAME_SPELLINGS: Final[tuple[str, ...]] = (
    "aws_secret_access_key",
    "AWS_SECRET_ACCESS_KEY",
    "aws-secret-access-key",
    "Aws Secret Access Key",
    "awssecretaccesskey",
)

# A name-to-value separator and the closing quote that separator opened, if any.
# The closing quote matters: the value is recognised as exactly forty characters
# followed by a character outside the alphabet, and a quote is such a character.
_ASSIGNMENT_FORMS: Final[tuple[tuple[str, str], ...]] = (
    ("=", ""),
    (" = ", ""),
    (": ", ""),
    ('="', '"'),
    (": '", "'"),
)


def _prefixed_value(prefix: str, form: tuple[str, str], value: str) -> str:
    """Render a kept prefix, its opening punctuation, a value, and any closer."""
    opening, closing = form
    return f"{prefix}{opening}{value}{closing}"


def credential_assignments() -> st.SearchStrategy[str]:
    """Draw a provider secret-key name assigned a forty-character value."""
    return st.builds(
        _prefixed_value,
        st.sampled_from(_CREDENTIAL_NAME_SPELLINGS),
        st.sampled_from(_ASSIGNMENT_FORMS),
        _run(_BASE64, 40),
    )


# --------------------------------------------------------------------------
# Class 3: private key block
# --------------------------------------------------------------------------

# The armour fragments, held apart so no armoured block appears in this source.
# The opening fragment, the closing fragment, and the tail they share are joined
# only inside the strategy below.
_ARMOUR_OPEN: Final[str] = "-----BEGIN"
_ARMOUR_CLOSE: Final[str] = "-----END"
_ARMOUR_TAIL: Final[str] = "PRIVATE KEY-----"

# The uppercase label between the keyword and the tail, blanks included.
_ARMOUR_LABELS: Final[tuple[str, ...]] = (" ", " RSA ", " EC ", " OPENSSH ", " ENCRYPTED ")


def _armoured(label: str, body: str) -> str:
    """Render an opening armour line, a body, and a closing armour line."""
    return f"{_ARMOUR_OPEN}{label}{_ARMOUR_TAIL}{body}{_ARMOUR_CLOSE}{label}{_ARMOUR_TAIL}"


def private_key_blocks() -> st.SearchStrategy[str]:
    """Draw an armoured block with a generated body."""
    return st.builds(
        _armoured,
        st.sampled_from(_ARMOUR_LABELS),
        st.text(alphabet=_BASE64 + "\n", min_size=0, max_size=80),
    )


# --------------------------------------------------------------------------
# Class 4: credential following the bearer keyword
# --------------------------------------------------------------------------

_BEARER_KEYWORDS: Final[tuple[str, ...]] = ("Bearer ", "bearer ", "BEARER  ", "bearer\t")


def bearer_credentials() -> st.SearchStrategy[str]:
    """Draw the keyword followed by a long run of unreserved characters."""
    return st.builds(
        _prefixed_value,
        st.sampled_from(_BEARER_KEYWORDS),
        st.sampled_from((("", ""), ("", "=="), ('"', '"'))),
        st.text(alphabet=_UNRESERVED, min_size=16, max_size=48),
    )


# --------------------------------------------------------------------------
# Class 5: credentials embedded in a connection string
# --------------------------------------------------------------------------

_SCHEMES: Final[tuple[str, ...]] = ("postgres", "postgresql", "mysql", "mongodb", "redis", "amqp")

# Single-label authorities only. A dotted authority written as a literal beside
# an authority separator would read as a contact address to a shape scanner, and
# the host is no part of what this class is about.
_AUTHORITIES: Final[tuple[str, ...]] = ("localhost", "cluster-internal", "cache-node")
_PATHS: Final[tuple[str, ...]] = ("", "/molt", "/molt?sslmode=verify-full")

# The scheme separator, held as its own fragment. Written inline it would put a
# separator, a name, a colon, a value, and an authority marker into one span of
# this source, which is the shape this class recognises; a scanner reading the
# tree cannot tell such a span from a real one.
_SCHEME_SEPARATOR: Final[str] = "://"


def _connection_string(scheme: str, user: str, credential: str, host: str, path: str) -> str:
    """Render a scheme, credentialed user information, an authority, and a path."""
    return f"{scheme}{_SCHEME_SEPARATOR}{user}:{credential}@{host}{path}"


def connection_string_credentials() -> st.SearchStrategy[str]:
    """Draw a connection string carrying user information."""
    return st.builds(
        _connection_string,
        st.sampled_from(_SCHEMES),
        st.text(alphabet=_AUTHORITY_NAME, min_size=1, max_size=12),
        st.text(alphabet=_AUTHORITY_CREDENTIAL, min_size=1, max_size=24),
        st.sampled_from(_AUTHORITIES),
        st.sampled_from(_PATHS),
    )


# --------------------------------------------------------------------------
# Already-replaced spans
# --------------------------------------------------------------------------

# The forms redaction itself produces, plus the placeholder standing alone and
# the placeholder sitting inside ordinary text. Each of these is what a payload
# looks like on a second pass, which is the case the guards in the recognisers
# exist for.
_REPLACED_FORMS: Final[tuple[str, ...]] = (
    REDACTION_PLACEHOLDER,
    f"Bearer {REDACTION_PLACEHOLDER}",
    f"aws_secret_access_key={REDACTION_PLACEHOLDER}",
    f"postgres://{REDACTION_PLACEHOLDER}@localhost/molt",
    f"the removed value was {REDACTION_PLACEHOLDER} and the rest stands",
    f"{REDACTION_PLACEHOLDER}{REDACTION_PLACEHOLDER}",
)


def _partly_replaced(credential: str, host: str) -> str:
    """Render user information whose name is replaced and whose value is not.

    This is the boundary the connection-string guard is stated against: the
    guard admits a replaced whole user information span and no other, so a span
    shaped like this one is replaced again and must then settle.
    """
    return f"postgres{_SCHEME_SEPARATOR}{REDACTION_PLACEHOLDER}:{credential}@{host}/molt"


def redacted_spans() -> st.SearchStrategy[str]:
    """Draw a span that has already been through redaction."""
    return st.one_of(
        st.sampled_from(_REPLACED_FORMS),
        st.builds(
            _partly_replaced,
            st.text(alphabet=_AUTHORITY_CREDENTIAL, min_size=1, max_size=16),
            st.sampled_from(_AUTHORITIES),
        ),
    )


# --------------------------------------------------------------------------
# Ordinary strings
# --------------------------------------------------------------------------

_NEUTRAL_TEXTS: Final[tuple[str, ...]] = (
    "",
    " ",
    "src/molt/redact/__init__.py",
    "git status --porcelain",
    "the run completed with no findings",
    "def redact(value): return value",
    "exit status 0",
    "Bearer word",
    "authorization required",
    "caf\u00e9 n\u00e9e \u00fcn\u00efcode",
    "-----",
)


def neutral_strings() -> st.SearchStrategy[str]:
    """Draw a string carrying no secret shape, including adversarial characters."""
    return st.one_of(st.sampled_from(_NEUTRAL_TEXTS), st.text(max_size=32))


def secret_shaped_strings() -> st.SearchStrategy[str]:
    """Draw one span of one of the five value shapes, one class per branch."""
    return st.one_of(
        access_key_identifiers(),
        credential_assignments(),
        private_key_blocks(),
        bearer_credentials(),
        connection_string_credentials(),
    )


# The separators between concatenated segments. The empty separator is included
# deliberately: captured text runs tokens together, and a shape abutting its
# neighbour is what makes a replacement's own boundary observable.
_SEGMENT_JOINERS: Final[tuple[str, ...]] = ("", " ", "\n", ", ", "; ")


def _joined_with(segments: list[str], joiner: str) -> str:
    """Concatenate drawn segments with one separator between them."""
    return joiner.join(segments)


def strings() -> st.SearchStrategy[str]:
    """Draw a string value: ordinary, one shape, already replaced, or a mixture."""
    segment = st.one_of(neutral_strings(), secret_shaped_strings(), redacted_spans())
    mixture = st.builds(
        _joined_with,
        st.lists(segment, min_size=2, max_size=3),
        st.sampled_from(_SEGMENT_JOINERS),
    )
    return st.one_of(neutral_strings(), secret_shaped_strings(), redacted_spans(), mixture)


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


def _leaves() -> st.SearchStrategy[JsonValue]:
    """Draw a scalar: a string, a real, an integer, a boolean, or an absent value."""
    return st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**53), max_value=2**53),
        st.floats(allow_nan=False, allow_infinity=False),
        strings(),
    )


def json_values(*, depth: int) -> st.SearchStrategy[JsonValue]:
    """Draw a JSON value nesting no deeper than the given number of levels.

    A depth of one or less admits scalars only, so the recursion terminates at a
    stated level rather than at a leaf count. Levels are built once each, not
    once per branch, so a large depth costs a linear number of strategies.
    """
    if depth <= 1:
        return _leaves()
    child = json_values(depth=depth - 1)
    return st.one_of(
        _leaves(),
        st.lists(child, max_size=3),
        st.dictionaries(payload_keys(), child, max_size=3),
    )


def payloads(depth: int = DEFAULT_PAYLOAD_DEPTH) -> st.SearchStrategy[JsonObject]:
    """Draw an Event payload: a mapping nesting to the given number of levels.

    The mapping itself counts as the first level, so the default of six admits
    five levels of nesting beneath it. A caller wanting to reach the recursion
    bound passes a depth above that bound instead.
    """
    if depth < 1:
        raise ValueError("a payload needs at least the level the mapping itself occupies")
    return st.dictionaries(payload_keys(), json_values(depth=depth - 1), max_size=4)
