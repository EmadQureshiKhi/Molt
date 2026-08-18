"""Every statement a path issues, against the privileges its role actually holds.

Six migrations exist because this check did not. Each of `016` through `021` grants a
privilege an earlier grant list omitted, and every one of them was found by a deployment
rather than by this suite, for one reason: the suite applies migrations and runs its
assertions under an administrative login, and an administrative login holds every
privilege there is. A missing grant is therefore invisible to coverage that is otherwise
end to end, and becomes load-bearing only where a path connects as the narrow role its
privileges were written for. Two reader views that could not count, a watcher that died
before its loop, an erasure that stopped in its disposition phase, an erasure sweep that
removed a session's events and then could not remove the session, and a change stream that
was refused for want of one table read: all five shapes are the same defect.

**What this module does is derive the demand from the source rather than restate it.** A
table of expected grants would only catch an omission somebody remembered to add to the
table, which is exactly the failure mode above. So the module imports each path's entry
point, reads which `molt` modules that pulled in, extracts from those modules every table
and privilege their statements imply, and asserts the cluster grants each one to the role
that path connects as. Adding a statement to a path that reaches a table its role cannot
touch fails here, with no list to maintain.

**The extraction is deliberately over-eager, and the surplus is enumerated rather than
filtered away.** A path imports shared store modules whose write statements belong to
another path, so some of what is implied is genuinely not needed. Each such absence is
recorded below with the reason it is an absence, which turns the noise into the part of
this module worth reading: a role narrower than the statements in its import graph is the
design, and the entries say why for each one. An absence that stops being deliberate fails
here as soon as the reason is deleted.

**The read-only role is checked for reads alone, which is its whole definition.** It holds
`SELECT` and nothing else on anything, so every write its import graph implies is by
construction not its business, and enumerating them individually would say only that.

**One privilege a path needs appears in no statement it issues, and it is derived from the
schema instead.** Deleting a row deletes the rows that cascade from it, and on this platform
the privilege for those is consulted while the cascade expression is built, which is to say
the deleting session must itself hold the delete on every table the cascade will reach. No
statement anywhere names those tables, so no amount of reading the source finds them: a run
removed the artifacts it was authorised to remove and stopped on a cascading child three
tables away. The second test below therefore reads the referential actions out of the
cluster and asserts that a role which may delete a parent may delete everything that
cascades from it, transitively.

**Each path's import graph is resolved in an interpreter of its own, and that is not
tidiness.** Imports are recorded process-wide, so resolving a second path in the process
that resolved the first attributes the first path's modules to the second and reports
demands no statement of that path makes. Purging the module table between roles would fix
the attribution and break every other test in the session that holds a reference to a
`molt` class, so the resolution is handed to a subprocess and this module is written to be
runnable as one.

**Validates: Requirements 27.2, 27.3, 44.1, 49.14**
"""

from __future__ import annotations

import importlib
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Final

import pytest

from molt.store.migrate import apply_migrations

pytestmark = pytest.mark.integration

# Each role, and the entry points of the paths that connect as it. These are the modules
# the deployment runs, not a guess: the eraser is the erasure engine with the certificate
# builder and the backup planner it calls, the writer is the ingest handler, the reader is
# the three read-only surfaces, and the watcher is the consumption loop.
ROLE_PATHS: Final[dict[str, tuple[str, ...]]] = {
    "molt_eraser": ("molt.erase.engine", "molt.attest.builder", "molt.backup"),
    "molt_writer": ("molt.collector.handler",),
    "molt_reader": ("molt.recall", "molt.mcpserver", "molt.retention"),
    "molt_watcher": ("molt.policy.watcher",),
}

