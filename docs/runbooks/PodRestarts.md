# Runbook: PodRestarts

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | any PayRun container in Kubernetes (the alert's `namespace`, `pod` and `container` labels) |
| **Alert** | `increase(kube_pod_container_status_restarts_total{namespace=~"payrun-.*"}[15m]) > 2` (`deploy/k8s/observability/alerts-kubernetes.yml`; Kubernetes only, needs kube-state-metrics, R28) |
| **Dashboard** | Platform, `http://127.0.0.1:4101/d/payrun-platform` ("Pod restarts", Kubernetes only) |

## What it means

A container in a `payrun-*` namespace restarted more than twice in 15 minutes: it is crash-looping or failing its liveness probe. This rule does not exist in Compose (no pods); there, the Platform dashboard's "Process starts in the last hour" panel shows restarts (each process has its own `service.instance.id`).

## Impact

Depends on the container. A restarting payout-worker delays payouts (its messages are redelivered, never lost); a restarting web or service answers 503 while down (BffAvailabilityFastBurn may follow).

## First checks

1. **Which container, how often:**

   ```promql
   sum by (namespace, pod, container) (increase(kube_pod_container_status_restarts_total{namespace=~"payrun-.*"}[15m]))
   ```

2. **Why it died:** `kubectl -n <namespace> describe pod <pod>` (last state: `OOMKilled`, exit code, failed probes) and `kubectl -n <namespace> logs <pod> -c <container> --previous`.
3. **Memory before the kill:**

   ```promql
   sum by (pod) (container_memory_working_set_bytes{namespace=~"payrun-.*", container!=""})
   ```

4. **The app's startup logs** (Loki); a failed startup check names the setting:

   ```logql
   {service_name=~"identity|payroll|payout-worker|bank-stub|web"} | event=~"startup.check_failed|settings.invalid|config.invalid|service.started"
   ```

## Likely causes

- `OOMKilled`: memory limit too low, or a leak (the soak test checks RSS growth).
- A failed startup check (`startup.check_failed`): a missing secret, a wrong key, migrations not applied.
- The liveness probe times out because the event loop is blocked (`asyncio_event_loop_lag_seconds` on the Service overview).
- A dependency down at startup (Postgres, Redis) with the readiness gate misconfigured.

## Mitigation

1. Fix the cause the termination reason shows: raise the memory limit, fix the secret or config, run the migration Job.
2. For a bad release, roll back the Deployment to the previous image tag.

## Confirm recovery

- No new restarts for 15 minutes, and the alert resolves:

  ```promql
  sum(increase(kube_pod_container_status_restarts_total{namespace=~"payrun-.*"}[15m]))
  ```
