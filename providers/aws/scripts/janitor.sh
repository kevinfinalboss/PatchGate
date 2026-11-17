#!/usr/bin/env bash
# Finds (and with --delete removes) resources that survive `terraform destroy` of the lab:
# detached volumes, unattached Elastic IPs and snapshots tagged by Patchgate.
set -euo pipefail

PROFILE="${AWS_PROFILE:-patchgate-dev}"
REGION="${AWS_REGION:-us-east-2}"
DELETE=false
[[ "${1:-}" == "--delete" ]] && DELETE=true

aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }

volumes=$(aws_ ec2 describe-volumes --filters Name=status,Values=available Name=tag-key,Values=Patchgate:Lab \
  --query 'Volumes[].VolumeId' --output text)
addresses=$(aws_ ec2 describe-addresses --query 'Addresses[?AssociationId==null].AllocationId' --output text)
snapshots=$(aws_ ec2 describe-snapshots --owner-ids self --filters Name=tag-key,Values=Patchgate:Lab \
  --query 'Snapshots[].SnapshotId' --output text)

echo "profile=$PROFILE region=$REGION"
echo "detached lab volumes: ${volumes:-none}"
echo "unattached elastic IPs: ${addresses:-none}"
echo "lab snapshots: ${snapshots:-none}"

if $DELETE; then
  for v in $volumes; do aws_ ec2 delete-volume --volume-id "$v" && echo "deleted $v"; done
  for a in $addresses; do aws_ ec2 release-address --allocation-id "$a" && echo "released $a"; done
  for s in $snapshots; do aws_ ec2 delete-snapshot --snapshot-id "$s" && echo "deleted $s"; done
elif [[ -n "$volumes$addresses$snapshots" ]]; then
  echo "run with --delete (or 'make janitor DELETE=1') to remove them"
fi
