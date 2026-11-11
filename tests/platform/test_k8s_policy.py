"""Policy checks on the rendered Kubernetes overlays (platform LLD §6.2, §9; PAY-NFR-13).

Each overlay (dev, uat, prod) is rendered with kustomize (no load-restrictor flag, C36) and checked:
pod security, probes and resources, the C1 env contract, sampling (C23), the web's port and probes
(C4), NetworkPolicies (ADR 0011), bank-stub placement and prod's payout-worker replicas (R14, C32),
and ExternalSecrets without plaintext Secrets in uat and prod (R29).

``scripts/ci/manifests.sh`` sets ``K8S_RENDERED_DIR`` to the output it already validated with
kubeconform. Otherwise the test renders a temporary copy of ``deploy/k8s`` itself (with a dummy dev
``secrets.env``), with ``kubectl kustomize`` or the pinned kubectl image.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s"
ENVS = ("dev", "uat", "prod")
KUBECTL_IMAGE = "registry.k8s.io/kubectl:v1.36.1"  # same pin as scripts/ci/lib.sh
FORBIDDEN_ENV = {"DATABASE_URL", "MIGRATOR_DATABASE_URL", "REDIS_URL"}
PAYRUN_APPS = {"web", "identity", "payroll", "payout-worker", "bank-stub"}
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "Job"}
TMP = "/tmp"  # noqa: S108 - the container mount path checked, not a file this test writes

Obj = dict[str, Any]


# ---------------------------------------------------------------- rendering


def _kustomize(directory: Path) -> str:
    if shutil.which("kubectl"):
        cmd = ["kubectl", "kustomize", str(directory)]
    elif shutil.which("docker"):
        rel = directory.relative_to(directory.parents[2])
        cmd = [
            "docker", "run", "--rm", "-v", f"{directory.parents[2]}:/work:ro", "-w", "/work",
            KUBECTL_IMAGE, "kustomize", str(rel),
        ]  # fmt: skip
    else:  # pragma: no cover - CI and dev machines have one of them
        pytest.skip("neither kubectl nor docker is available to render the overlays")
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[Obj]]:
    given = os.environ.get("K8S_RENDERED_DIR")
    texts: dict[str, str] = {}
    if given:
        for env in ENVS:
            texts[env] = (Path(given) / f"{env}.yaml").read_text()
    else:
        tree = tmp_path_factory.mktemp("k8s") / "deploy" / "k8s"
        shutil.copytree(K8S, tree, ignore=shutil.ignore_patterns("secrets.env"))
        (tree / "overlays" / "dev" / "secrets.env").write_text("POSTGRES_PASSWORD=dummy\n")
        for env in ENVS:
            texts[env] = _kustomize(tree / "overlays" / env)
    return {env: [d for d in yaml.safe_load_all(t) if d] for env, t in texts.items()}


def _of(objs: list[Obj], kind: str) -> list[Obj]:
    return [o for o in objs if o["kind"] == kind]


def _named(objs: list[Obj], kind: str, name: str) -> Obj:
    found = [o for o in _of(objs, kind) if o["metadata"]["name"] == name]
    assert len(found) == 1, f"expected one {kind}/{name}, found {len(found)}"
    return found[0]


def _pod(o: Obj) -> Obj:
    return cast(Obj, o["spec"]["template"]["spec"])


def _containers(o: Obj) -> Iterator[Obj]:
    pod = _pod(o)
    yield from pod.get("initContainers", [])
    yield from pod["containers"]


def _app(o: Obj) -> str:
    return str(o["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/name", ""))


def _env_view(objs: list[Obj], c: Obj) -> dict[str, str | None]:
    """The container's environment: envFrom ConfigMaps (in order), then env (values or refs)."""
    maps = {m["metadata"]["name"]: m.get("data", {}) for m in _of(objs, "ConfigMap")}
    out: dict[str, str | None] = {}
    for src in c.get("envFrom", []):
        if "configMapRef" in src:
            out.update(maps.get(src["configMapRef"]["name"], {}))
    for e in c.get("env", []):
        out[e["name"]] = e.get("value")
    return out


def _workloads(objs: list[Obj]) -> list[Obj]:
    return [o for o in objs if o["kind"] in WORKLOAD_KINDS]


# ---------------------------------------------------------------- pod security


