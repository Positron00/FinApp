#!/usr/bin/env bash
# CI job `manifests` (platform LLD §3.15, §9.6, §12; PAY-NFR-12, PAY-NFR-13; R32, C36, C38).
#
# Usage: scripts/ci/manifests.sh
#        MANIFESTS_ALLOW_UNTRACKED=1 scripts/ci/manifests.sh   (local, before the copies are first
#                                                               committed; ignored when CI is set)
#
#   1. K8s asset copies: scripts/sync-k8s-assets.sh, then no diff and no untracked file on the copy
#      paths (R32, C36). A stale committed copy fails here.
#   2. kustomize build (no load-restrictor flag, C36) | kubeconform -strict for dev, uat and prod,
#      with the vendored ExternalSecret CRD schemas (deploy/k8s/schemas, converted from External
#      Secrets Operator v2.13.0's CRDs with openapi2jsonschema's strict rules). Rendered output is
#      kept in ci-logs/k8s/<env>.yaml for the policy test.
#   3. tests/platform/test_k8s_policy.py and test_sync_k8s_assets.py on that output.
#   4. promtool check config / check rules / test rules (Compose rules and the Kubernetes-only
#      PodRestarts rule, R28), amtool check-config, otelcol-contrib validate (both Collector files),
#      docker compose config -q (base, and base + test override).
#
# Tools: kubectl and kubeconform binaries when present, else the images pinned in lib.sh; promtool,
# amtool and the Collector always run in the images Compose uses. Needs Docker and uv.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker uv git

COPY_PATHS=(
  deploy/k8s/base/bootstrap/init
  deploy/k8s/components/in-cluster-data/redis
  deploy/k8s/base/otel-collector/collector.yaml
  deploy/k8s/overlays/prod/collector.prod.yaml
)

group "k8s asset copies (sync-k8s-assets.sh; R32)"
scripts/sync-k8s-assets.sh
if ! git diff --exit-code -- "${COPY_PATHS[@]}"; then
  die "committed k8s asset copies are stale: run scripts/sync-k8s-assets.sh and commit the result"
fi
untracked="$(git status --porcelain --untracked-files=all -- "${COPY_PATHS[@]}")"
if [[ -n "$untracked" ]]; then
  printf '%s\n' "$untracked" >&2
  if [[ "${MANIFESTS_ALLOW_UNTRACKED:-0}" == 1 && -z "${CI:-}" ]]; then
    log "WARNING: copies not committed yet (MANIFESTS_ALLOW_UNTRACKED=1, local only); they match their sources"
  else
    die "k8s asset copies are not committed: run scripts/sync-k8s-assets.sh and commit the result"
  fi
fi

# The dev overlay's secretGenerator reads a git-ignored env file. When it is absent (CI), write one
# with dummy values for the keys dev-env.sh would write (never real values; removed afterwards).
dev_secrets=deploy/k8s/overlays/dev/secrets.env
if [[ ! -f "$dev_secrets" ]]; then
  tmp="$(mktemp -d)"
  on_exit "rm -rf '$tmp' '$CI_ROOT/$dev_secrets'"
  scripts/dev-env.sh --env-file "$tmp/env" --k8s-dev --k8s-dev-file "$tmp/k8s.env" >/dev/null
  sed -E 's/=.*/=dummy-not-a-secret/' "$tmp/k8s.env" >"$dev_secrets"
  log "wrote dummy $dev_secrets"
fi

mkdir -p ci-logs/k8s
for env in dev uat prod; do
  group "kustomize + kubeconform ($env)"
  kustomize_build "deploy/k8s/overlays/$env" >"ci-logs/k8s/$env.yaml"
  kubeconform_run <"ci-logs/k8s/$env.yaml"
done

group "k8s policy tests"
# The tests load the pycommon.testing plugin: install every workspace member first, as
# python.sh does (the root project installs nothing by itself; passed locally only because
# the dev venv was already synced).
uv sync --locked --all-packages
K8S_RENDERED_DIR="$CI_ROOT/ci-logs/k8s" uv run pytest -p no:cacheprovider \
  tests/platform/test_k8s_policy.py tests/platform/test_sync_k8s_assets.py

prom() { docker run --rm -v "$CI_ROOT/deploy:/deploy:ro" -w "$1" --entrypoint promtool "$PROMETHEUS_IMAGE" "${@:2}"; }

group "promtool"
# prometheus.yml loads /etc/prometheus/rules/*.yml, so mount the rules where Compose does.
docker run --rm -v "$CI_ROOT/deploy/observability/prometheus:/etc/prometheus:ro" \
  --entrypoint promtool "$PROMETHEUS_IMAGE" check config /etc/prometheus/prometheus.yml
prom /deploy/observability/prometheus check rules rules/recording.yml rules/alerts.yml
prom /deploy/observability/prometheus test rules tests/alerts_test.yml tests/slo_test.yml
prom /deploy/k8s/observability check rules alerts-kubernetes.yml
prom /deploy/k8s/observability test rules alerts-kubernetes_test.yml

group "amtool check-config"
docker run --rm -v "$CI_ROOT/deploy/observability:/cfg:ro" --entrypoint amtool "$ALERTMANAGER_IMAGE" \
  check-config /cfg/alertmanager.yml

group "otelcol-contrib validate"
for f in collector.yaml collector.prod.yaml; do
  docker run --rm -v "$CI_ROOT/deploy/observability:/cfg:ro" "$OTELCOL_IMAGE" validate --config="/cfg/$f"
  log "collector config valid: $f"
done

group "docker compose config"
# Interpolation needs every ${VAR:?}; use a throwaway env file, never the developer's .env.
cenv="$(mktemp -d)"
on_exit "rm -rf '$cenv'"
scripts/dev-env.sh --env-file "$cenv/env" >/dev/null
docker compose --env-file "$cenv/env" -f deploy/compose/compose.yaml \
  --profile core --profile obs --profile test config -q
docker compose --env-file "$cenv/env" -f deploy/compose/compose.yaml -f deploy/compose/compose.test.yaml \
  --profile core --profile obs --profile test config -q
log "compose files valid"

group "shellcheck and actionlint"
shell_scripts() { find scripts deploy -name '*.sh' -type f | LC_ALL=C sort; }
# shellcheck disable=SC2046  # one argument per script path (none has spaces)
docker run --rm -v "$CI_ROOT:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" -x $(shell_scripts)
docker run --rm -v "$CI_ROOT:/repo:ro" -w /repo "$ACTIONLINT_IMAGE" -no-color
log "scripts and workflows lint clean"
endgroup
