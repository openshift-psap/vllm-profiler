#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Validation/troubleshooting for the env-injector mutating webhook.
# Defaults are tuned for this repo; override via env vars as needed.
#
# Environment variables:
#   PROFILER_NS   - Namespace of the webhook (default: vllm-profiler)
#   SVC           - Service name of the webhook (default: env-injector)
#   MWC           - MutatingWebhookConfiguration name (default: env-injector-webhook)
#   TARGET_NS     - Namespace of pods to mutate (default: llm-d-inference-scheduler)
#   LABEL_KEY     - Label key to match (default: llm-d.ai/inferenceServing)
#   LABEL_VAL     - Label value to match (default: true)
#   INJECT_ENV    - Env var name expected to be injected (default: EXAMPLE_KEY)
#   DO_PATCH      - If "1", attempt to patch caBundle using patch-ca-bundle.sh (default: 0)
#   DO_SIMPLE_TEST - If "1", create a simple test pod to validate injection, then clean up (default: 0)
#
# Example:
#   PROFILER_NS=vllm-profiler TARGET_NS=llm-d-inference-scheduler DO_SIMPLE_TEST=1 ./validate_webhook.sh

PROFILER_NS="${PROFILER_NS:-vllm-profiler}"
SVC="${SVC:-env-injector}"
MWC="${MWC:-env-injector-webhook}"
TARGET_NS="${TARGET_NS:-llm-d-inference-scheduler}"
LABEL_KEY="${LABEL_KEY:-llm-d.ai/inferenceServing}"
LABEL_VAL="${LABEL_VAL:-true}"
INJECT_ENV="${INJECT_ENV:-EXAMPLE_KEY}"
DO_PATCH="${DO_PATCH:-0}"
DO_SIMPLE_TEST="${DO_SIMPLE_TEST:-0}"

have() { command -v "$1" >/dev/null 2>&1; }
KC="kubectl"
OC="oc"
LOGS_CMD="$KC logs"
if have "$OC"; then
  LOGS_CMD="$OC logs --insecure-skip-tls-verify-backend"
  KC="$OC"
fi

echo "=== Webhook validation ==="
echo "Profiler NS: $PROFILER_NS"
echo "Service:     $SVC"
echo "Webhook:     $MWC"
echo "Target NS:   $TARGET_NS"
echo "Label match: $LABEL_KEY=$LABEL_VAL"
echo "Inject env:  $INJECT_ENV"
echo

echo "1) Check MutatingWebhookConfiguration existence and config"
if ! $KC get mutatingwebhookconfiguration "$MWC" >/dev/null 2>&1; then
  echo "ERROR: MutatingWebhookConfiguration $MWC not found"
