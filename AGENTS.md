# AGENTS.md - vLLM Profiler Deployment Guide

## Project Overview

This project profiles vLLM inference servers on Kubernetes/OpenShift **without modifying vLLM source code**. It uses a Kubernetes mutating admission webhook to inject a PyTorch profiler (`sitecustomize.py`) into vLLM pods at startup via PYTHONPATH manipulation.

### Architecture

```
[Pod created with label vllm-profiler/enabled=true]
    → [K8s API server sends AdmissionReview to webhook]
    → [webhook.py injects PYTHONPATH + mounts ConfigMap]
    → [Pod starts, Python auto-loads sitecustomize.py]
    → [Import hook wraps Worker.execute_model with torch.profiler]
    → [After N forward passes, profiler emits Chrome trace JSON]
```

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `sitecustomize.py` | repo root | Profiler code injected into vLLM pods |
| `profiler_config.yaml` | repo root | Controls profiling behavior (ranges, activities, output) |
| `webhook.py` | repo root | Flask webhook server that intercepts pod creation |
| `manifests.yaml` | repo root | K8s resources: Namespace, Deployment, Service, MutatingWebhookConfiguration |
| `kustomization.yaml` | repo root | Bundles profiler files into a ConfigMap |
| `scripts/deploy.sh` | scripts/ | Full deployment orchestrator |
| `scripts/gen-certs.sh` | scripts/ | Generates TLS certs for webhook |
| `scripts/patch-ca-bundle.sh` | scripts/ | Patches webhook with CA bundle |
| `scripts/validate_webhook.sh` | scripts/ | End-to-end validation |
| `scripts/teardown.sh` | scripts/ | Removes all resources |
| `server_configs/` | directory | Pre-built KServe manifests for various models |

---

## Deploying to a New Cluster

### Prerequisites

- `oc` CLI installed and on PATH
- A kubeconfig file for the target cluster with admin access
- The target namespace (default: `kserve-e2e-perf`) must already exist on the cluster
- The webhook container image already pushed to `quay.io/mimehta/vllmprofiler:latest`

### Step-by-Step Deployment

```bash
# 1. Set KUBECONFIG to point to the target cluster
export KUBECONFIG=/path/to/kubeconfig-for-target-cluster

# 2. Verify connectivity
oc whoami  # Should return system:admin or similar admin user

# 3. Verify the target namespace exists (create if needed)
oc get namespace kserve-e2e-perf || oc create namespace kserve-e2e-perf

# 4. Run deployment (skip image build since it's already on quay.io)
./scripts/deploy.sh --skip-build

# 5. If you want to skip the built-in validation too:
./scripts/deploy.sh --skip-build --skip-validation
```

### What deploy.sh Does (7 Steps)

1. **Build & push image** (skipped with `--skip-build`)
2. **Delete existing resources** (idempotent - safe if nothing exists)
3. **Apply manifests.yaml** → creates namespace `vllm-profiler`, Deployment, Service, ServiceAccount, MutatingWebhookConfiguration
4. **Apply kustomization** (`oc apply -k .`) → creates ConfigMap `env-injector-files` in `kserve-e2e-perf` with `sitecustomize.py` + `profiler_config.yaml`
5. **Generate TLS certs** → self-signed CA + server cert, stored as Secret `env-injector-certs` in `vllm-profiler`
6. **Patch CA bundle** → reads CA from secret, patches MutatingWebhookConfiguration so API server trusts the webhook
7. **Validate** (skipped with `--skip-validation`)

### Post-Deployment Validation

```bash
# Quick validation (creates a test pod, verifies injection, cleans up)
TARGET_NS=kserve-e2e-perf LABEL_KEY="vllm-profiler/enabled" DO_SIMPLE_TEST=1 ./scripts/validate_webhook.sh

# Manual checks:
oc get pods -n vllm-profiler                         # Webhook pod should be 1/1 Running
oc get configmap env-injector-files -n kserve-e2e-perf  # DATA should be 2
oc get mutatingwebhookconfiguration env-injector-webhook -o jsonpath='{.webhooks[0].clientConfig.caBundle}' | wc -c  # Should be > 0
```

