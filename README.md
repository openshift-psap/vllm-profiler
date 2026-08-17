# vLLM Profiler - Kubernetes Mutating Admission Webhook

A Kubernetes-native profiling system for vLLM GPU workers that uses a mutating admission webhook to transparently inject PyTorch profiler instrumentation into vLLM serving pods.

## Overview

This system enables real-time torch profiling of vLLM model execution without requiring source code modifications or container rebuilds. It works by:

1. **Intercepting pod creation** via Kubernetes mutating admission webhook
2. **Injecting profiler code** via ConfigMap and environment variables
3. **Auto-loading profiler** when Python starts using sitecustomize.py
4. **Instrumenting vLLM** using import hooks to wrap `Worker.execute_model` with torch.profiler
5. **Capturing traces** of CPU+CUDA activity and exporting Chrome trace JSON files

## Architecture

```
┌─────────────────────────────────────────────────┐
│ User creates Pod with matching label:           │
│  - vllm-profiler/enabled=true                   │
│ Optional annotations for configuration:         │
│  - vllm.profiler/ranges="500-503"               │
│  - vllm.profiler/export-trace="false"           │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│ Mutating Webhook (webhook.py)                   │
│  - Checks namespace & label (OR logic)          │
│  - Injects: PYTHONPATH=/home/vllm/profiler      │
│  - Converts annotations to env vars             │
│  - Mounts: sitecustomize.py + config from CM    │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│ Pod starts → Python auto-loads sitecustomize.py │
│  Loads config from YAML & env vars              │
│  Installs import hook in sys.meta_path          │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│ vLLM imports gpu_worker module                  │
│  Import hook intercepts & wraps execute_model   │
│  Supports vLLM >= 0.12 and vLLM 0.11.x         │
└────────────────┬────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────┐
│ Profiler runs on configured ranges (e.g. 500-503│
│  Exports: /tmp/trace_rank{rank}_pid{pid}.json   │
└─────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Kubernetes/OpenShift cluster access
- `oc` or `kubectl` CLI
- `podman` or `docker` for building images
- Cluster admin permissions (for MutatingWebhookConfiguration)

### Deploy

```bash
# Deploy webhook and all components
./scripts/deploy.sh

# Or skip image build if using existing image on quay.io
./scripts/deploy.sh --skip-build
```

The deployment script will:
1. Build and push the webhook container image
2. Deploy webhook to `vllm-profiler` namespace
3. Create ConfigMap with profiler code in target namespace
4. Generate TLS certificates
5. Configure webhook with CA bundle
6. Validate deployment

### Configuration

Edit `manifests.yaml` to configure target namespace and label selectors:

```yaml
env:
  - name: TARGET_NAMESPACE
    value: "kserve-e2e-perf"
  # Label selector: pod with this label will be instrumented
  - name: TARGET_LABELS
    value: "vllm-profiler/enabled=true"
```

The webhook uses **OR logic** when multiple labels are specified (comma-separated) - a pod matching ANY of the specified labels will be profiled. No webhook rebuild needed to change labels.

### Updating Label Selectors Without Rebuilding

You can change the target labels without rebuilding the webhook container:

```bash
# Update TARGET_LABELS environment variable
oc set env deployment/env-injector -n vllm-profiler \
  TARGET_LABELS="vllm-profiler/enabled=true,app=vllm"

# Webhook pod will automatically restart with new configuration
# Verify new configuration:
oc logs -n vllm-profiler deployment/env-injector | grep "Target labels"
```

### Create Profiled Pod

Create a vLLM pod in the target namespace with the matching label:

```bash
# Basic: Pod will automatically be injected with profiler
kubectl run my-vllm-pod \
  -n kserve-e2e-perf \
  --labels="vllm-profiler/enabled=true" \
  --image=vllm/vllm-openai:latest \
  -- vllm serve <model-name>
```

Or use a pre-built server config from `server_configs/`:

```bash
# Deploy DeepSeek R1 with profiling enabled
oc apply -f server_configs/deepseek-r1-rhaiis-3.4-EA1.yaml
```

Or use pod annotations for custom profiler configuration:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-vllm-pod
  namespace: kserve-e2e-perf
  labels:
    vllm-profiler/enabled: "true"
  annotations:
    # Custom profiling ranges (multiple windows)
    vllm.profiler/ranges: "500-510,2000-2010"
    # Disable trace file export (reduce I/O)
    vllm.profiler/export-trace: "false"
    # Enable debug logging
    vllm.profiler/debug: "true"
spec:
  containers:
  - name: vllm
    image: vllm/vllm-openai:latest
    command: ["vllm", "serve", "<model-name>"]
```

### View Profiler Output

