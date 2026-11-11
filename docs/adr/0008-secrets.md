# 0008. Secrets: `.env` in dev and CI, External Secrets in Kubernetes

Status: accepted 2026-11-10
Context: the repo is public. The secrets are listed in plan §8. CI needs no real secrets.
Options:
1. `scripts/dev-env.sh` generates a git-ignored `.env` (dev, and CI throwaway). Kubernetes base has placeholder Secrets. uat and prod use **External Secrets Operator** backed by a cloud secret manager.
2. Sealed Secrets: encrypted secrets committed to the repo. Workable, but committing even encrypted secrets to a public repo is avoidable.
Recommendation: option 1. The startup checks refuse dev defaults and `dev-`/`test-` key ids in uat and prod.
Decision: the recommendation above, accepted by the user on 2026-11-10 (plan sign-off).
Consequences: the uat and prod overlays have `ExternalSecret` custom resources (spec R29), so kubeconform needs CRD schemas. Real secret-store setup is the user's.