@pytest.mark.parametrize("env", ENVS)
def test_every_workload_is_locked_down(rendered: dict[str, list[Obj]], env: str) -> None:
    """PAY-NFR-13: non-root, read-only root, no capabilities, RuntimeDefault, no SA token, /tmp."""
    workloads = _workloads(rendered[env])
    assert workloads
    for o in workloads:
        name = f"{env}:{o['kind']}/{o['metadata']['name']}"
        pod = _pod(o)
        assert pod["securityContext"]["runAsNonRoot"] is True, name
        assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}, name
        assert pod["automountServiceAccountToken"] is False, name
        assert pod.get("serviceAccountName"), name
        tmp_volumes = {v["name"] for v in pod["volumes"] if "emptyDir" in v}
        for c in _containers(o):
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, name
            assert sc["readOnlyRootFilesystem"] is True, name
            assert sc["capabilities"] == {"drop": ["ALL"]}, name
            assert c["resources"]["requests"].keys() >= {"cpu", "memory"}, name
            assert "memory" in c["resources"]["limits"], name
            tmp = [m for m in c.get("volumeMounts", []) if m["mountPath"] == TMP]
            assert tmp, f"{name}: no /tmp mount"
            assert tmp[0]["name"] in tmp_volumes, f"{name}: /tmp is not an emptyDir"


@pytest.mark.parametrize("env", ENVS)
def test_service_accounts_mount_no_token(rendered: dict[str, list[Obj]], env: str) -> None:
    """Every ServiceAccount has automountServiceAccountToken: false, and every pod names its own."""
    accounts = _of(rendered[env], "ServiceAccount")
    assert accounts
    assert all(sa["automountServiceAccountToken"] is False for sa in accounts)
    names = {sa["metadata"]["name"] for sa in accounts}
    for o in _workloads(rendered[env]):
        assert _pod(o)["serviceAccountName"] in names


@pytest.mark.parametrize("env", ENVS)
def test_probes_on_long_running_pods_only(rendered: dict[str, list[Obj]], env: str) -> None:
    """Deployments and StatefulSets have startup, liveness and readiness probes; Jobs have none."""
    for o in _workloads(rendered[env]):
        for c in _containers(o):
            probes = {"startupProbe", "livenessProbe", "readinessProbe"} & c.keys()
            if o["kind"] == "Job":
                assert not probes, o["metadata"]["name"]
            else:
                assert len(probes) == 3, o["metadata"]["name"]


@pytest.mark.parametrize("env", ENVS)
def test_web_port_and_probes(rendered: dict[str, list[Obj]], env: str) -> None:
    """C4: the web listens on 3000 and is probed on /api/livez and /api/readyz."""
    web = _named(rendered[env], "Deployment", "web")
    (c,) = list(_containers(web))
    assert c["ports"] == [{"name": "http", "containerPort": 3000}]
    assert c["startupProbe"]["httpGet"] == {"path": "/api/livez", "port": "http"}
    assert c["livenessProbe"]["httpGet"] == {"path": "/api/livez", "port": "http"}
    assert c["readinessProbe"]["httpGet"] == {"path": "/api/readyz", "port": "http"}
    assert _env_view(rendered[env], c)["PORT"] == "3000"
    svc = _named(rendered[env], "Service", "web")
    assert svc["spec"]["ports"][0]["port"] == 3000


@pytest.mark.parametrize("env", ENVS)
def test_python_services_probe_livez_and_readyz(rendered: dict[str, list[Obj]], env: str) -> None:
    """PAY-605: /livez for startup and liveness, /readyz for readiness, on port 8000."""
    for o in _of(rendered[env], "Deployment"):
        if _app(o) not in PAYRUN_APPS - {"web"}:
            continue
        (c,) = list(_containers(o))
        assert c["ports"] == [{"name": "http", "containerPort": 8000}]
        assert c["livenessProbe"]["httpGet"]["path"] == "/livez"
        assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"


# ---------------------------------------------------------------- env contract


@pytest.mark.parametrize("env", ENVS)
def test_no_url_style_connection_settings(rendered: dict[str, list[Obj]], env: str) -> None:
    """C1: no DATABASE_URL, MIGRATOR_DATABASE_URL or REDIS_URL in any ConfigMap or container env."""
    for m in _of(rendered[env], "ConfigMap"):
        assert not FORBIDDEN_ENV & set(m.get("data", {})), m["metadata"]["name"]
    for o in _workloads(rendered[env]):
        for c in _containers(o):
            assert not FORBIDDEN_ENV & {e["name"] for e in c.get("env", [])}, o["metadata"]["name"]


