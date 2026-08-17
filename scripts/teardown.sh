#!/bin/bash
set -euo pipefail

# vLLM Profiler Webhook Teardown Script
# Removes all resources created by the profiler webhook deployment

# Configuration
NAMESPACE="vllm-profiler"
TARGET_NAMESPACE="${TARGET_NAMESPACE:-downstream-llm-d}"
WEBHOOK_NAME="env-injector-webhook"
CONFIGMAP_NAME="env-injector-files"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Remove all resources created by the vLLM profiler webhook deployment.

Options:
    --force             Skip confirmation prompt
    --help              Show this help message

Environment Variables:
    TARGET_NAMESPACE    Namespace where ConfigMap was created (default: downstream-llm-d)

Resources that will be removed:
    - MutatingWebhookConfiguration: ${WEBHOOK_NAME}
    - Namespace: ${NAMESPACE} (includes Deployment, Service, ServiceAccount, Secret)
    - ConfigMap: ${CONFIGMAP_NAME} (in ${TARGET_NAMESPACE})

Note: Pods already injected with profiler code will continue running with
      profiler until they are restarted. The webhook will no longer inject
      profiler code into new pods.

EOF
    exit 0
}

# Parse arguments
FORCE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --force|-f)
            FORCE=true
            shift
            ;;
        --help|-h)
            usage
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            ;;
    esac
done

# Main teardown
main() {
    log_info "vLLM Profiler Webhook Teardown"
    echo ""
    log_warn "This will remove all webhook resources:"
    echo "  - MutatingWebhookConfiguration: ${WEBHOOK_NAME}"
    echo "  - Namespace: ${NAMESPACE}"
    echo "  - ConfigMap: ${CONFIGMAP_NAME} (in ${TARGET_NAMESPACE})"
    echo ""

    # Confirmation prompt
    if [ "$FORCE" = false ]; then
        read -p "Are you sure you want to proceed? (yes/no): " confirm
        if [ "$confirm" != "yes" ]; then
            log_info "Teardown cancelled"
            exit 0
        fi
    fi

    echo ""
    log_info "Starting teardown..."

    # Step 1: Delete MutatingWebhookConfiguration
    log_info "Step 1/3: Deleting MutatingWebhookConfiguration..."
    if oc delete mutatingwebhookconfiguration "${WEBHOOK_NAME}" --ignore-not-found=true; then
        log_success "MutatingWebhookConfiguration deleted"
    else
        log_warn "MutatingWebhookConfiguration not found or already deleted"
    fi

    # Step 2: Delete namespace
    log_info "Step 2/3: Deleting namespace ${NAMESPACE}..."
    if oc delete namespace "${NAMESPACE}" --ignore-not-found=true; then
        log_info "Waiting for namespace deletion to complete..."
        local timeout=30
        local elapsed=0
        while oc get namespace "${NAMESPACE}" &>/dev/null; do
            if [ $elapsed -ge $timeout ]; then
                log_warn "Namespace deletion is taking longer than expected (continuing in background)"
                break
            fi
            sleep 2
            elapsed=$((elapsed + 2))
        done
        log_success "Namespace ${NAMESPACE} deleted"
    else
        log_warn "Namespace not found or already deleted"
    fi

    # Step 3: Delete ConfigMap
    log_info "Step 3/3: Deleting ConfigMap in ${TARGET_NAMESPACE}..."
    if oc delete configmap "${CONFIGMAP_NAME}" -n "${TARGET_NAMESPACE}" --ignore-not-found=true; then
        log_success "ConfigMap deleted"
    else
        log_warn "ConfigMap not found or already deleted"
    fi

    echo ""
    log_success "Teardown complete!"
    echo ""
    log_info "All webhook resources have been removed."
    log_info "Existing pods with injected profiler will continue to run until restarted."
}

# Run main function
main
