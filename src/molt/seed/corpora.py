"""The synthetic tenant domains and the generated content the seed is built from.

Four invented tenants exist here, each with a domain of its own: a repository
name, a directory vocabulary, a service vocabulary, a data-shape vocabulary, a
symbol prefix, and a set of content markers. Every generated value is drawn from
one of those vocabularies, which is what makes a seeded corpus look like work
rather than like filler, and what makes a fragment of one tenant's domain sit near
that tenant's other content in vector space.

Two rules hold over everything in this module.

**Nothing here names anything real.** The tenants, the repositories, the services,
and the generated code are invented for this seed. No personal name, no address,
no calendar value, and no third-party product appears in any generated string, so
the seeded content passes the same metadata gate the source does.

**The revealing tokens are separable from the vocabulary.** A tenant's identity
lives in its slug, its display name, its repository name, its content markers, and
its directory names; its *idiom* lives in its services, its fields, and its symbol
prefix. A planted fragment keeps the idiom and loses the identity, so the fragment
is recoverable by meaning and not by matching a label. `ClientDomain.owner_tokens`
is the exact set the planting path strips and then asserts the absence of.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random
from typing import Final

__all__ = [
    "AGENT_CLI_NAMES",
    "DOMAINS",
    "MACHINE_IDS",
    "MAX_FRAGMENT_LINES",
    "MIN_FRAGMENT_LINES",
    "ClientDomain",
    "SeedVolumes",
    "assistant_text",
    "baseline_body",
    "code_fragment",
    "domain_of",
    "error_text",
    "fragment_line_count",
    "path_text",
    "procedure_body",
    "prompt_text",
    "scratch_value",
    "shell_text",
    "summary_body",
    "tool_name",
]

# The agent command-line tools seeded Sessions are attributed to, and the machine
# identifiers they ran on. Five names and four machines meet the floors of
# Requirement 28.2 with margin.
AGENT_CLI_NAMES: Final[tuple[str, ...]] = (
    "claude_code",
    "cursor",
    "codex",
    "gemini_cli",
    "copilot",
)
MACHINE_IDS: Final[tuple[str, ...]] = (
    "workstation-a1",
    "workstation-b2",
    "builder-c3",
    "builder-d4",
)

# The bounds a planted fragment's length is drawn from, as the contamination
# procedure states them.
MIN_FRAGMENT_LINES: Final[int] = 15
MAX_FRAGMENT_LINES: Final[int] = 60

# How many statement lines one generated function body carries, and the fixed
# indent those lines are written at.
_BODY_LINES: Final[int] = 5
_INDENT: Final[str] = "    "


@dataclass(frozen=True, slots=True)
class ClientDomain:
    """One invented tenant and the vocabulary its content is generated from."""

    slug: str
    display_name: str
    jurisdiction: str
    repository: str
    directories: tuple[str, ...]
    services: tuple[str, ...]
    fields: tuple[str, ...]
    symbol_prefix: str
    content_markers: tuple[str, ...]

    def __post_init__(self) -> None:
        """Refuse a domain missing any vocabulary the generators draw from."""
        for label, vocabulary in (
            ("directory", self.directories),
            ("service", self.services),
            ("field", self.fields),
            ("content marker", self.content_markers),
        ):
            if not vocabulary:
                raise ValueError(f"a seeded domain names at least one {label}")
        if not self.symbol_prefix:
            raise ValueError("a seeded domain names a symbol prefix")

    @property
    def owner_tokens(self) -> tuple[str, ...]:
        """Every token whose presence would reveal this tenant by exact match.

        The display name contributes both whole and word by word, because a
        fragment carrying one of its words would be as revealing as one carrying
        all of them.
        """
        words = tuple(part for part in self.display_name.split(" ") if part)
        return tuple(
            dict.fromkeys(
                (
                    self.slug,
                    self.display_name,
                    *words,
                    self.repository,
                    *self.content_markers,
                    *self.directories,
                )
            )
        )


# The four tenants. Slugs, display names, repositories, and markers are invented,
# and each vocabulary is disjoint from the other three so that a fragment drawn
# from one domain is recognisable as that domain's by its tokens alone.
DOMAINS: Final[tuple[ClientDomain, ...]] = (
    ClientDomain(
        slug="veltrine",
        display_name="Veltrine Holdings",
        jurisdiction="eu",
        repository="veltrine-ledgerkit",
        directories=("ledgerkit", "clearing", "tariffs"),
        services=("settlement_router", "tariff_ledger", "payout_batcher"),
        fields=("settlement_key", "tariff_band", "payout_window"),
        symbol_prefix="vlt",
        content_markers=("veltrine-ledgerkit", "vlt-settlement-key"),
    ),
    ClientDomain(
        slug="orbanic",
        display_name="Orbanic Systems",
        jurisdiction="us",
        repository="orbanic-fleetcore",
        directories=("fleetcore", "runbooks", "signals"),
        services=("dispatch_planner", "route_cache", "telemetry_fold"),
        fields=("dispatch_slot", "route_hash", "fold_window"),
        symbol_prefix="orb",
        content_markers=("orbanic-fleetcore", "orb-dispatch-slot"),
    ),
    ClientDomain(
        slug="quillstone",
        display_name="Quillstone Labs",
        jurisdiction="uk",
        repository="quillstone-assaykit",
        directories=("assaykit", "reagents", "batches"),
        services=("assay_scheduler", "reagent_pool", "batch_verifier"),
        fields=("assay_code", "reagent_lot", "batch_seal"),
        symbol_prefix="qst",
        content_markers=("quillstone-assaykit", "qst-assay-code"),
    ),
    ClientDomain(
        slug="mirebrook",
        display_name="Mirebrook Group",
        jurisdiction="eu",
        repository="mirebrook-yieldmap",
        directories=("yieldmap", "parcels", "surveys"),
        services=("parcel_indexer", "yield_model", "survey_merge"),
        fields=("parcel_ref", "yield_band", "survey_seal"),
        symbol_prefix="mbk",
        content_markers=("mirebrook-yieldmap", "mbk-parcel-ref"),
    ),
)


@dataclass(frozen=True, slots=True)
class SeedVolumes:
    """How much of each shape a generation produces.

    The defaults are the design's, which clear every floor of Requirement 28 with
    margin. A caller may lower them, which is what a test does so that a run costs
    seconds rather than minutes, and the floors are then the caller's to respect.

    A nesting depth is counted from a root Session at zero, which is what the
    schema's own check states, so the second-level Sessions are the ones stored at
    depth one and the third-level Session is the one stored at depth two. The
    third level is what carries Requirement 28.3, which asks for a nesting depth of
    at least two.
    """

    clients: int = 4
    sessions: int = 28
    events: int = 2600
    subagent_sessions_depth_two: int = 3
    subagent_sessions_depth_three: int = 1
    blended_artifacts: int = 5
    planted_fragments: int = 8
    working_rows_per_session: int = 3

    def __post_init__(self) -> None:
        """Refuse a volume that could not produce the shapes it names."""
        for label, count in (
            ("client", self.clients),
            ("session", self.sessions),
            ("event", self.events),
            ("blended artifact", self.blended_artifacts),
            ("planted fragment", self.planted_fragments),
        ):
            if count < 1:
                raise ValueError(f"a generation produces at least one {label}")
        if self.clients > len(DOMAINS):
            raise ValueError("the seed holds one domain per client and no more")
        if self.clients < 2:
            raise ValueError("cross-client contamination needs at least two clients")
        if self.working_rows_per_session < 0:
            raise ValueError("a working row count cannot be negative")
        if self.subagent_sessions_depth_two < 0 or self.subagent_sessions_depth_three < 0:
            raise ValueError("a subagent session count cannot be negative")
        nested = self.subagent_sessions_depth_two + self.subagent_sessions_depth_three
        if nested >= self.sessions:
            raise ValueError("a nested Session needs a root Session to hang off")


def domain_of(slug: str) -> ClientDomain:
    """The domain registered under a slug.

    Raises:
        KeyError: No seeded domain carries that slug.
    """
    for domain in DOMAINS:
        if domain.slug == slug:
            return domain
    raise KeyError(f"no seeded domain is registered under {slug!r}")


# ---------------------------------------------------------------------------
# Generated content, every value drawn from a domain's own vocabulary
# ---------------------------------------------------------------------------


def prompt_text(domain: ClientDomain, rng: Random) -> str:
    """A plausible operator prompt about one of the tenant's services."""
    service = rng.choice(domain.services)
    field = rng.choice(domain.fields)
    marker = rng.choice(domain.content_markers)
    return (
        f"In {marker} the {service} rejects a record whose {field} repeats. "
        f"Trace where {service} reads {field} and propose a narrower guard."
    )


