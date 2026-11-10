# 0011. Kubernetes egress: only payout-worker reaches the bank

Status: accepted 2026-11-10
Context: default-deny NetworkPolicies. In dev and uat the bank is the in-cluster bank-stub. In prod it would be an external provider whose hostname standard NetworkPolicies can't match.
Options:
1. dev/uat: a pod selector allowing payout-worker → bank-stub. prod: an `ipBlock` egress rule (`0.0.0.0/0` minus the cluster CIDRs) on port 443 for payout-worker only.
2. A CNI FQDN policy (Cilium/Calico): precise, but ties the deployment to one CNI.
Recommendation: option 1 now. Move to an FQDN policy when the cluster's CNI is known.
Decision: the recommendation above, accepted by the user on 2026-11-10 (plan sign-off).
Also allowed in uat and prod: every service and migrate Job → the managed Postgres CIDR (5432) and Redis (6379); External Secrets Operator → the cloud secrets API (443); the Collector → its telemetry backend; identity → SMTP (587) in prod. uat uses an in-cluster Mailpit.
Consequences: no pod has general internet egress. Each allow is an `ipBlock` with the provider's published CIDR, recorded in the overlay.
