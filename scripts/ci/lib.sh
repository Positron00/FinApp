#!/usr/bin/env bash
# Shared helpers for scripts/ci/*.sh (platform LLD §3.15; PAY-NFR-12). Source it first:
#
#   # shellcheck source=scripts/ci/lib.sh
#   source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
#
# It sets strict mode, moves to the repository root, pins every CI tool version, and gives:
#   log MSG...            timestamped message on stderr
#   group NAME / endgroup  collapsible log groups on GitHub Actions, banners locally
#   retry N CMD...         run CMD up to N times with a growing pause
#   on_exit CMD            register a cleanup command; all run (in reverse order) when the script exits,
#                          with the script's exit status in CI_EXIT_CODE
#   compose ARGS...        docker compose with .env and both stack files, every profile (like stack.sh)
#   scrub                  filter stdin, masking JWTs, PEM blocks and Bearer tokens (report_failure.sh)
#   need CMD...            fail early with a clear message when a required command is missing
#   kustomize_build DIR    render a kustomization (kubectl if present, else the pinned kubectl image)
#   kubeconform_run        validate stdin with kubeconform (binary if present, else the pinned image)
#   in_docker IMAGE ARGS   run a pinned tool image with the repo mounted read-only at /work
#
# Every script runs the same way locally and in CI; nothing here reads a repository secret.
# Bash 3.2 compatible (macOS /bin/bash): no mapfile or associative arrays, and arrays that may be
# empty expand as ${a[@]+"${a[@]}"} under set -u.
set -euo pipefail

CI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export CI_ROOT
cd "$CI_ROOT"

# ---------------------------------------------------------------- pinned tool versions
# Policy (platform LLD §10.3): the current stable release that is at least two weeks old on the
# date it was pinned (here 2026-11-10), pinned by version. Dependabot does not see these; bump by hand.
export K8S_VERSION="1.36.0"                                        # kubeconform schema version
export KUBECTL_IMAGE="registry.k8s.io/kubectl:v1.36.1"             # kustomize v5.8 inside
export KUBECONFORM_IMAGE="ghcr.io/yannh/kubeconform:v0.8.0"
export PROMETHEUS_IMAGE="prom/prometheus:v3.15.1"                  # same as Compose (promtool)
export ALERTMANAGER_IMAGE="prom/alertmanager:v0.34.1"              # same as Compose (amtool)
export OTELCOL_IMAGE="otel/opentelemetry-collector-contrib:0.164.0" # same as Compose
export TRIVY_IMAGE="aquasec/trivy:0.76.1"
export GITLEAKS_IMAGE="ghcr.io/gitleaks/gitleaks:v8.30.1"          # same as the pre-commit hook
export OASDIFF_IMAGE="tufin/oasdiff:v1.34.1"
export SHELLCHECK_IMAGE="koalaman/shellcheck:v0.11.0"
export ACTIONLINT_IMAGE="rhysd/actionlint:1.7.12"
export K6_IMAGE="${K6_IMAGE:-grafana/k6:1.8.2}"                    # same as scripts/ci/k6.sh

export FORCE_COLOR="${FORCE_COLOR:-1}"
mkdir -p ci-logs

# ---------------------------------------------------------------- logging
_script_name="$(basename "${BASH_SOURCE[1]:-$0}" .sh)"

log() { printf '[%s %s] %s\n' "$(date -u +%H:%M:%S)" "$_script_name" "$*" >&2; }

_group_open=0
group() {
  endgroup
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    printf '::group::%s\n' "$*"
  else
    printf '\n===== %s =====\n' "$*" >&2
  fi
  _group_open=1
}
endgroup() {
  if [[ "$_group_open" == 1 && -n "${GITHUB_ACTIONS:-}" ]]; then printf '::endgroup::\n'; fi
  _group_open=0
}

die() {
  log "ERROR: $*"
  exit 1
}

need() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"
  done
}

# ---------------------------------------------------------------- retry and cleanup
retry() {
  local n="$1" i=1
  shift
  until "$@"; do
    if ((i >= n)); then
      log "failed after $n attempts: $*"
      return 1
    fi
    log "attempt $i/$n failed, retrying in $((i * 5))s: $*"
    sleep $((i * 5))
    i=$((i + 1))
  done
}