def assistant_text(domain: ClientDomain, rng: Random) -> str:
    """A plausible assistant answer naming the same vocabulary the prompt did."""
    service = rng.choice(domain.services)
    field = rng.choice(domain.fields)
    return (
        f"The {service} treats {field} as unique per batch, so a repeated {field} "
        f"reaches the guard rather than the writer. Narrowing the guard to {field} "
        f"alone keeps the rest of {service} untouched."
    )


def error_text(domain: ClientDomain, rng: Random) -> str:
    """A plausible failure record from one of the tenant's services."""
    service = rng.choice(domain.services)
    field = rng.choice(domain.fields)
    return f"{service} refused the record: {field} was absent where the schema requires it"


def shell_text(domain: ClientDomain, rng: Random) -> str:
    """A plausible shell command run inside the tenant's repository."""
    directory = rng.choice(domain.directories)
    service = rng.choice(domain.services)
    return f"pytest {domain.repository}/{directory}/tests -k {service}"


def tool_name(rng: Random) -> str:
    """One of the tool names a seeded tool call carries."""
    return rng.choice(("read_file", "write_file", "search", "run_command", "list_directory"))


def path_text(domain: ClientDomain, rng: Random) -> str:
    """A path inside the tenant's repository, which reveals the tenant by design."""
    directory = rng.choice(domain.directories)
    service = rng.choice(domain.services)
    return f"{domain.repository}/{directory}/{service}.py"


