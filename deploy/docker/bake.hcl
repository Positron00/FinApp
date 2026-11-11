// docker buildx bake -f deploy/docker/bake.hcl [--set *.platform=linux/amd64] [--load]
// Five images from the repo root (platform LLD §10). Locally they build for the host
// architecture; CI sets linux/amd64. TAG defaults to "dev" (Compose uses APP_VERSION).
variable "TAG" { default = "dev" }
variable "GIT_SHA" { default = "unknown" }

group "default" { targets = ["identity", "payroll", "payout-worker", "bank-stub", "web"] }

target "_python" {
  context    = "."
  dockerfile = "deploy/docker/python-service.Dockerfile"
  args       = { VERSION = TAG, GIT_SHA = GIT_SHA }
}

target "identity" {
  inherits = ["_python"]
  args     = { PACKAGE = "identity", SERVICE_DIR = "identity" }
  tags     = ["payrun/identity:${TAG}"]
}

target "payroll" {
  inherits = ["_python"]
  args     = { PACKAGE = "payroll", SERVICE_DIR = "payroll" }
  tags     = ["payrun/payroll:${TAG}"]
}

target "payout-worker" {
  inherits = ["_python"]
  args     = { PACKAGE = "payout_worker", SERVICE_DIR = "payout_worker" }
  tags     = ["payrun/payout-worker:${TAG}"]
}

target "bank-stub" {
  inherits = ["_python"]
  args     = { PACKAGE = "bank_stub", SERVICE_DIR = "bank_stub" }
  tags     = ["payrun/bank-stub:${TAG}"]
}

target "web" {
  context    = "."
  dockerfile = "apps/web/Dockerfile"
  args       = { VERSION = TAG, GIT_SHA = GIT_SHA }
  tags       = ["payrun/web:${TAG}"]
}
