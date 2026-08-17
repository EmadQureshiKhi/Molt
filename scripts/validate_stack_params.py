#!/usr/bin/env python3.12
"""Validate a stack's deployment parameters against the template that declares them.

A deployment fails slowly and confusingly when a parameter is missing: the stack
starts, some resources are created, and the fault surfaces as a rejected value deep
in a change set. Checking first turns that into one line before anything is created,
which is what makes the deployment script safe to re-run.

Two checks run. Every template parameter that declares no default must be present in
the parameter file, and every value present must name a parameter the template
declares, because a value nobody reads is either a typo or a leftover. A declared
pattern is checked too where the template states one, so a value of the wrong shape
fails here rather than at the control plane.

The template is read with the document parser pinned in the dependency manifest, so
the check reads the declared structure rather than matching template text. The
parameter file is machine-readable and holds non-secret values only; a secret is
referenced by parameter name and its value never appears in a deployment argument.

Exit status is 0 when the stack's parameters are complete and 2 when they are not,
which is what the deployment script branches on.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML

EXIT_OK: Final[int] = 0
EXIT_INVALID: Final[int] = 2

# The template nodes read here, and the parameter fields that decide the checks.
PARAMETERS_NODE: Final[str] = "Parameters"

# The section of the parameter file whose values every stack shares.
COMMON_SECTION: Final[str] = "common"
DEFAULT_FIELD: Final[str] = "Default"
PATTERN_FIELD: Final[str] = "AllowedPattern"


def load_template(path: Path) -> Mapping[str, object]:
    """Parse one template into plain data."""
    reader = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as handle:
        document: object = reader.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"{path} is no template document")
    return {str(key): value for key, value in document.items()}


def declared_parameters(template: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    """Every parameter a template declares, with its own fields."""
    node = template.get(PARAMETERS_NODE, {})
    if not isinstance(node, dict):
        return {}
    declared: dict[str, Mapping[str, object]] = {}
    for name, fields in node.items():
        declared[str(name)] = (
            {str(key): value for key, value in fields.items()} if isinstance(fields, dict) else {}
        )
    return declared


def required_parameters(template: Mapping[str, object]) -> tuple[str, ...]:
    """The parameters a deployment must supply, being those declaring no default."""
    return tuple(
        name
        for name, fields in sorted(declared_parameters(template).items())
        if DEFAULT_FIELD not in fields
    )


def _section(document: Mapping[str, object], key: str, path: Path) -> Mapping[str, str]:
    """One section of the parameter file, as text values."""
    node = document.get(key)
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise ValueError(f"{path} holds no mapping of values under {key}")
    return {str(name): str(value) for name, value in node.items()}


def load_values(
    path: Path,
    stack: str,
    *,
    declared: Sequence[str] = (),
) -> Mapping[str, str]:
    """Read one stack's values, taking the shared section for the parameters it declares.

    The shared section holds the values every stack repeats — the deployment name,
    the parameter prefix, the policy language edition — so they are stated once
    rather than eleven times. A shared value is applied only where the template
    declares a parameter of that name, and a stack's own section overrides it.
    """
    document: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} is no parameter document")
    narrowed = {str(key): value for key, value in document.items()}
    shared = _section(narrowed, COMMON_SECTION, path)
    own = _section(narrowed, stack, path)
    permitted = set(declared)
    merged = {name: value for name, value in shared.items() if name in permitted}
    merged.update(own)
    return merged


def findings(
    template: Mapping[str, object],
    values: Mapping[str, str],
    *,
    resolved: Sequence[str] = (),
) -> tuple[str, ...]:
    """Every reason this stack's parameters are not deployable, one line each.

    A resolved name is one the deployment supplies from an earlier stack's output
    rather than from the parameter file, which is where a bucket name, a key
    resource name, and a subnet identifier come from. It counts as present.
    """
    declared = declared_parameters(template)
    supplied = set(values) | set(resolved)
    reported: list[str] = []
    for name in required_parameters(template):
        if name not in supplied:
            reported.append(f"missing required parameter {name}")
    for name in sorted(values):
        if name not in declared:
            reported.append(f"unknown parameter {name}")
            continue
        pattern = declared[name].get(PATTERN_FIELD)
        if isinstance(pattern, str) and re.fullmatch(pattern, values[name]) is None:
            reported.append(f"parameter {name} does not match the shape the template declares")
    return tuple(reported)


def overrides(values: Mapping[str, str]) -> tuple[str, ...]:
    """The values as one assignment per line, in the form the deployment command takes."""
    return tuple(f"{name}={values[name]}" for name in sorted(values))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_stack_params",
        description="Check a stack's deployment parameters against its template.",
    )
    parser.add_argument("--template", required=True, help="path to the template")
    parser.add_argument("--params", required=True, help="path to the parameter file")
    parser.add_argument("--stack", required=True, help="key within the parameter file")
    parser.add_argument(
        "--resolved",
        action="append",
        default=[],
        help="parameter supplied from an earlier stack's output rather than from the file",
    )
    parser.add_argument(
        "--print-overrides",
        action="store_true",
        help="print one assignment per line for the deployment command",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Validate one stack's parameters and optionally print them for the caller."""
    arguments = _build_parser().parse_args(argv)
    try:
        template = load_template(Path(arguments.template))
        values = load_values(
            Path(arguments.params),
            arguments.stack,
            declared=tuple(declared_parameters(template)),
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"validate: {error}", file=sys.stderr)
        return EXIT_INVALID

    reported = findings(template, values, resolved=tuple(arguments.resolved))
    for line in reported:
        print(f"validate: {arguments.stack}: {line}", file=sys.stderr)
    if reported:
        return EXIT_INVALID

    if arguments.print_overrides:
        for assignment in overrides(values):
            print(assignment)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
