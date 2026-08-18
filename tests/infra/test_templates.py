"""Structural assertions over the infrastructure templates and the deployment values.

The templates are the thing that turns this design's topology decisions into
created resources, so the resources they declare, the resources they promise never
to declare, and the privileges they grant are asserted here rather than trusted.

Every assertion below reads a parsed document: the resource mapping, each resource's
own properties, and each policy statement. Nothing here matches template text, so no
assertion depends on key order, comment placement, or scalar folding style. The
parser is the one declared in the dependency manifest, pinned exactly like every
other tool the checks depend on.

Three claims carry the most weight. No charged resource the cost ceiling rules out
appears anywhere: no load balancer, no target group, no listener, no address
translation gateway, no interface endpoint, and no per-secret secret store. No
secret value appears anywhere: every secret is referenced by the name of the
parameter that holds it, and the shapes a credential takes are refused outright. And
exactly one role in the whole deployment may sign, counted across every identity
policy of every stack rather than in one template, which is what the checkpoint
signer's hosting decision rests on.

A fourth claim was added when a deployment established the need for it. A policy that
names a role as a principal is validated against the role's existence when the policy's
resource is created, so a stack of their own creates the roles ahead of the key and the
bucket, and the permissions those roles hold that name the key or the bucket are
attached back to them by the stacks that create those resources. Every role named as a
principal is asserted to exist, creatable by some template earlier in the order than
the stack naming it.

The suite reads tracked files and runs one local validation process. It reaches no
cloud account, creates nothing, and needs no credential of any kind, which is the
same property it asserts of the templates it reads.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML

from molt.config.resolve import SETTINGS
from molt.providers.registry import (
    EMBEDDING_PROVIDERS,
    EMBEDDING_ROLE,
    TEXT_PROVIDERS,
    TEXT_ROLE,
)

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPOSITORY_ROOT / "infra"
TEMPLATE_DIR: Final[Path] = INFRA_DIR / "templates"
PARAMS_FILE: Final[Path] = INFRA_DIR / "params" / "demo.json"
SCRIPT_DIR: Final[Path] = REPOSITORY_ROOT / "scripts"
VALIDATOR: Final[Path] = SCRIPT_DIR / "validate_stack_params.py"

# The twelve stacks, named in deployment order. The order is asserted against the
# deployment script, so a stack added to one and not the other is a failure.
#
# The gateway stack sits after both function stacks because a permission naming a
# function that does not exist is refused, and before the distribution because the
# distribution's origin is the endpoint it creates. It exists because a function's own
# endpoint cannot serve an anonymous request on an account the provider has not verified,
# which is the same restriction that refuses the distribution outright.
#
# The roles stack sits third because a policy that names a principal is refused when
# the principal does not exist: the key policy names the console execution role and
# the bucket policy names all three roles, so the roles are created before either. The
# position is asserted below rather than only stated here.
STACK_ORDER: Final[tuple[str, ...]] = (
    "network",
    "parameters",
    "roles",
    "kms",
    "storage",
    "collector",
    "console",
    "gateway",
    "cdn",
    "watcher",
    "mcp",
    "observability",
)

# Every resource type that must appear somewhere, with the stack that owns it.
REQUIRED_RESOURCES: Final[tuple[tuple[str, str], ...]] = (
    ("network", "AWS::EC2::VPC"),
    ("network", "AWS::EC2::Subnet"),
    ("network", "AWS::EC2::InternetGateway"),
    ("network", "AWS::EC2::SecurityGroup"),
    ("parameters", "AWS::SSM::Parameter"),
    ("roles", "AWS::IAM::Role"),
    ("kms", "AWS::KMS::Key"),
    ("kms", "AWS::KMS::Alias"),
    # The identity side of the key policy and of the bucket policy. Each is attached
    # to a role the roles stack already created, by the stack that creates the
    # resource the grant names, which is what breaks the cycle between the two.
    ("kms", "AWS::IAM::Policy"),
    ("storage", "AWS::S3::Bucket"),
    ("storage", "AWS::S3::BucketPolicy"),
    ("storage", "AWS::IAM::Policy"),
    ("collector", "AWS::Lambda::Function"),
    ("collector", "AWS::Lambda::Url"),
    ("collector", "AWS::Logs::LogGroup"),
    ("console", "AWS::Lambda::Function"),
    ("console", "AWS::Lambda::Url"),
    ("console", "AWS::Logs::LogGroup"),
    ("console", "AWS::Events::Rule"),
    # The regional endpoints, one per function, with the route that carries every method
    # and path through and the permission that admits only this endpoint.
    ("gateway", "AWS::ApiGatewayV2::Api"),
    ("gateway", "AWS::ApiGatewayV2::Integration"),
    ("gateway", "AWS::ApiGatewayV2::Route"),
    ("gateway", "AWS::ApiGatewayV2::Stage"),
    ("gateway", "AWS::Lambda::Permission"),
    ("cdn", "AWS::CloudFront::Distribution"),
    ("watcher", "AWS::ECS::Cluster"),
    ("watcher", "AWS::ECS::TaskDefinition"),
    ("watcher", "AWS::ECS::Service"),
    ("mcp", "AWS::ECS::TaskDefinition"),
    ("mcp", "AWS::ECS::Service"),
    ("observability", "AWS::Logs::MetricFilter"),
    ("observability", "AWS::CloudWatch::Alarm"),
)

# Every resource type the cost ceiling rules out. A template declaring one of these
# would introduce a charge the design states it does not carry.
FORBIDDEN_RESOURCE_TYPES: Final[tuple[str, ...]] = (
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "AWS::ElasticLoadBalancingV2::TargetGroup",
    "AWS::ElasticLoadBalancingV2::Listener",
    "AWS::ElasticLoadBalancingV2::ListenerRule",
    "AWS::ElasticLoadBalancing::LoadBalancer",
    "AWS::EC2::NatGateway",
    "AWS::EC2::VPCEndpoint",
    "AWS::EC2::VPCEndpointService",
    "AWS::SecretsManager::Secret",
    "AWS::SecretsManager::RotationSchedule",
)

# Credential shapes refused outright wherever a string appears. Each names a way a
# secret gets committed by accident rather than on purpose.
SECRET_SHAPES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("a connection string", re.compile(r"postgres(?:ql)?://[^\s\"']*:[^\s\"']*@")),
    ("an inline password", re.compile(r"password\s*[:=]\s*[^\s\"'{}$]{4,}", re.IGNORECASE)),
    ("a bearer token", re.compile(r"bearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE)),
    ("a static access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b")),
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a bare account identifier", re.compile(r"(?<![\w.:/-])\d{12}(?![\w.:/-])")),
)

# The prefix every configuration key of this system carries. A variable a template
# sets under this prefix is a claim that the process reads it, and the surface is the
# only thing that decides whether it does.
CONFIGURATION_PREFIX: Final[str] = "MOLT_"

# The four stacks whose processes connect to the cluster, and the two variable names
# each must set. The names are read off the surface by the configuration key they
# carry rather than spelled again here, so a renamed variable moves the assertion with
# it instead of quietly passing against a name nothing reads.
CLUSTER_STACKS: Final[tuple[str, ...]] = ("collector", "console", "watcher", "mcp")
DSN_PARAM_ENV: Final[str] = next(
    setting.env for setting in SETTINGS if setting.key == "store.dsn_param"
)
ROLE_ENV: Final[str] = next(setting.env for setting in SETTINGS if setting.key == "store.role")

# The certificate-bearing keys of the surface that carry no default, read off the
# surface by section rather than spelled again here. A key with a default resolves
# without the deployment naming it; a key without one is a value only the deployment
# knows — a bucket generated from the account and the region, a key another stack
# provisions — so the stack that signs must be given it or refuse at startup.
CERTIFICATE_SECTION: Final[str] = "certificate."
REQUIRED_CERTIFICATE_ENV: Final[tuple[str, ...]] = tuple(
    setting.env
    for setting in SETTINGS
    if setting.key.startswith(CERTIFICATE_SECTION) and setting.default is None
)

# The provider-role keys of the surface, paired by role: the key naming where a
# role's credential is held, and the key selecting which implementation that role
# resolves to. Both spellings are read off the surface and the pairing is by the role
# each key names, so a role added to the surface is covered without this changing.
PROVIDER_SECTION: Final[str] = "providers."
CREDENTIAL_PARAM_SUFFIX: Final[str] = "_credential_param"
SURFACE_ENV_BY_KEY: Final[Mapping[str, str]] = {setting.key: setting.env for setting in SETTINGS}


def _credential_and_selection() -> Mapping[str, tuple[str, str]]:
    """Each provider role's credential-name key and its selection key, by role."""
    paired: dict[str, tuple[str, str]] = {}
    for key, env in SURFACE_ENV_BY_KEY.items():
        if not key.startswith(PROVIDER_SECTION) or not key.endswith(CREDENTIAL_PARAM_SUFFIX):
            continue
        role = key[len(PROVIDER_SECTION) : -len(CREDENTIAL_PARAM_SUFFIX)]
        selection = SURFACE_ENV_BY_KEY.get(f"{PROVIDER_SECTION}{role}")
        assert selection is not None, (
            f"the surface names a credential for the {role} role but no key selecting "
            "which implementation that role resolves to"
        )
        paired[role] = (env, selection)
    return paired


CREDENTIAL_AND_SELECTION: Final[Mapping[str, tuple[str, str]]] = _credential_and_selection()

# The names each provider role's registry accepts, so a selection is checked against
# what can actually be resolved rather than only for being present.
REGISTERED_PROVIDER_NAMES: Final[Mapping[str, frozenset[str]]] = {
    EMBEDDING_ROLE: frozenset(EMBEDDING_PROVIDERS),
    TEXT_ROLE: frozenset(TEXT_PROVIDERS),
}

# The parameter hierarchy every secret is referenced through, and the operations a
# role uses to read one.
PARAMETER_PREFIX_REFERENCE: Final[str] = "${ParameterPrefix}"
PARAMETER_READ_ACTIONS: Final[frozenset[str]] = frozenset({"ssm:GetParameter", "ssm:GetParameters"})

# The signing operation, granted to one role and no other.
SIGN_ACTION: Final[str] = "kms:Sign"

# The condition key an exception to a denial is written against. It resolves to the role
# for a session assumed from that role, which is what a not-principal element does not do
# and why the exception is written this way.
PRINCIPAL_ARN_CONDITION_KEY: Final[str] = "aws:PrincipalArn"

# The operations a denial on the evidence bucket must cover, and the ones it must not.
#
# It must cover every way to reach an object or enumerate the bucket, which is what
# "no principal outside these roles may read or write the evidence" means.
#
# It must not cover the bucket's own administration. A resource policy denying its own
# replacement is unrecoverable, because an explicit denial there overrides every identity
# policy — so such a statement locks out the principal that deploys it and leaves the
# bucket repairable only by the account root. It is also security theatre: a principal
# able to rewrite the policy could rewrite it to permit itself.
DATA_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
    }
)
SELF_LOCKING_ACTIONS: Final[tuple[str, ...]] = (
    "s3:PutBucketPolicy",
    "s3:DeleteBucketPolicy",
    "s3:GetBucketPolicy",
)

# The stack that creates every role this deployment names as a principal, and the two
# stacks whose policies name one. A policy naming a principal that does not exist is
# refused when the policy's resource is created, so this ordering is a create-time
# requirement rather than a preference, and it is asserted below.
ROLES_STACK: Final[str] = "roles"
PRINCIPAL_NAMING_STACKS: Final[tuple[str, ...]] = ("kms", "storage")

