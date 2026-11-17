# Single-account example

Hub and spoke in one AWS account, no AWS Organizations needed. The quickest way to try Patchgate.

```bash
cd examples/single-account
terraform init
terraform apply -var notification_email=you@example.com   # confirm the SNS email
```

Launch any EC2 instance with:

- instance profile `Patchgate-Instance`
- tag `Patchgate:Managed=true`
- optional tag `Patchgate:Profile=<profile>` (defaults: `base-linux` / `base-windows`)

Within minutes it is tagged `Patchgate:BootstrapStatus=Success`. Follow the run in Step Functions → `patchgate-bootstrap`.

Differences from multi-account mode: the event bus has no organization policy and the hub can only assume `Patchgate-Execution` in its own account.

Remove everything with `terraform destroy`.