_cleanups=()
on_exit() { _cleanups+=("$*"); }
_run_cleanups() {
  local rc=$? i
  export CI_EXIT_CODE=$rc # cleanups can read the script's exit status
  endgroup
  for ((i = ${#_cleanups[@]} - 1; i >= 0; i--)); do
    eval "${_cleanups[i]}" || log "cleanup failed: ${_cleanups[i]}"
  done
  if ((rc != 0)); then log "FAILED (exit $rc)"; else log "passed"; fi
  return "$rc"
}
trap _run_cleanups EXIT

# ---------------------------------------------------------------- compose (as in scripts/stack.sh)
compose() {
  docker compose --env-file "${ENV_FILE:-.env}" -f deploy/compose/compose.yaml \
    -f deploy/compose/compose.test.yaml --profile core --profile obs --profile test "$@"
}

# Value of a key in .env (no quotes stripped; ports and plain values only).
env_val() { grep -E "^$1=" "${ENV_FILE:-.env}" | tail -n1 | cut -d= -f2-; }

# ---------------------------------------------------------------- secrets scrubbing
# Masks what pycommon's redactor masks (JWTs, PEM bodies, Bearer tokens), plus hex runs that look
# like the generated passwords. CI uses throwaway values, but a log tail posted to an issue on a
# public repository is scrubbed anyway.
scrub() {
  # Python rather than sed: the same regexes on GNU and BSD (macOS) systems.
  python3 -c '
import re, sys
text = sys.stdin.read()
text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED_PEM]", text, flags=re.S)
text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", text)
text = re.sub(r"([Bb]earer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
text = re.sub(r"(?i)((?:password|secret|api_key|token)[A-Za-z_]*\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
text = re.sub(r"\b[0-9a-f]{48,}\b", "[REDACTED_HEX]", text)
sys.stdout.write(text)
'
}

# ---------------------------------------------------------------- containerised tools
in_docker() {
  local image="$1"
  shift
  docker run --rm -v "$CI_ROOT:/work:ro" -w /work "$image" "$@"
}

kustomize_build() {
  local dir="$1"
  # No load-restrictor flag (C36): shared assets are synced copies (scripts/sync-k8s-assets.sh).
  if command -v kubectl >/dev/null 2>&1; then
    kubectl kustomize "$dir"
  else
    docker run --rm -v "$CI_ROOT:/work:ro" -w /work "$KUBECTL_IMAGE" kustomize "$dir"
  fi
}

kubeconform_run() {
  local args=(-strict -summary -kubernetes-version "$K8S_VERSION"
    -schema-location default
    -schema-location 'deploy/k8s/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json')
  if command -v kubeconform >/dev/null 2>&1; then
    kubeconform "${args[@]}" -
  else
    docker run --rm -i -v "$CI_ROOT:/work:ro" -w /work "$KUBECONFORM_IMAGE" "${args[@]}" -
  fi
}

# ---------------------------------------------------------------- images
IMAGES=(identity payroll payout-worker bank-stub web)

# bake_images TAG [PLATFORM]: build the five images from deploy/docker/bake.hcl and load them as
# payrun/<img>:TAG. On GitHub Actions (ACTIONS_RESULTS_URL exported by
# crazy-max/ghaction-github-runtime) each image reads and writes its own `type=gha` cache scope,
# shared by the images and e2e jobs.
bake_images() {
  local tag="$1" platform="${2:-}" args=() img
  [[ -n "$platform" ]] && args+=(--set "*.platform=$platform")
  if [[ -n "${ACTIONS_RESULTS_URL:-}${ACTIONS_CACHE_URL:-}" ]]; then
    for img in "${IMAGES[@]}"; do
      args+=(--set "$img.cache-from=type=gha,scope=image-$img"
        --set "$img.cache-to=type=gha,mode=max,scope=image-$img")
    done
  fi
  TAG="$tag" GIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)" \
    docker buildx bake -f deploy/docker/bake.hcl ${args[@]+"${args[@]}"} --load
}