else
  $KC get mutatingwebhookconfiguration "$MWC" -o jsonpath='{.webhooks[0].name}{"\n"}' || true
  echo "- failurePolicy: $($KC get mutatingwebhookconfiguration "$MWC" -o jsonpath='{.webhooks[0].failurePolicy}')" || true
  echo "- rules:"
  $KC get mutatingwebhookconfiguration "$MWC" -o jsonpath='{.webhooks[0].rules[*].resources}{" "}{.webhooks[0].rules[*].operations}{" "}{.webhooks[0].rules[*].scope}{"\n"}' || true
  CAB="$($KC get mutatingwebhookconfiguration "$MWC" -o jsonpath='{.webhooks[0].clientConfig.caBundle}' 2>/dev/null || echo "")"
  CAB_LEN=${#CAB}
  echo "- caBundle length: $CAB_LEN"
  if [ "$CAB_LEN" -eq 0 ]; then
    echo "WARNING: caBundle is empty. API server will not be able to call the webhook."
    if [ "$DO_PATCH" = "1" ]; then
      if [ -f "${SCRIPT_DIR}/patch-ca-bundle.sh" ]; then
        echo "Patching caBundle using patch-ca-bundle.sh..."
        NS="$PROFILER_NS" SVC="$SVC" "${SCRIPT_DIR}/patch-ca-bundle.sh" || true
      else
        echo "patch-ca-bundle.sh not found in scripts directory."
      fi
    fi
  fi
fi
echo

echo "2) Check TLS secret and keys"
if ! $KC -n "$PROFILER_NS" get secret "${SVC}-certs" >/dev/null 2>&1; then
  echo "ERROR: Secret ${SVC}-certs not found in $PROFILER_NS"
else
  echo "- Secret ${SVC}-certs present"
  $KC -n "$PROFILER_NS" get secret "${SVC}-certs" -o jsonpath='{.type}{" "}{.data.tls\.crt}{" "}{.data.tls\.key}{"\n"}' | awk '{print "- type:",$1,"crt_set:",(length($2)>0),"key_set:",(length($3)>0)}'
fi
echo

echo "3) Check Service and Endpoints"
$KC -n "$PROFILER_NS" get svc "$SVC" -o wide || true
$KC -n "$PROFILER_NS" get endpoints "$SVC" -o wide || true
echo

echo "4) Check webhook pod and logs"
POD="$($KC -n "$PROFILER_NS" get pod -l app=env-injector -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [ -z "$POD" ]; then
  echo "ERROR: No webhook pod found with label app=env-injector"
else
  echo "- Pod: $POD"
  $KC -n "$PROFILER_NS" get pod "$POD" -o wide || true
  echo "- Logs (last 50 lines):"
  $LOGS_CMD -n "$PROFILER_NS" "$POD" --tail=50 || echo "WARNING: Could not fetch logs (TLS/backend issue?)"
fi
echo

echo "5) Verify target Deployment templates carry the label ($LABEL_KEY=$LABEL_VAL)"
$KC -n "$TARGET_NS" get deploy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.metadata.labels}{"\n"}{end}' | sed 's/^/- /'
echo

echo "6) Minimal path test (optional DO_SIMPLE_TEST=$DO_SIMPLE_TEST)"
if [ "$DO_SIMPLE_TEST" = "1" ]; then
  TEST_POD="envinj-test-$(date +%s)"
  echo "- Creating test pod $TEST_POD in $TARGET_NS with label $LABEL_KEY=$LABEL_VAL"
  $KC -n "$TARGET_NS" run "$TEST_POD" \
    --image=registry.k8s.io/pause:3.9 \
    --labels="${LABEL_KEY}=${LABEL_VAL}" \
    --restart=Never >/dev/null
  echo "- Waiting up to 60s for pod to be created"
  $KC -n "$TARGET_NS" wait --for=condition=PodScheduled --timeout=60s "pod/$TEST_POD" >/dev/null 2>&1 || true
  echo "- Checking injected env in created pod (name=value):"
  if have jq; then
    $KC -n "$TARGET_NS" get pod "$TEST_POD" -o json \
      | jq -r '.spec.containers[0].env // [] | .[] | "\(.name)=\(.value // "")"' || true
  else
    $KC -n "$TARGET_NS" get pod "$TEST_POD" -o jsonpath='{range .spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' 2>/dev/null || echo "No env list found"
  fi
  echo
  echo "- Cleaning up test pod"
  $KC -n "$TARGET_NS" delete pod "$TEST_POD" --ignore-not-found >/dev/null 2>&1 || true
else
  echo "- Skipping test pod creation. To enable, run with DO_SIMPLE_TEST=1"
fi
echo

echo "7) Summary"
echo "- If caBundle length is 0, patch it (DO_PATCH=1 ./patch-ca-bundle.sh)."
echo "- Ensure Service has endpoints (webhook pod Ready)."
echo "- Ensure target Deployment templates include label ${LABEL_KEY}=${LABEL_VAL}."
echo "- Create a labeled test pod (DO_SIMPLE_TEST=1) and check env contains ${INJECT_ENV}."
echo "Done."