# How a role name is carried where a template names one: a parameter whose name ends
# this way holds a role name, in the parameter file as well as in the template.
ROLE_NAME_SUFFIX: Final[str] = "RoleName"

# The two resource types that carry an identity's privileges: a role with its own
# inline policies, and a policy attached by name to roles that already exist. Both are
# walked wherever a grant is counted, because a grant moved from one to the other is
# the same privilege held by the same principal and has to be counted the same way.
ROLE_TYPE: Final[str] = "AWS::IAM::Role"
ATTACHED_POLICY_TYPE: Final[str] = "AWS::IAM::Policy"

# The one operation permitted to name an open resource, and the condition key that
# narrows it. The operation accepts no resource of its own, so the namespace
# condition is the narrowing.
NAMESPACE_CONDITIONED_ACTION: Final[str] = "cloudwatch:PutMetricData"
NAMESPACE_CONDITION_KEY: Final[str] = "cloudwatch:namespace"

# The operation a task execution role uses to obtain a registry token. Like the metric
# publication it accepts no resource of its own, so an open resource is the only form
# expressible; unlike it, no condition narrows it, and what it confers is a token for
# the caller's own account. The pull it enables is granted separately and is scoped to
# one repository, which is where the narrowing actually lives.
REGISTRY_AUTHORISATION_ACTION: Final[str] = "ecr:GetAuthorizationToken"

# Every operation permitted to name an open resource, because each accepts none.
RESOURCELESS_ACTIONS: Final[frozenset[str]] = frozenset(
    {NAMESPACE_CONDITIONED_ACTION, REGISTRY_AUTHORISATION_ACTION}
)

# The declared ingest concurrency ceiling, and the parameter that carries it.
RESERVED_CONCURRENCY_PARAMETER: Final[str] = "ReservedConcurrency"
RESERVED_CONCURRENCY_DEFAULT: Final[int] = 10

# The values the deployment script resolves from the outputs of stacks already
# deployed rather than from the parameter file. None of them may appear in the
# parameter file, because a resource name resolved at deployment time is not a value
# an operator maintains by hand.
#
# The two role resource names are here because a stack of their own creates the roles
# and the two function stacks run under them, so each function stack is
# given its role rather than declaring one. The certificate bucket's resource name is
# not: the only statement that named it is the console role's certificate grant, and
# that statement is now attached by the storage stack, which holds the bucket and
# constructs the name itself.
RESOLVED_AT_DEPLOYMENT: Final[tuple[str, ...]] = (
    "CollectorExecutionRoleArn",
    "ConsoleExecutionRoleArn",
    "SigningKeyArn",
    "CertificateBucketName",
    "ConsoleOriginDomain",
    "PublicSubnetIds",
    "TaskSecurityGroupId",
    "ClusterName",
)

# The count parameter both task services take, the default it carries, and the two
# stacks that declare it. A literal count is what makes a first deployment wait out a
# rollback: a service creation waits for steady state, and no task reaches steady
# state before the image its stack names exists, so the count has to be a value the
# deployment supplies rather than a constant in the template.
DESIRED_COUNT_PARAMETER: Final[str] = "DesiredCount"
DESIRED_COUNT_DEFAULT: Final[int] = 1
DESIRED_COUNT_FLOOR: Final[int] = 0
TASK_STACKS: Final[tuple[str, ...]] = ("watcher", "mcp")

# The tool server's transport that reads a peer on its own standard input. A task
# carrying it has no peer, so it serves its tools and exits; a service of it is a
# restart loop rather than a running server, which is why the count and the transport
# have to agree.
PROCESS_TRANSPORT: Final[str] = "stdio"

# The two functions a regional endpoint is put in front of, and the two values that keep
# such an endpoint transparent: the route key matching every method and path, and the
# platform's unnamed stage, which serves at the root rather than under a path segment.
FRONTED_STACKS: Final[tuple[str, ...]] = ("collector", "console")
CATCH_ALL_ROUTE: Final[str] = "$default"
UNNAMED_STAGE: Final[str] = "$default"

# The one form a value an operator must still supply is written in, and the separator
# a multi-valued parameter states several of them with. One form is what makes the
# outstanding set checkable: a placeholder that reads like a plausible resource name
# is a value a deployment accepts and a role creation then refuses.
PLACEHOLDER_SHAPE: Final[re.Pattern[str]] = re.compile(r"REPLACE_WITH_[A-Z0-9_]+")
PLACEHOLDER_SEPARATOR: Final[str] = ","

# The section of the parameter file every stack shares, which carries values an
# operator supplies too and is therefore checked alongside the per-stack sections.
COMMON_SECTION: Final[str] = "common"

# The longest description the platform accepts on a template. Past it the template is
# refused whole, at change-set creation, with a fault that names a length and nothing
# about the deployment.
TEMPLATE_DESCRIPTION_CAP: Final[int] = 1024


@dataclass(frozen=True, slots=True)
class Template:
    """One parsed template, reduced to the nodes the assertions walk."""

    stack: str
    document: Mapping[str, object]

    @property
    def resources(self) -> Mapping[str, Mapping[str, object]]:
        node = self.document.get("Resources")
        if not isinstance(node, dict):
            return {}
        return {
            str(name): _as_mapping(body, f"resource {name} of {self.stack}")
            for name, body in node.items()
        }

    @property
    def parameters(self) -> Mapping[str, Mapping[str, object]]:
        node = self.document.get("Parameters")
        if not isinstance(node, dict):
            return {}
        return {
            str(name): _as_mapping(body, f"parameter {name} of {self.stack}")
            for name, body in node.items()
        }

    def of_type(self, resource_type: str) -> tuple[Mapping[str, object], ...]:
        """Every resource of one type, as its properties mapping."""
        return tuple(
            _as_mapping(body.get("Properties", {}), "properties")
            for body in self.resources.values()
            if body.get("Type") == resource_type
        )

    @property
    def types(self) -> frozenset[str]:
        return frozenset(
            str(body.get("Type")) for body in self.resources.values() if "Type" in body
        )


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    """Narrow a parsed node to a mapping with string keys."""
    assert isinstance(value, Mapping), f"{label} is no mapping"
    return {str(key): item for key, item in value.items()}


def _as_sequence(value: object) -> Sequence[object]:
    """Read a node that may be one item or a list of them as a list either way."""
    if isinstance(value, list):
        return value
    return [value]


def _load(path: Path) -> Mapping[str, object]:
    reader = YAML(typ="safe")
    with path.open("r", encoding="utf-8") as handle:
        document: object = reader.load(handle)
    return _as_mapping(document, f"the template {path.name}")


