# Infrastructure definitions

Ten stacks, deployed in one order, described by templates that render the same
resources on every run. That repeatability is what makes `deploy.sh` idempotent and
what lets a template change be applied by re-running it (Requirements 34.6, 27.9).

Deploy with `./deploy.sh`, tear down with `./teardown.sh`, and check the parameters
without touching an account with `./deploy.sh --dry-run`.

## What each stack creates

| Stack | Creates | Satisfies |
|---|---|---|
| `network` | One virtual network, two public subnets, an internet gateway and its default route, and one security group declaring no inbound rule. No address translation gateway and no interface endpoint. | 33.10, 33.11 |
| `parameters` | Every parameter name a component reads a secret or a setting from, each in the standard tier, carrying a non-secret placeholder and no value. Includes the ingest signing secret parameter. | 30.8, 33.12, 47.2 |
| `kms` | The asymmetric signing key and its alias. The signing operation is granted to the console execution role and denied to every other principal, the administrative path included. | 30.9, 34.4 |
| `storage` | The certificate bucket with object lock enabled at creation in governance mode with a short retention, versioning, blocked public access, required encryption, and a policy denying unencrypted writes and every principal outside the named roles. | 21.13, 30.11, 34.4 |
| `collector` | The ingest function, its HTTPS function endpoint, its declared reserved concurrency, its execution role, and its log group. | 5.12, 34.1 |
| `console` | The console function, its HTTPS function endpoint, its execution role, its log group, and the scheduled rule that invokes the checkpoint signer entry point inside the same function. | 25.14, 34.2, 45.1 |
| `cdn` | The distribution fronting the console function endpoint as its single origin, on its own default certificate and generated host name. | 34.2, 34.9 |
| `watcher` | The task cluster, and the task definition and service for the policy watcher in a public subnet with no inbound listener. | 23.12, 33.11, 34.2 |
| `mcp` | The task definition and service for the tool server, in a public subnet with no inbound listener, under the read-only role. | 33.11, 34.2, 40.5 |
| `observability` | Log groups, metric filters, alarms, and the parameter holding the bounded metric cardinality ceiling. | 31.5, 33.13, 33.14 |

## What is deliberately absent

No Application Load Balancer, no target group, and no HTTPS listener: an
Application Load Balancer HTTPS listener needs a certificate that cannot be issued
for its own generated host name, and it carries an hourly charge with no free
allowance (Requirement 34.9). No address translation gateway and no interface
endpoint: the subnets are public with no inbound rule, and the outbound path costs
nothing per hour (Requirement 33.10). No per-secret secret store: every secret is a
standard-tier parameter, which carries no per-parameter monthly charge
(Requirement 33.12).

## Secrets

No template, parameter file, or deployment argument carries a secret value. A
template declares a parameter name and a non-secret placeholder; the provisioning
scripts write the real value straight from the generator into the parameter store
and print nothing. A component receives parameter names through its environment and
resolves the values itself at run time.

One consequence is worth stating: a template cannot declare an encrypted parameter,
so each declared name is created as a plain parameter holding the placeholder and
`scripts/provision_roles.sh` replaces it with an encrypted parameter. The script
treats a parameter still holding the placeholder as unset, which is what keeps a
second run from rotating anything.

## Authentication posture of every reachable endpoint

| Endpoint | Reachable by | Authenticated by |
|---|---|---|
| Ingest function endpoint | Anyone who can resolve the generated host name | The bearer token and the body signature keyed with the ingest signing secret, both checked in the function on every request, with a request older than the configured age refused. There is no request signing at the cloud layer, because a capture hook holds no cloud credential. |
| Console function endpoint | Anyone who can resolve the generated host name, though the distribution is the intended caller | The console's own access credential and its signed session material, checked in the function. There is no request signing at the cloud layer, because a distribution cannot sign as a cloud principal. |
| Console distribution | The public, as a public console must be | Nothing of its own. Every request it forwards is authenticated by the console. |
| Policy watcher task | Nothing. No port mapping, no load balancer, no inbound rule. | Not applicable; the health route is in-process only. |
| Tool server task | Nothing over the network. No port mapping, no load balancer, no inbound rule. | Not applicable; it is reached over the process transport and holds the read-only role. |

## Least privilege

| Role | Holds |
|---|---|
| `molt-collector-exec` | Read on its own four parameter names, decryption of the parameter key, the embedding model resource, metric publication conditioned on the namespace, and its own log streams. |
| `molt-console-exec` | Read on its own four parameter names, decryption of the parameter key, the signing operation on the one key, the certificate prefix of the bucket, the text model resources, metric publication conditioned on the namespace, and its own log streams. |
| `molt-watcher-task` | Read on one parameter name, decryption of the parameter key, and metric publication conditioned on the namespace. No signing, no bucket, no model. |
| `molt-mcp-task` | Read on two parameter names, decryption of the parameter key, and metric publication conditioned on the namespace. No signing, no bucket, no write path. |
| `molt-verifier` | Public-key retrieval on the signing key and read on the certificate prefix. No signing. |

The signing operation appears in exactly one role's policy. The only statement with
an open resource is metric publication, which accepts no resource of its own and is
conditioned on the namespace instead.

## Cross-stack values

`deploy.sh` resolves the signing key resource name, the bucket resource name, the
console endpoint host name, the subnet identifiers, the security group identifier,
and the task cluster name from the outputs of stacks already deployed, so those
values appear in no parameter file. Everything else comes from `params/demo.json`,
whose shared section holds the values every stack repeats.