@pytest.mark.parametrize("env", ENVS)
def test_python_workloads_have_the_c1_keys(rendered: dict[str, list[Obj]], env: str) -> None:
    """C1, C2, C47: every Python service and Job gets DB_* (with its C2 role) and COMPANY_ID."""
    objs = rendered[env]
    for o in _workloads(objs):
        app = _app(o)
        if app not in PAYRUN_APPS - {"web"} and app != "bootstrap-owner":
            continue
        (c,) = list(_containers(o))
        view = _env_view(objs, c)
        for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_SCHEMA", "COMPANY_ID"):
            assert key in view, f"{o['metadata']['name']}: {key}"
        schema = view["DB_SCHEMA"]
        role = "migrator" if o["kind"] == "Job" else "app"
        assert view["DB_USER"] == f"{schema}_{role}", o["metadata"]["name"]


@pytest.mark.parametrize("env", ENVS)
def test_sampling_is_parentbased_always_on(rendered: dict[str, list[Obj]], env: str) -> None:
    """C23: OTEL_TRACES_SAMPLER=parentbased_always_on everywhere; no OTEL_TRACES_SAMPLER_ARG."""
    objs = rendered[env]
    checked = 0
    for o in _workloads(objs):
        for c in _containers(o):
            if "/payrun/" not in f"/{c['image']}":
                continue  # third-party images (Postgres, Redis, Mailpit, the Collector)
            checked += 1
            view = _env_view(objs, c)
            assert view.get("OTEL_TRACES_SAMPLER") == "parentbased_always_on", o["metadata"]["name"]
            assert "OTEL_TRACES_SAMPLER_ARG" not in view
    assert checked >= 5


def test_prod_collector_tail_samples(rendered: dict[str, list[Obj]]) -> None:
    """R27, C23: only prod's Collector config has tail_sampling (errors, > 300 ms, 10%)."""
    for env in ENVS:
        (cfg,) = [
            m
            for m in _of(rendered[env], "ConfigMap")
            if m["metadata"]["name"].startswith("otel-collector-config")
        ]
        collector = yaml.safe_load(cfg["data"]["collector.yaml"])
        processors = collector["service"]["pipelines"]["traces"]["processors"]
        if env == "prod":
            policies = {p["type"] for p in collector["processors"]["tail_sampling"]["policies"]}
            assert policies == {"status_code", "latency", "probabilistic"}
            assert "tail_sampling" in processors
        else:
            assert "tail_sampling" not in processors


@pytest.mark.parametrize("env", ENVS)
def test_environment_settings(rendered: dict[str, list[Obj]], env: str) -> None:
    """ENV per overlay; uat and prod: https origin, __Host-sid, TRUSTED_PROXY_HOPS >= 1 (C62)."""
    objs = rendered[env]
    assert _named(objs, "ConfigMap", "payrun-env")["data"]["ENV"] == env
    assert {o["metadata"].get("namespace") for o in objs if o["kind"] != "Namespace"} == {
        f"payrun-{env}"
    }
    (c,) = list(_containers(_named(objs, "Deployment", "web")))
    view = _env_view(objs, c)
    if env == "dev":
        assert view["SESSION_COOKIE_NAME"] == "sid"
        assert view["TRUSTED_PROXY_HOPS"] == "0"
    else:
        assert view["SESSION_COOKIE_NAME"] == "__Host-sid"
        assert int(str(view["TRUSTED_PROXY_HOPS"])) >= 1
        assert str(view["PUBLIC_BASE_URL"]).startswith("https://")


# ---------------------------------------------------------------- topology and replicas


def test_prod_payout_worker_has_zero_replicas(rendered: dict[str, list[Obj]]) -> None:
    """R14, C32: prod runs no payout-worker until a bank-provider ADR exists."""
    assert _named(rendered["prod"], "Deployment", "payout-worker")["spec"]["replicas"] == 0
    assert _named(rendered["uat"], "Deployment", "payout-worker")["spec"]["replicas"] == 2
    assert _named(rendered["dev"], "Deployment", "payout-worker")["spec"]["replicas"] == 1


def test_prod_bank_base_url_is_a_placeholder(rendered: dict[str, list[Obj]]) -> None:
    """R14: prod points BANK_BASE_URL at a provider placeholder the startup check refuses."""
    url = _named(rendered["prod"], "ConfigMap", "payout-worker-config")["data"]["BANK_BASE_URL"]
    assert url.startswith("https://")
    assert url.split("/")[2].endswith(".invalid")
    for env in ("dev", "uat"):
        cfg = _named(rendered[env], "ConfigMap", "payout-worker-config")
        assert cfg["data"]["BANK_BASE_URL"] == "http://bank-stub:8000"


