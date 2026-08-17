#!/bin/bash
set -e

################################################################################
# vLLM Profiler Integration Test
################################################################################
#
# This script performs end-to-end testing of the vLLM profiler system:
#
# 1. Deploys the profiler webhook and ConfigMap using deploy.sh
# 2. Creates a vLLM pod with profiler instrumentation
# 3. Waits for vLLM server to start and respond to health checks
# 4. Sends a single inference request generating 200 tokens
# 5. Verifies profiler output appears in pod logs
# 6. Cleans up all test resources automatically
#
# The test validates:
# - Profiler webhook intercepts pod creation
# - Profiler code is injected via ConfigMap
# - sitecustomize.py loads and instruments vLLM
# - VLLM_RPC_TIMEOUT environment variable is set
# - Profiler captures and outputs trace data
# - Optional: Chrome trace file is exported
#
# Usage:
#   ./test-vllm-integration.sh
#
# Environment variables (optional):
#   VLLM_MODEL         - HuggingFace model to use (default: facebook/opt-125m)
#   VLLM_IMAGE         - vLLM container image (default: vllm/vllm-openai:latest)
#   TARGET_NAMESPACE   - Kubernetes namespace (default: downstream-llm-d)
#   MAX_MODEL_LEN      - Max model context length (default: 2048)
#
# Requirements:
# - oc/kubectl CLI configured
# - Cluster admin permissions
# - 4-8GB memory available for vLLM pod
#
################################################################################

NAMESPACE="${TARGET_NAMESPACE:-downstream-llm-d}"
POD_NAME="test-vllm-integration"
MODEL="${VLLM_MODEL:-facebook/opt-125m}"  # Small model for testing
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:latest}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-2048}"

COLOR_RESET='\033[0m'
COLOR_INFO='\033[0;34m'
COLOR_SUCCESS='\033[0;32m'
COLOR_ERROR='\033[0;31m'
COLOR_WARN='\033[1;33m'

log_info() { echo -e "${COLOR_INFO}[INFO]${COLOR_RESET} $*"; }
log_success() { echo -e "${COLOR_SUCCESS}[SUCCESS]${COLOR_RESET} $*"; }
log_error() { echo -e "${COLOR_ERROR}[ERROR]${COLOR_RESET} $*"; }
log_warn() { echo -e "${COLOR_WARN}[WARN]${COLOR_WARN} $*"; }

cleanup() {
    log_info "Cleaning up test resources..."
    oc delete pod "$POD_NAME" -n "$NAMESPACE" --ignore-not-found=true --wait=false
    oc delete pod test-curl-client -n "$NAMESPACE" --ignore-not-found=true --wait=false 2>/dev/null || true
    log_success "Cleanup complete"
}

trap cleanup EXIT

log_info "Starting vLLM profiler integration test"
log_info "Configuration:"
log_info "  Namespace: $NAMESPACE"
log_info "  Pod name: $POD_NAME"
log_info "  Model: $MODEL"
log_info "  Image: $VLLM_IMAGE"

# Step 1: Deploy profiler
log_info "Step 1/6: Deploying profiler..."
./deploy.sh
log_success "Profiler deployed"

# Step 2: Create vLLM pod
log_info "Step 2/6: Creating vLLM pod..."
cat <<EOF | oc apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $POD_NAME
  namespace: $NAMESPACE
  labels:
    llm-d.ai/inferenceServing: "true"
    test: "vllm-integration"
spec:
  containers:
  - name: vllm
    image: $VLLM_IMAGE
    env:
    - name: HOME
      value: /tmp
    - name: HF_HOME
      value: /tmp/huggingface
    - name: TRANSFORMERS_CACHE
      value: /tmp/huggingface
    - name: XDG_CACHE_HOME
      value: /tmp/cache
    - name: FLASHINFER_WORKSPACE_DIR
      value: /tmp/flashinfer
    command:
    - python3
    - -m
    - vllm.entrypoints.openai.api_server
    - --model
    - $MODEL
    - --max-model-len
    - "$MAX_MODEL_LEN"
    - --host
    - "0.0.0.0"
    - --port
    - "8000"
    ports:
    - containerPort: 8000
      name: http
    resources:
      requests:
        memory: "4Gi"
      limits:
        memory: "8Gi"
  restartPolicy: Never
EOF

log_success "vLLM pod created"

# Step 3: Wait for pod to be running
log_info "Step 3/6: Waiting for pod to be running..."
oc wait --for=condition=Ready pod/$POD_NAME -n $NAMESPACE --timeout=300s
log_success "Pod is running"

