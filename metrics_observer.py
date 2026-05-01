#!/usr/bin/env python3
"""
Prometheus metrics observer for vLLM saturation detection.

Runs as a sidecar container alongside vLLM. Polls the /metrics endpoint,
tracks generation token throughput, and writes a signal file when throughput
plateaus (indicating the system is saturated and in steady state).

The signal file triggers profiling in sitecustomize.py.

Configuration via environment variables (see defaults below).
No external dependencies — stdlib only.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

PORT = int(os.environ.get("METRICS_OBSERVER_PORT", "8000"))
POLL_INTERVAL = float(os.environ.get("METRICS_OBSERVER_POLL_INTERVAL", "5"))
WINDOW_SIZE = int(os.environ.get("METRICS_OBSERVER_WINDOW_SIZE", "6"))
CV_THRESHOLD = float(os.environ.get("METRICS_OBSERVER_CV_THRESHOLD", "0.10"))
MIN_THROUGHPUT = float(os.environ.get("METRICS_OBSERVER_MIN_THROUGHPUT", "1.0"))
SIGNAL_FILE = os.environ.get("METRICS_OBSERVER_SIGNAL_FILE", "/tmp/profiler_start")
OUTPUT_FILE = os.environ.get("METRICS_OBSERVER_OUTPUT", "/tmp/metrics_observer.json")
WARMUP = float(os.environ.get("METRICS_OBSERVER_WARMUP", "30"))

METRICS_URL = f"http://localhost:{PORT}/metrics"

TRACKED_COUNTERS = ["vllm:generation_tokens", "vllm:prompt_tokens"]
TRACKED_GAUGES = [
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:kv_cache_usage_perc",
]


def log(msg):
    print(f"[metrics-observer] {msg}", file=sys.stderr, flush=True)


def parse_metric(text, name):
    """Extract first matching metric value from Prometheus text format."""
    for line in text.split("\n"):
        if line.startswith("#"):
            continue
        if not line.startswith(name):
            continue
        # name or name{labels} value
        parts = line.split()
        if len(parts) >= 2:
            try:
                return float(parts[-1])
            except ValueError:
                continue
    return None


def fetch_metrics():
    """Fetch and parse relevant metrics from vLLM."""
    try:
        req = urllib.request.Request(METRICS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError):
        return None

    result = {}
    for name in TRACKED_COUNTERS + TRACKED_GAUGES:
        val = parse_metric(text, name)
        if val is not None:
            result[name] = val
    return result if result else None


def detect_plateau(throughput_history):
    """Return True if throughput has plateaued (low coefficient of variation)."""
    if len(throughput_history) < WINDOW_SIZE:
        return False
    recent = throughput_history[-WINDOW_SIZE:]
    if all(t < MIN_THROUGHPUT for t in recent):
        return False
    mean = sum(recent) / len(recent)
    if mean < MIN_THROUGHPUT:
        return False
    variance = sum((t - mean) ** 2 for t in recent) / len(recent)
    cv = (variance ** 0.5) / mean
    return cv < CV_THRESHOLD


def main():
    log(f"Starting: port={PORT} poll={POLL_INTERVAL}s window={WINDOW_SIZE} "
        f"cv_thresh={CV_THRESHOLD} min_tput={MIN_THROUGHPUT} warmup={WARMUP}s")
    log(f"Signal file: {SIGNAL_FILE}")
    log(f"Metrics URL: {METRICS_URL}")

    if os.path.exists(SIGNAL_FILE):
        os.remove(SIGNAL_FILE)
        log("Removed stale signal file")

    log(f"Waiting {WARMUP}s for vLLM warmup...")
    time.sleep(WARMUP)

    samples = []
    throughput_history = []
    prev_gen_tokens = None
    prev_time = None
    signal_sent = False

    config = {
        "port": PORT,
        "poll_interval": POLL_INTERVAL,
        "window_size": WINDOW_SIZE,
        "cv_threshold": CV_THRESHOLD,
        "min_throughput": MIN_THROUGHPUT,
        "signal_file": SIGNAL_FILE,
        "warmup": WARMUP,
    }

    while True:
        metrics = fetch_metrics()
        now = time.time()

        if metrics is None:
            log("Failed to fetch metrics, retrying...")
            time.sleep(POLL_INTERVAL)
            continue

        gen_tokens = metrics.get("vllm:generation_tokens", 0)
        throughput = 0.0

        if prev_gen_tokens is not None and prev_time is not None:
            dt = now - prev_time
            if dt > 0:
                throughput = (gen_tokens - prev_gen_tokens) / dt

        prev_gen_tokens = gen_tokens
        prev_time = now

        if throughput > 0 or throughput_history:
            throughput_history.append(throughput)

        sample = {
            "timestamp": now,
            "generation_tokens": gen_tokens,
            "prompt_tokens": metrics.get("vllm:prompt_tokens", 0),
            "throughput_tok_s": round(throughput, 2),
            "num_requests_running": metrics.get("vllm:num_requests_running", 0),
            "num_requests_waiting": metrics.get("vllm:num_requests_waiting", 0),
            "kv_cache_usage_perc": metrics.get("vllm:kv_cache_usage_perc", 0),
        }
        samples.append(sample)

        running = sample["num_requests_running"]
        waiting = sample["num_requests_waiting"]
        log(f"tput={throughput:.1f} tok/s  running={running}  "
            f"waiting={waiting}  samples={len(throughput_history)}/{WINDOW_SIZE}")

        if not signal_sent and detect_plateau(throughput_history):
            log(f"Throughput plateau detected! "
                f"mean={sum(throughput_history[-WINDOW_SIZE:])/WINDOW_SIZE:.1f} tok/s")

            with open(SIGNAL_FILE, "w") as f:
                f.write(json.dumps({
                    "triggered_at": datetime.now(timezone.utc).isoformat(),
                    "throughput_tok_s": round(throughput, 2),
                    "mean_throughput": round(
                        sum(throughput_history[-WINDOW_SIZE:]) / WINDOW_SIZE, 2
                    ),
                    "samples_collected": len(samples),
                }))
            log(f"Signal file written: {SIGNAL_FILE}")
            signal_sent = True

            output = {
                "config": config,
                "signal_triggered_at": datetime.now(timezone.utc).isoformat(),
                "throughput_at_trigger": round(throughput, 2),
                "samples": samples,
            }
            try:
                with open(OUTPUT_FILE, "w") as f:
                    json.dump(output, f, indent=2)
                log(f"Metrics dump written: {OUTPUT_FILE}")
            except Exception as e:
                log(f"Failed to write output: {e}")

            log("Observer done. Exiting.")
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
