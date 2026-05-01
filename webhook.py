#!/usr/bin/env python3
import base64
import json
import os
import logging
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, request

app = Flask(__name__)

# Configuration via environment variables
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "")
# Legacy single label matching (deprecated but still supported)
TARGET_LABEL_KEY = os.getenv("TARGET_LABEL_KEY", "")
TARGET_LABEL_VALUE = os.getenv("TARGET_LABEL_VALUE", "")
# New: support multiple labels (comma-separated "key=value" pairs, ANY match triggers injection)
# Example: "llm-d.ai/inferenceServing=true,app=vllm,role=worker"
# Pod with ANY of these labels will be instrumented
TARGET_LABELS = os.getenv("TARGET_LABELS", "")
INJECT_ENV_NAME = os.getenv("INJECT_ENV_NAME", "")
INJECT_ENV_VALUE = os.getenv("INJECT_ENV_VALUE", "")
PORT = int(os.getenv("WEBHOOK_PORT", "8443"))
TLS_CERT_FILE = os.getenv("TLS_CERT_FILE", "/tls/tls.crt")
TLS_KEY_FILE = os.getenv("TLS_KEY_FILE", "/tls/tls.key")
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.DEBUG), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("env-injector")

FILES_VOLUME_NAME = "env-injector-files"
FILES_CONFIGMAP_NAME = "env-injector-files"
FILE_KEYS = [
    {"key": "sitecustomize.py", "mountPath": "/home/vllm/profiler/sitecustomize.py"},
    {"key": "profiler_config.yaml", "mountPath": "/home/vllm/profiler/profiler_config.yaml"},
    {"key": "metrics_observer.py", "mountPath": "/home/vllm/profiler/metrics_observer.py"},
]

OBSERVER_SIDECAR_IMAGE = os.getenv("OBSERVER_SIDECAR_IMAGE", "python:3.11-slim")
OBSERVER_SHARED_VOLUME_NAME = "profiler-signal"
OBSERVER_SHARED_MOUNT_PATH = "/tmp"

# Profiler configuration annotation prefix
PROFILER_ANNOTATION_PREFIX = "vllm.profiler/"

def parse_target_labels(labels_str: str) -> List[Tuple[str, str]]:
    """
    Parse TARGET_LABELS environment variable into list of (key, value) tuples.

    Format: "key1=value1,key2=value2,..."
    Example: "llm-d.ai/inferenceServing=true,app=vllm"
    Returns: [("llm-d.ai/inferenceServing", "true"), ("app", "vllm")]
    """
    if not labels_str:
        return []

    labels = []
    for pair in labels_str.split(','):
        pair = pair.strip()
        if '=' in pair:
            key, value = pair.split('=', 1)  # Split on first '=' only
            labels.append((key.strip(), value.strip()))
        else:
            logger.warning(f"Invalid label pair (missing '='): '{pair}'")
    return labels

def matches_any_label(pod_labels: Dict[str, str], target_labels: List[Tuple[str, str]]) -> bool:
    """
    Check if pod has ANY of the target labels (OR logic).

    Returns True if any (key, value) pair in target_labels matches pod_labels.
    """
    for key, value in target_labels:
        if pod_labels.get(key) == value:
            logger.debug(f"Pod matched on label '{key}'='{value}'")
            return True
    return False