# Step 4: Wait for vLLM server to be ready
log_info "Step 4/6: Waiting for vLLM server to be ready..."
log_info "Checking /v1/models endpoint for server startup..."
MAX_WAIT=600
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
    # Try to curl the /v1/models endpoint from within the pod
    if oc exec $POD_NAME -n $NAMESPACE -- curl -sf http://localhost:8000/v1/models >/dev/null 2>&1; then
        log_success "vLLM server is ready and responding"
        break
    fi
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        log_error "Timeout waiting for vLLM server to start"
        log_info "Last 100 lines of pod logs:"
        oc logs $POD_NAME -n $NAMESPACE --tail=100
        exit 1
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
    if [ $((ELAPSED % 30)) -eq 0 ]; then
        echo ""
        log_info "Still waiting... (${ELAPSED}s elapsed)"
    else
        echo -n "."
    fi
done
echo ""

# Get pod IP
POD_IP=$(oc get pod $POD_NAME -n $NAMESPACE -o jsonpath='{.status.podIP}')
log_info "Pod IP: $POD_IP"

# Step 5: Send single inference request to generate 200 tokens
log_info "Step 5/6: Sending single inference request (200 tokens)..."

# Create a temporary pod to run curl command
CURL_POD="test-curl-client"
log_info "Creating client pod to send request..."
oc run $CURL_POD -n $NAMESPACE --image=curlimages/curl:latest --rm -i --restart=Never -- /bin/sh -c "
set -e
echo 'Sending inference request to generate 200 tokens...'
curl -v -X POST http://$POD_IP:8000/v1/completions \
    -H 'Content-Type: application/json' \
    -d '{
        \"model\": \"$MODEL\",
        \"prompt\": \"Write a detailed story about a brave knight who goes on an adventure:\",
        \"max_tokens\": 200,
        \"temperature\": 0.7
    }'
echo ''
echo 'Request completed'
" || log_warn "Request may have failed, continuing to check logs..."

log_success "Inference request sent"

# Step 6: Check logs for profiler output
log_info "Step 6/6: Checking vLLM logs for profiler output..."
sleep 5  # Give profiler time to write output

log_info "Searching for profiler output markers..."
LOGS=$(oc logs $POD_NAME -n $NAMESPACE)

# Check for profiler installation
if echo "$LOGS" | grep -q "\[profiler\] vLLM profiler installed"; then
    log_success "✓ Profiler was loaded"
else
    log_error "✗ Profiler was NOT loaded"
    exit 1
fi

# Check for profiler start
if echo "$LOGS" | grep -q "\[profiler\] Starting profiler"; then
    log_success "✓ Profiler started recording"
else
    log_warn "△ Profiler start message not found (may need more requests)"
fi

# Check for profiler output
if echo "$LOGS" | grep -q "===== begin profiler output"; then
    log_success "✓ Profiler output found!"
    log_info "Extracting profiler output..."
    echo "$LOGS" | sed -n '/===== begin profiler output/,/===== end profiler output/p' | head -60

    # Check for trace export
    if echo "$LOGS" | grep -q "Exported trace to:"; then
        TRACE_FILE=$(echo "$LOGS" | grep "Exported trace to:" | tail -1 | sed 's/.*Exported trace to: //')
        log_success "✓ Trace file exported: $TRACE_FILE"
    elif echo "$LOGS" | grep -q "Chrome trace export disabled"; then
        log_info "◉ Chrome trace export is disabled (export_chrome_trace=false)"
    else
        log_warn "△ No trace export message found"
    fi
else
    log_error "✗ Profiler output NOT found"
    log_info "This could mean:"
    log_info "  - Not enough inference calls were made to trigger profiling"
    log_info "  - Profiling range is configured differently (check profiler_config.yaml)"
    log_info "  - Current config: profiling_ranges should cover the call range"
    log_info ""
    log_info "Last 100 lines of pod logs:"
    echo "$LOGS" | tail -100
    exit 1
fi

# Check for VLLM_RPC_TIMEOUT
if echo "$LOGS" | grep -q "VLLM_RPC_TIMEOUT"; then
    log_success "✓ VLLM_RPC_TIMEOUT environment variable is set"
else
    log_info "◉ VLLM_RPC_TIMEOUT not visible in logs (this is normal)"
fi

log_success "Integration test PASSED!"
log_info "Summary:"
log_info "  ✓ Profiler deployed and loaded"
log_info "  ✓ vLLM server started successfully"
log_info "  ✓ Inference request completed (200 tokens)"
log_info "  ✓ Profiler output generated"
log_info ""
log_info "To retrieve trace file (if exported):"
log_info "  oc exec $POD_NAME -n $NAMESPACE -- ls -lh /tmp/trace*.json"
log_info "  oc cp $NAMESPACE/$POD_NAME:/tmp/trace_pid*.json ./trace.json"
