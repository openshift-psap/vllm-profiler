#!/bin/bash
set -euo pipefail

# vLLM Profiler Webhook Deployment Script
# Builds, pushes, and deploys the mutating admission webhook for vLLM profiling

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# Configuration
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-podman}"
IMAGE_NAME="${IMAGE_NAME:-vllmprofiler}"
IMAGE_REGISTRY="${IMAGE_REGISTRY:-quay.io/mimehta}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
FULL_IMAGE="${IMAGE_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
MANIFESTS_FILE="manifests.yaml"
NAMESPACE="vllm-profiler"
TARGET_NAMESPACE="${TARGET_NAMESPACE:-kserve-e2e-perf}"

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

Deploy the vLLM profiler mutating admission webhook to Kubernetes.

Options:
    --skip-build        Skip container image build and push
    --skip-validation   Skip webhook validation after deployment
    --help              Show this help message

Environment Variables:
    CONTAINER_RUNTIME   Container runtime to use (default: podman)
    IMAGE_REGISTRY      Image registry (default: quay.io/mimehta)
    IMAGE_TAG           Image tag (default: latest)
    TARGET_NAMESPACE    Namespace to inject profiler into (default: downstream-llm-d)

Examples:
    # Full deployment
    $0

    # Skip building image (use existing)
    $0 --skip-build

    # Use different namespace
    TARGET_NAMESPACE=my-namespace $0

EOF
    exit 0
}

# Parse arguments
SKIP_BUILD=false
SKIP_VALIDATION=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --skip-validation)
            SKIP_VALIDATION=true
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

# Main deployment
main() {
    log_info "Starting vLLM Profiler Webhook deployment"
    log_info "Configuration:"
    echo "  - Container Runtime: ${CONTAINER_RUNTIME}"
    echo "  - Image: ${FULL_IMAGE}"
    echo "  - Webhook Namespace: ${NAMESPACE}"
    echo "  - Target Namespace: ${TARGET_NAMESPACE}"
    echo ""

    # Step 1: Build and push container image
    if [ "$SKIP_BUILD" = false ]; then
        log_info "Step 1/7: Building container image..."
        if ! ${CONTAINER_RUNTIME} build --runtime=runc -t "${IMAGE_NAME}" .; then
            log_error "Container build failed"
            exit 1
        fi
        log_success "Container image built"

        log_info "Tagging image as ${FULL_IMAGE}..."
        ${CONTAINER_RUNTIME} tag "localhost/${IMAGE_NAME}" "${FULL_IMAGE}"

        log_info "Pushing image to registry..."
        if ! ${CONTAINER_RUNTIME} push "${FULL_IMAGE}"; then
            log_error "Image push failed"
            exit 1
        fi
        log_success "Container image pushed to ${FULL_IMAGE}"
    else
        log_warn "Skipping container build (--skip-build specified)"
    fi

    # Step 2: Delete existing resources (idempotent deployment)
    log_info "Step 2/7: Removing existing resources (if any)..."
    oc delete -f "${MANIFESTS_FILE}" --ignore-not-found=true
    log_info "Waiting for resources to be deleted..."
    sleep 5
    log_success "Existing resources removed"

    # Step 3: Apply manifests
    log_info "Step 3/7: Deploying webhook resources..."
    if ! oc apply -f "${MANIFESTS_FILE}"; then
        log_error "Failed to apply manifests"
        exit 1
    fi
    log_success "Webhook resources deployed"

    # Step 4: Apply kustomization (ConfigMap)
    log_info "Step 4/7: Creating ConfigMap with profiler code..."
    if ! oc apply -k .; then
        log_error "Failed to apply kustomization"
        exit 1
    fi
    log_success "ConfigMap created in ${TARGET_NAMESPACE}"

    # Step 5: Generate TLS certificates
    log_info "Step 5/7: Generating TLS certificates..."
    if ! bash "${SCRIPT_DIR}/gen-certs.sh"; then
        log_error "Certificate generation failed"
        exit 1
    fi
    log_success "TLS certificates generated"

    # Step 6: Patch webhook with CA bundle
    log_info "Step 6/7: Patching webhook with CA bundle..."
    if ! bash "${SCRIPT_DIR}/patch-ca-bundle.sh"; then
        log_error "CA bundle patching failed"
        exit 1
    fi
    log_success "Webhook CA bundle configured"

    # Step 7: Validate deployment
    if [ "$SKIP_VALIDATION" = false ]; then
        log_info "Step 7/7: Validating webhook deployment..."
        if ! DO_SIMPLE_TEST=1 "${SCRIPT_DIR}/validate_webhook.sh"; then
            log_warn "Webhook validation reported issues (see output above)"
        else
            log_success "Webhook validation passed"
        fi
    else
        log_warn "Skipping validation (--skip-validation specified)"
    fi

    echo ""
    log_success "Deployment complete!"
    echo ""
    log_info "Next steps:"
    echo "  1. Create a pod in namespace '${TARGET_NAMESPACE}' with label:"
    echo "     llm-d.ai/inferenceServing=true"
    echo "  2. Check pod logs for profiler output after ~100-150 model executions"
    echo "  3. Retrieve trace file from pod for Chrome trace visualization"
    echo ""
    log_info "To remove all resources, run: ./scripts/teardown.sh"
}

# Run main function
main