def extract_profiler_env_from_annotations(annotations: Dict[str, str]) -> List[Dict[str, str]]:
    """
    Extract profiler configuration from pod annotations and convert to environment variables.

    Supported annotations:
        vllm.profiler/ranges: "50-100,200-300"
        vllm.profiler/activities: "CPU,CUDA"
        vllm.profiler/record-shapes: "true"
        vllm.profiler/with-stack: "true"
        vllm.profiler/memory: "true"
        vllm.profiler/output: "trace_custom.json"
        vllm.profiler/debug: "true"

    Returns list of {"name": "ENV_VAR", "value": "value"} dicts.
    """
    env_vars = []

    # Mapping from annotation suffix to environment variable name
    annotation_to_env = {
        "ranges": "VLLM_PROFILER_RANGES",
        "activities": "VLLM_PROFILER_ACTIVITIES",
        "record-shapes": "VLLM_PROFILER_RECORD_SHAPES",
        "with-stack": "VLLM_PROFILER_WITH_STACK",
        "memory": "VLLM_PROFILER_MEMORY",
        "output": "VLLM_PROFILER_OUTPUT",
        "export-trace": "VLLM_PROFILER_EXPORT_TRACE",
        "debug": "VLLM_PROFILER_DEBUG",
        "nixl-handshake": "VLLM_PROFILER_NIXL_HANDSHAKE",
        "nixl-handshake-output": "VLLM_PROFILER_NIXL_HANDSHAKE_OUTPUT",
        "signal-mode": "VLLM_PROFILER_SIGNAL_MODE",
        "signal-file": "VLLM_PROFILER_SIGNAL_FILE",
        "duration": "VLLM_PROFILER_DURATION",
    }

    for annotation_suffix, env_name in annotation_to_env.items():
        annotation_key = f"{PROFILER_ANNOTATION_PREFIX}{annotation_suffix}"
        if annotation_key in annotations:
            env_vars.append({
                "name": env_name,
                "value": annotations[annotation_key]
            })
            logger.debug(f"Found profiler annotation '{annotation_key}' -> {env_name}='{annotations[annotation_key]}'")

    return env_vars

