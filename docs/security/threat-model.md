# Threat model

Patchgate can run commands as root/SYSTEM on every managed server in every connected account. That makes it a high-value target. This document lists the main threats and the controls designed against them. Method: STRIDE over the main assets and trust boundaries.

## Assets

- Hub account and its roles (control over the entire fleet).
- `Patchgate-Execution` roles in spoke accounts.
- The catalog (defines what code runs on servers).
- Approval tokens and links.
- Secrets referenced by the catalog (licenses, agent CIDs).
- Inventory, logs and compliance evidence.

## Trust boundaries

1. GitHub repository and CI/CD → hub account.
2. Hub account → spoke accounts (AssumeRole).
3. Spoke account → instance (SSM agent).
4. Email recipient → approval API (internet-facing).
5. Instance tags (writable by spoke users) → hub workflows.

## Threats and controls

| # | Threat (STRIDE) | Scenario | Controls |
|---|---|---|---|
| T1 | Elevation of privilege | Spoke user tags an instance to make Patchgate run attacker-chosen commands | Tags only select **catalog** profiles; never interpolated into scripts without allow-list validation; SSM documents use `allowedPattern` |
| T2 | Tampering | Malicious change to the catalog installs a backdoor on every server | Branch protection, required reviews, CODEOWNERS on `core/catalog/`, `sha256` required, signed commits recommended |
| T3 | Tampering | Agent installer replaced at the download source | Checksum verified before install, fail closed; artifacts hosted in a versioned, private S3 bucket |
| T4 | Spoofing | Forged or replayed approval link approves a production rollout | Single-use task tokens, links expire, signed URLs, approver email allow-list, optional confirmation page (no approval on GET from link scanners) |
| T5 | Elevation of privilege | Compromised spoke uses its access to reach the hub | Spokes have no permissions in the hub except `PutEvents` on one bus; hub validates and normalizes every event |
| T6 | Elevation of privilege | Execution role abused for actions beyond Patchgate | Least-privilege policy: only `Patchgate-*` documents, only `Patchgate:Managed=true` instances, only `Patchgate:*` tag keys |
| T7 | Spoofing | Long-lived CI credentials stolen | GitHub OIDC with repository/branch conditions; no access keys |
| T8 | Information disclosure | Secrets leaked in command output, logs or Step Functions history | Secrets resolved on the instance at runtime; scripts never echo secrets; execution data logging reviewed per state |
| T9 | Repudiation | No proof of who approved or what changed | Approvals recorded with identity and timestamp; Step Functions history; CloudTrail in all accounts; evidence stored in a versioned bucket with Object Lock (Phase 6) |
| T10 | Denial of service | Bad patch breaks production fleet | Rings, soak time, health checks, snapshots and automatic rollback; production gated by approval |
| T11 | Denial of service | Event flood (mass launches) exhausts quotas | Bounded retries, concurrency limits and SQS buffering where needed |

## Residual risks

- A hub administrator is effectively an administrator of the fleet. Protect the hub with SCPs, MFA, break-glass procedures and minimal human access.
- Third-party agents run with high privileges by design; Patchgate verifies integrity, not vendor trustworthiness.

## Review

Revisit this document whenever a phase adds a new trust boundary (for example the approval API in Phase 4 or Azure in Phase 7).
