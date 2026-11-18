# Architecture

> Status: AWS provider; Phases 0–4a implemented and verified in a lab (see the [roadmap](roadmap.md)).

## Goals

- **Secure from the first boot:** instances receive agents and a hardened baseline automatically.
- **Controlled change:** patches move through rings with explicit human approval before production.
- **Recoverable:** every change is preceded by a snapshot and followed by health checks; failures roll back.
- **Many accounts, one control point:** a central hub governs any number of workload accounts.
- **Declarative and auditable:** everything is defined in Git; every action leaves evidence.
- **Cloud-agnostic core:** adding a cloud means adding a provider, not rewriting the platform.

## Non-goals (for now)

- Managing containers or serverless workloads (Patchgate targets VMs).
- Replacing an EDR/antivirus — Patchgate installs and verifies the ones you choose.
- Multi-region active/active control plane.

## Building blocks

```mermaid
flowchart TB
    subgraph Core["core/ (cloud-agnostic)"]
        SCH[Schemas]
        CAT[Catalog<br/>agents · profiles · policies]
        SCR[In-VM scripts<br/>installers · Chocolatey · health checks]
        HRD[Hardening<br/>Ansible · DSC]
        LIB[Python lib<br/>rings · approvals · compliance · CloudEvents]
    end
    subgraph Prov["providers/aws/"]
        IAC[Terraform + StackSets]
        WF[Step Functions workflows]
        FN[Lambda adapters]
        SSMD[SSM documents]
    end
    CAT --> WF
    LIB --> FN
    SCR --> SSMD
    HRD --> SSMD
    SCH -. validates .-> CAT
```

## AWS deployment model: hub and spoke

```mermaid
flowchart LR
    subgraph Org["AWS Organizations"]
        MGMT["Management account<br/>billing · Organizations only"]
        subgraph SecOU["OU: Security"]
            HUB["Hub (tooling) account"]
        end
        subgraph WlOU["OU: Workloads"]
            DEV["Spoke: dev"]
            PROD["Spoke: prod"]
        end
    end
    MGMT -- "StackSets (service-managed,<br/>auto-deploy to OU)" --> DEV
    MGMT -- " " --> PROD
    DEV -- "EC2 events" --> HUB
    PROD -- "EC2 events" --> HUB
    HUB -- "AssumeRole Patchgate-Execution" --> DEV
    HUB -- " " --> PROD
```

| Account | Contains |
|---|---|
| **Management** | Organizations, StackSets administration, billing. No workloads, no Patchgate runtime. |
| **Hub** | Central EventBridge bus, Step Functions workflows, approval API, Lambdas, SNS, inventory/evidence bucket, Image Builder pipeline, delegated admin for Security Hub and Inspector. |
| **Spoke** (each workload account) | `Patchgate-Execution` role (trusts hub roles only), EventBridge rule forwarding EC2 events to the hub, SSM documents, instance profile, CloudWatch log groups. |

**Single-account mode:** hub and spoke resources are deployed into the same account. The workflows are identical; only the account list changes ([ADR 0002](adr/0002-hub-and-spoke-multi-account.md)).

**Onboarding:** spoke resources are a CloudFormation StackSet with automatic deployment to the Workloads OU. A new account created in that OU is managed without manual steps.

## Flows

### Bootstrap (Day 1)

```mermaid
sequenceDiagram
    participant EC2 as Instance (spoke)
    participant EB as EventBridge (spoke → hub)
    participant SF as Step Functions (hub)
    participant L as Lambda resolve (hub)
    participant SSM as SSM Run Command (spoke)
    EC2->>EB: state = running
    EB->>SF: start bootstrap execution
    SF->>L: describe instance (assume Patchgate-Execution)
    L-->>SF: managed? profile, OS, SSM ready
    alt not managed or already bootstrapped
        SF-->>SF: end
    else SSM agent not online yet
        SF->>SF: wait and retry (bounded)
    end
    SF->>SSM: run install-profile (agents, Chocolatey, hardening)
    SSM->>EC2: execute core scripts
    SF->>SSM: poll status
    SF->>EC2: tag Patchgate:BootstrapStatus
    SF->>SF: emit CloudEvent + notify
```

Triggered by events rather than `user_data` so every run is visible, retried and auditable ([ADR 0004](adr/0004-event-driven-bootstrap.md)).

### Drift (Day 2)

A State Manager association runs the `Patchgate-Drift` document every 30 minutes. The executor in
the VM re-checks the last applied plan when the profile's interval is due, corrects or reports per
category (`agents`, `apps`, `hardening`), reports only outside the profile's maintenance window,
and publishes findings as `io.patchgate` events that the hub emails. `patchgate-cli instance
reconcile` sends the same document with `Force=true` to check now
([ADR 0009](adr/0009-drift-modes-per-category-and-maintenance-window.md)).

