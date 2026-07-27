# vLLM Profiler Configuration Examples

This document shows various ways to configure the vLLM profiler.

## Configuration Priority

Configuration is loaded in this order (later sources override earlier ones):

1. **Hardcoded defaults** in sitecustomize.py
2. **profiler_config.yaml** file (mounted from ConfigMap)
3. **Environment variables** (highest priority)
4. **Pod annotations** (converted to environment variables by webhook)

## Method 1: Using profiler_config.yaml (Recommended for defaults)

Edit `profiler_config.yaml` and redeploy the ConfigMap:

```yaml
# profiler_config.yaml
profiling_ranges: "500-510,2000-2010"  # Multiple ranges!
activities: "CPU,CUDA"
options:
  record_shapes: true
  with_stack: false    # Disabled by default to reduce overhead
  profile_memory: false
output:
  export_chrome_trace: true
  file_pattern: "/tmp/trace_rank{rank}_pid{pid}.json"
```

Then update the ConfigMap:

```bash
oc delete configmap env-injector-files -n kserve-e2e-perf
oc apply -k .
```

**Advantages:**
- Single file to manage
- No pod restarts needed (only new pods get new config)
- Easy to version control

## Method 2: Using Pod Annotations (Recommended for per-pod customization)

Add annotations to your pod spec to override profiler settings for that specific pod:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-vllm-pod
  namespace: kserve-e2e-perf
  labels:
    vllm-profiler/enabled: "true"
  annotations:
    # Profile two ranges: calls 500-510 and 2000-2010
    vllm.profiler/ranges: "500-510,2000-2010"

    # Only profile CUDA activity (skip CPU)
    vllm.profiler/activities: "CUDA"

    # Enable memory profiling
    vllm.profiler/memory: "true"

    # Custom output filename
    vllm.profiler/output: "/tmp/my_custom_trace_rank{rank}.json"

    # Enable debug logging
    vllm.profiler/debug: "true"
spec:
  containers:
  - name: vllm
    image: vllm/vllm-openai:latest
    # ... rest of pod spec
```

**Advantages:**
- Per-pod customization
- No ConfigMap changes needed
- Immediate effect on new pods

## Method 3: Using Environment Variables

If you're creating pods manually or via other tools, you can inject environment variables directly:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: my-vllm-pod
  namespace: kserve-e2e-perf
  labels:
    vllm-profiler/enabled: "true"
spec:
  containers:
  - name: vllm
    image: vllm/vllm-openai:latest
    env:
    - name: VLLM_PROFILER_RANGES
      value: "500-510,2000-2010"
    - name: VLLM_PROFILER_ACTIVITIES
      value: "CPU,CUDA"
    - name: VLLM_PROFILER_MEMORY
      value: "true"
    - name: VLLM_PROFILER_OUTPUT
      value: "/tmp/trace_rank{rank}_pid{pid}.json"
    # ... rest of container spec
```

**Advantages:**
- Full control over each pod
- Works with any deployment tool (Helm, Kustomize, etc.)

## Supported Configuration Options

### Profiling Ranges

Specify which model execution calls to profile:

| Method | Example | Effect |
|--------|---------|--------|
| ConfigMap | `profiling_ranges: "500-510"` | Profile calls 500-510 |
| Annotation | `vllm.profiler/ranges: "500-510,2000-2010"` | Profile 500-510 AND 2000-2010 |
| Env Var | `VLLM_PROFILER_RANGES="500-510"` | Profile calls 500-510 |

**Format:** `"start-end"` or `"start1-end1,start2-end2,..."` for multiple ranges.

**Note:** The profiler uses a gate file mechanism (`/tmp/profiler_gate`). The counter resets each time the gate is activated, so the same range works for every profile run regardless of model speed.

### Activities

Control what to profile:

| Value | Description |
|-------|-------------|
| `"CPU"` | CPU activity only |
| `"CUDA"` | CUDA/GPU activity only |
| `"CPU,CUDA"` | Both (default) |

### Boolean Options

Set to `"true"` or `"false"`:

| Option | Default | Description |
|--------|---------|-------------|
| `record-shapes` / `VLLM_PROFILER_RECORD_SHAPES` | `true` | Record tensor shapes |
| `with-stack` / `VLLM_PROFILER_WITH_STACK` | `false` | Capture Python stack traces (adds overhead) |
| `memory` / `VLLM_PROFILER_MEMORY` | `false` | Profile memory allocations |
| `debug` / `VLLM_PROFILER_DEBUG` | `false` | Enable debug logging |

### Output File Pattern

Customize the output trace filename:

| Placeholder | Replaced With |
|-------------|---------------|
| `{pid}` | Process ID |
| `{rank}` | Tensor parallel rank (from torch.distributed or LOCAL_RANK) |

**Examples:**
- `"/tmp/trace_rank{rank}_pid{pid}.json"` → `/tmp/trace_rank0_pid455.json` (default)
- `"/tmp/trace_pid{pid}.json"` → `/tmp/trace_pid455.json`
- `"/tmp/deepseek_steady_state_rank{rank}.json"` → `/tmp/deepseek_steady_state_rank0.json`

## Common Use Cases

### Use Case 1: Steady-state profiling (default)

