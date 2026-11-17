# AGENTS.md — providers/aws/

AWS implementation of the [provider contract](../README.md). Root rules in [/AGENTS.md](../../AGENTS.md) still apply.

## Layout

| Path | Contents |
|---|---|
| `terraform/modules/hub/` | Central event bus, Step Functions, approval API, Lambdas, SNS, inventory bucket |
| `terraform/modules/spoke/` | Execution role trusted by the hub, EventBridge forwarder, SSM documents, instance profile |
| `terraform/modules/lab/` | Disposable demo VPC + Linux/Windows instances (never production) |
| `terraform/live/` | Root modules that wire the modules per environment |
| `stacksets/` | CloudFormation for spoke resources, auto-deployed to the workload OU |
| `workflows/` | Step Functions definitions (ASL JSON) |
| `functions/` | Lambda handlers implementing the contract (Python 3.12) |
| `ssm/` | SSM documents that call `core/scripts/` |
| `tests/` | Contract tests with `moto` |

## Rules

- **Region:** `us-east-2` only for hub and spokes (the org region SCP blocks most regional services in `us-east-1` for member accounts). Only the management-account foundation uses `us-east-1`. Step Functions SDK integrations run in the hub's region; do not assume cross-region.
- **Cross-account:** the hub assumes `Patchgate-Execution` in spokes. Trust policies name the exact hub role ARNs; no `*` principals.
- **Spoke resources are defined twice** (Terraform module for single-account/dev, StackSet for OU auto-deploy). Change both in the same PR and keep resource names identical.
- **Tags as state:** Patchgate writes only tags with the `Patchgate:` prefix; IAM conditions enforce this (`aws:TagKeys`).
- **SSM Run Command** targets only instances tagged `Patchgate:Managed=true` (enforced by IAM condition, not only by code).
- **Cost:** prefer serverless and on-demand. No NAT Gateway, no always-on instances outside `lab/`. Mention any new monthly cost in the PR.
- **Never run `terraform apply` or `destroy`** without explicit human approval. `plan` and `validate` are fine.

## Checks

```
terraform fmt -check -recursive providers/aws/terraform
tflint --recursive
checkov -d providers/aws
pytest providers/aws/tests
```