def scratch_value(domain: ClientDomain, rng: Random) -> str:
    """Disposable scratch content, of the kind the working tier holds."""
    field = rng.choice(domain.fields)
    return f"pending review of {field} in the current batch"


def fragment_line_count(rng: Random) -> int:
    """A fragment length drawn from the bounds the contamination procedure states."""
    return rng.randint(MIN_FRAGMENT_LINES, MAX_FRAGMENT_LINES)


def code_fragment(domain: ClientDomain, rng: Random, *, lines: int) -> str:
    """Generate a code-like fragment in one tenant's idiom.

    The fragment names the tenant's services, fields, and symbol prefix, which is
    what places its vector near that tenant's other content. It names no
    repository, no directory, no content marker, no slug, and no path, so the
    planting path has nothing to strip out of a fragment generated here and the
    absence assertion it performs holds by construction rather than by cleanup.

    Args:
        domain: The tenant whose idiom the fragment is written in.
        rng: The seeded generator every drawn value comes from.
        lines: Roughly how many lines the fragment carries. The generator emits
            whole functions, so the result is the least whole number of functions
            that reaches the requested length.

    Returns:
        The fragment as text, with no trailing newline.
    """
    per_function = _BODY_LINES + 2
    functions = max(1, -(-lines // per_function))
    produced: list[str] = []
    for index in range(functions):
        service = domain.services[index % len(domain.services)]
        primary = rng.choice(domain.fields)
        secondary = rng.choice(domain.fields)
        produced.append(f"def {domain.symbol_prefix}_resolve_{service}(record, cursor):")
        produced.append(f'{_INDENT}"""Resolve one {service} record by its {primary}."""')
        produced.append(f"{_INDENT}{primary} = record[{primary!r}]")
        produced.append(f"{_INDENT}{secondary} = record.get({secondary!r})")
        produced.append(
            f"{_INDENT}bounded = cursor.lookup({service!r}, {primary}, limit={rng.randint(2, 64)})"
        )
        produced.append(f"{_INDENT}if {secondary} is None:")
        produced.append(f"{_INDENT}{_INDENT}return bounded")
        produced.append(
            f"{_INDENT}return [row for row in bounded if row.{secondary} == {secondary}]"
        )
        produced.append("")
    return "\n".join(produced).rstrip("\n")


def summary_body(domain: ClientDomain, rng: Random) -> str:
    """A summary distilled from one Session's own Events."""
    service = rng.choice(domain.services)
    field = rng.choice(domain.fields)
    return (
        f"The run narrowed a duplicate {field} in {service} to the guard rather than "
        f"the writer, and left the remaining paths of {service} untouched."
    )


def baseline_body(domains: tuple[ClientDomain, ...], rng: Random) -> str:
    """A behavioural baseline distilled from Events across several tenants."""
    services = ", ".join(rng.choice(domain.services) for domain in domains)
    return (
        "Across the observed runs the agent reads a failing record before it reads "
        f"the writer, in every one of {services}, and it changes a guard before it "
        "changes a schema."
    )


def procedure_body(domains: tuple[ClientDomain, ...], rng: Random) -> str:
    """A learned procedure distilled from tool-call sequences that succeeded."""
    fields = ", ".join(rng.choice(domain.fields) for domain in domains)
    return (
        "To resolve a duplicate key rejection: read the guard, confirm which column "
        f"the uniqueness spans, narrow the guard to that column, then re-run the "
        f"batch. Observed on {fields}."
    )