@pytest.mark.parametrize("env", ENVS)
def test_bank_stub_only_in_dev_and_uat(rendered: dict[str, list[Obj]], env: str) -> None:
    """Plan §13: bank-stub (and its migration) is deployed in dev and uat only."""
    names = {o["metadata"]["name"] for o in rendered[env]}
    has = {n for n in names if n.startswith(("bank-stub", "migrate-bank-stub"))}
    if env == "prod":
        assert not has
        assert not any(_app(o) == "bank-stub" for o in _workloads(rendered[env]))
    else:
        assert {"bank-stub"} <= has


def test_prod_deployments_spread_across_zones(rendered: dict[str, list[Obj]]) -> None:
    """Platform LLD §9.2: topology spread by zone in prod."""
    for o in _of(rendered["prod"], "Deployment"):
        (tsc,) = _pod(o)["topologySpreadConstraints"]
        assert tsc["topologyKey"] == "topology.kubernetes.io/zone"
        assert tsc["whenUnsatisfiable"] == "ScheduleAnyway"


@pytest.mark.parametrize("env", ENVS)
def test_every_deployment_has_a_pdb(rendered: dict[str, list[Obj]], env: str) -> None:
    pdbs = {
        p["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"]
        for p in _of(rendered[env], "PodDisruptionBudget")
    }
    for o in _of(rendered[env], "Deployment"):
        assert _app(o) in pdbs, o["metadata"]["name"]


# ---------------------------------------------------------------- jobs


@pytest.mark.parametrize("env", ENVS)
def test_jobs(rendered: dict[str, list[Obj]], env: str) -> None:
    """§9.3: db-bootstrap runs the synced SQL; versioned migrations; bootstrap-owner --if-empty."""
    objs = rendered[env]
    jobs = {j["metadata"]["name"]: j for j in _of(objs, "Job")}
    migrations = sorted(n for n in jobs if n.startswith("migrate-"))
    expected = ["identity", "payouts", "payroll"] + ([] if env == "prod" else ["bank-stub"])
    suffixes = set()
    for svc in expected:
        (name,) = [n for n in migrations if re.fullmatch(rf"migrate-{svc}-\d+-\d+-\d+", n)]
        suffixes.add(name.removeprefix(f"migrate-{svc}"))
        (c,) = list(_containers(jobs[name]))
        assert c["command"] == ["alembic", "-c", "/app/alembic.ini", "upgrade", "head"]
    assert len(suffixes) == 1, f"migration Jobs disagree on the version suffix: {suffixes}"
    assert len(migrations) == len(expected)

    boot = jobs["db-bootstrap"]
    (c,) = list(_containers(boot))
    assert c["command"] == ["sh", "/init/lib/run-init.sh"]
    (vol,) = [v for v in _pod(boot)["volumes"] if v["name"] == "init"]
    mounted = {i["path"] for i in vol["configMap"]["items"]}
    init = ROOT / "deploy" / "compose" / "init"
    sources = {str(p.relative_to(init)) for p in init.rglob("*") if p.is_file()}
    assert mounted == {s for s in sources if not s.endswith(".DS_Store")}
    skip = _env_view(objs, c).get("SKIP_BANK_STUB")
    assert skip == ("1" if env == "prod" else "0")

    owner = jobs["bootstrap-owner"]
    (c,) = list(_containers(owner))
    assert "--if-empty" in c["args"]
    assert ("--password-env" in c["args"]) is (env == "dev")


# ---------------------------------------------------------------- network policies


def _selects(policy: Obj) -> dict[str, str]:
    return cast(dict[str, str], policy["spec"]["podSelector"].get("matchLabels", {}))


@pytest.mark.parametrize("env", ENVS)
def test_default_deny_and_dns(rendered: dict[str, list[Obj]], env: str) -> None:
    """ADR 0011: default deny both ways for every pod, plus kube-dns egress on 53."""
    objs = rendered[env]
    deny = _named(objs, "NetworkPolicy", "default-deny")
    assert deny["spec"]["podSelector"] == {}
    assert sorted(deny["spec"]["policyTypes"]) == ["Egress", "Ingress"]
    assert "ingress" not in deny["spec"]
    assert "egress" not in deny["spec"]
    dns = _named(objs, "NetworkPolicy", "allow-dns")
    ports = {(p["protocol"], p["port"]) for p in dns["spec"]["egress"][0]["ports"]}
    assert ports == {("UDP", 53), ("TCP", 53)}


