# Providers

A provider connects Patchgate's cloud-agnostic `core/` to a specific cloud. Each provider lives in `providers/<cloud>/` and implements the contract below with that cloud's native services.

| Provider | Status |
|---|---|
| [`aws/`](aws/) | In development |
| `azure/` | Planned — Phase 7 (via AWS SSM hybrid nodes) and Phase 8 (native) |

## Responsibilities

**The core decides _what_ should happen; the provider decides _how_ it happens in a given cloud.**

| Core owns | Provider owns |
|---|---|
| Catalog and schemas (agents, profiles, patch policies) | Infrastructure as code |
| In-VM scripts and hardening content | Orchestration (Step Functions, Durable Functions, …) |
| Ring selection, approval rules, rollback criteria | Identity and cross-account access |
| Compliance calculations and reports | Event sources and delivery |
| Normalized event model (CloudEvents) | Executing commands, patches and snapshots |

## The contract

The contract is intentionally small. It grows only when a second provider proves an operation is common (see [ADR 0001](../docs/adr/0001-core-and-providers.md)).

| Operation | Purpose | AWS | Azure (planned) |
|---|---|---|---|
| `describe_instance(ref)` → `Instance` | OS, tags, profile, readiness | EC2 `DescribeInstances` + SSM `DescribeInstanceInformation` | Resource Graph + Arc / VM agent status |
| `run(ref, script, params)` → `RunId` | Execute a core script on the VM | SSM Run Command | VM Run Command |
| `get_run_status(run_id)` → `RunStatus` | Poll execution result | `GetCommandInvocation` | Run Command instance view |
| `scan_patches(ref, policy)` → `PatchReport` | List missing patches | SSM Patch Manager (`Scan`) | Azure Update Manager assessment |
| `install_patches(ref, policy)` → `RunId` | Apply approved patches | SSM Patch Manager (`Install`) | Azure Update Manager deployment |
| `snapshot(ref)` → `SnapshotId` | Pre-change backup | EBS snapshot / AWS Backup | Managed disk snapshot |
| `restore(ref, snapshot_id)` | Rollback | Root volume replacement | Disk swap |
| `set_state(ref, key, value)` | Persist status on the resource | Tags `Patchgate:*` | Tags `Patchgate:*` |
| `normalize_event(raw)` → `CloudEvent` | Translate native events | EventBridge → CloudEvent | Event Grid → CloudEvent |

`ref` is a provider-neutral `InstanceRef` (`provider`, `account`, `region`, `id`).

### Normalized events

All providers emit [CloudEvents 1.0](https://cloudevents.io/) with `type` from this list:

| Type | Emitted when |
|---|---|
| `io.patchgate.instance.launched` | A managed instance becomes running |
| `io.patchgate.bootstrap.succeeded` / `.failed` | Bootstrap finishes |
| `io.patchgate.patch.scanned` | A scan completes |
| `io.patchgate.patch.approval_requested` | A ring waits for approval |
| `io.patchgate.patch.approved` / `.rejected` | An approver decides |
| `io.patchgate.patch.succeeded` / `.failed` / `.rolled_back` | A patch run ends |
| `io.patchgate.drift.detected` | Hardening drift is detected |

## Adding a provider

1. Open an issue and write an ADR describing the native service mapping.
2. Create `providers/<cloud>/` with its own `AGENTS.md` and `README.md`.
3. Implement every contract operation and pass the shared contract tests.
4. Provide a single-account example in `examples/`.
5. Do **not** modify `core/` to make your provider work. If you must, that signals a contract gap: discuss it in the ADR.
