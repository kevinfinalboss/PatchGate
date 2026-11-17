# Multi-account example

Hub in a Security OU account, spokes onboarded automatically in the Workloads OU.

1. `providers/aws/terraform/live/bootstrap` — state bucket (management account).
2. `providers/aws/terraform/live/hub` — control plane in the hub account (`organization_id` set).
3. `providers/aws/terraform/live/foundation` — OUs, budgets, CloudTrail and the **spoke StackSet** targeting the Workloads OU.

Any account created in, or moved into, the Workloads OU receives the spoke automatically; accounts that leave the OU have it removed.