@pytest.mark.parametrize("env", ENVS)
def test_only_payout_worker_reaches_the_bank(rendered: dict[str, list[Obj]], env: str) -> None:
    """ADR 0011: bank egress (bank-stub or the internet ipBlock) is payout-worker's alone."""
    for p in _of(rendered[env], "NetworkPolicy"):
        for rule in p["spec"].get("egress", []):
            for peer in rule.get("to", []):
                to_stub = (
                    peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name")
                    == "bank-stub"
                )
                to_internet = peer.get("ipBlock", {}).get("cidr") == "0.0.0.0/0"
                if to_stub or to_internet:
                    assert _selects(p) == {"app.kubernetes.io/name": "payout-worker"}, p
                if to_internet:
                    assert env == "prod"
                    assert {"10.0.0.0/8", "169.254.0.0/16"} <= set(peer["ipBlock"]["except"])
                    assert [q["port"] for q in rule["ports"]] == [443]


@pytest.mark.parametrize("env", ["uat", "prod"])
def test_managed_data_egress_is_ipblock(rendered: dict[str, list[Obj]], env: str) -> None:
    """ADR 0011: managed Postgres and Redis are reached through ipBlock rules on 5432 and 6379."""
    objs = rendered[env]
    for name, port in (("egress-to-managed-postgres", 5432), ("egress-to-managed-redis", 6379)):
        (rule,) = _named(objs, "NetworkPolicy", name)["spec"]["egress"]
        assert "ipBlock" in rule["to"][0]
        assert [p["port"] for p in rule["ports"]] == [port]
    assert not _of(objs, "StatefulSet"), "uat and prod use managed data (ADR 0007)"


@pytest.mark.parametrize("env", ENVS)
def test_every_pod_is_covered_by_a_policy(rendered: dict[str, list[Obj]], env: str) -> None:
    """Each workload is selected by its own NetworkPolicy (name or component), not just the deny."""
    objs = rendered[env]
    selectors = [_selects(p) for p in _of(objs, "NetworkPolicy")]
    for o in _workloads(objs):
        labels = o["spec"]["template"]["metadata"]["labels"]
        own = [s for s in selectors if s and all(labels.get(k) == v for k, v in s.items())]
        assert own, o["metadata"]["name"]


# ---------------------------------------------------------------- secrets


def _secret_refs(objs: list[Obj]) -> set[str]:
    refs: set[str] = set()
    for o in _workloads(objs):
        for c in _containers(o):
            refs |= {s["secretRef"]["name"] for s in c.get("envFrom", []) if "secretRef" in s}
            for e in c.get("env", []):
                ref = e.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    refs.add(ref["name"])
    return refs


@pytest.mark.parametrize("env", ["uat", "prod"])
def test_uat_and_prod_use_external_secrets(rendered: dict[str, list[Obj]], env: str) -> None:
    """R29, ADR 0008: no Secret objects; every referenced Secret is an ExternalSecret target."""
    objs = rendered[env]
    assert not _of(objs, "Secret"), "plaintext Secret objects in " + env
    targets = {e["spec"]["target"]["name"] for e in _of(objs, "ExternalSecret")}
    assert _secret_refs(objs) <= targets
    for es in _of(objs, "ExternalSecret"):
        store = {"kind": "ClusterSecretStore", "name": "payrun-secrets"}
        assert es["spec"]["secretStoreRef"] == store
        for item in es["spec"]["data"]:
            assert item["remoteRef"]["key"].startswith(f"payrun/{env}/"), item


def test_dev_secrets_come_from_the_generated_secret(rendered: dict[str, list[Obj]]) -> None:
    """Dev maps every secret key from payrun-dev-secrets; base placeholders hold only REPLACE_ME."""
    objs = rendered["dev"]
    names = {s["metadata"]["name"] for s in _of(objs, "Secret")}
    assert _secret_refs(objs) <= names
    assert any(n.startswith("payrun-dev-secrets-") for n in names)
    for s in _of(objs, "Secret"):
        if not s["metadata"]["name"].startswith("payrun-dev-secrets"):
            assert set(s["stringData"].values()) == {"REPLACE_ME"}, s["metadata"]["name"]
    for o in _workloads(objs):
        for c in _containers(o):
            env_names = {e["name"] for e in c.get("env", []) if "valueFrom" in e}
            from_placeholder = {
                s["secretRef"]["name"] for s in c.get("envFrom", []) if "secretRef" in s
            }
            for secret in from_placeholder:
                keys = set(_named(objs, "Secret", secret)["stringData"])
                missing = keys - env_names
                assert not missing, f"{o['metadata']['name']}: {missing} would stay REPLACE_ME"