def _string_leaves(value: object) -> Iterator[str]:
    """Yield every string held anywhere inside a parsed node, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)


def _policy_documents(template: Template) -> Iterator[tuple[str, Mapping[str, object]]]:
    """Every policy document a template declares, with the resource that holds it."""
    for name, body in template.resources.items():
        properties = _as_mapping(body.get("Properties", {}), f"properties of {name}")
        for field in ("AssumeRolePolicyDocument", "PolicyDocument", "KeyPolicy"):
            node = properties.get(field)
            if isinstance(node, Mapping):
                yield name, _as_mapping(node, f"{field} of {name}")
        for policy in _as_sequence(properties.get("Policies", [])):
            if isinstance(policy, Mapping):
                nested = policy.get("PolicyDocument")
                if isinstance(nested, Mapping):
                    yield name, _as_mapping(nested, f"policy of {name}")


CONDITIONAL_NODE: Final[str] = "Fn::If"

# The node a conditional branch names to stand for no value at all, which is how a
# statement is omitted rather than emptied.
ABSENT_VALUE: Final[Mapping[str, str]] = {"Ref": "AWS::NoValue"}


def _unconditioned(item: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    """One entry of a statement list as the statements it can resolve to.

    A statement may be written as a conditional whose branches are a statement and
    the absent value, which is the only way to omit a statement the platform would
    otherwise refuse for naming a resource that was not supplied. Resolving it here
    rather than at each caller is what keeps a condition from being a place to hide
    a statement: every check in this file walks statements through this, so wrapping
    a grant in a condition changes when it is created and not whether it is seen.
    """
    if tuple(item) != (CONDITIONAL_NODE,):
        return (item,)
    branches = _as_sequence(item[CONDITIONAL_NODE])
    resolved: list[Mapping[str, object]] = []
    for branch in branches[1:]:
        if isinstance(branch, Mapping) and branch != ABSENT_VALUE:
            resolved.extend(_unconditioned(_as_mapping(branch, "a conditional statement")))
    return tuple(resolved)


def _statements(document: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(
        resolved
        for item in _as_sequence(document.get("Statement", []))
        if isinstance(item, Mapping)
        for resolved in _unconditioned(_as_mapping(item, "a policy statement"))
    )


def _conditioned_statements(
    template: Template,
) -> tuple[tuple[str, str, Mapping[str, object]], ...]:
    """Every statement a template states only under a condition, with that condition.

    Yields the resource holding the policy, the condition's name, and the statement,
    so a case can assert that a particular grant is conditional rather than merely
    present.
    """
    found: list[tuple[str, str, Mapping[str, object]]] = []
    for holder, document in _policy_documents(template):
        for item in _as_sequence(document.get("Statement", [])):
            if not isinstance(item, Mapping) or tuple(item) != (CONDITIONAL_NODE,):
                continue
            branches = _as_sequence(item[CONDITIONAL_NODE])
            named = str(branches[0])
            for branch in branches[1:]:
                if isinstance(branch, Mapping) and branch != ABSENT_VALUE:
                    found.append((holder, named, _as_mapping(branch, "a conditional statement")))
    return tuple(found)


def _role_arns(node: object) -> tuple[str, ...]:
    """Every constructed role resource name a node names, in order.

    A principal and a condition value hold the same names under different wrappers — one
    carries the principal type as a key, the other does not — so a comparison of the two
    reads the names rather than the nodes.
    """
    return tuple(leaf for leaf in _string_leaves(node) if ":role/" in leaf)


def _actions(statement: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(item) for item in _as_sequence(statement.get("Action", [])))


def _properties(template: Template, name: str) -> Mapping[str, object]:
    body = template.resources[name]
    return _as_mapping(body.get("Properties", {}), f"properties of {name}")


def _resolved_role_names(template: Template, node: object) -> tuple[str, ...]:
    """The role names a node names, resolving a parameter reference through its default.

    A role is named either as a literal, which is how the stack that creates it names
    it, or as a reference to a parameter carrying the name, which is how a stack that
    only names an existing role does it. Both are read to the same names here, so a
    grant attached to a role by parameter is counted against the role the parameter
    resolves to rather than against the parameter's spelling.
    """
    names: list[str] = []
    for item in _as_sequence(node):
        if isinstance(item, str):
            names.append(item)
            continue
        if not isinstance(item, Mapping):
            continue
        referenced = item.get("Ref")
        assert isinstance(referenced, str), (
            f"a role name in the {template.stack} template is neither a literal nor a "
            "reference to a parameter, so which role it names cannot be read"
        )
        declared = template.parameters.get(referenced)
        default = declared.get("Default") if declared is not None else None
        assert isinstance(default, str), (
            f"the {template.stack} template names a role through {referenced}, which "
            "declares no default, so which role it names is unreadable here"
        )
        names.append(default)
    return tuple(names)


def _identity_policies(
    template: Template,
) -> Iterator[tuple[str, tuple[str, ...], Mapping[str, object]]]:
    """Every identity policy of one template, with its holder and the roles it binds.

    Both shapes are yielded: a role's own inline policies and its trust document, and
    a policy resource attached to roles by name. The subject of the second is read
    from its role list, so the privilege is attributed to the role that ends up
    holding it rather than to the stack that happened to declare it.
    """
    for name, body in template.resources.items():
        kind = body.get("Type")
        if kind not in {ROLE_TYPE, ATTACHED_POLICY_TYPE}:
            continue
        properties = _properties(template, name)
        documents: list[Mapping[str, object]] = []
        if kind == ROLE_TYPE:
            subject = _resolved_role_names(template, properties.get("RoleName"))
            for policy in _as_sequence(properties.get("Policies", [])):
                if not isinstance(policy, Mapping):
                    continue
                nested = policy.get("PolicyDocument")
                if isinstance(nested, Mapping):
                    documents.append(_as_mapping(nested, f"policy of {name}"))
            trust = properties.get("AssumeRolePolicyDocument")
            if isinstance(trust, Mapping):
                documents.append(_as_mapping(trust, f"trust policy of {name}"))
        else:
            subject = _resolved_role_names(template, properties.get("Roles"))
            assert subject, f"{name} in {template.stack} attaches a policy to no role"
            documents.append(_as_mapping(properties["PolicyDocument"], f"policy of {name}"))
        for document in documents:
            yield name, subject, document


def _identity_grants(action: str) -> tuple[str, ...]:
    """Every role granted one operation by an identity policy, with where it is stated.

    Each entry is the stack, the resource stating the grant, and the role holding it,
    which is what makes the assertion below name a principal rather than a template.
    """
    holders: list[str] = []
    for stack, template in TEMPLATES.items():
        for holder, subject, document in _identity_policies(template):
            for statement in _statements(document):
                if statement.get("Effect") != "Allow" or action not in _actions(statement):
                    continue
                holders.extend(f"{stack}:{holder}:{role}" for role in subject)
    return tuple(dict.fromkeys(holders))


def _roles_created() -> Mapping[str, str]:
    """Every role name some template creates, with the stack that creates it."""
    created: dict[str, str] = {}
    for stack, template in TEMPLATES.items():
        for name, body in template.resources.items():
            if body.get("Type") != ROLE_TYPE:
                continue
            declared = _properties(template, name).get("RoleName")
            for role in _resolved_role_names(template, declared):
                created[role] = stack
    return created


def _principals_named(template: Template) -> tuple[str, ...]:
    """Every role this account holds that a policy of one template names as a principal.

    Read from the principal and excepted-principal nodes of every statement, resolved
    through `Fn::Sub` to the role name the constructed resource name ends with. A
    service principal and the account root name no role of this deployment and are
    skipped, because neither is a resource a template of this repository creates.
    """
    named: list[str] = []
    for _, document in _policy_documents(template):
        for statement in _statements(document):
            for field in ("Principal", "NotPrincipal"):
                for leaf in _string_leaves(statement.get(field)):
                    if ":role/" not in leaf:
                        continue
                    reference = leaf.rsplit(":role/", 1)[-1].strip("${}")
                    declared = template.parameters.get(reference)
                    default = declared.get("Default") if declared is not None else None
                    named.append(default if isinstance(default, str) else reference)
    return tuple(dict.fromkeys(named))


def _environment(template: Template) -> Iterator[tuple[str, str, object]]:
    """Every environment variable a template sets, with its resource and its value node.

    Two shapes, because the two hosting choices declare them differently: a function
    carries a mapping under `Environment.Variables`, and a task definition carries a
    list of name and value pairs per container. Both are read here so an assertion
    about the configured surface covers every deployed process rather than the
    functions alone.
    """
    for name, body in template.resources.items():
        properties = _as_mapping(body.get("Properties", {}), f"properties of {name}")
        node = properties.get("Environment")
        if isinstance(node, Mapping):
            variables = node.get("Variables")
            if isinstance(variables, Mapping):
                for key, value in variables.items():
                    yield (name, str(key), value)
        for container in _as_sequence(properties.get("ContainerDefinitions", [])):
            if not isinstance(container, Mapping):
                continue
            for entry in _as_sequence(container.get("Environment", [])):
                if isinstance(entry, Mapping) and "Name" in entry:
                    yield (name, str(entry["Name"]), entry.get("Value"))


def _configured(stack: str) -> Mapping[str, object]:
    """The configuration variables one stack sets, keyed by name."""
    return {
        name: value
        for _, name, value in _environment(TEMPLATES[stack])
        if name.startswith(CONFIGURATION_PREFIX)
    }


TEMPLATES: Final[Mapping[str, Template]] = {
    stack: Template(stack, _load(TEMPLATE_DIR / f"{stack}.yaml")) for stack in STACK_ORDER
}
PARAMS: Final[Mapping[str, object]] = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
DEPLOY_TEXT: Final[str] = (INFRA_DIR / "deploy.sh").read_text(encoding="utf-8")
TEARDOWN_TEXT: Final[str] = (INFRA_DIR / "teardown.sh").read_text(encoding="utf-8")

# Every script of the repository, by name. A value a deployment is given rather than
# holding by hand is either printed by one of these or by nothing in the tree, and
# which of the two is what the outstanding accounting below states and checks.
SCRIPT_TEXT: Final[Mapping[str, str]] = {
    path.name: path.read_text(encoding="utf-8") for path in sorted(SCRIPT_DIR.glob("*.sh"))
}


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def test_every_stack_has_a_template_that_parses() -> None:
    for stack in STACK_ORDER:
        assert TEMPLATES[stack].resources, f"the {stack} template declares no resource"


def test_no_template_description_exceeds_the_platform_cap() -> None:
    """A description past the cap makes the platform refuse the whole template.

    The refusal is a validation error at change-set creation, so it arrives partway
    through a staged deployment: the stacks before it are created, the stack carrying
    the long description is not, and the reported fault names a length rather than
    anything about the deployment. Three templates were over the cap and each would
    have failed at its own step, one after another, which is a slow way to learn a
    fixed limit.

    The cap is on the description alone. A comment carries none, so reasoning that
    outgrows the cap belongs above the description rather than inside it, and every
    template that hit this now states its reasoning that way.
    """
    for stack in STACK_ORDER:
        described = TEMPLATES[stack].document.get("Description", "")
        assert isinstance(described, str), f"the {stack} template describes itself oddly"
        assert len(described) <= TEMPLATE_DESCRIPTION_CAP, (
            f"the {stack} template's description is {len(described)} characters, past "
            f"the {TEMPLATE_DESCRIPTION_CAP} the platform admits, so the platform "
            "refuses the template rather than the description; move the reasoning "
            "into a comment above it"
        )


def test_every_required_resource_type_is_declared_by_its_stack() -> None:
    for stack, resource_type in REQUIRED_RESOURCES:
        assert resource_type in TEMPLATES[stack].types, (
            f"the {stack} template declares no {resource_type}"
        )


def test_the_deployment_script_declares_the_stacks_in_the_asserted_order() -> None:
    positions = [DEPLOY_TEXT.index(f"\n  {stack}\n") for stack in STACK_ORDER]
    assert positions == sorted(positions)


def test_the_deployment_script_can_stop_at_a_named_stack() -> None:
    """The staged sequence the setup document prescribes has to be executable.

    Four stacks need no application code, the cluster roles are provisioned only once
    the parameter names exist, the two function stacks need an archive in the bucket,
    and the two task stacks need an image in a registry. A script that only ever
    deploys all ten reaches a function stack before any archive exists and fails
    there, which is a rollback rather than a staging point — so the document would
    prescribe an order the tool could not follow. The stop is asserted to be by stack
    name rather than by a count, because a count silently means a different stack the
    moment the order changes, and to be validated against the order, so a typo is
    refused before anything is created.
    """
    assert "--through" in DEPLOY_TEXT, "the deployment script offers no way to stage"
    assert 'through="$2"' in DEPLOY_TEXT, "--through takes no stack name"
    assert "no stack of the deployment order" in DEPLOY_TEXT, (
        "--through accepts a name the deployment order does not hold"
    )
    assert '"${stack}" == "${through}"' in DEPLOY_TEXT, (
        "the stop compares the stack reached against the stack named"
    )


def test_the_deployment_script_can_omit_a_stack_the_account_refuses() -> None:
    """A stack that cannot be created must not make the stacks after it unreachable.

    A refusal is not always about this repository. An account can be denied a resource
    type outright — a new one is denied a content distribution until the provider
    verifies it — and the denial arrives as a create failure partway along the order.
    Every stack after the refused one is then unreachable, though none of them depends
    on it, so the deployment stops at the one thing nobody can fix from here.

    Three claims. The omission is by stack name, validated against the order through
    the same check the stopping point uses, so a misspelled name is refused rather than
    quietly deploying the stack it meant to omit. It may be given more than once,
    because an account refusing one resource type often refuses more. And it is
    announced rather than silent: a stack that was never created is a missing part of
    the deployment, and a reader of the log has to be able to see which.
    """
    assert "--skip" in DEPLOY_TEXT, "the deployment script offers no way to omit a stack"
    assert "skipped+=" in DEPLOY_TEXT, "--skip collects no more than one stack name"
    assert "is_skipped" in DEPLOY_TEXT, "the loop consults no omission list"
    assert "named_stack" in DEPLOY_TEXT, (
        "--skip is not validated against the deployment order, so a misspelled name "
        "would omit nothing and report success"
    )
    assert "skipping" in DEPLOY_TEXT, "an omission is not announced"


def test_the_teardown_script_deletes_in_the_reverse_order() -> None:
    """Reverse order over the stacks the teardown script names, and the one it omits.

    The ordering assertion is unchanged for every stack the script names. What is new
    is the second assertion, and it is a gap rather than a licence: the teardown script
    does not delete the roles stack, so a teardown leaves three named roles behind and
    the next deployment of the roles stack fails on a name already taken. The omission
    is named exactly, so the case fails if any other stack drops out of the script and
    fails again the moment the roles stack is added to it, which is when this exception
    has to be deleted. The script is not this suite's to change.
    """
    named = tuple(stack for stack in STACK_ORDER if f"\n  {stack}\n" in TEARDOWN_TEXT)
    positions = [TEARDOWN_TEXT.index(f"\n  {stack}\n") for stack in reversed(named)]
    assert positions == sorted(positions)
    absent = tuple(stack for stack in STACK_ORDER if stack not in named)
    assert absent == (ROLES_STACK,), (
        f"the teardown script names no deletion for {absent or 'no stack'}; the roles "
        "stack is the one known omission and every other stack must be deleted"
    )


# ---------------------------------------------------------------------------
# Absences the cost ceiling depends on
# ---------------------------------------------------------------------------


def test_no_template_declares_a_forbidden_charged_resource() -> None:
    for stack, template in TEMPLATES.items():
        for forbidden in FORBIDDEN_RESOURCE_TYPES:
            assert forbidden not in template.types, f"the {stack} template declares {forbidden}"


def test_no_service_attaches_a_load_balancer() -> None:
    for stack in ("watcher", "mcp"):
        for service in TEMPLATES[stack].of_type("AWS::ECS::Service"):
            assert "LoadBalancers" not in service, f"the {stack} service attaches a load balancer"
            assert "ServiceRegistries" not in service


def test_no_template_mentions_a_load_balancer_or_translation_gateway_anywhere() -> None:
    for stack, template in TEMPLATES.items():
        for leaf in _string_leaves(template.document.get("Resources")):
            folded = leaf.lower()
            assert "loadbalancer" not in folded, f"the {stack} template names a load balancer"
            assert "natgateway" not in folded, f"the {stack} template names a translation gateway"
            assert "vpcendpoint" not in folded, f"the {stack} template names an interface endpoint"


# ---------------------------------------------------------------------------
# The configured surface: what each process is actually given
# ---------------------------------------------------------------------------


def test_every_configuration_variable_a_template_sets_is_a_key_the_surface_declares() -> None:
    """A variable the surface declares nothing for is read by nobody.

    This is the failure that hides: the deployment succeeds, the variable is visibly
    set on the function, and the process resolves the key it does declare from its
    default or refuses for a value an operator can see is there. Nothing in the
    template's own text says which of the two happened, so the surface is asked.
    """
    declared = {setting.env for setting in SETTINGS}
    for stack, template in TEMPLATES.items():
        for holder, name, _ in _environment(template):
            if not name.startswith(CONFIGURATION_PREFIX):
                continue
            assert name in declared, (
                f"{holder} in {stack} sets {name}, which the configuration surface "
                "declares no setting for, so the process reads nothing from it"
            )


def test_every_stack_that_connects_to_the_cluster_names_its_parameter_and_its_role() -> None:
    """Presence, not just correctness of spelling.

    The connection parameter carries no default, because a default naming where a
    credential lives would ship a reference no operator chose. So a stack that omits
    it does not fall back: it refuses at startup.
    """
    for stack in CLUSTER_STACKS:
        configured = _configured(stack)
        assert DSN_PARAM_ENV in configured, f"the {stack} stack names no connection parameter"
        assert ROLE_ENV in configured, f"the {stack} stack names no database role"


def test_each_stack_declares_the_role_its_connection_parameter_names() -> None:
    """The label and the connection must agree, because the label is checked.

    The read-only guarantee of the verification path refuses a connection whose
    configured label is not the read-only role, so a stack pointed at the reader
    connection while labelled something wider refuses its own reads. The label is a
    claim and the parameter path is the privilege, and a disagreement between them is
    a deployment that fails for a reason no code change explains.
    """
    for stack in CLUSTER_STACKS:
        configured = _configured(stack)
        assert {DSN_PARAM_ENV, ROLE_ENV} <= configured.keys(), (
            f"the {stack} stack sets neither name, which the case above states"
        )
        path = " ".join(_string_leaves(configured[DSN_PARAM_ENV]))
        named = path.rsplit("/", 1)[-1].strip()
        role = " ".join(_string_leaves(configured[ROLE_ENV])).strip()
        assert named == role, (
            f"the {stack} stack connects through {named!r} while declaring the {role!r} role"
        )


def test_every_stack_given_a_provider_credential_also_selects_that_role_provider() -> None:
    """A credential without a selection hands a process the wrong provider's secret.

    This is the gap the case above cannot see. That one asks whether every variable a
    template sets is a key the surface declares, and a variable a template *omits* is
    invisible to it. Both selection keys carry a default, so an omitted selection does
    not refuse: the process resolves the default implementation while holding the
    credential of the one the deployment meant to use. The default's own builder then
    demands values no template sets, so the deployment refuses at startup for a
    missing region rather than for the selection nobody made, and the reported fault
    names neither the credential nor the provider.

    Which stacks are checked comes from the templates rather than from a list here, so
    a process given a provider credential is covered the moment it is given one. The
    selected name is checked against the registry too, because a name the registry
    does not hold is refused before anything is imported.
    """
    assert CREDENTIAL_AND_SELECTION, "the surface pairs no credential key with a selection key"
    holding: list[str] = []
    for stack in STACK_ORDER:
        configured = _configured(stack)
        for role, (credential_env, selection_env) in sorted(CREDENTIAL_AND_SELECTION.items()):
            if credential_env not in configured:
                continue
            holding.append(f"{stack}:{role}")
            assert selection_env in configured, (
                f"the {stack} stack sets {credential_env} but no {selection_env}, so "
                f"the process resolves the default {role} provider while holding "
                "another provider's credential"
            )
            selected = " ".join(_string_leaves(configured[selection_env])).strip()
            registered = REGISTERED_PROVIDER_NAMES.get(role)
            if registered is not None:
                assert selected in registered, (
                    f"the {stack} stack selects {selected!r} for the {role} role, "
                    f"which the registry does not hold; the names are "
                    f"{', '.join(sorted(registered))}"
                )
    assert holding, "no stack is given a provider credential, so this case checks nothing"


def _stacks_running_as(role: str) -> tuple[str, ...]:
    """Every stack whose function runs under one role, traced through the roles stack.

    The chain is structural rather than textual: the role resource carrying that name
    is found in the stack that creates it, the output publishing its resource name is
    the one whose value reads that resource, and a function running under the role is
    one whose `Role` names the parameter the deployment fills from that output. So a
    process moved to another role, or a role published under another output, moves
    the answer with it instead of matching a spelling that no longer means anything.
    """
    creating = _roles_created().get(role)
    assert creating is not None, f"no template creates the role {role}"
    template = TEMPLATES[creating]
    resources = tuple(
        name
        for name, body in template.resources.items()
        if body.get("Type") == ROLE_TYPE
        and role in _resolved_role_names(template, _properties(template, name).get("RoleName"))
    )
    outputs = template.document.get("Outputs")
    assert isinstance(outputs, Mapping), f"the {creating} template publishes no output"
    published = tuple(
        str(key)
        for key, node in outputs.items()
        if any(
            resource in tuple(_string_leaves(_as_mapping(node, "an output").get("Value")))
            for resource in resources
        )
    )
    assert published, f"the {creating} stack creates {role} and publishes no resource name for it"
    running: list[str] = []
    for stack, other in TEMPLATES.items():
        for function in other.of_type("AWS::Lambda::Function"):
            taken = tuple(_string_leaves(function.get("Role")))
            if any(name in taken for name in published):
                running.append(stack)
    return tuple(dict.fromkeys(running))


def test_the_signing_process_is_given_every_certificate_value_only_it_can_know() -> None:
    """A grant with no configured target is a permission the process cannot use.

    The signing role is found by the operation granted to it rather than named, and
    the process is found by the role it runs under, so hosting the signer elsewhere
    moves this assertion with it. The grant and the process are now declared by two
    different stacks — the key stack attaches the signing permission, the console
    stack runs the function under the role it is attached to — which is exactly why
    the link is followed through the role instead of assuming one stack holds both.
    Each key checked is a certificate key the surface leaves without a default, which
    makes it required: a process granted the write but given no bucket refuses at
    startup, and one given a prefix other than the prefix its grant narrows to is
    refused by the bucket.
    """
    signing = _identity_grants(SIGN_ACTION)
    assert signing, "no identity policy grants the signing operation"
    assert REQUIRED_CERTIFICATE_ENV, "the surface declares no required certificate key"
    for entry in signing:
        role = entry.rsplit(":", 1)[-1]
        hosts = _stacks_running_as(role)
        assert hosts, f"the signing role {role} is the role of no deployed function"
        for stack in hosts:
            configured = _configured(stack)
            for env in REQUIRED_CERTIFICATE_ENV:
                assert env in configured, (
                    f"the {stack} stack runs as {role}, which signs certificates, but "
                    f"sets no {env}, which the surface requires and gives no default for"
                )


# ---------------------------------------------------------------------------
# Secrets: none held, every one referenced by name
# ---------------------------------------------------------------------------


def test_no_template_embeds_a_value_shaped_like_a_secret() -> None:
    for stack, template in TEMPLATES.items():
        for leaf in _string_leaves(template.document):
            for description, pattern in SECRET_SHAPES:
                assert pattern.search(leaf) is None, f"the {stack} template embeds {description}"


def test_the_parameter_file_embeds_no_value_shaped_like_a_secret() -> None:
    for leaf in _string_leaves(PARAMS):
        for description, pattern in SECRET_SHAPES:
            assert pattern.search(leaf) is None, f"the parameter file embeds {description}"


def test_every_declared_parameter_resource_is_in_the_standard_tier() -> None:
    for stack, template in TEMPLATES.items():
        for parameter in template.of_type("AWS::SSM::Parameter"):
            assert parameter.get("Tier") == "Standard", (
                f"a parameter in the {stack} template is not in the standard tier"
            )


def test_the_ingest_signing_secret_is_declared_as_a_parameter_resource() -> None:
    declared = tuple(
        leaf
        for parameter in TEMPLATES["parameters"].of_type("AWS::SSM::Parameter")
        for leaf in _string_leaves(parameter.get("Name"))
    )
    assert any("ingress-secret" in name for name in declared), (
        "no parameter resource declares the ingest signing secret"
    )


def test_no_parameter_resource_carries_a_literal_value() -> None:
    for stack, template in TEMPLATES.items():
        for parameter in template.of_type("AWS::SSM::Parameter"):
            value = parameter.get("Value")
            assert isinstance(value, Mapping), (
                f"a parameter in the {stack} template carries a literal value"
            )


def test_every_role_reads_secrets_by_parameter_name_only() -> None:
    for stack, template in TEMPLATES.items():
        for holder, document in _policy_documents(template):
            for statement in _statements(document):
                if not PARAMETER_READ_ACTIONS.intersection(_actions(statement)):
                    continue
                references = tuple(_string_leaves(statement.get("Resource")))
                assert references, f"{holder} in {stack} reads a parameter with no resource named"
                for reference in references:
                    if reference.startswith("arn:"):
                        assert PARAMETER_PREFIX_REFERENCE in reference, (
                            f"{holder} in {stack} names a parameter outside the hierarchy"
                        )


# ---------------------------------------------------------------------------
# Least privilege
# ---------------------------------------------------------------------------


def test_the_signing_operation_is_granted_to_exactly_one_role_in_the_whole_deployment() -> None:
    """Exactly one principal may sign, which Requirement 30.9 is the obligation for.

    The grant no longer sits in the role's own declaration: the roles stack declares
    the role and cannot name the key, and the key stack attaches the signing
    permission to it by name once the key exists. The obligation is unchanged, so the
    assertion moved with the grant rather than being dropped or loosened. What is
    counted is every identity policy of every stack — a role's inline policies and
    every policy attached to roles by name — reduced to the roles that end up holding
    the operation. A second role gaining `kms:Sign` anywhere, in any stack, by either
    shape, adds an entry and fails this exactly as a second inline grant did before,
    and so does attaching the existing policy to a second role, which is the new way
    the exclusivity could be lost.
    """
    holders = _identity_grants(SIGN_ACTION)
    assert holders == ("kms:ConsoleExecutionRoleSigningPolicy:molt-console-exec",), (
        f"the signing operation is held by {holders or 'nobody'}"
    )
    signing = frozenset(entry.rsplit(":", 1)[-1] for entry in holders)
    assert len(signing) == 1, f"more than one role may sign: {sorted(signing)}"


def test_the_key_policy_grants_signing_to_one_principal_and_denies_every_other() -> None:
    key = TEMPLATES["kms"].of_type("AWS::KMS::Key")[0]
    policy = _as_mapping(key["KeyPolicy"], "the key policy")
    allowed = [
        statement
        for statement in _statements(policy)
        if statement.get("Effect") == "Allow" and SIGN_ACTION in _actions(statement)
    ]
    denied = [
        statement
        for statement in _statements(policy)
        if statement.get("Effect") == "Deny" and SIGN_ACTION in _actions(statement)
    ]
    assert len(allowed) == 1, "the key policy allows signing in more than one statement"
    assert len(denied) == 1, "the key policy states no denial of signing to other principals"

    # The exception is a condition on the requesting principal, and must not be a
    # not-principal element. That element is matched against the principal that made the
    # request, and a function does not request as its role — it requests as a session
    # assumed from that role, a different name — so an exception written that way matches
    # nothing the function sends and the denial applies to the one role the policy grants
    # signing to. It shipped that way: the scheduled checkpoint ran on time and every run
    # failed on an explicit denial, which reads like a missing grant and is its opposite.
    assert "NotPrincipal" not in denied[0], (
        "the signing denial excepts a principal with a not-principal element; a role "
        "named there does not match a session assumed from it, so the denial catches the "
        "very role the policy grants signing to"
    )
    assert denied[0].get("Principal") == "*", (
        "the signing denial names a principal set narrower than every principal, so a "
        "principal outside it is neither denied nor accounted for"
    )
    condition = _as_mapping(denied[0].get("Condition", {}), "the denial condition")
    unlike = _as_mapping(condition.get("StringNotLike", {}), "the excepted principal")
    # Compared as the role names each side constructs rather than as every string in each
    # node, because the granted principal carries its own key and the condition value does
    # not, and neither wrapper is part of the claim.
    excepted = _role_arns(unlike.get(PRINCIPAL_ARN_CONDITION_KEY))
    assert excepted, (
        f"the signing denial is not conditioned on {PRINCIPAL_ARN_CONDITION_KEY}, so what "
        "it excepts cannot be read"
    )
    granted = _role_arns(allowed[0].get("Principal"))
    assert granted == excepted, (
        f"the denial excepts {excepted} where signing is granted to {granted}, so the "
        "policy denies a principal it grants or grants one it denies"
    )


def test_no_administrative_key_statement_carries_the_signing_operation() -> None:
    key = TEMPLATES["kms"].of_type("AWS::KMS::Key")[0]
    policy = _as_mapping(key["KeyPolicy"], "the key policy")
    for statement in _statements(policy):
        if statement.get("Sid") == "AdministrationWithoutSigning":
            assert SIGN_ACTION not in _actions(statement)


def test_only_an_operation_that_accepts_no_resource_names_an_open_one() -> None:
    """Every identity policy is walked, inline and attached alike.

    A grant moved out of a role's own declaration into a policy the key or storage
    stack attaches is the same privilege held by the same principal, so it is checked
    here on the same terms; otherwise the restructure would have moved statements out
    of this case's reach.

    Two operations in this deployment accept no resource of their own, so a narrowed
    resource is not available to either and an open one is the only expressible form.
    Each is admitted on terms of its own rather than by widening the rule. The metric
    publication is narrowed by a namespace condition, which is asserted. The registry
    token call cannot be narrowed at all — it answers a token for the caller's own
    account and nothing else — so what is asserted instead is that it is alone in its
    statement, which is what stops an open resource being reached by bundling a second
    operation alongside it. That bundling is not hypothetical: the four registry
    operations and the two log operations were once one statement under one resource,
    and the resource was the log group.
    """
    for stack, template in TEMPLATES.items():
        for holder, _, document in _identity_policies(template):
            for statement in _statements(document):
                resources = tuple(_string_leaves(statement.get("Resource")))
                if "*" not in resources:
                    continue
                actions = _actions(statement)
                assert len(actions) == 1 and actions[0] in RESOURCELESS_ACTIONS, (
                    f"{holder} in {stack} names an open resource for {actions}; only an "
                    "operation that accepts no resource may, and only on its own"
                )
                if actions[0] == NAMESPACE_CONDITIONED_ACTION:
                    condition = _as_mapping(statement.get("Condition", {}), "condition")
                    equals = _as_mapping(condition.get("StringEquals", {}), "condition values")
                    assert NAMESPACE_CONDITION_KEY in equals, (
                        f"{holder} in {stack} publishes metrics with no namespace condition"
                    )


def test_each_function_is_reachable_through_an_endpoint_that_changes_nothing() -> None:
    """The regional endpoints exist because the functions' own cannot serve anyone here.

    A function endpoint configured for anonymous access is refused by the platform on an
    account the provider has not verified, however correct its resource policy. So each
    function is fronted by a regional endpoint instead, and what is asserted is that
    fronting it changes nothing about what the function receives or who may reach it.

    Four claims. Each endpoint carries every method and every path through, because the
    application's own route table is what decides which paths exist and which of them
    authenticate — a route list repeated here would be a second answer to that question,
    free to drift from the first. Each serves the platform's unnamed stage, because a
    named one becomes the first path segment and every link in the console's own pages is
    written from the root, so a prefix would serve pages whose links all break. Each
    permission is conditioned on its own endpoint, so the grant admits requests arriving
    through it rather than through any endpoint of the account. And each stage bounds the
    request rate, which matters more here than it usually would: the account's whole
    concurrency allowance equals the floor the platform keeps unreserved, so no function
    can reserve any, and this throttle is the only bound left on a leaked credential.
    """
    gateway = TEMPLATES["gateway"]
    fronted = {
        name: body
        for name, body in gateway.resources.items()
        if body.get("Type") == "AWS::ApiGatewayV2::Api"
    }
    assert len(fronted) == len(FRONTED_STACKS), (
        f"the gateway fronts {len(fronted)} functions where {len(FRONTED_STACKS)} are "
        "deployed, so one of them is reachable only by direct invocation"
    )

    for name, body in fronted.items():
        assert _as_mapping(body["Properties"], f"{name}").get("ProtocolType") == "HTTP"

    routes = tuple(
        _as_mapping(properties, "a route")
        for properties in gateway.of_type("AWS::ApiGatewayV2::Route")
    )
    assert routes, "the gateway declares no route, so no request reaches a function"
    for route in routes:
        assert route.get("RouteKey") == CATCH_ALL_ROUTE, (
            f"a gateway route matches {route.get('RouteKey')} rather than every method "
            "and path, so which paths exist is answered here as well as in the "
            "application's own route table"
        )

    stages = tuple(
        _as_mapping(properties, "a stage")
        for properties in gateway.of_type("AWS::ApiGatewayV2::Stage")
    )
    assert len(stages) == len(fronted), "an endpoint has no stage, so it serves nothing"
    for stage in stages:
        assert stage.get("StageName") == UNNAMED_STAGE, (
            f"a stage is named {stage.get('StageName')}, which becomes the first path "
            "segment of every address; the console's pages link from the root, so a "
            "prefix serves pages whose every link is wrong"
        )
        settings = _as_mapping(stage["DefaultRouteSettings"], "the route settings")
        assert "ThrottlingRateLimit" in settings, "a stage bounds no request rate"
        assert "ThrottlingBurstLimit" in settings, "a stage bounds no burst"

    permissions = tuple(
        _as_mapping(properties, "a permission")
        for properties in gateway.of_type("AWS::Lambda::Permission")
    )
    assert len(permissions) == len(fronted), (
        "an endpoint has no permission to invoke its function, so every request through "
        "it is refused"
    )
    for permission in permissions:
        assert permission.get("Principal") == "apigateway.amazonaws.com"
        source = tuple(_string_leaves(permission.get("SourceArn")))
        assert source, (
            "a gateway permission names no source, so it admits requests arriving "
            "through any endpoint of the account rather than only this one"
        )


def test_the_tool_server_states_its_transport_and_its_count_agrees_with_it() -> None:
    """A transport left to a default, and a service that cannot stay up under it.

    The tool server serves two transports. One reads a peer on its own standard input;
    the other listens. Which is right is a property of how the server is reached, so it
    is the deployment's choice, and the template stated neither — so the process
    resolved the surface default, got the process transport, served its tools to nobody,
    and exited. The service restarted it about once a minute, indefinitely, and every
    restart logged a clean start-up and a clean shutdown, which is the least alarming
    way for a deployment to be broken.

    Two claims, because the variable alone would not have caught it. The transport is
    set explicitly, so no deployed process resolves a default nobody chose. And the
    delivered desired count agrees with the delivered transport: a process transport is
    delivered at a count of zero, because a service of it is a restart loop rather than
    a running server. The second is what makes the first more than a formality — it
    refuses the configuration that was actually deployed, which named a count of one
    beside a transport that cannot hold one.
    """
    configured = _configured("mcp")
    transport_variable = next(
        (name for name in configured if name.endswith("_TRANSPORT")),
        None,
    )
    assert transport_variable is not None, (
        "the mcp stack sets no transport variable, so the deployed process resolves "
        "whichever transport the surface defaults to rather than one the deployment "
        "chose"
    )
    declared = tuple(_string_leaves(configured[transport_variable]))
    parameter = next((name for name in declared if name in TEMPLATES["mcp"].parameters), None)
    assert parameter is not None, (
        f"{transport_variable} is set from {declared}, which names no parameter of the "
        "stack, so an operator cannot choose the transport without editing the template"
    )
    delivered = _as_mapping(PARAMS["mcp"], "the mcp parameters")
    chosen = str(delivered.get(parameter, TEMPLATES["mcp"].parameters[parameter].get("Default")))
    assert chosen in _as_sequence(TEMPLATES["mcp"].parameters[parameter]["AllowedValues"]), (
        f"the delivered transport {chosen} is not one the template admits"
    )
    if chosen == PROCESS_TRANSPORT:
        assert int(str(delivered[DESIRED_COUNT_PARAMETER])) == DESIRED_COUNT_FLOOR, (
            f"the tool server is delivered on the {PROCESS_TRANSPORT} transport at a "
            f"count of {delivered[DESIRED_COUNT_PARAMETER]}; that transport reads a peer "
            "on standard input, so a task carrying it exits and the service restarts it "
            "forever"
        )


def test_no_statement_grants_operations_of_two_services_under_one_resource() -> None:
    """One resource cannot be correct for two services, so a statement mixing them is wrong.

    This is the shape of a defect that reached a deployment. A task execution role
    granted four registry operations and two log operations in a single statement whose
    one resource was the log group, so the registry grant was scoped to a log group and
    no task could pull its image. Nothing refused the template: every action was
    legitimate, the resource was a real ARN, and the statement read plausibly. The
    failure surfaced as a service unable to place a task, naming an authorisation call
    and saying nothing about a resource being wrong.

    A statement's resource is what its actions are granted over, so actions of different
    services in one statement mean at least one of them is granted over a resource that
    cannot be its own. Asserting on the service prefix rather than on any particular
    pairing is what makes this catch the next instance rather than this one.
    """
    for stack, template in TEMPLATES.items():
        for holder, _, document in _identity_policies(template):
            for statement in _statements(document):
                actions = _actions(statement)
                services = {action.split(":", 1)[0] for action in actions if ":" in action}
                assert len(services) <= 1, (
                    f"{holder} in {stack} grants {sorted(services)} operations in one "
                    f"statement over one resource, {actions}; split them so each set is "
                    "scoped to a resource of its own service"
                )


def test_no_role_policy_grants_a_wildcard_operation() -> None:
    for stack, template in TEMPLATES.items():
        for holder, _, document in _identity_policies(template):
            for statement in _statements(document):
                if statement.get("Effect") != "Allow":
                    continue
                for action in _actions(statement):
                    assert action != "*", f"{holder} in {stack} grants every operation"
                    assert not action.endswith(":*"), (
                        f"{holder} in {stack} grants every operation of a service"
                    )


def test_the_bucket_policy_denies_unencrypted_writes_and_outside_principals() -> None:
    policy = TEMPLATES["storage"].of_type("AWS::S3::BucketPolicy")[0]
    document = _as_mapping(policy["PolicyDocument"], "the bucket policy")
    identifiers = {str(statement.get("Sid")) for statement in _statements(document)}
    assert "DenyUnencryptedWrites" in identifiers
    assert "DenyEveryPrincipalOutsideTheNamedRoles" in identifiers
    outside = next(
        statement
        for statement in _statements(document)
        if statement.get("Sid") == "DenyEveryPrincipalOutsideTheNamedRoles"
    )
    assert outside.get("Effect") == "Deny"
    # Conditioned on the requesting principal rather than written as a not-principal
    # element, for the reason the signing denial carries: a role named in a not-principal
    # element does not match a session assumed from that role, so such a denial catches
    # every role it means to except.
    assert "NotPrincipal" not in outside, (
        "the bucket denies every principal outside the named roles with a not-principal "
        "element, which does not match the sessions those roles are assumed as, so it "
        "denies the roles it names"
    )
    assert outside.get("Principal") == "*"
    excepted = tuple(
        _string_leaves(
            _as_mapping(
                _as_mapping(outside.get("Condition", {}), "the denial condition").get(
                    "StringNotLike", {}
                ),
                "the excepted principals",
            ).get(PRINCIPAL_ARN_CONDITION_KEY)
        )
    )
    assert excepted, (
        f"the bucket denial is not conditioned on {PRINCIPAL_ARN_CONDITION_KEY}, so which "
        "principals it excepts cannot be read"
    )

    # The denial reaches the evidence and stops short of the bucket's own administration.
    # A resource policy that denies its own replacement is unrecoverable: an explicit
    # denial there is not overridable by any identity policy, so a denial of every
    # operation also denies the deploying principal the ability to correct it, and the
    # bucket can then be neither repaired, emptied, nor deleted by anything but the
    # account root. That happened to this bucket's predecessor.
    #
    # Nothing is given up by the boundary. A principal permitted to rewrite this policy
    # could rewrite it to permit itself, so the denial never stood between such a principal
    # and an object; what protects a written certificate is Object Lock, declared on the
    # bucket where no policy edit reaches it.
    denied_actions = frozenset(_actions(outside))
    assert denied_actions, "the bucket denial names no operation"
    assert "s3:*" not in denied_actions, (
        "the bucket denies every operation to principals outside the named roles, which "
        "denies the deploying principal the ability to replace this policy and leaves the "
        "bucket repairable only by the account root"
    )
    for administrative in SELF_LOCKING_ACTIONS:
        assert administrative not in denied_actions, (
            f"the bucket denial covers {administrative}, so this policy denies its own "
            "replacement and the bucket cannot be recovered by the principal that deployed it"
        )
    assert denied_actions >= DATA_ACTIONS, (
        "the bucket denial leaves an evidence operation reachable by a principal outside "
        f"the named roles; {sorted(DATA_ACTIONS - denied_actions)} is not denied"
    )


def test_every_role_a_policy_names_as_a_principal_exists_before_that_policy() -> None:
    """The failure this restructure answers, asserted rather than remembered.

    A key policy or a bucket policy naming a role as a principal is validated when the
    resource is created: the platform refuses the whole statement if the principal does
    not exist yet. Naming a role a later stack creates is therefore not a soft
    ordering preference, it is a create failure, and naming one no template creates at
    all can never succeed. Both directions are checked here — some template creates
    every named role, and the stack creating it comes earlier in the deployment order
    than the stack naming it.
    """
    created = _roles_created()
    assert created, "no template creates a role"
    for stack, template in TEMPLATES.items():
        for role in _principals_named(template):
            creating = created.get(role)
            assert creating is not None, (
                f"the {stack} template names {role} as a principal and no template "
                "creates it, so the resource carrying that policy cannot be created"
            )
            assert STACK_ORDER.index(creating) < STACK_ORDER.index(stack), (
                f"the {stack} template names {role} as a principal while {creating} "
                "creates it no earlier, so the principal does not exist yet"
            )


def test_the_roles_stack_precedes_every_stack_whose_policy_names_a_role() -> None:
    for stack in PRINCIPAL_NAMING_STACKS:
        assert STACK_ORDER.index(ROLES_STACK) < STACK_ORDER.index(stack), (
            f"{stack} names a role as a principal before the roles stack creates one"
        )
    assert TEMPLATES[ROLES_STACK].of_type(ROLE_TYPE), "the roles stack creates no role"


def test_the_roles_stack_resolves_no_value_from_a_later_stack() -> None:
    """What makes the roles stack deployable third, stated as a check.

    Its permissions are the ones whose resource is constructible from the account, the
    region, and a parameter, so nothing it declares waits on a resource that does not
    exist yet. A parameter carrying a value the deployment resolves from a later
    stack's outputs would put the cycle straight back, so the presence of one is the
    failure.
    """
    declared = frozenset(TEMPLATES[ROLES_STACK].parameters)
    for name in RESOLVED_AT_DEPLOYMENT:
        assert name not in declared, (
            f"the roles stack takes {name}, which the deployment resolves from a "
            "stack's outputs, so it could no longer be created before them"
        )


def test_neither_function_stack_creates_a_role_and_each_takes_its_own_as_a_parameter() -> None:
    """The function stacks stopped creating roles, and must not start again.

    A role created here is created after the key and the bucket, which name it, so a
    template that reverts to creating one restores the failure. The function's `Role`
    is asserted to be a reference to a parameter rather than a reference to a resource
    of its own, and the parameter is asserted to be one the deployment resolves.
    """
    for stack in ("collector", "console"):
        template = TEMPLATES[stack]
        assert ROLE_TYPE not in template.types, (
            f"the {stack} template creates a role again, which the stacks naming it "
            "as a principal are created before"
        )
        function = template.of_type("AWS::Lambda::Function")[0]
        taken = _as_mapping(function["Role"], f"the role of the {stack} function")
        referenced = taken.get("Ref")
        assert isinstance(referenced, str), (
            f"the {stack} function does not take its role from a parameter"
        )
        assert referenced in template.parameters, (
            f"the {stack} function names {referenced}, which the template declares no parameter for"
        )
        assert referenced in RESOLVED_AT_DEPLOYMENT, (
            f"the {stack} function takes {referenced}, which the deployment resolves "
            "from no stack's outputs"
        )


def test_every_role_name_the_parameter_file_states_is_a_role_some_template_creates() -> None:
    """A role name is the whole of the link between the stacks, so it is checked.

    The key stack and the storage stack take role *names* rather than resource names,
    which is what lets them run before the functions and resolve nothing. The cost is
    that a mistyped name is a name that resolves to nothing: the key policy would be
    refused at creation and an attached policy would fail to find its role. Both the
    template defaults and the values the parameter file supplies are checked against
    the roles the templates actually create.
    """
    created = frozenset(_roles_created())
    for stack, template in TEMPLATES.items():
        for name, fields in template.parameters.items():
            if not name.endswith(ROLE_NAME_SUFFIX):
                continue
            default = fields.get("Default")
            assert isinstance(default, str) and default in created, (
                f"the {stack} template defaults {name} to {default!r}, which no template creates"
            )
        section = PARAMS.get(stack)
        if not isinstance(section, dict):
            continue
        for name, value in section.items():
            if str(name).endswith(ROLE_NAME_SUFFIX):
                assert value in created, (
                    f"the parameter file states {value!r} for {name} of {stack}, which "
                    "no template creates"
                )


# ---------------------------------------------------------------------------
# Shape of each service
# ---------------------------------------------------------------------------


def test_the_task_security_group_declares_no_inbound_rule() -> None:
    for group in TEMPLATES["network"].of_type("AWS::EC2::SecurityGroup"):
        assert "SecurityGroupIngress" not in group, (
            "the task security group declares an inbound rule"
        )


def test_both_tasks_run_in_public_subnets_with_no_port_mapping() -> None:
    for stack in ("watcher", "mcp"):
        service = TEMPLATES[stack].of_type("AWS::ECS::Service")[0]
        network = _as_mapping(service["NetworkConfiguration"], "network configuration")
        awsvpc = _as_mapping(network["AwsvpcConfiguration"], "the network configuration")
        assert awsvpc.get("AssignPublicIp") == "ENABLED"
        definition = TEMPLATES[stack].of_type("AWS::ECS::TaskDefinition")[0]
        for container in _as_sequence(definition["ContainerDefinitions"]):
            assert "PortMappings" not in _as_mapping(container, "a container definition")


def test_the_collector_declares_the_configured_reserved_concurrency() -> None:
    """The ingest ceiling is the parameter's, and the default is still the design's.

    The property is stated conditionally rather than unconditionally, because a
    reservation is not always grantable: the platform refuses one that would leave the
    account's unreserved pool below a floor of its own, so an account whose entire
    allowance equals that floor can grant none of any size. The condition is what lets
    such an account create the function; the default is what keeps the reservation the
    posture a deployment gets unless it says otherwise.

    Both are asserted, because either alone would let the ceiling quietly disappear. A
    conditional property whose default was the omission would mean most deployments ran
    with no reservation and nothing said so. A default of the documented ceiling with an
    unconditional property would mean the account above cannot deploy at all.
    """
    function = TEMPLATES["collector"].of_type("AWS::Lambda::Function")[0]
    stated = _as_mapping(function["ReservedConcurrentExecutions"], "the concurrency ceiling")
    branches = _as_sequence(stated[CONDITIONAL_NODE])
    assert tuple(stated) == (CONDITIONAL_NODE,), (
        "the ingest ceiling is stated unconditionally, so an account that can grant no "
        "reservation cannot create the function at all"
    )
    condition = _as_mapping(TEMPLATES["collector"].document["Conditions"], "conditions")[
        str(branches[0])
    ]
    assert RESERVED_CONCURRENCY_PARAMETER in tuple(_string_leaves(condition)), (
        "the condition guarding the ingest ceiling tests something other than the "
        "parameter carrying it"
    )
    assert _as_mapping(branches[1], "the stated ceiling").get("Ref") == (
        RESERVED_CONCURRENCY_PARAMETER
    )
    assert branches[2] == dict(ABSENT_VALUE), (
        "the other branch states some other ceiling rather than none; a reservation of "
        "zero throttles the function to nothing rather than leaving it unreserved"
    )
    parameter = TEMPLATES["collector"].parameters[RESERVED_CONCURRENCY_PARAMETER]
    assert parameter.get("Default") == str(RESERVED_CONCURRENCY_DEFAULT), (
        "the default ingest ceiling is not the documented one, so a deployment that "
        "names no value gets a posture the design did not ask for"
    )


def test_both_functions_declare_an_explicit_endpoint_authentication_posture() -> None:
    for stack in ("collector", "console"):
        endpoint = TEMPLATES[stack].of_type("AWS::Lambda::Url")[0]
        assert "AuthType" in endpoint, f"the {stack} endpoint declares no authentication posture"
        assert endpoint["AuthType"] in {"NONE", "AWS_IAM"}


def test_the_distribution_has_one_origin_and_it_is_the_console_endpoint() -> None:
    distribution = TEMPLATES["cdn"].of_type("AWS::CloudFront::Distribution")[0]
    configuration = _as_mapping(distribution["DistributionConfig"], "the distribution")
    origins = _as_sequence(configuration["Origins"])
    assert len(origins) == 1, "the distribution declares more than one origin"
    origin = _as_mapping(origins[0], "the origin")
    domain = _as_mapping(origin["DomainName"], "the origin host name")
    assert domain.get("Ref") == "ConsoleOriginDomain"
    behaviour = _as_mapping(configuration["DefaultCacheBehavior"], "the behaviour")
    assert behaviour.get("TargetOriginId") == origin.get("Id")


def test_the_distribution_uses_its_own_certificate_and_generated_host_name() -> None:
    distribution = TEMPLATES["cdn"].of_type("AWS::CloudFront::Distribution")[0]
    configuration = _as_mapping(distribution["DistributionConfig"], "the distribution")
    certificate = _as_mapping(configuration["ViewerCertificate"], "the certificate")
    assert certificate == {"CloudFrontDefaultCertificate": True}
    assert "Aliases" not in configuration, "the distribution declares a custom host name"


def test_the_scheduled_rule_invokes_the_checkpoint_entry_point_in_the_console_function() -> None:
    rule = TEMPLATES["console"].of_type("AWS::Events::Rule")[0]
    targets = _as_sequence(rule["Targets"])
    assert len(targets) == 1
    target = _as_mapping(targets[0], "the rule target")
    reached = tuple(_string_leaves(target.get("Arn")))
    assert "ConsoleFunction" in reached, (
        "the rule targets something other than the console function"
    )
    carried = tuple(_string_leaves(target.get("Input")))
    assert any("CheckpointEntryPoint" in item for item in carried), (
        "the rule carries no checkpoint entry point"
    )


def test_the_bucket_enables_object_lock_at_creation_in_governance_mode() -> None:
    bucket = TEMPLATES["storage"].of_type("AWS::S3::Bucket")[0]
    assert bucket.get("ObjectLockEnabled") is True
    configuration = _as_mapping(bucket["ObjectLockConfiguration"], "the lock configuration")
    rule = _as_mapping(configuration["Rule"], "the lock rule")
    retention = _as_mapping(rule["DefaultRetention"], "the default retention")
    assert retention.get("Mode") == "GOVERNANCE"
    versioning = _as_mapping(bucket["VersioningConfiguration"], "versioning")
    assert versioning.get("Status") == "Enabled"
    access = _as_mapping(bucket["PublicAccessBlockConfiguration"], "public access")
    assert all(value is True for value in access.values())
    assert "BucketEncryption" in bucket


def test_the_teardown_script_releases_the_retention_before_deleting() -> None:
    assert "--bypass-governance-retention" in TEARDOWN_TEXT
    release = TEARDOWN_TEXT.index("put-object-retention")
    delete = TEARDOWN_TEXT.index("delete-object ")
    assert release < delete, "the retention is not released before the version is deleted"


# ---------------------------------------------------------------------------
# Deployment parameter validation
# ---------------------------------------------------------------------------


def _validate(params: Path, stack: str) -> subprocess.CompletedProcess[str]:
    """Run the validator over one parameter file, reaching no account.

    The command is an argument vector whose every element is either this
    interpreter, a path this module composed, or a name from the fixed stack list,
    so nothing a caller supplies reaches it and no shell is involved.
    """
    return subprocess.run(  # noqa: S603 - a fixed vector of this module's own values
        [
            sys.executable,
            str(VALIDATOR),
            "--template",
            str(TEMPLATE_DIR / f"{stack}.yaml"),
            "--params",
            str(params),
            "--stack",
            stack,
            *[argument for name in RESOLVED_AT_DEPLOYMENT for argument in ("--resolved", name)],
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_deployment_script_resolves_every_cross_stack_value() -> None:
    for name in RESOLVED_AT_DEPLOYMENT:
        assert name in DEPLOY_TEXT, f"the deployment script resolves no {name}"
        for stack in STACK_ORDER:
            section = PARAMS.get(stack)
            if isinstance(section, dict):
                assert name not in section, f"{name} is held by hand in the {stack} section"


def test_the_delivered_parameter_file_validates_for_every_stack() -> None:
    for stack in STACK_ORDER:
        outcome = _validate(PARAMS_FILE, stack)
        assert outcome.returncode == 0, f"{stack} did not validate: {outcome.stderr}"


def test_validation_rejects_a_missing_parameter(tmp_path: Path) -> None:
    reduced = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    del reduced["console"]["CodeBucket"]
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps(reduced), encoding="utf-8")
    outcome = _validate(incomplete, "console")
    assert outcome.returncode != 0
    assert "missing required parameter CodeBucket" in outcome.stderr


def test_validation_rejects_a_parameter_the_template_declares_nothing_for(
    tmp_path: Path,
) -> None:
    extended = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
    extended["console"]["NotDeclaredAnywhere"] = "value"
    surplus = tmp_path / "surplus.json"
    surplus.write_text(json.dumps(extended), encoding="utf-8")
    outcome = _validate(surplus, "console")
    assert outcome.returncode != 0
    assert "unknown parameter NotDeclaredAnywhere" in outcome.stderr


# ---------------------------------------------------------------------------
# Values the deployment still has to be given
# ---------------------------------------------------------------------------


def test_both_task_services_take_their_desired_count_as_a_parameter() -> None:
    """A literal count blocks the whole deployment, not only its own stack.

    The count is read off the parsed service rather than matched in template text,
    so a template that reverts to a constant fails here: a literal is no reference
    node, and the assertion that it is one is the first thing to give way. The
    parameter file names it for both stacks too, because the point of the parameter
    is that a first deployment can bring the service up at zero, before any image
    exists, and raise it once one does.
    """
    for stack in TASK_STACKS:
        service = TEMPLATES[stack].of_type("AWS::ECS::Service")[0]
        declared = _as_mapping(service["DesiredCount"], f"the desired count of {stack}")
        assert declared.get("Ref") == DESIRED_COUNT_PARAMETER, (
            f"the {stack} service does not take its desired count from a parameter"
        )
        parameter = TEMPLATES[stack].parameters[DESIRED_COUNT_PARAMETER]
        assert parameter.get("Default") == DESIRED_COUNT_DEFAULT, (
            f"the {stack} count parameter carries another default"
        )
        assert parameter.get("MinValue") == DESIRED_COUNT_FLOOR, (
            f"the {stack} count parameter admits no deployment at zero"
        )
        section = PARAMS.get(stack)
        assert isinstance(section, dict), f"the parameter file holds no {stack} section"
        assert DESIRED_COUNT_PARAMETER in section, (
            f"the parameter file states no desired count for {stack}"
        )


def _outstanding_placeholders(section: str, values: Mapping[str, object]) -> tuple[str, ...]:
    """Every value in one section still written in the placeholder form, by name.

    A multi-valued parameter is checked element by element, because a pair whose
    first element is real and whose second is a placeholder deploys and then fails
    at role creation exactly as a wholly unset one does.
    """
    found: list[str] = []
    for name, value in values.items():
        for leaf in _string_leaves(value):
            for element in leaf.split(PLACEHOLDER_SEPARATOR):
                if PLACEHOLDER_SHAPE.fullmatch(element.strip()):
                    found.append(f"{section}.{name}")
    return tuple(dict.fromkeys(found))


def _placeholders_held(params: Mapping[str, object]) -> tuple[str, ...]:
    """Every placeholder the parameter file still holds, by section and name.

    The scan covers the shared section and every stack section, which is the whole
    of the file's value-bearing structure. Nothing is narrowed: an accounted-for
    value is recognised by the declaration below rather than by being skipped here,
    so a placeholder that nobody declared is still found.
    """
    held: list[str] = []
    for section in (COMMON_SECTION, *STACK_ORDER):
        values = params.get(section)
        if isinstance(values, dict):
            held.extend(
                _outstanding_placeholders(
                    section, {str(name): value for name, value in values.items()}
                )
            )
    return tuple(held)


class Supply(StrEnum):
    """How a value no checkout can hold arrives, which decides how it is checked."""

    BUILD_OUTPUT = "printed by a build this repository performs"
    BUILD_UNPUBLISHED = "produced by a build no script of this repository performs"
    OPERATOR_INPUT = "an operator input that reaches a process's environment and no policy"


@dataclass(frozen=True, slots=True)
class Outstanding:
    """One deployment value still owed, with the mechanism that supplies it.

    Attributes:
        section: The section of the parameter file the value sits in.
        parameter: The template parameter the value is read as.
        supply: How the value arrives, which selects the checks below.
        evidence: The tracked file the accounting is read against — the build that
            prints the value, the definition a build would read, or the template
            whose policy statement takes the value as its resource. A path rather
            than a sentence, so the claim is checked instead of believed.
    """

    section: str
    parameter: str
    supply: Supply
    evidence: Path


# The build that hands both function stacks their archive location back. Its usage
# block states the shape: it uploads the archive and prints the bucket and the key on
# standard output, one assignment per line, in the form the deployment command takes.
PACKAGING_SCRIPT: Final[Path] = SCRIPT_DIR / "package_functions.sh"

# The container definition both task stacks run from. It is in the tree; nothing in
# the tree pushes what it builds to a registry, so the image reference is owed to a
# publishing step that does not exist yet rather than to a step nobody has run.
CONTAINER_DEFINITION: Final[Path] = REPOSITORY_ROOT / "Dockerfile"

# The one operation the documented default provider needs, and the two role parameters
# naming what it may be called on. Neither name is owed: the delivered configuration
# selects the external implementation for both provider roles, so nothing in the
# delivered path makes this call, and the grants are stated only where a name was
# supplied. What is asserted about them is therefore not that they are outstanding but
# that their absence stays a permission nobody holds — the case below refuses both the
# placeholder role creation would reject and the wildcard that would keep the statement
# by granting every model in the account.
MODEL_INVOCATION_ACTION: Final[str] = "bedrock:InvokeModel"
MODEL_RESOURCE_PARAMETERS: Final[tuple[str, ...]] = ("EmbeddingModelArn", "TextModelArns")

# What a deployment still owes, declared as data. Two directions are asserted against
# it below: nothing in the parameter file may hold a placeholder this does not
# account for, and nothing accounted for here may have stopped being a placeholder.
# Nothing is owed. Every value that was once declared here has been supplied: the
# packaging build printed the bucket and the key both function stacks take, the four
# model identifiers were verified against the providers the delivered configuration
# selects rather than invented, and the container image was built from the definition in
# the tree and pushed to a registry, so both task stacks name it by digest.
#
# The table is empty rather than deleted, and the machinery around it is kept, because
# the guard it provides is about what happens next: the case below refuses any
# placeholder the parameter file gains that nothing accounts for, and the accounting
# vocabulary is what an entry would be written in. An empty table with a live guard is
# the difference between a deployment that is supplied and a deployment nobody is
# checking.
OUTSTANDING_VALUES: Final[tuple[Outstanding, ...]] = ()

ACCOUNTED_FOR: Final[frozenset[str]] = frozenset(
    f"{entry.section}.{entry.parameter}" for entry in OUTSTANDING_VALUES
)


def _scripts_printing(parameter: str) -> tuple[str, ...]:
    """Every script that hands one value back in the assignment form, by name."""
    return tuple(name for name, text in sorted(SCRIPT_TEXT.items()) if f"{parameter}=" in text)


def _configuration_variables_taking(stack: str, parameter: str) -> tuple[str, ...]:
    """Every configuration variable of one stack whose value comes from a named parameter."""
    return tuple(
        name
        for name, value in _configured(stack).items()
        if parameter in tuple(_string_leaves(value))
    )


def _statements_naming(stack: str, parameter: str) -> tuple[Mapping[str, object], ...]:
    """Every policy statement of one stack that takes a named parameter as its resource."""
    return tuple(
        statement
        for _, document in _policy_documents(TEMPLATES[stack])
        for statement in _statements(document)
        if parameter in tuple(_string_leaves(statement.get("Resource")))
    )


def test_every_placeholder_the_parameter_file_holds_is_accounted_for() -> None:
    """The pre-condition a deployment cannot silently proceed without.

    Every value in question reaches a policy document or a function's code location,
    so an unsupplied one is not a cosmetic gap: role creation refuses an operation on
    a resource name that names nothing, and a function refuses a code location that
    holds no object. Without a case here those failures arrive as a stack rollback
    minutes in; with one they arrive as a line before anything is created.

    What is asserted is not that no placeholder remains — several must, because a
    bucket, an image reference, and an account's own model names cannot be invented
    in a checkout. It is that every placeholder remaining is one the declaration
    above names, with the mechanism that supplies it. So this passes on a fresh
    checkout and fails the moment a parameter is added with a placeholder and
    forgotten, which is the drift a gate that always fails cannot report.
    """
    for name in _placeholders_held(PARAMS):
        assert name in ACCOUNTED_FOR, (
            f"{name} holds a placeholder that nothing accounts for; declare it "
            "outstanding with the mechanism that supplies it, or supply the value"
        )


def test_the_detector_finds_a_placeholder_when_one_is_present() -> None:
    """The two cases above iterate a set, and an empty set makes both of them silent.

    Every value the deployment once owed has been supplied, so the accounting is empty
    and the parameter file holds no placeholder. That is the desired state and it is
    also the state in which a broken detector is indistinguishable from a supplied
    deployment: if the shape a placeholder is written in changed, or the walk stopped
    descending into sections, both cases would pass over nothing and report a
    fully-supplied deployment however many placeholders the file held.

    So the detector is exercised against a document composed here. A placeholder in a
    stack's section is found and named by section and parameter, and a real value beside
    it is not, which is the discrimination the cases above rest on.
    """
    composed: Mapping[str, object] = {
        COMMON_SECTION: {"DeploymentName": "molt"},
        STACK_ORDER[0]: {
            "Supplied": "10.0.0.0/16",
            "Owed": "REPLACE_WITH_SOMETHING_NOBODY_HAS",
        },
    }

    found = _placeholders_held(composed)

    assert found == (f"{STACK_ORDER[0]}.Owed",), (
        "the placeholder detector did not find exactly the one placeholder composed "
        f"for it, answering {found} instead; the two cases either side of this one "
        "iterate what it finds, so a detector that finds nothing makes both silent"
    )


def test_the_delivered_parameter_file_owes_nothing() -> None:
    """The claim the empty accounting makes, stated so it is checked rather than implied.

    A reader of the accounting above sees an empty table and has to know whether that
    means every value is supplied or that nobody has looked. This is the difference,
    asserted: the delivered parameter file holds no placeholder, so a deployment from
    this checkout needs no value invented at the command line.

    It fails the day a template gains a parameter whose value cannot be held in a
    checkout, which is the day an entry belongs in the table again.
    """
    held = _placeholders_held(PARAMS)

    assert not held, (
        f"the delivered parameter file still owes {', '.join(held)}; either supply each "
        "value or declare it outstanding with the mechanism that supplies it"
    )
    assert not OUTSTANDING_VALUES, (
        "the accounting declares values outstanding while the parameter file holds no "
        "placeholder, so the declaration has outlived the gap"
    )


def test_every_value_accounted_for_is_still_a_placeholder() -> None:
    """The direction that stops the declaration outliving the gap.

    An entry left in place after its value is supplied would make the accounting a
    stale comment: the gate would keep naming a value as owed that a deployment
    already has, and a reader would learn to distrust the list. So a filled-in value
    is a failure of this case, and the fix it asks for is a deletion.
    """
    held = frozenset(_placeholders_held(PARAMS))
    names = tuple(f"{entry.section}.{entry.parameter}" for entry in OUTSTANDING_VALUES)
    assert len(set(names)) == len(names), "a value is accounted for twice"
    for name in names:
        assert name in held, (
            f"{name} is declared outstanding but holds no placeholder any more; "
            "remove its entry so the declaration keeps stating what is actually owed"
        )


def test_every_value_accounted_for_names_a_mechanism_the_repository_bears_out() -> None:
    """The accounting is checked against the tree rather than taken on trust.

    Three claims per entry. The parameter is one a template actually takes, so a
    value declared outstanding is a value whose supply changes something. The
    mechanism named is a file that exists. And the kind of supply agrees with the
    tree: a value said to be printed by a build is printed by the build named, and a
    value said to be produced by a build nothing publishes is printed by no script at
    all — so the day a publishing step is added, this case says to reclassify it.

    An operator input is checked in both directions at once, which is what stops the
    kind being a place to put a value that is really a permission: it reaches a
    process as a configuration variable of the stack that declares it, no script
    prints it, and no policy statement anywhere takes it as a resource. A value a
    policy names as its resource is owed to whoever grants the permission rather than
    to a process's environment, and it fails here.
    """
    for entry in OUTSTANDING_VALUES:
        name = f"{entry.section}.{entry.parameter}"
        assert entry.section in (COMMON_SECTION, *STACK_ORDER), (
            f"{name} names no section of the parameter file"
        )
        declaring = tuple(
            stack for stack, template in TEMPLATES.items() if entry.parameter in template.parameters
        )
        assert declaring, (
            f"{name} is declared outstanding but no template takes a parameter of "
            "that name, so supplying it would change nothing"
        )
        if entry.section != COMMON_SECTION:
            assert entry.section in declaring, (
                f"{name} is declared outstanding under a stack whose template does "
                f"not take it; the stacks taking it are {', '.join(declaring)}"
            )
        assert entry.evidence.is_file(), (
            f"{name} names {entry.evidence.name} as its mechanism, which is no file "
            "of this repository"
        )
        printing = _scripts_printing(entry.parameter)
        if entry.supply is Supply.BUILD_OUTPUT:
            assert entry.evidence.name in printing, (
                f"{name} is accounted for as {Supply.BUILD_OUTPUT} by "
                f"{entry.evidence.name}, which prints no value of that name"
            )
        else:
            assert not printing, (
                f"{name} is accounted for as {entry.supply} while "
                f"{', '.join(printing)} prints it; account for it as "
                f"{Supply.BUILD_OUTPUT} instead"
            )
        if entry.supply is Supply.OPERATOR_INPUT:
            for stack in declaring:
                carried = _configuration_variables_taking(stack, entry.parameter)
                assert carried, (
                    f"{name} is accounted for as {Supply.OPERATOR_INPUT} but the "
                    f"{stack} stack takes the parameter without setting any "
                    "configuration variable from it, so no process reads it"
                )
                granted = _statements_naming(stack, entry.parameter)
                assert not granted, (
                    f"{name} is accounted for as {Supply.OPERATOR_INPUT} while a "
                    f"policy statement of the {stack} stack takes it as a resource, "
                    "so it is a permission rather than a setting a process reads; a "
                    "value a policy names is not an operator input"
                )


def test_the_model_permission_is_stated_only_where_a_model_resource_was_supplied() -> None:
    """The shape that keeps an unsupplied grant from becoming a wide one.

    Role creation refuses a resource name that is neither well formed nor a wildcard,
    so a placeholder there is not a value waiting to be filled in — it is a stack that
    cannot be created, and it failed twice that way before this case existed. Two ways
    out of that are wrong in opposite directions. Writing a wildcard keeps the
    statement by granting every model in the account, which is a real privilege bought
    to satisfy a call the delivered configuration never makes. Deleting the statement
    outright removes the documented default provider's path, so an operator who
    selects it gets a denial with nothing naming what to grant.

    The third way is asserted here: the resource name defaults to empty, the statement
    is stated only when it is not, and supplying a name restores the grant with no
    template change. So four claims. Each model resource parameter admits emptiness and
    is delivered empty, which makes the delivered deployment one that holds no model
    permission at all. Every grant of the operation anywhere in the deployment is
    conditional. Each such condition tests its own parameter against the empty value,
    rather than testing something unrelated and coincidentally holding. And no grant of
    the operation names an open resource, which is the shortcut this refuses.
    """
    roles = TEMPLATES[ROLES_STACK]
    delivered = PARAMS.get(ROLES_STACK)
    supplied = frozenset(delivered) if isinstance(delivered, Mapping) else frozenset()
    for parameter in MODEL_RESOURCE_PARAMETERS:
        declared = roles.parameters.get(parameter)
        assert declared is not None, (
            f"the {ROLES_STACK} stack declares no {parameter}, so the model grant "
            "names a resource from somewhere this case cannot read"
        )
        assert declared.get("Type") == "String", (
            f"{parameter} is not a plain string, so it admits no empty value a "
            "condition can test and the grant cannot be made conditional"
        )
        assert declared.get("Default") == "", (
            f"{parameter} defaults to {declared.get('Default')!r} rather than to the "
            "empty value, so an unsupplied deployment states a grant naming it"
        )
        assert parameter not in supplied, (
            f"the parameter file supplies {parameter}; the delivered configuration "
            "selects the external provider for both provider roles, so a model grant "
            "is a permission nothing in the delivered path uses"
        )

    conditioned = {
        (holder, statement.get("Sid")): named
        for template in TEMPLATES.values()
        for holder, named, statement in _conditioned_statements(template)
        if MODEL_INVOCATION_ACTION in _actions(statement)
    }
    granting = tuple(
        (stack, holder, statement)
        for stack, template in TEMPLATES.items()
        for holder, document in _policy_documents(template)
        for statement in _statements(document)
        if MODEL_INVOCATION_ACTION in _actions(statement)
    )
    assert granting, (
        f"no policy grants {MODEL_INVOCATION_ACTION} anywhere, so the documented "
        "default provider has no path at all rather than an unsupplied one"
    )
    for stack, holder, statement in granting:
        named = conditioned.get((holder, statement.get("Sid")))
        assert named is not None, (
            f"{holder} in {stack} grants {MODEL_INVOCATION_ACTION} unconditionally, so "
            "the stack cannot be created until a model resource name is supplied"
        )
        definition = _as_mapping(
            _as_mapping(TEMPLATES[stack].document.get("Conditions", {}), "conditions")[named],
            f"the {named} condition",
        )
        tested = frozenset(_string_leaves(definition))
        guarded_on = next(
            (name for name in MODEL_RESOURCE_PARAMETERS if name in tested),
            None,
        )
        assert guarded_on is not None, (
            f"the {named} condition guarding {holder} in {stack} tests none of the "
            "model resource parameters, so what it holds on is unrelated to whether a "
            "resource name was supplied"
        )
        assert "" in tested, (
            f"the {named} condition tests {guarded_on} against something other than "
            "the empty value, so the delivered deployment's emptiness is not what "
            "omits the grant"
        )
        assert guarded_on in tuple(_string_leaves(statement.get("Resource"))), (
            f"{holder} in {stack} is guarded on {guarded_on} while naming a different "
            "resource, so the condition and the grant are about different values"
        )
        assert "*" not in tuple(_string_leaves(statement.get("Resource"))), (
            f"{holder} in {stack} names an open resource for {MODEL_INVOCATION_ACTION}, "
            "which grants every model in the account to keep a statement the delivered "
            "configuration never uses"
        )