### Expected Success Criteria

- `env-injector` pod is `1/1 Running` in namespace `vllm-profiler`
- ConfigMap `env-injector-files` exists in `kserve-e2e-perf` with DATA=2
- MutatingWebhookConfiguration `env-injector-webhook` has a non-empty caBundle
- Test pod with label `vllm-profiler/enabled=true` gets `PYTHONPATH=/home/vllm/profiler` injected

---

## Changing Target Namespace

The target namespace (where profiled pods run) defaults to `kserve-e2e-perf`. To change it:

1. Edit `manifests.yaml`: change `TARGET_NAMESPACE` env var value in the Deployment
2. Edit `kustomization.yaml`: change the `namespace` field in the configMapGenerator
3. Re-run deployment

Or pass it as an environment variable:
```bash
TARGET_NAMESPACE=my-namespace ./scripts/deploy.sh --skip-build
```

---

## Updating Profiler Configuration

After editing `profiler_config.yaml` or `sitecustomize.py`:

```bash
export KUBECONFIG=/path/to/kubeconfig

# Delete and recreate the ConfigMap
oc delete configmap env-injector-files -n kserve-e2e-perf
oc apply -k .

# Restart any running vLLM pods to pick up new config
oc delete inferenceservice <name> -n kserve-e2e-perf
oc apply -f server_configs/<config>.yaml
```

The webhook itself does NOT need restarting when profiler config changes.

---

## Running a Profiling Session

```bash
export KUBECONFIG=/path/to/kubeconfig

# 1. Deploy a model (pick from server_configs/)
oc apply -f server_configs/deepseek-r1-rhaiis-3.4-EA1.yaml

# 2. Watch pod startup
oc get pods -n kserve-e2e-perf -w

# 3. Verify profiler loaded
oc logs -n kserve-e2e-perf <pod-name> -f 2>&1 | grep '\[profiler\]'
# Expected: "[profiler] vLLM profiler installed - will profile ranges: [(500, 510)]"

# 4. Send traffic to trigger profiling (need to reach call count in profiling_ranges)
# Use guidellm, curl, or any load generator

# 5. Collect traces
mkdir -p profiles/<run-name>
oc exec -n kserve-e2e-perf <pod> -c kserve-container -- \
  bash -c 'tar cf - /tmp/trace_rank*.json' \
  | tar xf - -C profiles/<run-name>/ --strip-components=1
```

---

## Teardown

```bash
export KUBECONFIG=/path/to/kubeconfig
TARGET_NAMESPACE=kserve-e2e-perf ./scripts/teardown.sh --force
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Profiler not loading (no `[profiler]` in logs) | Webhook not injecting | Check webhook pod logs, verify labels match |
| caBundle is empty | Certs not patched | Run `scripts/gen-certs.sh && scripts/patch-ca-bundle.sh` |
| Profiler loaded but never starts | Not enough traffic | Send more requests to reach the profiling range |
| Pod labels don't match | Wrong label on InferenceService | Add `vllm-profiler/enabled: "true"` to InferenceService labels |

---

## Key Environment Variables for Scripts

| Variable | Default | Used By |
|----------|---------|---------|
| `KUBECONFIG` | `~/.kube/config` | All scripts (standard kubectl/oc) |
| `TARGET_NAMESPACE` | `kserve-e2e-perf` | deploy.sh, teardown.sh |
| `CONTAINER_RUNTIME` | `podman` | deploy.sh (image build) |
| `IMAGE_REGISTRY` | `quay.io/mimehta` | deploy.sh |
| `IMAGE_TAG` | `latest` | deploy.sh |
| `NS` | `vllm-profiler` | gen-certs.sh, patch-ca-bundle.sh |
| `SVC` | `env-injector` | gen-certs.sh, patch-ca-bundle.sh |

---



## Label Matching

The webhook instruments pods with label: `vllm-profiler/enabled=true`

All `server_configs/*.yaml` files already include this label on the InferenceService. If profiling external manifests, add this label to the InferenceService metadata (it propagates to pods via KServe).
