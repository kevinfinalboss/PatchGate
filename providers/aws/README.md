# providers/aws/

AWS implementation of the [provider contract](../README.md).

> Status: multi-account onboarding, bootstrap, hardening with drift, application lifecycle and drift control are implemented and verified in the lab; the approval engine (rollouts with email approval) is implemented. Patch Manager, Image Builder, Inspector and the reporting services below are planned for Phases 4c-2–6.

## Services used

| Concern | Service |
|---|---|
| Multi-account | AWS Organizations, CloudFormation StackSets (service-managed) |
| Events | Amazon EventBridge (cross-account bus) |
| Orchestration | AWS Step Functions |
| Execution on instances | AWS Systems Manager Run Command, State Manager |
| Patching | AWS Systems Manager Patch Manager |
| Snapshots | Amazon EBS snapshots / AWS Backup |
| Approvals | Amazon SES (one signed link per approver), API Gateway HTTP API, AWS Lambda, Step Functions task tokens |
| Images | EC2 Image Builder |
| Vulnerabilities and compliance | Amazon Inspector, AWS Security Hub |
| Inventory and reports | SSM Resource Data Sync, Amazon S3, Amazon Athena |

## Deployment modes

| Mode | Accounts | Spoke resources via |
|---|---|---|
| Single-account | 1 (hub = spoke) | Terraform `spoke` module |
| Multi-account | Management + Hub + N workloads | StackSet auto-deployed to the Workloads OU |

Region: `us-east-2` for all hub and spoke resources. Management-account foundation resources stay in `us-east-1`.

See [AGENTS.md](AGENTS.md) for layout and rules, and [docs/architecture.md](../../docs/architecture.md) for the design.

## Using Patchgate with your own Terraform

Patchgate records results as `Patchgate:Bootstrap*` tags on your instances. If you manage instances with Terraform, ignore those keys or the next `apply` will remove them:

```hcl
lifecycle {
  ignore_changes = [
    tags["Patchgate:BootstrapStatus"],
    tags["Patchgate:BootstrapDigest"],
    tags["Patchgate:BootstrapExecution"],
  ]
}
```

To opt an instance in, set `Patchgate:Managed = "true"` and optionally `Patchgate:Profile` (defaults: `base-linux` / `base-windows`), and use the `Patchgate-Instance` instance profile.

## Managing configuration with patchgate-cli

Configuration (profiles, app sets, agents, controls, baselines) lives in the `patchgate-config`
DynamoDB table in the hub account; instances only carry tags. Install with `pip install -e .`.

```bash
# --config-profile: hub account (configuration); --profile: account that owns the instance
patchgate-cli --config-profile hub list appsets
patchgate-cli --config-profile hub describe profile web-windows
patchgate-cli --config-profile hub apply -f my-appset.yaml --dry-run
patchgate-cli --config-profile hub copy appset dotnet8 dotnet8-team-a   # customise a built-in
patchgate-cli --config-profile hub history appset dotnet8-team-a
patchgate-cli --config-profile hub rollback appset dotnet8-team-a --version 2

patchgate-cli --config-profile hub --profile dev instance show i-0123456789abcdef0
patchgate-cli --config-profile hub --profile dev instance appsets i-0123456789abcdef0 --add dotnet8
patchgate-cli --profile dev instance reconcile i-0123456789abcdef0   # drift check now

# Rollouts (ADR 0010): approver groups, and each account's ring and approver groups
patchgate-cli --config-profile hub apply -f platform-approvers.yaml    # kind: ApproverGroup
patchgate-cli --config-profile hub account set 111111111111 --ring 0 --approvers platform
patchgate-cli --config-profile hub account set 222222222222 --ring 1 --approvers platform --allow payments
```

Profiles and app sets are **gated**: `apply` keeps instances on the current revision and starts a
rollout that releases the new one ring by ring (the CLI prints `pending`). Follow it with:

```bash
patchgate-cli --config-profile hub rollout list
patchgate-cli --config-profile hub rollout status AppSet-dotnet8-1a2b3c4d5e6f-1763481600
patchgate-cli --config-profile hub rollout cancel AppSet-dotnet8-1a2b3c4d5e6f-1763481600
```

Approval emails come from Amazon SES. After the first hub deploy, click the verification email
AWS sends to the sender address. While the account is in the SES sandbox, every approver address
must be verified too (`aws sesv2 create-email-identity --email-identity <address>`). Accounts and approver
groups (people's emails) live only in the configuration table, never in Git. Instances in
unregistered accounts are treated as the last (most protected) ring.

`instance reconcile` runs the `Patchgate-Drift` document with `Force=true`: it ignores the
profile's interval and maintenance window, corrects according to the per-category modes, waits
for the result and prints the end of the output (`--no-wait` returns right away). It needs
`ssm:SendCommand` on that document and the instance, and `ssm:GetCommandInvocation`, in the
account that owns the instance.

Every write is validated (schema and references). Built-in documents (`managedBy = patchgate`) are
re-applied by each release; copy them to customise. Direct console edits are allowed, but invalid
documents are refused by the resolver and reported by email.
