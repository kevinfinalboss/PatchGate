# providers/aws/

AWS implementation of the [provider contract](../README.md).

> Status: Phase 0 — structure only. Modules land in Phase 1 (multi-account onboarding) and Phase 2 (bootstrap).

## Services used

| Concern | Service |
|---|---|
| Multi-account | AWS Organizations, CloudFormation StackSets (service-managed) |
| Events | Amazon EventBridge (cross-account bus) |
| Orchestration | AWS Step Functions |
| Execution on instances | AWS Systems Manager Run Command, State Manager |
| Patching | AWS Systems Manager Patch Manager |
| Snapshots | Amazon EBS snapshots / AWS Backup |
| Approvals | Amazon SNS (email), Amazon API Gateway, AWS Lambda |
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
