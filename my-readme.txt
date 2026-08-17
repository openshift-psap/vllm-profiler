================================================================================
  vLLM Profiler - Personal Quick Reference
================================================================================

TABLE OF CONTENTS
  1. What This Project Does (Big Picture)
  2. Project Structure / Key Files
  3. How the Profiling Works (End-to-End Flow)
  4. Prerequisites Checklist
  5. First-Time Setup (From Scratch)
  6. Day-to-Day: Running a Profiling Session
  7. Collecting the Profile Traces
  8. Viewing Traces
  9. Changing the Profiling Configuration
  10. Updating the ConfigMap on the Cluster
  11. Customizing Profile File Names
  12. Troubleshooting
  13. Tearing Everything Down
  14. Quick Command Cheat Sheet

================================================================================
1. WHAT THIS PROJECT DOES (BIG PICTURE)
================================================================================

This project profiles vLLM inference servers running on Kubernetes (OpenShift)
WITHOUT modifying any vLLM source code. It uses a Kubernetes mutating admission
webhook to automatically inject a PyTorch profiler into vLLM pods at startup.

The profiler captures CPU and CUDA kernel activity during a configurable window
of forward passes (e.g., calls 2000-2010 of Worker.execute_model) and exports
Chrome trace JSON files that can be visualized in chrome://tracing.

Target cluster:  H200 cluster (psap-rhaiis-h200.ibm-rh-ai.rhperfscale.org)
Target namespace: kserve-e2e-perf
GPU node:         psap-rhaiis-h200-gpu-worker-1-k5pc4 (8x NVIDIA H200)

================================================================================
2. PROJECT STRUCTURE / KEY FILES
================================================================================

Core profiler code (injected into vLLM pods):
  sitecustomize.py       - The profiler itself. Auto-loaded by Python via
                           PYTHONPATH. Installs an import hook that wraps
                           Worker.execute_model with torch.profiler.
  profiler_config.yaml   - Controls what/when/how to profile (ranges,
                           activities, output file pattern, etc.)

Webhook server (runs as its own pod in vllm-profiler namespace):
  webhook.py             - Flask app that intercepts pod creation via
                           MutatingAdmissionWebhook. Injects PYTHONPATH env
                           var and mounts the ConfigMap with profiler files.
  Dockerfile             - Builds the webhook container image.
  requirements.txt       - Python deps for webhook (flask).

Kubernetes manifests:
  manifests.yaml         - Namespace, Deployment, Service, ServiceAccount,
                           MutatingWebhookConfiguration for the webhook.
  kustomization.yaml     - Bundles sitecustomize.py + profiler_config.yaml
                           into a ConfigMap (env-injector-files) in the
                           target namespace.

Deployment scripts (in scripts/):
  scripts/deploy.sh           - Full deployment: build image, apply manifests,
                                create ConfigMap, generate TLS certs, patch CA.
  scripts/gen-certs.sh        - Generates self-signed TLS cert for the webhook.
  scripts/patch-ca-bundle.sh  - Patches the MutatingWebhookConfiguration with
                                the CA bundle so the API server trusts the webhook.
  scripts/validate_webhook.sh - Validates the entire webhook setup (certs, pod,
                                endpoints, labels, optional test pod).
  scripts/teardown.sh         - Removes all webhook resources.

Test scripts (in tests/):
  tests/test-profiler.sh            - Profiler test script.
  tests/test-vllm-integration.sh    - End-to-end integration test.
  tests/test-profiler-features.yaml - Test fixture YAML.

Server configs (what you deploy to run profiling):
  server_configs/        - KServe ServingRuntime + InferenceService YAMLs
                           for different models and RHAIIS versions.
                           Example: deepseek-r1-rhaiis-3.4-EA1.yaml

Collected profiles:
  profiles/              - Local copies of trace JSON files, organized
                           by run (e.g., deepseek-rhaiis-3.3-profiles/).

Analysis scripts (in analysis/):
  analysis/compare_deepseek_profiles.py  - Compare DeepSeek profile traces.
  analysis/compare_profiles.py           - Compare GPT-OSS profile traces.
  analysis/import_manual_runs_json_v2.py - Import guidellm benchmark results.
  analysis/CONFIGURATION_EXAMPLES.md     - Older configuration examples.

