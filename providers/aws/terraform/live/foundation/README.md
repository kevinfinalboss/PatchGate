# foundation

Persistent landing zone for Patchgate, applied in the **management account**. Unlike the lab, this stack is not destroyed between test sessions.

It builds on an existing AWS Organization (not managed here) and adds:

| Resource | Purpose |
|---|---|
| OUs `Security` and `Workloads` | Hub goes to Security; dev/prod to Workloads |
| Member accounts | `patchgate-hub`, `patchgate-dev`, `patchgate-prod` (protected by `prevent_destroy`) |
| Budget `patchgate-monthly` | Alerts at 40% / 80% actual and 100% forecast |
| Budget `patchgate-kill-switch` + action | Attaches `patchgate-deny-new-workloads` SCP to Workloads at 100% actual |
| Organization CloudTrail | Management events from all accounts, 90-day retention |

Region restriction and organization-escape SCPs already exist at the root and are left untouched.

## Usage

```bash
# once: create the state bucket
terraform -chdir=../bootstrap init && terraform -chdir=../bootstrap apply

cp backend.hcl.example backend.hcl          # fill in the bucket name
cp terraform.tfvars.example terraform.tfvars  # fill in emails
terraform init -backend-config=backend.hcl
terraform plan
```

## Recovering from the kill switch

Detach `patchgate-deny-new-workloads` from the Workloads OU (Organizations console → Policies) after reviewing costs.