# The privilege each statement shape implies. The change stream is here because this
# platform serves a core changefeed only to a principal holding `SELECT` on every table
# named in it, and a statement naming its tables in a list rather than after `FROM` is
# precisely the shape that went unnoticed until the stream was refused on a real cluster.
STATEMENT_SHAPES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("SELECT", re.compile(r"\bFROM\s+([a-z_]+)", re.IGNORECASE)),
    ("SELECT", re.compile(r"\bJOIN\s+([a-z_]+)", re.IGNORECASE)),
    ("SELECT", re.compile(r"\bCHANGEFEED\s+FOR\s+([a-z_,\s]+?)\s+WITH\b", re.IGNORECASE)),
    ("INSERT", re.compile(r"\bINSERT\s+INTO\s+([a-z_]+)", re.IGNORECASE)),
    ("INSERT", re.compile(r"\bUPSERT\s+INTO\s+([a-z_]+)", re.IGNORECASE)),
    ("UPDATE", re.compile(r"\bUPDATE\s+([a-z_]+)\s+SET", re.IGNORECASE)),
    ("DELETE", re.compile(r"\bDELETE\s+FROM\s+([a-z_]+)", re.IGNORECASE)),
)

# The change stream names its tables as a list, so one match yields several tables.
TABLE_LIST_SEPARATOR: Final[str] = ","

# A long statement is written as adjacent string literals, which the language joins and a
# reader of the source sees separated by a quote, a line break, and an indent. Collapsing
# that seam is what lets a shape match across it, and without it the one statement whose
# tables are named in a list rather than after `FROM` matched nothing at all — which is how
# the change stream's missing read stayed missing.
LITERAL_SEAM: Final[re.Pattern[str]] = re.compile(r"[\"']\s*[\"']")

# The migration runner is an operator action under an administrative login and never a
# service role, so its own bookkeeping table is no service role's business.
EXEMPT_TABLES: Final[frozenset[str]] = frozenset({"schema_migration"})

# The role that holds reads and nothing else, by the definition migration 007 states.
READ_ONLY_ROLE: Final[str] = "molt_reader"
READ_PRIVILEGE: Final[str] = "SELECT"

# The absences that are deliberate, each with the reason it is one. Every entry is a
# statement some module in that path's import graph carries, which the role is not granted
# and must not be granted.
DOCUMENTED_ABSENCES: Final[dict[tuple[str, str, str], str]] = {
    ("molt_writer", "capability", "SELECT"): (
        "the ingest path's read of the capability record is best-effort by construction: a "
        "refusal is logged at debug and swallowed, and every accessor then reports each "
        "platform fact as unprobed rather than as absent, because an empty record is the "
        "honest reading when the role that looked is not allowed to look"
    ),
    ("molt_writer", "capability", "INSERT"): (
        "probing the cluster and recording the answer is an operator action and the "
        "watcher's start-up, never the ingest path's, which only ever reads what a probe "
        "already recorded"
    ),
    ("molt_eraser", "capability", "INSERT"): (
        "the erasure path reads whether this cluster serves a user-initiated backup and "
        "records nothing: the probe that writes that row is run by an operator"
    ),
    ("molt_eraser", "ledger", "INSERT"): (
        "the append is the ingest path's, reached through a shared chain module the erasure "
        "path imports for the digest reads it performs over rows it is removing"
    ),
    ("molt_eraser", "session", "INSERT"): (
        "creating a session belongs to the path that captures one; the erasure path reads a "
        "session, closes it, and deletes it, and holds exactly those three"
    ),
    ("molt_eraser", "working_memory", "INSERT"): (
        "writing scratch state is the capture path's; the erasure path purges the working "
        "tier of a tenant and so holds the read and the delete alone"
    ),
    ("molt_eraser", "procedure_retrieval", "INSERT"): (
        "recording that a procedure was retrieved is the recall path's write; the erasure "
        "path reads the three procedural tables to weigh a procedure's standing and removes "
        "them by cascade from the procedure itself"
    ),
    ("molt_eraser", "procedure_outcome", "INSERT"): (
        "recording how a session using a procedure turned out is the recall path's write, "
        "for the same reason the retrieval row is"
    ),
    ("molt_eraser", "procedure_confidence_change", "INSERT"): (
        "recording a movement in a procedure's confidence is the recall path's write, for "
        "the same reason the retrieval row is"
    ),
    ("molt_watcher", "approval_queue", "UPDATE"): (
        "resolving a queued approval is the console's write, performed by an operator "
        "signing in; the watcher queues an entry and must not be able to answer it, because "
        "an approval is a record of a human decision"
    ),
}

