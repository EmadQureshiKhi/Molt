"""The compiled shape recognisers the Redactor applies to captured strings.

Six classes of secret material are recognised. Five of them are shapes carried
by a string value itself, and those five are compiled into one alternation, so
a string is scanned once rather than once per class. That single pass is what
keeps a large payload inside its latency bound: a loop over five expressions
would walk the same bytes five times. The sixth class is a property of the key
a value sits under rather than of the value, so it is compiled separately and
matched against key names.

The five value shapes, stated in words:

1. **Access key identifier.** One of a fixed set of four-letter uppercase
   resource-type prefixes followed by a further sixteen characters of the
   uppercase alphanumeric alphabet, bounded on both sides so that a longer run
   of the same alphabet is not a partial match. The whole span is replaced.
2. **Long credential in assignment context.** A name spelling out the cloud
   provider's secret access key, a separator, then a value of exactly forty
   characters drawn from the base64 alphabet. The name and separator are kept
   and only the value is replaced, so the record still says what was removed.
3. **Private key block.** The opening armour line, the body, and the closing
   armour line, replaced as one span. The body repetition is bounded and lazy,
   so an unterminated block cannot drive the scan quadratic.
4. **Credential following the bearer keyword.** The keyword is kept and the
   long unreserved-character run that follows it is replaced.
5. **Credentials embedded in a connection string.** The scheme separator is
   kept, the user information ahead of the authority separator is replaced,
   and the host, port, and path are left alone, because those are not
   credentials and removing them would cost the record its meaning.

No example of any of these shapes appears in this file. Each shape is stated in
words and expressed as the character classes below, because a plausible example
would be indistinguishable from a real leak to anything scanning this source.

The sixth class matches on the key. A key is normalised by splitting it at
case transitions, folding it to lower case, and collapsing every run of
non-alphanumeric characters to a single separator; the key is sensitive when
any configured name occurs in the normalised form as a whole separated word.
Whole-word matching rather than substring matching is deliberate: a name that
merely contains a sensitive word, such as a field naming an author, is not a
credential, and redacting it would cost the record content it is entitled to
keep.

Idempotence is a property of the expressions rather than of the caller. The
replacement placeholder is not a member of any alphabet a value branch admits,
and the branches whose replacement lands in a position a later scan revisits
carry an explicit guard against the placeholder, so a second pass over an
already-replaced string finds nothing left to replace.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Final

__all__ = [
    "BUILTIN_SENSITIVE_KEY_PATTERN",
    "BUILTIN_SENSITIVE_NAMES",
    "PATTERN_CLASS_NAMES",
    "REDACTION_PLACEHOLDER",
    "VALUE_SHAPE_PATTERN",
    "is_sensitive_key",
    "normalise_key",
    "sensitive_key_pattern",
    "substitute_value_shapes",
]

# The one replacement span. It is fixed rather than configurable so that every
# consumer of a redacted payload recognises a removal without being told what
# the producer's configuration was.
REDACTION_PLACEHOLDER: Final[str] = "[MOLT_REDACTED]"

# The six classes, named so a test can state which class it exercises without
# restating an expression.
PATTERN_CLASS_NAMES: Final[tuple[str, ...]] = (
    "access_key_identifier",
    "long_credential_assignment",
    "private_key_block",
    "bearer_credential",
    "connection_string_credential",
    "sensitive_sibling_key",
)

# --------------------------------------------------------------------------
# Shared fragments
# --------------------------------------------------------------------------

# A guard refusing a match that would begin at an existing placeholder. It is
# derived from the placeholder rather than spelled again, so the two cannot
# drift apart.
_PLACEHOLDER_GUARD: Final[str] = f"(?!{re.escape(REDACTION_PLACEHOLDER)})"

# The same refusal applied to the trailing edge of a match. A fixed-length value
# shape needs both edges guarded, not just the leading one: replacing a span that
# followed a longer run shortens that run, and a run shortened to exactly the
# fixed length would then match on a second pass a shape it did not match on the
# first. Refusing a run that sits immediately against a placeholder is what makes
# the two passes agree, because such a run is the remnant of a longer run that
# the fixed length already declined rather than a value shape in its own right.
_TRAILING_PLACEHOLDER_GUARD: Final[str] = f"(?!{re.escape(REDACTION_PLACEHOLDER)})"

# Either quote character, admitted around a value in an assignment.
_QUOTE: Final[str] = "['\"]"

# A name-to-value separator: an optional run of blanks, one of the two
# assignment characters, another optional run of blanks, and an optional quote.
_ASSIGNMENT: Final[str] = rf"[ \t]{{0,4}}[:=][ \t]{{0,4}}{_QUOTE}?"

# --------------------------------------------------------------------------
# Class 1: access key identifier
# --------------------------------------------------------------------------

# The resource-type prefixes the provider assigns to key identifiers. They are
# held as separate short strings and joined at import time, so this file holds
# no span of the shape it recognises.
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

_ACCESS_KEY_IDENTIFIER: Final[str] = (
    r"(?<![A-Z0-9])(?:" + "|".join(_ACCESS_KEY_PREFIXES) + r")[A-Z0-9]{16}(?![A-Z0-9])"
)

# --------------------------------------------------------------------------
# Class 2: long credential in assignment context
# --------------------------------------------------------------------------

_CREDENTIAL_NAME: Final[str] = r"(?i:aws[_\- ]?secret[_\- ]?access[_\- ]?key)"

# Exactly forty characters of the base64 alphabet, bounded at both edges so a
# longer run is not a partial match. The alphabet bound alone is insufficient on
# the trailing edge: the placeholder opens with a character outside the alphabet,
# so a longer run whose tail has already been replaced would satisfy an
# alphabet-only bound at exactly forty characters. Both bounds together mean a
# run this branch declines on one pass it declines on every pass.
_LONG_BASE64_VALUE: Final[str] = (
    rf"[A-Za-z0-9/+=]{{40}}(?![A-Za-z0-9/+=]){_TRAILING_PLACEHOLDER_GUARD}"
)

_LONG_CREDENTIAL_ASSIGNMENT: Final[str] = (
    rf"(?P<keep_credential_name>{_CREDENTIAL_NAME}{_ASSIGNMENT})"
    rf"{_PLACEHOLDER_GUARD}{_LONG_BASE64_VALUE}"
)

# --------------------------------------------------------------------------
# Class 3: private key block
# --------------------------------------------------------------------------

# Bounded and lazy: the engine walks forward to the closing armour line at most
# once per opening line, so an unterminated block costs one linear pass rather
# than a quadratic one.
_ARMOUR_BODY: Final[str] = r"[\s\S]{0,65536}?"

_PRIVATE_KEY_BLOCK: Final[str] = (
    r"-----BEGIN[A-Z ]{0,40}PRIVATE KEY-----"
    + _ARMOUR_BODY
    + r"-----END[A-Z ]{0,40}PRIVATE KEY-----"
)

# --------------------------------------------------------------------------
# Class 4: credential following the bearer keyword
# --------------------------------------------------------------------------

_BEARER_KEYWORD: Final[str] = r"(?i:bearer)[ \t]+"

# The unreserved and base64url alphabets, long enough that an ordinary English
# word following the keyword is not mistaken for a credential.
_BEARER_VALUE: Final[str] = r"[A-Za-z0-9\-._~+/]{16,}={0,2}"

_BEARER_CREDENTIAL: Final[str] = (
    rf"(?P<keep_bearer>{_BEARER_KEYWORD}{_QUOTE}?){_PLACEHOLDER_GUARD}{_BEARER_VALUE}"
)

# --------------------------------------------------------------------------
# Class 5: credentials embedded in a connection string
# --------------------------------------------------------------------------

# The branch is anchored at the scheme separator rather than at the scheme
# name. Anchoring at a literal pair of characters is what lets the engine skip
# most of a large payload, and the scheme itself is outside the match, so it
# survives into the redacted string along with the host and the path.
_AUTHORITY_USER: Final[str] = r"[^\s/?#@:]{1,128}"
_AUTHORITY_CREDENTIAL: Final[str] = r"[^\s/?#@]{0,256}"

_REDACTED_USER_GUARD: Final[str] = f"(?!{re.escape(REDACTION_PLACEHOLDER)}@)"

_CONNECTION_STRING_CREDENTIAL: Final[str] = (
    rf"(?P<keep_scheme_separator>://){_REDACTED_USER_GUARD}"
    rf"{_AUTHORITY_USER}:{_AUTHORITY_CREDENTIAL}(?=@)"
)

# --------------------------------------------------------------------------
# The single alternation
# --------------------------------------------------------------------------

# Ordered most specific first, because alternation is leftmost-first: the
# armoured block is tried ahead of the shapes its body could otherwise be read
# as. Every branch begins with a literal or a narrow class, which is what lets
# the engine reject most positions of a large payload on one character.
VALUE_SHAPE_PATTERN: Final[re.Pattern[str]] = re.compile(
    "|".join(
        (
            _PRIVATE_KEY_BLOCK,
            _ACCESS_KEY_IDENTIFIER,
            _LONG_CREDENTIAL_ASSIGNMENT,
            _BEARER_CREDENTIAL,
            _CONNECTION_STRING_CREDENTIAL,
        )
    )
)

# The groups whose content is context rather than credential. At most one
# participates in any single match, and its content is carried through ahead of
# the placeholder.
_KEEP_GROUP_NAMES: Final[tuple[str, ...]] = (
    "keep_credential_name",
    "keep_bearer",
    "keep_scheme_separator",
)


def _replacement(match: re.Match[str]) -> str:
    """Render one match as its kept context followed by the placeholder."""
    for name in _KEEP_GROUP_NAMES:
        kept: str | None = match.group(name)
        if kept is not None:
            return kept + REDACTION_PLACEHOLDER
    return REDACTION_PLACEHOLDER


def substitute_value_shapes(text: str) -> str:
    """Replace every recognised value shape in one pass over the string.

    The result is returned even when nothing matched, so the caller decides
    whether a change occurred by comparing values rather than by trusting that
    a match implies a difference. Those two are not the same: a branch can fire
    on a span that already holds the placeholder and produce identical text.
    """
    return VALUE_SHAPE_PATTERN.sub(_replacement, text)


# --------------------------------------------------------------------------
# Class 6: sensitive key names
# --------------------------------------------------------------------------

# The names recognised without any operator configuration. Configured names
# extend this set rather than replacing it, so no configuration can narrow the
# floor. Names composed with a word already present are omitted: whole-word
# matching already recognises them.
BUILTIN_SENSITIVE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "dsn",
        "encryption_key",
        "passphrase",
        "passwd",
        "password",
        "private_key",
        "pwd",
        "secret",
        "session_key",
        "signing_key",
        "ssh_key",
        "token",
    }
)

_CASE_TRANSITION: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NAME_SEPARATOR_RUN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")
_MATCHES_NOTHING: Final[re.Pattern[str]] = re.compile(r"(?!)")


def normalise_key(key: str) -> str:
    """Reduce a key to lower-case words joined by a single separator.

    Case transitions become separators before folding, so a key written in
    medial capitals reduces to the same words as the same key written with
    explicit separators.
    """
    return _NAME_SEPARATOR_RUN.sub("_", _CASE_TRANSITION.sub("_", key).casefold())


def _compile_names(names: frozenset[str]) -> re.Pattern[str]:
    """Compile one alternation matching any name as a whole separated word."""
    words = {normalise_key(name).strip("_") for name in names}
    normalised = sorted(word for word in words if word)
    if not normalised:
        return _MATCHES_NOTHING
    body = "|".join(re.escape(name) for name in normalised)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])")


BUILTIN_SENSITIVE_KEY_PATTERN: Final[re.Pattern[str]] = _compile_names(BUILTIN_SENSITIVE_NAMES)


@lru_cache(maxsize=32)
def sensitive_key_pattern(extra_names: frozenset[str]) -> re.Pattern[str]:
    """Return the key matcher for the built-in set extended by the given names.

    The result is cached because a capture path builds it once per payload and
    compiling an alternation is far dearer than matching one.
    """
    if not extra_names:
        return BUILTIN_SENSITIVE_KEY_PATTERN
    return _compile_names(BUILTIN_SENSITIVE_NAMES | extra_names)


def is_sensitive_key(key: str, pattern: re.Pattern[str]) -> bool:
    """Report whether a key names something whose value must not be kept."""
    return pattern.search(normalise_key(key)) is not None