### Change rollout with approval (Day 2)

Profiles and app sets are **gated** ([ADR 0010](adr/0010-approval-engine-rings-and-approver-groups.md)): a new revision reaches instances ring by ring, and rings that need approval ask each approver group for its own instances.

```mermaid
flowchart TD
    C[patchgate-cli apply<br/>profile or app set] --> P[Pin current revision<br/>config audit starts the rollout]
    P --> PL[Plan: running managed instances<br/>by ring and approver group]
    PL --> R0[Ring 0: release, re-plan via bootstrap]
    R0 --> H0{All runs succeeded?}
    H0 -- no --> RV[Revert the ring + email] --> STOP[Stop]
    H0 -- yes --> SOAK[Soak]
    SOAK --> AP{Each approver group<br/>email link, task token}
    AP -- rejected / expired --> KEEP[That group keeps the previous revision]
    AP -- approved --> R1[Ring 1: release to approved groups, re-plan]
    R1 --> H1{Healthy?}
    H1 -- no --> RV
    H1 -- yes --> DONE[Released everywhere if every ring took it<br/>summary email]
```

- **Ring and group** come from the account (`patchgate-cli account set`); instances may pick another allowed group with `Patchgate:Approvers`. Unregistered accounts count as the last ring.
- **Approval:** the approvals function stores the request (with the task token) and emails each member a signed, expiring link (SES). The link opens a confirmation page; only its POST records a decision, once per member. When the group's `minApprovals` is reached, or anyone rejects, the workflow resumes.
- **Health:** a ring is healthy when every instance's bootstrap run succeeded; otherwise, with `onFailure: revert`, its groups get the previous revision back and their instances re-plan.
- **OS patching** (Phase 4c-2) reuses this workflow with Patch Manager, snapshots and rollback.

### Emergency patch (Phase 6)

An Inspector finding with CVSS ≥ 9 on a managed instance starts the patch workflow for the affected instances only, with a shortened soak time and an expedited approval.

## Identity and permissions

- Hub roles (`patchgate-hub-orchestrator`, `patchgate-hub-resolver`, `patchgate-hub-rollout-fn`) may assume only `Patchgate-Execution`, and only in accounts of the organization (`aws:ResourceOrgID`), so onboarded accounts work without hub changes ([ADR 0006](adr/0006-org-wide-hub-access-and-stackset-spokes.md)).
- `Patchgate-Execution` trusts those exact hub role ARNs and allows:
  - `ssm:SendCommand` only with `Patchgate-*` documents and on instances tagged `Patchgate:Managed=true`;
  - `ec2:CreateTags` only for tag keys `Patchgate:*`;
  - read-only describe calls.
- The rollout workflow (`patchgate-hub-rollout`) only invokes the rollout and approvals functions and starts the bootstrap workflow; the approvals function may only send email from the configured SES identity and resume the rollout workflow.
- CI/CD uses GitHub OIDC; no long-lived access keys.

See the [threat model](security/threat-model.md).

## Tags

| Tag | Set by | Meaning |
|---|---|---|
| `Patchgate:Managed` | User | `true` opts the instance in |
| `Patchgate:Profile` | User | Catalog profile to apply |
| `Patchgate:AppSets` | User / `patchgate-cli` | App sets added to the profile |
| `Patchgate:Env` | User | Free-form environment label (`dev`, `prod`, `lab`); informational only |
| `Patchgate:Ring` | User | Ring override, honoured only where the account allows it (single-account mode); the ring normally comes from the account (ADR 0010) |
| `Patchgate:Approvers` | User | Approver group override, limited to the groups the account allows |
| `Patchgate:BootstrapStatus` | Patchgate | `Success` / `Failed` |
| `Patchgate:LastPatch` | Patchgate | Timestamp and result of the last patch |

## Other providers

The core (catalog, in-VM installers, hardening engines, plan composition) is cloud-agnostic. Another
cloud would be a new `providers/<cloud>/` implementing the [provider contract](../providers/README.md)
with that cloud's native services, without changes to `core/` ([ADR 0001](adr/0001-core-and-providers.md)).
No other provider is planned at the moment.

## Cost guardrails

- Workload region `us-east-2` (same pricing as `us-east-1`); the organization region SCP blocks most regional services in `us-east-1` for member accounts. Management-account resources (state bucket, CloudTrail, budgets) live in `us-east-1`.
- Serverless control plane (pay per use).
- No NAT Gateway: lab instances use a public subnet with no inbound rules and IMDSv2.
- AWS Budget with alerts and a Budget Action that blocks new instances above the limit.
- Ephemeral lab: created per test session, destroyed afterwards; a TTL reaper terminates instances older than 2 hours.
- `lab` is disposable and destroyed after each test session.