Run logs (in logs/):
  logs/3.2.5-logs.txt         - RHAIIS 3.2.5 GPT-OSS profiling logs.
  logs/3.2.5-deepseek-logs.txt - RHAIIS 3.2.5 DeepSeek profiling logs.
  logs/3.3-logs.txt           - RHAIIS 3.3 GPT-OSS profiling logs.
  logs/3.3-deepseek-logs.txt  - RHAIIS 3.3 DeepSeek profiling logs.
  logs/guidellm-command.txt   - guidellm load generator commands.

Other:
  demo-vllm-profiler.ipynb  - Interactive notebook walking through the flow.

================================================================================
3. HOW THE PROFILING WORKS (END-TO-END FLOW)
================================================================================

  [You deploy a vLLM InferenceService with the right labels]
       |
       v
  [K8s API server sends AdmissionReview to webhook]
       |
       v
  [webhook.py checks namespace + labels, builds JSON patch]
       |  Injects:
       |    - env PYTHONPATH=/home/vllm/profiler
       |    - volume mount: sitecustomize.py -> /home/vllm/profiler/
       |    - volume mount: profiler_config.yaml -> /home/vllm/profiler/
       v
  [Pod starts, Python auto-loads sitecustomize.py via PYTHONPATH]
       |
       v
  [Import hook intercepts "import vllm.v1.worker.gpu_worker"]
       |
       v
  [Wraps Worker.execute_model with torch.profiler]
       |
       v
  [execute_model call counter increments on each forward pass]
       |
       v
  [At call #2000: profiler starts recording CPU+CUDA activity]
       |
       v
  [At call #2010: profiler stops, prints stats table, exports trace JSON]
       |
       v
  [Trace file saved at /tmp/trace_rank{N}_pid{P}_range2000-2010.json]
       |
       v
  [You copy trace files locally with oc cp, view in chrome://tracing]

Label matching:
  The webhook instruments pods that have the label it's configured to look
  for. Check what the webhook is currently watching:

    oc get deployment env-injector -n vllm-profiler \
      -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="TARGET_LABELS")].value}'

  Currently configured:  vllm-profiler/enabled=true

  This means the InferenceService metadata.labels MUST include:
    vllm-profiler/enabled: "true"

  All server_configs/*.yaml files in this repo already have this label
  set on the InferenceService. If you're profiling someone else's manifest,
  add this label to the InferenceService (NOT the ServingRuntime) so that
  the resulting pods get instrumented.

  Note: manifests.yaml has a different default (llm-d.ai/inferenceServing),
  but the running webhook was patched to use vllm-profiler/enabled instead.
  If you ever redeploy the webhook from scratch with deploy.sh, update
  manifests.yaml to match.

================================================================================
4. PREREQUISITES CHECKLIST
================================================================================

Before you can profile, verify ALL of the following are in place:

  [ ] Cluster access
      - oc login to the H200 cluster
      - Verify: oc whoami  -->  should show kube:admin

  [ ] Webhook pod running
      - Verify: oc get pods -n vllm-profiler
      - Should show env-injector pod 1/1 Running

  [ ] MutatingWebhookConfiguration exists with valid CA
      - Verify: oc get mutatingwebhookconfiguration env-injector-webhook
      - Check caBundle is populated (not empty):
        oc get mutatingwebhookconfiguration env-injector-webhook \
          -o jsonpath='{.webhooks[0].clientConfig.caBundle}' | wc -c
        --> should be > 0 (typically ~1700 chars)

  [ ] TLS secret exists
      - Verify: oc get secret env-injector-certs -n vllm-profiler

  [ ] ConfigMap with profiler code exists in target namespace
      - Verify: oc get configmap env-injector-files -n kserve-e2e-perf
      - Should have DATA=2 (sitecustomize.py + profiler_config.yaml)

  [ ] GPU node is Ready with GPUs available
      - Verify: oc get node psap-rhaiis-h200-gpu-worker-1-k5pc4
      - Check GPU allocation:
        oc describe node psap-rhaiis-h200-gpu-worker-1-k5pc4 | grep nvidia.com/gpu
        --> nvidia.com/gpu should show 0 in Allocated (all 8 free)

  [ ] PVC with model weights exists
      - Verify: oc get pvc model-pvc -n kserve-e2e-perf
      - Should be Bound

  [ ] ServiceAccount exists
      - Verify: oc get sa sa -n kserve-e2e-perf

  [ ] Secrets exist (HF token + image pull)
      - Verify: oc get secret storage-config -n kserve-e2e-perf
      - Verify: oc get secret npalaska-image-pull -n kserve-e2e-perf

  [ ] No conflicting InferenceServices already running
      - Verify: oc get inferenceservice -n kserve-e2e-perf
      - Should show "No resources found" (or nothing using the GPU node)

  Quick all-in-one check:
      TARGET_NS=kserve-e2e-perf DO_SIMPLE_TEST=1 ./scripts/validate_webhook.sh

================================================================================
5. FIRST-TIME SETUP (FROM SCRATCH)
================================================================================

If the webhook has never been deployed, or you need to rebuild everything:

  Step 1: Log in to the cluster
  -----------------------------
    oc login https://api.psap-rhaiis-h200.ibm-rh-ai.rhperfscale.org:6443

  Step 2: Run the full deployment
  -------------------------------
    ./scripts/deploy.sh

    This does 7 things:
      1. Builds the webhook container image (podman build)
      2. Pushes it to quay.io/mimehta/vllmprofiler
      3. Deletes any existing resources
      4. Applies manifests.yaml (namespace, deployment, service, webhook config)
      5. Applies kustomization (creates ConfigMap with profiler code)
      6. Generates TLS certificates (scripts/gen-certs.sh)
      7. Patches webhook with CA bundle (scripts/patch-ca-bundle.sh)

    Or skip the image build if it's already pushed:
      ./scripts/deploy.sh --skip-build

  Step 3: Validate
  ----------------
    TARGET_NS=kserve-e2e-perf DO_SIMPLE_TEST=1 ./scripts/validate_webhook.sh

  That's it. The webhook is now watching for pods in kserve-e2e-perf
  with the matching labels and will inject the profiler automatically.

================================================================================
6. DAY-TO-DAY: RUNNING A PROFILING SESSION
================================================================================

Assuming the webhook is already deployed (see section 4 to verify):

  Step 1: Log in (if session expired)
  ------------------------------------
    oc login https://api.psap-rhaiis-h200.ibm-rh-ai.rhperfscale.org:6443

  Step 2: Verify prereqs are in place
  ------------------------------------
    oc whoami
    oc get pods -n vllm-profiler
    oc get configmap env-injector-files -n kserve-e2e-perf

  Step 3: Verify the forward pass range
  ---------------------------------------
    Check what range is configured on the cluster (this is what pods will use):

      oc get configmap env-injector-files -n kserve-e2e-perf \
        -o jsonpath='{.data.profiler_config\.yaml}' | grep profiling_ranges

    Expected output:  profiling_ranges: "2000-2010"

    If you need a different range, edit profiler_config.yaml locally and
    push it (see section 10).

  Step 4: Verify sitecustomize.py is up to date on the cluster
  --------------------------------------------------------------
    If you've edited sitecustomize.py locally, make sure the cluster has
    the latest version. Compare sizes as a quick check:

      # Cluster version size
      oc get configmap env-injector-files -n kserve-e2e-perf \
        -o jsonpath='{.data.sitecustomize\.py}' | wc -c

      # Local version size
      wc -c sitecustomize.py

    If they differ, update the ConfigMap:
      oc delete configmap env-injector-files -n kserve-e2e-perf
      oc apply -k .

    Note: Only new pods pick up ConfigMap changes. Already-running pods
    keep using the old version until restarted.

  Step 5: Check no existing workloads are using the GPUs
  -------------------------------------------------------
    oc get inferenceservice -n kserve-e2e-perf
    oc describe node psap-rhaiis-h200-gpu-worker-1-k5pc4 | grep nvidia.com/gpu

  Step 6: Deploy a model server
  ------------------------------
    Pick a config from server_configs/:

      # DeepSeek R1 on RHAIIS 3.4 EA1
      oc apply -f server_configs/deepseek-r1-rhaiis-3.4-EA1.yaml

      # GPT-OSS 120B on RHAIIS 3.4 EA2
      oc apply -f server_configs/gptoss-rhaiis-3.4-EA2.yaml

  Step 7: Wait for the model to load
  ------------------------------------
    Watch the pod come up:
      oc get pods -n kserve-e2e-perf -w

    Check logs for profiler installation confirmation:
      oc logs -n kserve-e2e-perf <pod-name> -f 2>&1 | grep '\[profiler\]'

    You should see:
      [profiler] vLLM profiler installed - will profile ranges: [(2000, 2010)]
      [profiler] Successfully wrapped vllm.v1.worker.gpu_worker.Worker.execute_model

    If the ranges shown don't match what you expected, the ConfigMap is
    out of date. Update it (section 10) and redeploy the pod.

  Step 8: Send traffic to trigger profiling
  -------------------------------------------
    The profiler activates after the configured number of execute_model calls
    (default: call #2000). You need to send enough inference requests to
    reach that count.

    Option A: Use guidellm or another load generator pointed at the service.
    Option B: Use a curl pod to send requests manually:

      # Get the service ClusterIP
      oc get svc -n kserve-e2e-perf

      # Run a curl pod
      oc run curl-test -n kserve-e2e-perf --rm -i --restart=Never \
        --image=curlimages/curl:latest -- \
        curl -X POST http://<service-name>:8080/v1/completions \
          -H 'Content-Type: application/json' \
          -d '{"model":"deepseek-ai/DeepSeek-R1-0528","prompt":"Hello","max_tokens":200}'

  Step 9: Watch for profiler completion
  --------------------------------------
    oc logs -n kserve-e2e-perf <pod-name> 2>&1 | grep '\[profiler\]'

    Expected sequence:
      [profiler] Starting profiler for range 2000-2010 (call #2000)
      [profiler] Stopping profiler for range 2000-2010 (call #2010)
      [profiler] Exported trace to: /tmp/trace_rank0_pid455_range2000-2010.json

    With TP=8 you'll see this from each rank (rank0 through rank7).

================================================================================
7. COLLECTING THE PROFILE TRACES
================================================================================

  Step 1: Find the pod name
  --------------------------
    oc get pods -n kserve-e2e-perf

    For InferenceService-based deployments, the pod name will be something
    like: deepseek-r1-0528-profiler-3-4-ea1-predictor-xxxxx-xxxxx

  Step 2: Check which trace files were exported
  -----------------------------------------------
    oc logs -n kserve-e2e-perf <pod-name> 2>&1 | grep "Exported trace to"

    Or list them directly:
    oc exec -n kserve-e2e-perf <pod-name> -c kserve-container \
      -- ls -lh /tmp/trace_rank*.json

  Step 3: Create a local folder and copy the files
  --------------------------------------------------
    mkdir -p profiles/<run-name>

    Option A - Copy individually:
      POD=<pod-name>
      NS=kserve-e2e-perf
      CONTAINER=kserve-container
      oc cp ${NS}/${POD}:/tmp/trace_rank0_pid455_range2000-2010.json \
        profiles/<run-name>/ -c ${CONTAINER}

    Option B - Copy all at once using tar:
      POD=<pod-name>
      oc exec -n kserve-e2e-perf ${POD} -c kserve-container -- \
        bash -c 'tar cf - /tmp/trace_rank*.json' \
        | tar xf - -C profiles/<run-name>/ --strip-components=1

    Option C - Loop over ranks (TP=8):
      POD=<pod-name>
      for rank in 0 1 2 3 4 5 6 7; do
        file=$(oc exec -n kserve-e2e-perf ${POD} -c kserve-container \
          -- bash -c "ls /tmp/trace_rank${rank}_*.json 2>/dev/null")
        if [ -n "$file" ]; then
          oc cp kserve-e2e-perf/${POD}:${file} profiles/<run-name>/ \
            -c kserve-container
        fi
      done

  Step 4: Validate the copied files
  -----------------------------------
    for f in profiles/<run-name>/trace_rank*.json; do
      echo -n "$(basename $f): "
      python3 -c "
        import json
        data = json.load(open('$f'))
        print(f'valid JSON, {len(data.get(\"traceEvents\", []))} trace events')
      "
    done

    Expected output (DeepSeek R1 with TP=8, 10 forward passes):
      trace_rank0_pid455_range2000-2010.json: valid JSON, 89007 trace events
      trace_rank1_pid456_range2000-2010.json: valid JSON, 89019 trace events
      ... (8 files total)

================================================================================
8. VIEWING TRACES
================================================================================

  Option A: Chrome tracing
    1. Open Chrome browser
    2. Go to chrome://tracing
    3. Click "Load" and select a trace JSON file
    4. Use W/S to zoom, A/D to pan, click events for details

  Option B: Perfetto (better for large traces)
    1. Go to https://ui.perfetto.dev
    2. Click "Open trace file"
    3. Load the JSON file

  Tip: Start with rank 0 to get an overview. Compare ranks to check
  for load imbalance (especially with MoE models like DeepSeek R1).

================================================================================
9. CHANGING THE PROFILING CONFIGURATION
================================================================================

  --- Profiling Range ---

  The range controls which execute_model calls get profiled.
  Current default: "2000-2010" (10 forward passes after 2000-call warmup)

  Three ways to set it (in increasing priority, highest wins):

    1. profiler_config.yaml (applies to all pods):
         profiling_ranges: "500-503"

    2. Pod annotation (per-pod override):
         annotations:
           vllm.profiler/ranges: "500-503"

    3. Environment variable (highest priority):
         env:
           - name: VLLM_PROFILER_RANGES
             value: "500-503"

  Multiple windows:
    profiling_ranges: "500-503,2000-2003"

  --- Other Settings ---

  All configurable via profiler_config.yaml, annotations, or env vars:

    Setting              Config YAML key          Env var
    -------              ---------------          -------
    Activities           activities               VLLM_PROFILER_ACTIVITIES
    Record shapes        options.record_shapes    VLLM_PROFILER_RECORD_SHAPES
    Stack traces         options.with_stack       VLLM_PROFILER_WITH_STACK
    Memory profiling     options.profile_memory   VLLM_PROFILER_MEMORY
    Output file pattern  output.file_pattern      VLLM_PROFILER_OUTPUT
    Export chrome trace  output.export_chrome_trace  VLLM_PROFILER_EXPORT_TRACE
    Debug logging        advanced.debug           VLLM_PROFILER_DEBUG

  Available placeholders in file_pattern:
    {pid}   - Process ID
    {rank}  - Tensor parallel rank (from torch.distributed or LOCAL_RANK)

================================================================================
10. UPDATING THE CONFIGMAP ON THE CLUSTER
================================================================================

After editing profiler_config.yaml or sitecustomize.py locally:

  Step 1: Delete the old ConfigMap and recreate it
  -------------------------------------------------
    oc delete configmap env-injector-files -n kserve-e2e-perf
    oc apply -k .

    What does "oc apply -k ." do?
      -k tells oc to use Kustomize. The "." means the current directory.
      It reads kustomization.yaml, which bundles sitecustomize.py and
      profiler_config.yaml into a ConfigMap called "env-injector-files"
      in the kserve-e2e-perf namespace.

  Step 2: Restart vLLM pods to pick up the new config
  ----------------------------------------------------
    New pods will automatically get the updated ConfigMap.
    Existing pods need to be restarted:

    For InferenceService:
      oc delete inferenceservice <name> -n kserve-e2e-perf
      oc apply -f server_configs/<config>.yaml

    For Deployments:
      oc rollout restart deployment/<name> -n kserve-e2e-perf

  Note: The webhook itself does NOT need to be restarted when you
  update the profiler config. Only the vLLM pods need restarting.

================================================================================
11. CUSTOMIZING PROFILE FILE NAMES
================================================================================

Three methods, in increasing priority (highest wins):

  Method 1: Edit profiler_config.yaml
  -------------------------------------
    output:
      file_pattern: "/tmp/my_custom_name_rank{rank}_pid{pid}.json"

    Then update the ConfigMap (section 10).

  Method 2: Per-pod annotation
  -----------------------------
    annotations:
      vllm.profiler/output: "/tmp/deepseek_r1_steady_state_rank{rank}.json"

  Method 3: Environment variable
  --------------------------------
    env:
      - name: VLLM_PROFILER_OUTPUT
        value: "/tmp/my_experiment_rank{rank}_pid{pid}.json"

================================================================================
12. TROUBLESHOOTING
================================================================================

  Problem: "Unauthorized" when running oc commands
  -------------------------------------------------
    Your login token expired. Re-authenticate:
      oc login https://api.psap-rhaiis-h200.ibm-rh-ai.rhperfscale.org:6443

  Problem: Profiler not loading (no [profiler] messages in pod logs)
  -------------------------------------------------------------------
    1. Check the webhook pod is running:
       oc get pods -n vllm-profiler
    2. Check the webhook logs for the pod creation event:
       oc logs -n vllm-profiler <webhook-pod> --tail=50
       Look for "Proceeding with injection" vs "Namespace mismatch"
    3. Verify PYTHONPATH was injected:
       oc get pod <vllm-pod> -n kserve-e2e-perf -o jsonpath='{.spec.containers[0].env}'
       Should contain PYTHONPATH=/home/vllm/profiler
    4. Verify the ConfigMap was mounted:
       oc get pod <vllm-pod> -n kserve-e2e-perf -o jsonpath='{.spec.containers[0].volumeMounts}'

  Problem: Profiler loaded but never starts recording
  -----------------------------------------------------
    The model hasn't received enough requests to reach the profiling range.
    Check the current call count by searching logs for the range message:
      oc logs <pod> -n kserve-e2e-perf 2>&1 | grep "Starting profiler"
    If nothing, send more traffic. Default range starts at call #2000.

  Problem: caBundle is empty (webhook not being called)
  -------------------------------------------------------
    Re-run the cert generation and patching:
      bash scripts/gen-certs.sh
      bash scripts/patch-ca-bundle.sh

  Problem: Pod labels don't match webhook selector
  ---------------------------------------------------
    Check what label the webhook is looking for:
      oc get deployment env-injector -n vllm-profiler \
        -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="TARGET_LABELS")].value}'

    Currently: vllm-profiler/enabled=true

    Make sure your InferenceService has this in metadata.labels:
      vllm-profiler/enabled: "true"

    All server_configs/ files in this repo already have it.
    If profiling someone else's manifest, add the label to the
    InferenceService (the label propagates to pods via KServe).

  Problem: "oc cp" fails or trace files are truncated
  -----------------------------------------------------
    For KServe pods, specify the container name:
      oc cp ... -c kserve-container
    For large files, use the tar method (section 7, Option B).

  Full validation script:
    TARGET_NS=kserve-e2e-perf DO_SIMPLE_TEST=1 ./scripts/validate_webhook.sh

================================================================================
13. TEARING EVERYTHING DOWN
================================================================================

  Remove all profiler resources:
    ./scripts/teardown.sh --force

  This removes:
    - MutatingWebhookConfiguration
    - vllm-profiler namespace (webhook pod, service, secret)
    - ConfigMap in the target namespace

  Note: Already-running pods keep profiling until restarted.

  To also remove a deployed InferenceService:
    oc delete inferenceservice <name> -n kserve-e2e-perf
    oc delete servingruntime <name> -n kserve-e2e-perf

================================================================================
14. QUICK COMMAND CHEAT SHEET
================================================================================

  # Login
  oc login https://api.psap-rhaiis-h200.ibm-rh-ai.rhperfscale.org:6443

  # Check who I am
  oc whoami

  # Check webhook is running
  oc get pods -n vllm-profiler

  # Check ConfigMap exists
  oc get configmap env-injector-files -n kserve-e2e-perf

  # Check forward pass range on cluster
  oc get configmap env-injector-files -n kserve-e2e-perf \
    -o jsonpath='{.data.profiler_config\.yaml}' | grep profiling_ranges

  # Check sitecustomize.py size matches local (quick sync check)
  oc get configmap env-injector-files -n kserve-e2e-perf \
    -o jsonpath='{.data.sitecustomize\.py}' | wc -c

  # Check GPU availability
  oc describe node psap-rhaiis-h200-gpu-worker-1-k5pc4 | grep nvidia.com/gpu

  # Check for existing workloads
  oc get inferenceservice -n kserve-e2e-perf

  # Deploy a model for profiling
  oc apply -f server_configs/deepseek-r1-rhaiis-3.4-EA1.yaml

  # Watch pod startup
  oc get pods -n kserve-e2e-perf -w

  # Follow profiler logs
  oc logs -n kserve-e2e-perf <pod> -f 2>&1 | grep '\[profiler\]'

  # List trace files in pod
  oc exec -n kserve-e2e-perf <pod> -c kserve-container \
    -- ls -lh /tmp/trace_rank*.json

  # Copy all traces locally (tar method)
  oc exec -n kserve-e2e-perf <pod> -c kserve-container -- \
    bash -c 'tar cf - /tmp/trace_rank*.json' \
    | tar xf - -C profiles/<run-name>/ --strip-components=1

  # Update profiler config on cluster
  oc delete configmap env-injector-files -n kserve-e2e-perf
  oc apply -k .

  # Delete a profiling run
  oc delete inferenceservice <name> -n kserve-e2e-perf
  oc delete servingruntime <name> -n kserve-e2e-perf

  # Full validation
  TARGET_NS=kserve-e2e-perf DO_SIMPLE_TEST=1 ./scripts/validate_webhook.sh

  # Tear down everything
  ./scripts/teardown.sh --force