Connection = Any


@dataclass(frozen=True, slots=True)
class AppliedSchema:
    """A schema holding every migration, with the tables the grants were written for."""

    connection: Connection
    schema: str


def _tables_named(match: str) -> tuple[str, ...]:
    """Every table one match names, which is several where a statement lists them."""
    return tuple(
        named.strip().lower() for named in match.split(TABLE_LIST_SEPARATOR) if named.strip()
    )


def _loaded_modules(entries: tuple[str, ...]) -> dict[str, ModuleType]:
    """Import each entry point and return every `molt` module that pulled in."""
    for entry in entries:
        importlib.import_module(entry)
    return {
        name: module
        for name, module in sys.modules.items()
        if name.startswith("molt.") and getattr(module, "__file__", None) is not None
    }


def _demanded(role: str) -> dict[tuple[str, str], str]:
    """Every table and privilege one path's modules imply, and where each is named.

    Resolved in an interpreter of its own, for the reason the module docstring gives, and
    read back as one line per demand so nothing is deserialised from a subprocess.
    """
    completed = subprocess.run(  # noqa: S603 - a fixed interpreter and this file's own path
        [sys.executable, str(pathlib.Path(__file__).resolve()), role],
        capture_output=True,
        text=True,
        check=True,
    )
    demanded: dict[tuple[str, str], str] = {}
    for line in completed.stdout.splitlines():
        privilege, table, named = line.split()
        demanded.setdefault((table, privilege), named)
    return demanded


def _report(role: str) -> None:
    """Print one line per demand this path makes, for the parent process to read."""
    for name, module in sorted(_loaded_modules(ROLE_PATHS[role]).items()):
        written = pathlib.Path(str(module.__file__)).read_text(encoding="utf-8")
        source = LITERAL_SEAM.sub("", written)
        for privilege, shape in STATEMENT_SHAPES:
            for match in shape.findall(source):
                for table in _tables_named(match):
                    sys.stdout.write(f"{privilege} {table} {name}\n")


@pytest.fixture(scope="module")
def applied(fresh_schema: Connection) -> AppliedSchema:
    """Apply every migration into this module's own schema and read its name back."""
    apply_migrations(fresh_schema)
    with fresh_schema.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        schema = str(cursor.fetchall()[0][0])
    return AppliedSchema(connection=fresh_schema, schema=schema)


def _real_tables(applied: AppliedSchema) -> frozenset[str]:
    """Every table the migrations created, so a matched word that names none is dropped."""
    with applied.connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (applied.schema,),
        )
        return frozenset(str(row[0]) for row in cursor.fetchall())


def _cascades(applied: AppliedSchema) -> dict[str, frozenset[str]]:
    """Each table, and the tables a delete of one of its rows cascades to."""
    with applied.connection.cursor() as cursor:
        cursor.execute(
            "SELECT ccu.table_name, tc.table_name "
            "FROM information_schema.table_constraints AS tc "
            "JOIN information_schema.referential_constraints AS rc "
            "  ON rc.constraint_name = tc.constraint_name "
            "JOIN information_schema.constraint_column_usage AS ccu "
            "  ON ccu.constraint_name = tc.constraint_name "
            "WHERE tc.constraint_type = %s AND rc.delete_rule = %s AND tc.table_schema = %s",
            ("FOREIGN KEY", "CASCADE", applied.schema),
        )
        children: dict[str, set[str]] = {}
        for parent, child in cursor.fetchall():
            children.setdefault(str(parent), set()).add(str(child))
    return {parent: frozenset(named) for parent, named in children.items()}


def _reached(parents: frozenset[str], cascades: dict[str, frozenset[str]]) -> frozenset[str]:
    """Every table a delete of these parents reaches, following cascades to the end.

    Transitively, because a cascade's own child cascades too, and the privilege is consulted
    for the whole expression rather than for its first step.
    """
    seen: set[str] = set()
    pending = list(parents)
    while pending:
        table = pending.pop()
        for child in cascades.get(table, frozenset()):
            if child not in seen:
                seen.add(child)
                pending.append(child)
    return frozenset(seen)