def build_env_patch_for_pod(pod: Dict[str, Any], env_vars: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """
    Build JSON patch operations to inject environment variables into all containers.

    Args:
        pod: The pod object to patch
        env_vars: List of {"name": "ENV_NAME", "value": "value"} dicts

    Returns:
        List of JSON patch operations
    """
    patch: List[Dict[str, Any]] = []
    containers = pod.get("spec", {}).get("containers", [])

    logger.debug("Building env patch for %d container(s) with %d env var(s)", len(containers), len(env_vars))

    for container_index, container in enumerate(containers):
        env_list = container.get("env", [])
        cname = container.get("name", f"idx-{container_index}")
        logger.debug("Inspecting container '%s' (index=%d); current env count=%d", cname, container_index, len(env_list))

        # Build list of env vars to add
        env_to_add = []

        for env_var in env_vars:
            env_name = env_var["name"]
            env_value = env_var["value"]

            # Check if env var already exists
            existing_index = -1
            for i, item in enumerate(env_list):
                if item.get("name") == env_name:
                    existing_index = i
                    break

            if existing_index >= 0:
                # Replace existing env var
                logger.debug("Container '%s': replacing existing env '%s' at index %d", cname, env_name, existing_index)
                patch.append({
                    "op": "replace",
                    "path": f"/spec/containers/{container_index}/env/{existing_index}/value",
                    "value": env_value,
                })
            else:
                # Queue for addition
                env_to_add.append({"name": env_name, "value": env_value})

        # Add all new env vars in one operation
        if env_to_add:
            if env_list:
                # Container already has env list, append each var
                for env_var in env_to_add:
                    logger.debug("Container '%s': appending env '%s'", cname, env_var["name"])
                    patch.append({
                        "op": "add",
                        "path": f"/spec/containers/{container_index}/env/-",
                        "value": env_var,
                    })
            else:
                # Container has no env list, create it with all vars
                logger.debug("Container '%s': creating env list with %d var(s)", cname, len(env_to_add))
                patch.append({
                    "op": "add",
                    "path": f"/spec/containers/{container_index}/env",
                    "value": env_to_add,
                })

    logger.debug("Patch operations prepared: %s", json.dumps(patch))
    return patch

def build_files_volume_patch_for_pod(pod: Dict[str, Any]) -> List[Dict[str, Any]]:
    patch: List[Dict[str, Any]] = []
    spec = pod.get("spec", {}) or {}
    volumes = spec.get("volumes", [])
    containers = spec.get("containers", [])

    # Add the files volume if missing
    volume_present = any(v.get("name") == FILES_VOLUME_NAME for v in volumes)
    if not volume_present:
        logger.debug("Adding volume '%s' from ConfigMap '%s'", FILES_VOLUME_NAME, FILES_CONFIGMAP_NAME)
        if volumes:
            patch.append({
                "op": "add",
                "path": "/spec/volumes/-",
                "value": {
                    "name": FILES_VOLUME_NAME,
                    "configMap": {
                        "name": FILES_CONFIGMAP_NAME
                    }
                }
            })
        else:
            patch.append({
                "op": "add",
                "path": "/spec/volumes",
                "value": [{
                    "name": FILES_VOLUME_NAME,
                    "configMap": {
                        "name": FILES_CONFIGMAP_NAME
                    }
                }]
            })
    else:
        logger.debug("Volume '%s' already present; skipping add", FILES_VOLUME_NAME)

    # For each container, add volumeMounts for each file using subPath
    for idx, c in enumerate(containers):
        mounts = c.get("volumeMounts", [])
        existing_mount_paths = {m.get("mountPath") for m in mounts}
        add_list = []
        for f in FILE_KEYS:
            if f["mountPath"] in existing_mount_paths:
                logger.debug("Container %s already has mountPath %s", c.get("name", idx), f["mountPath"])
                continue
            add_list.append({
                "name": FILES_VOLUME_NAME,
                "mountPath": f["mountPath"],
                "subPath": f["key"],
                "readOnly": True,
            })
        if add_list:
            if mounts:
                for m in add_list:
                    logger.debug("Adding volumeMount to container %s: %s", c.get("name", idx), m)
                    patch.append({
                        "op": "add",
                        "path": f"/spec/containers/{idx}/volumeMounts/-",
                        "value": m
                    })
            else:
                logger.debug("Creating volumeMounts for container %s with %d mount(s)", c.get("name", idx), len(add_list))
                patch.append({
                    "op": "add",
                    "path": f"/spec/containers/{idx}/volumeMounts",
                    "value": add_list
                })

    logger.debug("Files volume/mount patch prepared: %s", json.dumps(patch))
    return patch


def extract_observer_env_from_annotations(annotations: Dict[str, str]) -> List[Dict[str, str]]:
    """Extract observer-specific env vars from pod annotations."""
    observer_annotation_to_env = {
        "observer-poll-interval": "METRICS_OBSERVER_POLL_INTERVAL",
        "observer-window-size": "METRICS_OBSERVER_WINDOW_SIZE",
        "observer-cv-threshold": "METRICS_OBSERVER_CV_THRESHOLD",
        "observer-min-throughput": "METRICS_OBSERVER_MIN_THROUGHPUT",
        "observer-warmup": "METRICS_OBSERVER_WARMUP",
        "observer-port": "METRICS_OBSERVER_PORT",
    }
    env_vars = []
    for suffix, env_name in observer_annotation_to_env.items():
        key = f"{PROFILER_ANNOTATION_PREFIX}{suffix}"
        if key in annotations:
            env_vars.append({"name": env_name, "value": annotations[key]})
    return env_vars


def detect_vllm_port(pod: Dict[str, Any]) -> str:
    """Auto-detect vLLM metrics port from pod spec."""
    for c in pod.get("spec", {}).get("containers", []):
        if c.get("name") == "vllm":
            for p in c.get("ports", []):
                if p.get("name") == "vllm":
                    return str(p.get("containerPort", 8000))
    return "8000"


def build_observer_sidecar_patch(pod: Dict[str, Any], annotations: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Build JSON patch to inject metrics-observer sidecar container and shared /tmp volume.
    """
    patch: List[Dict[str, Any]] = []
    spec = pod.get("spec", {}) or {}
    containers = spec.get("containers", [])
    volumes = spec.get("volumes", [])

    # Check if observer already injected
    if any(c.get("name") == "metrics-observer" for c in containers):
        logger.debug("metrics-observer sidecar already present; skipping")
        return patch

    # Shared /tmp volume
    shared_vol_present = any(v.get("name") == OBSERVER_SHARED_VOLUME_NAME for v in volumes)
    if not shared_vol_present:
        vol_entry = {"name": OBSERVER_SHARED_VOLUME_NAME, "emptyDir": {}}
        if volumes:
            patch.append({"op": "add", "path": "/spec/volumes/-", "value": vol_entry})
        else:
            patch.append({"op": "add", "path": "/spec/volumes", "value": [vol_entry]})

    # Mount shared /tmp into existing vllm container(s)
    for idx, c in enumerate(containers):
        if c.get("name") != "vllm":
            continue
        mounts = c.get("volumeMounts", [])
        already_mounted = any(
            m.get("name") == OBSERVER_SHARED_VOLUME_NAME for m in mounts
        )
        if not already_mounted:
            mount = {
                "name": OBSERVER_SHARED_VOLUME_NAME,
                "mountPath": OBSERVER_SHARED_MOUNT_PATH,
            }
            if mounts:
                patch.append({"op": "add", "path": f"/spec/containers/{idx}/volumeMounts/-", "value": mount})
            else:
                patch.append({"op": "add", "path": f"/spec/containers/{idx}/volumeMounts", "value": [mount]})

    # Build observer env vars
    observer_env = extract_observer_env_from_annotations(annotations)
    vllm_port = detect_vllm_port(pod)
    # Auto-set port if not overridden by annotation
    if not any(e["name"] == "METRICS_OBSERVER_PORT" for e in observer_env):
        observer_env.append({"name": "METRICS_OBSERVER_PORT", "value": vllm_port})

    # Build sidecar container
    sidecar = {
        "name": "metrics-observer",
        "image": OBSERVER_SIDECAR_IMAGE,
        "command": ["python", "/home/vllm/profiler/metrics_observer.py"],
        "env": observer_env,
        "volumeMounts": [
            {
                "name": OBSERVER_SHARED_VOLUME_NAME,
                "mountPath": OBSERVER_SHARED_MOUNT_PATH,
            },
            {
                "name": FILES_VOLUME_NAME,
                "mountPath": "/home/vllm/profiler/metrics_observer.py",
                "subPath": "metrics_observer.py",
                "readOnly": True,
            },
        ],
        "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"},
            "limits": {"cpu": "200m", "memory": "128Mi"},
        },
    }

    patch.append({"op": "add", "path": "/spec/containers/-", "value": sidecar})
    logger.debug("Observer sidecar patch prepared with port=%s", vllm_port)
    return patch


@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200


@app.route("/mutate", methods=["POST"])
def mutate():
    review = request.get_json(force=True, silent=True) or {}
    req = review.get("request", {})
    uid = req.get("uid")

    logger.debug("AdmissionReview received: uid=%s kind=%s.%s op=%s",
                 uid,
                 req.get("kind", {}).get("group", ""),
                 req.get("kind", {}).get("kind", ""),
                 req.get("operation", ""))

    # Default AdmissionReview response: allow without changes
    response_body: Dict[str, Any] = {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid,
            "allowed": True,
        },
    }

    if req.get("kind", {}).get("kind") != "Pod":
        logger.debug("Skipping non-Pod kind")
        return jsonify(response_body)

    namespace = req.get("namespace", "")
    obj = req.get("object", {}) or {}
    metadata = obj.get("metadata", {}) or {}
    labels = metadata.get("labels", {}) or {}
    annotations = metadata.get("annotations", {}) or {}
    name = metadata.get("name", "")

    logger.debug("Pod admission: ns=%s name=%s labels=%s", namespace, name, labels)

    # Check namespace
    if not TARGET_NAMESPACE:
        logger.debug("TARGET_NAMESPACE not configured; allowing without patch")
        return jsonify(response_body)
    if namespace != TARGET_NAMESPACE:
        logger.debug("Namespace mismatch: got '%s' expected '%s'; allowing without patch", namespace, TARGET_NAMESPACE)
        return jsonify(response_body)

    # Check labels (support both new and legacy modes)
    label_match = False

    if TARGET_LABELS:
        # New mode: multiple labels with OR logic
        target_label_pairs = parse_target_labels(TARGET_LABELS)
        logger.debug(f"Using multi-label matching (OR logic): {target_label_pairs}")
        if target_label_pairs:
            label_match = matches_any_label(labels, target_label_pairs)
            if not label_match:
                logger.debug(f"Pod has none of the target labels {target_label_pairs}; allowing without patch")
                return jsonify(response_body)
        else:
            logger.debug("TARGET_LABELS set but empty/invalid; allowing without patch")
            return jsonify(response_body)
    elif TARGET_LABEL_KEY and TARGET_LABEL_VALUE:
        # Legacy mode: single label matching
        logger.debug(f"Using legacy single-label matching: {TARGET_LABEL_KEY}={TARGET_LABEL_VALUE}")
        if labels.get(TARGET_LABEL_KEY) != TARGET_LABEL_VALUE:
            logger.debug("Label mismatch: pod label '%s'='%s' expected value '%s'; allowing without patch",
                         TARGET_LABEL_KEY, labels.get(TARGET_LABEL_KEY), TARGET_LABEL_VALUE)
            return jsonify(response_body)
        label_match = True
    else:
        logger.debug("No label selector configured (neither TARGET_LABELS nor TARGET_LABEL_KEY/VALUE); allowing without patch")
        return jsonify(response_body)

    if not INJECT_ENV_NAME:
        logger.debug("INJECT_ENV_NAME not configured; allowing without patch")
        return jsonify(response_body)

    # If we get here, namespace and labels matched
    logger.debug(f"Pod matched! Proceeding with injection.")

    # Collect all environment variables to inject
    env_vars_to_inject = [{"name": INJECT_ENV_NAME, "value": INJECT_ENV_VALUE}]

    # Extract and add profiler-specific configuration from annotations
    profiler_env_vars = extract_profiler_env_from_annotations(annotations)
    if profiler_env_vars:
        logger.debug(f"Adding {len(profiler_env_vars)} profiler environment variable(s) from annotations")
        env_vars_to_inject.extend(profiler_env_vars)

    # Build environment variable patches
    patch_ops = build_env_patch_for_pod(obj, env_vars_to_inject)

    # Mount configuration files
    patch_ops.extend(build_files_volume_patch_for_pod(obj))

    # Inject observer sidecar if annotation present
    observer_annotation = f"{PROFILER_ANNOTATION_PREFIX}observer"
    if annotations.get(observer_annotation, "").lower() in ("true", "1", "yes"):
        logger.debug("Observer sidecar requested via annotation")
        patch_ops.extend(build_observer_sidecar_patch(obj, annotations))

    if patch_ops:
        logger.debug("Emitting JSONPatch with %d operation(s)", len(patch_ops))
        patch_str = json.dumps(patch_ops)
        response_body["response"]["patchType"] = "JSONPatch"
        response_body["response"]["patch"] = base64.b64encode(patch_str.encode("utf-8")).decode("utf-8")
    else:
        logger.debug("No patch operations generated; allowing without patch")

    return jsonify(response_body)


if __name__ == "__main__":
    # Basic validation on startup
    if not os.path.exists(TLS_CERT_FILE) or not os.path.exists(TLS_KEY_FILE):
        raise SystemExit(f"TLS cert/key not found at {TLS_CERT_FILE} / {TLS_KEY_FILE}")

    logger.info("Starting env-injector webhook on port %d", PORT)
    logger.info("Target namespace: %s", TARGET_NAMESPACE)

    if TARGET_LABELS:
        logger.info("Label selector mode: MULTI (OR logic)")
        logger.info("Target labels (any match): %s", TARGET_LABELS)
    elif TARGET_LABEL_KEY and TARGET_LABEL_VALUE:
        logger.info("Label selector mode: SINGLE (legacy)")
        logger.info("Target label: %s=%s", TARGET_LABEL_KEY, TARGET_LABEL_VALUE)
    else:
        logger.warning("No label selector configured!")

    logger.info("Injection: %s=%s", INJECT_ENV_NAME, INJECT_ENV_VALUE)
    logger.info("TLS cert=%s key=%s", TLS_CERT_FILE, TLS_KEY_FILE)
    logger.info("Log level: %s", LOG_LEVEL)

    app.run(
        host="0.0.0.0",
        port=PORT,
        ssl_context=(TLS_CERT_FILE, TLS_KEY_FILE),
        debug=False,
    )


