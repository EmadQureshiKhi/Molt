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
the signing operation appears in exactly one role's policy, which is what the
checkpoint signer's hosting decision rests on.

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
from pathlib import Path
from typing import Final

from ruamel.yaml import YAML

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPOSITORY_ROOT / "infra"
TEMPLATE_DIR: Final[Path] = INFRA_DIR / "templates"
PARAMS_FILE: Final[Path] = INFRA_DIR / "params" / "demo.json"
VALIDATOR: Final[Path] = REPOSITORY_ROOT / "scripts" / "validate_stack_params.py"

# The ten stacks, named in deployment order. The order is asserted against the
# deployment script, so a stack added to one and not the other is a failure.
STACK_ORDER: Final[tuple[str, ...]] = (
    "network",
    "parameters",
    "kms",
    "storage",
    "collector",
    "console",
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
    ("kms", "AWS::KMS::Key"),
    ("kms", "AWS::KMS::Alias"),
    ("storage", "AWS::S3::Bucket"),
    ("storage", "AWS::S3::BucketPolicy"),
    ("collector", "AWS::Lambda::Function"),
    ("collector", "AWS::Lambda::Url"),
    ("collector", "AWS::IAM::Role"),
    ("collector", "AWS::Logs::LogGroup"),
    ("console", "AWS::Lambda::Function"),
    ("console", "AWS::Lambda::Url"),
    ("console", "AWS::IAM::Role"),
    ("console", "AWS::Logs::LogGroup"),
    ("console", "AWS::Events::Rule"),
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

# The parameter hierarchy every secret is referenced through, and the operations a
# role uses to read one.
PARAMETER_PREFIX_REFERENCE: Final[str] = "${ParameterPrefix}"
PARAMETER_READ_ACTIONS: Final[frozenset[str]] = frozenset({"ssm:GetParameter", "ssm:GetParameters"})

# The signing operation, granted to one role and no other.
SIGN_ACTION: Final[str] = "kms:Sign"

# The one operation permitted to name an open resource, and the condition key that
# narrows it. The operation accepts no resource of its own, so the namespace
# condition is the narrowing.
NAMESPACE_CONDITIONED_ACTION: Final[str] = "cloudwatch:PutMetricData"
NAMESPACE_CONDITION_KEY: Final[str] = "cloudwatch:namespace"

# The declared ingest concurrency ceiling, and the parameter that carries it.
RESERVED_CONCURRENCY_PARAMETER: Final[str] = "ReservedConcurrency"
RESERVED_CONCURRENCY_DEFAULT: Final[int] = 10

# The values the deployment script resolves from the outputs of stacks already
# deployed rather than from the parameter file. None of them may appear in the
# parameter file, because a resource name resolved at deployment time is not a value
# an operator maintains by hand.
RESOLVED_AT_DEPLOYMENT: Final[tuple[str, ...]] = (
    "SigningKeyArn",
    "CertificateBucketArn",
    "ConsoleFunctionUrlDomain",
    "PublicSubnetIds",
    "TaskSecurityGroupId",
    "ClusterName",
)


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


def _statements(document: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(
        _as_mapping(item, "a policy statement")
        for item in _as_sequence(document.get("Statement", []))
        if isinstance(item, Mapping)
    )


def _actions(statement: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(item) for item in _as_sequence(statement.get("Action", [])))


TEMPLATES: Final[Mapping[str, Template]] = {
    stack: Template(stack, _load(TEMPLATE_DIR / f"{stack}.yaml")) for stack in STACK_ORDER
}
PARAMS: Final[Mapping[str, object]] = json.loads(PARAMS_FILE.read_text(encoding="utf-8"))
DEPLOY_TEXT: Final[str] = (INFRA_DIR / "deploy.sh").read_text(encoding="utf-8")
TEARDOWN_TEXT: Final[str] = (INFRA_DIR / "teardown.sh").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def test_every_stack_has_a_template_that_parses() -> None:
    for stack in STACK_ORDER:
        assert TEMPLATES[stack].resources, f"the {stack} template declares no resource"


def test_every_required_resource_type_is_declared_by_its_stack() -> None:
    for stack, resource_type in REQUIRED_RESOURCES:
        assert resource_type in TEMPLATES[stack].types, (
            f"the {stack} template declares no {resource_type}"
        )


def test_the_deployment_script_declares_the_stacks_in_the_asserted_order() -> None:
    positions = [DEPLOY_TEXT.index(f"\n  {stack}\n") for stack in STACK_ORDER]
    assert positions == sorted(positions)


def test_the_teardown_script_deletes_in_the_reverse_order() -> None:
    positions = [TEARDOWN_TEXT.index(f"\n  {stack}\n") for stack in reversed(STACK_ORDER)]
    assert positions == sorted(positions)


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


def test_the_signing_operation_appears_in_exactly_one_role_policy() -> None:
    holders: list[str] = []
    for stack, template in TEMPLATES.items():
        for holder, document in _policy_documents(template):
            if template.resources[holder].get("Type") != "AWS::IAM::Role":
                continue
            for statement in _statements(document):
                if statement.get("Effect") == "Allow" and SIGN_ACTION in _actions(statement):
                    holders.append(f"{stack}:{holder}")
    assert holders == ["console:ConsoleExecutionRole"], (
        f"the signing operation is held by {holders or 'nobody'}"
    )


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
    assert "NotPrincipal" in denied[0], "the signing denial names no excepted principal"
    granted = tuple(_string_leaves(allowed[0].get("Principal")))
    excepted = tuple(_string_leaves(denied[0].get("NotPrincipal")))
    assert granted == excepted, "the excepted principal is not the granted principal"


def test_no_administrative_key_statement_carries_the_signing_operation() -> None:
    key = TEMPLATES["kms"].of_type("AWS::KMS::Key")[0]
    policy = _as_mapping(key["KeyPolicy"], "the key policy")
    for statement in _statements(policy):
        if statement.get("Sid") == "AdministrationWithoutSigning":
            assert SIGN_ACTION not in _actions(statement)


def test_only_the_namespace_conditioned_statement_names_an_open_resource() -> None:
    for stack, template in TEMPLATES.items():
        for holder, document in _policy_documents(template):
            if template.resources[holder].get("Type") != "AWS::IAM::Role":
                continue
            for statement in _statements(document):
                resources = tuple(_string_leaves(statement.get("Resource")))
                if "*" not in resources:
                    continue
                actions = _actions(statement)
                assert actions == (NAMESPACE_CONDITIONED_ACTION,), (
                    f"{holder} in {stack} names an open resource for {actions}"
                )
                condition = _as_mapping(statement.get("Condition", {}), "condition")
                equals = _as_mapping(condition.get("StringEquals", {}), "condition values")
                assert NAMESPACE_CONDITION_KEY in equals, (
                    f"{holder} in {stack} publishes metrics with no namespace condition"
                )


def test_no_role_policy_grants_a_wildcard_operation() -> None:
    for stack, template in TEMPLATES.items():
        for holder, document in _policy_documents(template):
            if template.resources[holder].get("Type") != "AWS::IAM::Role":
                continue
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
    assert "NotPrincipal" in outside


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
    function = TEMPLATES["collector"].of_type("AWS::Lambda::Function")[0]
    declared = _as_mapping(function["ReservedConcurrentExecutions"], "the concurrency ceiling")
    assert declared.get("Ref") == RESERVED_CONCURRENCY_PARAMETER
    parameter = TEMPLATES["collector"].parameters[RESERVED_CONCURRENCY_PARAMETER]
    assert parameter.get("Default") == RESERVED_CONCURRENCY_DEFAULT


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
    assert domain.get("Ref") == "ConsoleFunctionUrlDomain"
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