def _held(applied: AppliedSchema, role: str) -> frozenset[tuple[str, str]]:
    """Every table and privilege pair one role holds in the applied schema."""
    with applied.connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name, privilege_type FROM information_schema.table_privileges "
            "WHERE table_schema = %s AND grantee = %s",
            (applied.schema, role),
        )
        return frozenset((str(table), str(privilege)) for table, privilege in cursor.fetchall())


@pytest.mark.parametrize("role", sorted(ROLE_PATHS))
def test_every_statement_a_path_issues_is_granted_to_the_role_it_connects_as(
    applied: AppliedSchema, role: str
) -> None:
    """No path names a table and a privilege the cluster withholds from its own role.

    A failure here is one of two things and the message says which is more likely: a
    statement was added to a path whose role cannot issue it, in which case a new migration
    grants it, or the statement belongs to another path and the absence is deliberate, in
    which case it is recorded above with its reason.
    """
    real_tables = _real_tables(applied)
    held = _held(applied, role)

    missing = sorted(
        (table, privilege, named)
        for (table, privilege), named in _demanded(role).items()
        if table in real_tables
        and table not in EXEMPT_TABLES
        and (table, privilege) not in held
        and (role, table, privilege) not in DOCUMENTED_ABSENCES
        and not (role == READ_ONLY_ROLE and privilege != READ_PRIVILEGE)
    )

    reported = "\n".join(
        f"  {privilege} on {table}, named in {named}" for table, privilege, named in missing
    )
    assert missing == [], (
        f"{role} issues statements the cluster does not grant it:\n{reported}\n"
        "Either grant these in a new migration, or record each as a deliberate absence "
        "with the reason it is one."
    )


@pytest.mark.parametrize("role", sorted(ROLE_PATHS))
def test_a_role_that_may_delete_a_parent_may_delete_what_cascades_from_it(
    applied: AppliedSchema, role: str
) -> None:
    """A cascading delete needs the privilege on every table it reaches, not just the parent.

    This is the one demand no statement states. The deleting session's own privilege is
    consulted while the cascade expression is built, so a role holding the delete on a parent
    and not on a child cannot delete the parent at all — and finds that out at the moment it
    tries, which for an erasure is after it has already removed everything else.
    """
    held = _held(applied, role)
    deletes = frozenset(table for table, privilege in held if privilege == "DELETE")
    cascades = _cascades(applied)

    unreachable = sorted(_reached(deletes, cascades) - deletes)
    assert unreachable == [], (
        f"{role} may delete rows whose cascade reaches {unreachable}, which it may not "
        "delete, so the parent delete is refused while its cascade expression is built. "
        "Grant the delete on each of these in a new migration."
    )


@pytest.mark.parametrize("role", sorted(ROLE_PATHS))
def test_no_recorded_absence_has_quietly_become_a_grant(applied: AppliedSchema, role: str) -> None:
    """A privilege recorded as deliberately withheld is still withheld.

    The reasons above are load-bearing rather than commentary. Each says a role must not
    hold a privilege, so a later migration granting one of them would leave a written
    justification for an arrangement that no longer holds, which is worse than never having
    written it down.
    """
    held = _held(applied, role)
    granted = sorted(
        (table, privilege)
        for (named_role, table, privilege) in DOCUMENTED_ABSENCES
        if named_role == role and (table, privilege) in held
    )
    assert granted == [], (
        f"{role} now holds privileges recorded as deliberately absent: {granted}. "
        "Either revoke them or remove the recorded reason."
    )


# Run as a script, this module resolves one path's import graph and prints what that path
# demands. It exists so the resolution happens in an interpreter that has imported no other
# path, which is the only way the attribution is correct, and it is here rather than in a
# second file so the shapes and the path table have exactly one definition.
if __name__ == "__main__":
    _report(sys.argv[1])