Profile 10 forward passes after warmup completes (JIT compilation finishes within ~50 passes, so 500 is well into steady state):

**ConfigMap (current default):**
```yaml
profiling_ranges: "500-510"
```

### Use Case 2: Profile multiple windows

Compare performance at different stages (warmup, steady-state, late execution):

**Annotation:**
```yaml
annotations:
  vllm.profiler/ranges: "50-60,500-510,2000-2010"
```

### Use Case 3: Memory profiling

Enable memory profiling to find memory leaks or allocations:

**Annotation:**
```yaml
annotations:
  vllm.profiler/memory: "true"
  vllm.profiler/ranges: "500-600"  # Longer range for memory analysis
```

### Use Case 4: CUDA-only profiling

Skip CPU profiling to reduce overhead and focus on GPU performance:

**Annotation:**
```yaml
annotations:
  vllm.profiler/activities: "CUDA"
  vllm.profiler/with-stack: "false"  # Further reduce overhead
```

### Use Case 5: Per-rank trace files (tensor parallelism)

When using tensor parallelism (e.g., TP=8 for DeepSeek R1), each rank produces its own trace file:

**ConfigMap (default behavior):**
```yaml
output:
  file_pattern: "/tmp/trace_rank{rank}_pid{pid}.json"
```

With TP=8, you'll get 8 trace files: `trace_rank0_pid455.json` through `trace_rank7_pid462.json`.

### Use Case 6: Custom output for A/B comparisons

Use custom filenames to distinguish between experiments:

**Annotation:**
```yaml
annotations:
  vllm.profiler/output: "/tmp/deepseek_r1_rhaiis_3.4_rank{rank}.json"
```

## Testing Configuration

To test your configuration without creating a full vLLM deployment:

**1. Check startup messages:**

```bash
oc logs <pod-name> -n kserve-e2e-perf 2>&1 | grep '\[profiler\]'
```

Look for:
```
[profiler] vLLM profiler installed - will profile ranges: [(500, 510)]
```

**2. Enable debug mode to see configuration details:**

```yaml
annotations:
  vllm.profiler/debug: "true"
```

Then check logs for:
```
[profiler-config] Loaded configuration:
  Ranges: [(500, 510), (2000, 2010)]
  Activities: ['CPU', 'CUDA']
  Output: /tmp/trace_rank{rank}_pid{pid}.json
```

**3. Verify profiler activation:**

Watch logs for profiler start/stop messages:

```bash
oc logs -f <pod-name> -n kserve-e2e-perf 2>&1 | grep '\[profiler\]'
```

Expected output:
```
[profiler] Starting profiler for range 500-510 (call #500)
[profiler] Stopping profiler for range 500-510 (call #510)
[profiler] Exported trace to: /tmp/trace_rank0_pid455_range500-510.json
```

## Changing Configuration Without Rebuilding

### For all new pods (via ConfigMap):

```bash
# 1. Edit profiler_config.yaml
vim profiler_config.yaml

# 2. Update ConfigMap
oc delete configmap env-injector-files -n kserve-e2e-perf
oc apply -k .

# 3. New pods will use new configuration automatically
# Existing pods need to be restarted
```

### For specific pods (via annotations):

Just create pods with different annotations - no rebuild needed!

```bash
# Create pod with custom profiling ranges
kubectl run my-vllm-pod \
  --namespace=kserve-e2e-perf \
  --labels="vllm-profiler/enabled=true" \
  --annotations="vllm.profiler/ranges=2000-2010" \
  --image=vllm/vllm-openai:latest \
  -- vllm serve <model-name>
```

## Troubleshooting

### Configuration not being applied

**Check webhook logs:**
```bash
oc logs -n vllm-profiler deployment/env-injector | grep profiler
```

**Verify annotations were detected:**
```
Found profiler annotation 'vllm.profiler/ranges' -> VLLM_PROFILER_RANGES='500-510,2000-2010'
```

### YAML config not loading

**Check if PyYAML is installed in vLLM container:**
```bash
oc exec <pod-name> -n kserve-e2e-perf -c kserve-container -- python -c "import yaml; print('OK')"
```

If PyYAML is missing, configuration will fall back to environment variables and hardcoded defaults.

### Wrong profiling ranges

**Check environment variables in pod:**
```bash
oc exec <pod-name> -n kserve-e2e-perf -c kserve-container -- env | grep VLLM_PROFILER
```

**Check what the ConfigMap has:**
```bash
oc get configmap env-injector-files -n kserve-e2e-perf \
  -o jsonpath='{.data.profiler_config\.yaml}' | grep profiling_ranges
```

## Best Practices

1. **Use ConfigMap for defaults** - Set sensible defaults in profiler_config.yaml
2. **Use annotations for customization** - Override per pod as needed
3. **Start with conservative ranges** - Use narrow ranges initially (10 calls), expand as needed
4. **Disable stack traces under load** - `with_stack: false` significantly reduces overhead
5. **Use the gate file mechanism** - The counter resets on gate activation, making the same range reusable
6. **Enable debug mode during testing** - Helps verify configuration is applied correctly
7. **Use per-rank output patterns** - Always include `{rank}` in file_pattern for TP deployments