The profiler activates after the configured number of model execution calls (default: calls #500-503):

```bash
# Watch for profiler output
oc logs -n kserve-e2e-perf <pod-name> -f 2>&1 | grep '\[profiler\]'

# Expected sequence:
# [profiler] vLLM profiler installed - will profile ranges: [(500, 503)]
# [profiler] Starting profiler for range 500-503 (call #500)
# [profiler] Stopping profiler for range 500-503 (call #503)
# [profiler] Exported trace to: /tmp/trace_rank0_pid455_range500-503.json

# Retrieve trace files
oc exec -n kserve-e2e-perf <pod-name> -c kserve-container -- \
  bash -c 'tar cf - /tmp/trace_rank*.json' \
  | tar xf - -C profiles/<run-name>/ --strip-components=1

# Open in Chrome: navigate to chrome://tracing and load trace.json
# Or use Perfetto: https://ui.perfetto.dev
```

### Teardown

```bash
# Remove all webhook resources
TARGET_NAMESPACE=kserve-e2e-perf ./scripts/teardown.sh

# Or skip confirmation prompt
TARGET_NAMESPACE=kserve-e2e-perf ./scripts/teardown.sh --force
```

## Project Structure

```
vllm-profiler/
├── sitecustomize.py            # Profiler import hook (injected into pods)
├── profiler_config.yaml        # Default profiler configuration
├── webhook.py                  # Flask mutating admission webhook
├── manifests.yaml              # Kubernetes resources
├── kustomization.yaml          # ConfigMap generator
├── Dockerfile                  # Webhook container image
├── requirements.txt            # Python dependencies
├── AGENTS.md                   # Agent deployment instructions
├── CONFIGURATION_EXAMPLES.md   # Configuration guide
├── README.md                   # This file
├── demo-vllm-profiler.ipynb    # Interactive walkthrough notebook
├── scripts/
│   ├── deploy.sh               # Full deployment automation
│   ├── teardown.sh             # Cleanup script
│   ├── gen-certs.sh            # TLS certificate generation
│   ├── patch-ca-bundle.sh      # Webhook CA bundle patching
│   └── validate_webhook.sh     # Validation tool
├── tests/
│   ├── test-profiler.sh        # Standalone profiler testing
│   ├── test-vllm-integration.sh # End-to-end integration test
│   └── test-profiler-features.yaml # Feature testing examples
├── server_configs/             # Pre-built KServe manifests for models
│   ├── deepseek-r1-rhaiis-3.4-EA1.yaml
│   ├── gptoss-vllm-v0.17.0.yaml
│   └── ...                     # Various model/version combos
├── analysis/                   # Profile comparison scripts
├── profiles/                   # Collected trace files (local)
└── logs/                       # Profiling session logs
```

## How It Works

### 1. Admission Webhook (webhook.py)

Flask-based mutating webhook that:
- Listens for Pod CREATE operations
- Filters by namespace and **label selectors (OR logic)**
- Extracts profiler configuration from pod annotations
- Converts annotations to environment variables
- Injects `PYTHONPATH=/home/vllm/profiler` environment variable
- Mounts `sitecustomize.py` and `profiler_config.yaml` from ConfigMap

### 2. Profiler Import Hook (sitecustomize.py)

Python module that:
- Auto-loads when Python starts (via PYTHONPATH)
- Loads configuration from **3 sources** (priority order):
  1. Environment variables (highest priority)
  2. `profiler_config.yaml` file
  3. Hardcoded defaults (lowest priority)
- Installs a `sys.meta_path` finder to intercept vLLM worker module imports
- Supports multiple vLLM versions (>= 0.12 via `vllm.v1.worker.gpu_worker`, 0.11.x via `vllm.worker.worker`)
- Wraps `Worker.execute_model` with torch.profiler
- Records CPU+CUDA activity for configured call ranges
- Exports Chrome trace JSON file per rank

### 3. Profiler Configuration

Configuration is managed via `ProfilerConfig` class with multi-source support:

**Default settings** (from profiler_config.yaml):
```yaml
profiling_ranges: "500-503"     # Steady-state profiling (3 forward passes)
activities: "CPU,CUDA"
options:
  record_shapes: true
  with_stack: true              # Enables attributing perf changes to code paths
  profile_memory: false
output:
  export_chrome_trace: true
  file_pattern: "/tmp/trace_rank{rank}_pid{pid}.json"
```

**Per-pod override** (via annotations):
```yaml
annotations:
  vllm.profiler/ranges: "500-510,2000-2010"   # Multiple profiling windows
  vllm.profiler/export-trace: "false"          # Disable trace export
  vllm.profiler/debug: "true"                  # Enable debug logging
  vllm.profiler/activities: "CPU,CUDA"
  vllm.profiler/record-shapes: "true"
  vllm.profiler/with-stack: "true"
  vllm.profiler/memory: "false"
  vllm.profiler/output: "/tmp/custom_trace.json"
```

See [CONFIGURATION_EXAMPLES.md](CONFIGURATION_EXAMPLES.md) for comprehensive configuration guide.

## Advanced Usage

### Environment Variables

**Webhook Configuration:**
- `TARGET_NAMESPACE`: Namespace to target (default: "kserve-e2e-perf")
- `TARGET_LABELS`: Comma-separated label selectors with OR logic (e.g., "key1=val1,key2=val2")
- `TARGET_LABEL_KEY`: Legacy single label key (deprecated, use TARGET_LABELS)
- `TARGET_LABEL_VALUE`: Legacy single label value (deprecated, use TARGET_LABELS)
- `INJECT_ENV_NAME`: Environment variable to inject (default: "PYTHONPATH")
- `INJECT_ENV_VALUE`: Environment variable value (default: "/home/vllm/profiler")
- `LOG_LEVEL`: Webhook logging level (default: "DEBUG")

**Deployment:**
- `CONTAINER_RUNTIME`: Container runtime to use (default: "podman")
- `IMAGE_REGISTRY`: Image registry (default: "quay.io/mimehta")
- `IMAGE_TAG`: Image tag (default: "latest")
- `TARGET_NAMESPACE`: Target namespace for ConfigMap (default: "kserve-e2e-perf")

**Profiler Configuration (injected via pod annotations or set manually):**
- `VLLM_PROFILER_RANGES`: Profiling call ranges (e.g., "500-503" or "500-503,2000-2003")
- `VLLM_PROFILER_ACTIVITIES`: Activities to profile (e.g., "CPU,CUDA")
- `VLLM_PROFILER_RECORD_SHAPES`: Record tensor shapes (true/false)
- `VLLM_PROFILER_WITH_STACK`: Capture stack traces (true/false)
- `VLLM_PROFILER_MEMORY`: Profile memory allocations (true/false)
- `VLLM_PROFILER_OUTPUT`: Custom trace output file pattern
- `VLLM_PROFILER_EXPORT_TRACE`: Enable/disable trace export (true/false)
- `VLLM_PROFILER_DEBUG`: Enable debug logging (true/false)

### Testing

**Integration Test (Recommended):**

Run the complete end-to-end integration test:

```bash
# Deploys profiler, creates vLLM pod, runs inference, verifies profiler output
./tests/test-vllm-integration.sh
```

This test:
- Deploys the profiler webhook and ConfigMap
- Creates a vLLM pod with the latest vLLM image
- Waits for vLLM server to be ready (checks /v1/models endpoint)
- Runs vLLM serve with a small test model (facebook/opt-125m)
- Sends a single inference request generating 200 tokens
- Verifies profiler output in the logs
- Cleans up all test resources automatically

**Feature Tests:**

Test specific profiler features:

```bash
# Deploy profiler first
./scripts/deploy.sh

# Run feature tests
oc apply -f tests/test-profiler-features.yaml

# Verify results (check logs, env vars, etc.)

# Cleanup
oc delete -f tests/test-profiler-features.yaml
```

**Standalone Test:**

Test the profiler standalone with an existing vLLM pod:

```bash
# Requires access to a pod running vLLM
./tests/test-profiler.sh
```

### Customizing Profiler Settings

**Method 1: Update ConfigMap (affects all new pods):**

Edit `profiler_config.yaml` and update the ConfigMap:

```yaml
profiling_ranges: "2000-2010"  # Change profiling window
activities: "CPU,CUDA"
options:
  profile_memory: true         # Enable memory profiling
  record_shapes: true
```

Then update ConfigMap (no webhook rebuild needed):

```bash
# Delete and recreate ConfigMap with updated configuration
oc delete configmap env-injector-files -n kserve-e2e-perf
oc apply -k .

# New pods will automatically get the updated configuration
# Existing pods need to be restarted to pick up changes
```

**Method 2: Per-pod configuration (via annotations):**

Add annotations to your pod spec (no ConfigMap update needed):

```yaml
metadata:
  annotations:
    vllm.profiler/ranges: "2000-2010"
    vllm.profiler/memory: "true"
    vllm.profiler/export-trace: "false"
```

**Method 3: Test different configurations:**

See `tests/test-profiler-features.yaml` for examples of different configurations.

## Key Features

### 1. Multiple Label Selectors with OR Logic

The webhook supports multiple label selectors - a pod matching **ANY** of the configured labels will be profiled:

```yaml
TARGET_LABELS: "vllm-profiler/enabled=true,app=vllm"
```

This eliminates the need to rebuild the webhook when adding new pod types to profile.

### 2. Multiple Profiling Ranges

Profile multiple non-contiguous call ranges in a single session:

```yaml
vllm.profiler/ranges: "500-510,2000-2010,5000-5010"
```

This is useful for:
- Capturing warmup vs steady-state performance
- Comparing different phases of model execution
- Reducing profiling overhead while still capturing key intervals

### 3. Optional Trace Export

Disable trace file export to reduce I/O overhead in production:

```yaml
vllm.profiler/export-trace: "false"  # Still prints profiler table to logs
```

### 4. Dynamic Configuration

No webhook rebuilds needed - configure profiling via:
- **ConfigMap** (cluster-wide defaults)
- **Pod annotations** (per-pod overrides)
- **Environment variables** (highest priority)

### 5. Zero Code Changes

Profiling is completely transparent to the application:
- No vLLM source code modifications
- No container rebuilds
- No application downtime
- Automatic instrumentation via import hooks

### 6. Multi-Version vLLM Support

The profiler automatically detects and instruments:
- vLLM >= 0.12: `vllm.v1.worker.gpu_worker.Worker.execute_model`
- vLLM 0.11.x: `vllm.worker.worker.Worker.execute_model`

## What Requires Rebuild vs Runtime Update

### No Rebuild Required ✅

These changes can be made without rebuilding the webhook container:

1. **Change target labels:**
   ```bash
   oc set env deployment/env-injector -n vllm-profiler TARGET_LABELS="new,labels,here"
   ```

2. **Change target namespace:**
   ```bash
   oc set env deployment/env-injector -n vllm-profiler TARGET_NAMESPACE="new-namespace"
   ```

3. **Update profiler configuration (ConfigMap):**
   ```bash
   oc delete configmap env-injector-files -n kserve-e2e-perf
   oc apply -k .
   ```

4. **Per-pod configuration:**
   - Just add annotations to your pod spec

### Rebuild Required 🔨

These changes require rebuilding and redeploying the webhook:

1. **Changes to webhook.py logic**
2. **Changes to Python dependencies (requirements.txt)**
3. **Changes to Dockerfile**

To rebuild:
```bash
./scripts/deploy.sh  # Rebuilds container image and redeploys
```

## Troubleshooting

### Webhook not injecting profiler

Check webhook logs:
```bash
oc logs -n vllm-profiler deployment/env-injector
```

Verify webhook configuration:
```bash
oc get mutatingwebhookconfiguration env-injector-webhook -o yaml
```

### Profiler not loading in pod

Check pod has correct environment:
```bash
oc get pod <pod-name> -n kserve-e2e-perf -o jsonpath='{.spec.containers[0].env}' | python3 -m json.tool
```

Check pod has volume mount:
```bash
oc get pod <pod-name> -n kserve-e2e-perf -o jsonpath='{.spec.containers[0].volumeMounts}' | python3 -m json.tool
```

Check pod logs for profiler messages:
```bash
oc logs <pod-name> -n kserve-e2e-perf 2>&1 | grep '\[profiler\]'
```

### Profiler not triggering

The profiler only activates after reaching the configured call count (default: call #500). Send enough inference requests to reach that threshold.

```bash
# Example with vLLM OpenAI-compatible API
curl http://<service-url>:8080/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "...", "prompt": "Hello", "max_tokens": 200}'
```

### caBundle is empty

Re-generate certs and patch:
```bash
bash scripts/gen-certs.sh
bash scripts/patch-ca-bundle.sh
```

### Validation tool

Run comprehensive validation:
```bash
TARGET_NS=kserve-e2e-perf LABEL_KEY="vllm-profiler/enabled" DO_SIMPLE_TEST=1 ./scripts/validate_webhook.sh
```

## Resources Created

**Namespace: vllm-profiler**
- Deployment: `env-injector` (webhook)
- Service: `env-injector` (HTTPS on port 443)
- ServiceAccount: `env-injector`
- Secret: `env-injector-certs` (TLS certificates)

**Target Namespace: kserve-e2e-perf** (configurable)
- ConfigMap: `env-injector-files` (contains sitecustomize.py and profiler_config.yaml)

**Cluster-wide:**
- MutatingWebhookConfiguration: `env-injector-webhook`

## Security Considerations

- Webhook requires cluster admin permissions to create MutatingWebhookConfiguration
- Uses self-signed TLS certificates (suitable for development/testing)
- Failure policy is `Ignore` - webhook failures won't block pod creation
- ConfigMap is mounted read-only into pods
- Profiler code runs with same permissions as vLLM process
