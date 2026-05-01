"""
sitecustomize.py - Auto-loaded PyTorch profiler for vLLM workers

This module is automatically loaded by Python when it starts (via PYTHONPATH).
It installs an import hook that intercepts vllm.v1.worker.gpu_worker module
loading and wraps Worker.execute_model with torch.profiler instrumentation.

The profiler records CPU+CUDA activity for configured call ranges, then exports
Chrome trace JSON files for visualization.

Configuration sources (in priority order):
1. Environment variables (e.g., VLLM_PROFILER_RANGES="50-100,200-300")
2. profiler_config.yaml file (if present)
3. Hardcoded defaults
"""
import sys
import os
import importlib
import importlib.util
import importlib.abc
from typing import List, Tuple, Optional

# ==============================================================================
# vLLM Configuration
# ==============================================================================

# Set vLLM RPC timeout (in milliseconds)
os.environ.setdefault('VLLM_RPC_TIMEOUT', '1800000')

# ==============================================================================
# Configuration Management
# ==============================================================================

class ProfilerConfig:
    """Manages profiler configuration from multiple sources."""

    def __init__(self):
        self.ranges: List[Tuple[int, int]] = []
        self.activities: List[str] = ["CPU", "CUDA"]
        self.record_shapes: bool = True
        self.with_stack: bool = True
        self.profile_memory: bool = False
        self.with_modules: bool = False
        self.export_chrome_trace: bool = True
        self.output_file_pattern: str = "/tmp/trace_pid{pid}_range{start}-{end}.json"
        self.table_enabled: bool = True
        self.table_sort_by: str = "cuda_time_total"
        self.table_row_limit: int = 50
        self.print_stats: bool = True
        self.target_module: str = "vllm.v1.worker.gpu_worker"
        self.target_class: str = "Worker"
        self.target_method: str = "execute_model"
        self.debug: bool = False
        self.signal_mode: bool = False
        self.signal_file: str = "/tmp/profiler_start"
        self.profile_duration: int = 50

        self._load_config()

    def _load_config(self):
        """Load configuration from environment variables and config file."""
        # First, try to load from YAML file
        self._load_from_yaml()

        # Then override with environment variables (highest priority)
        self._load_from_env()

        # Validate and parse ranges
        if not self.ranges:
            # Default range if none specified
            self.ranges = [(100, 150)]

        if self.debug:
            print(f"[profiler-config] Loaded configuration:")
            print(f"  Ranges: {self.ranges}")
            print(f"  Activities: {self.activities}")
            print(f"  Output: {self.output_file_pattern}")

    def _load_from_yaml(self):
        """Load configuration from profiler_config.yaml if present."""
        config_path = os.path.join(
            os.path.dirname(__file__) or "/home/vllm/profiler",
            "profiler_config.yaml"
        )

        if not os.path.exists(config_path):
            return

        try:
            import yaml
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            if config:
                # Parse profiling ranges
                if 'profiling_ranges' in config:
                    self.ranges = self._parse_ranges(config['profiling_ranges'])

                # Activities
                if 'activities' in config:
                    self.activities = [a.strip() for a in config['activities'].split(',')]

                # Options
                opts = config.get('options', {})
                self.record_shapes = opts.get('record_shapes', self.record_shapes)
                self.with_stack = opts.get('with_stack', self.with_stack)
                self.profile_memory = opts.get('profile_memory', self.profile_memory)
                self.with_modules = opts.get('with_modules', self.with_modules)

                # Output configuration
                output = config.get('output', {})
                self.export_chrome_trace = output.get('export_chrome_trace', self.export_chrome_trace)
                self.output_file_pattern = output.get('file_pattern', self.output_file_pattern)
                self.print_stats = output.get('print_stats', self.print_stats)

                table = output.get('table', {})
                self.table_enabled = table.get('enabled', self.table_enabled)
                self.table_sort_by = table.get('sort_by', self.table_sort_by)
                self.table_row_limit = table.get('row_limit', self.table_row_limit)

                # Advanced settings
                adv = config.get('advanced', {})
                self.target_module = adv.get('target_module', self.target_module)
                self.target_class = adv.get('target_class', self.target_class)
                self.target_method = adv.get('target_method', self.target_method)
                self.debug = adv.get('debug', self.debug)

        except ImportError:
            # PyYAML not available, skip file-based config
            pass
        except Exception as e:
            print(f"[profiler-config] Warning: Failed to load config file: {e}")

    def _load_from_env(self):
        """Load configuration from environment variables."""
        # Profiling ranges
        if 'VLLM_PROFILER_RANGES' in os.environ:
            self.ranges = self._parse_ranges(os.environ['VLLM_PROFILER_RANGES'])

        # Activities
        if 'VLLM_PROFILER_ACTIVITIES' in os.environ:
            self.activities = [a.strip() for a in os.environ['VLLM_PROFILER_ACTIVITIES'].split(',')]

        # Options
        if 'VLLM_PROFILER_RECORD_SHAPES' in os.environ:
            self.record_shapes = os.environ['VLLM_PROFILER_RECORD_SHAPES'].lower() in ('true', '1', 'yes')

        if 'VLLM_PROFILER_WITH_STACK' in os.environ:
            self.with_stack = os.environ['VLLM_PROFILER_WITH_STACK'].lower() in ('true', '1', 'yes')

        if 'VLLM_PROFILER_MEMORY' in os.environ:
            self.profile_memory = os.environ['VLLM_PROFILER_MEMORY'].lower() in ('true', '1', 'yes')

        # Output file pattern
        if 'VLLM_PROFILER_OUTPUT' in os.environ:
            self.output_file_pattern = os.environ['VLLM_PROFILER_OUTPUT']

        # Chrome trace export
        if 'VLLM_PROFILER_EXPORT_TRACE' in os.environ:
            self.export_chrome_trace = os.environ['VLLM_PROFILER_EXPORT_TRACE'].lower() in ('true', '1', 'yes')

        # Debug mode
        if 'VLLM_PROFILER_DEBUG' in os.environ:
            self.debug = os.environ['VLLM_PROFILER_DEBUG'].lower() in ('true', '1', 'yes')

        # Signal mode
        if 'VLLM_PROFILER_SIGNAL_MODE' in os.environ:
            self.signal_mode = os.environ['VLLM_PROFILER_SIGNAL_MODE'].lower() in ('true', '1', 'yes')
        if 'VLLM_PROFILER_SIGNAL_FILE' in os.environ:
            self.signal_file = os.environ['VLLM_PROFILER_SIGNAL_FILE']
        if 'VLLM_PROFILER_DURATION' in os.environ:
            self.profile_duration = int(os.environ['VLLM_PROFILER_DURATION'])

    def _parse_ranges(self, ranges_str: str) -> List[Tuple[int, int]]:
        """
        Parse profiling ranges from string format.

        Examples:
            "100-150" -> [(100, 150)]
            "50-100,200-300" -> [(50, 100), (200, 300)]
            "0-50,100-150,300-350" -> [(0, 50), (100, 150), (300, 350)]
        """
        ranges = []
        for range_str in ranges_str.split(','):
            range_str = range_str.strip()
            if '-' in range_str:
                try:
                    start, end = range_str.split('-')
                    ranges.append((int(start), int(end)))
                except ValueError as e:
                    print(f"[profiler-config] Warning: Invalid range '{range_str}': {e}")
        return ranges

    def get_output_filename(self, pid: Optional[int] = None, rank: Optional[int] = None,
                           range_start: Optional[int] = None, range_end: Optional[int] = None) -> str:
        """Generate output filename with substitutions."""
        filename = self.output_file_pattern
        filename = filename.replace('{pid}', str(pid or os.getpid()))
        if rank is not None:
            filename = filename.replace('{rank}', str(rank))
        if range_start is not None:
            filename = filename.replace('{start}', str(range_start))
        if range_end is not None:
            filename = filename.replace('{end}', str(range_end))
        return filename


# Global configuration instance
_config = ProfilerConfig()


# ==============================================================================
# NIXL Handshake Recorder
# ==============================================================================

_NIXL_CONNECTOR_MODULE = "vllm.distributed.kv_transfer.kv_connector.v1.nixl_connector"

class NixlHandshakeRecorder:
    """Records per-call timing of NixlConnector._nixl_handshake."""

    def __init__(self):
        self.records = []
        self.output_path = os.environ.get(
            'VLLM_PROFILER_NIXL_HANDSHAKE_OUTPUT',
            '/tmp/nixl_handshake_timings.json'
        )
        self.enabled = os.environ.get(
            'VLLM_PROFILER_NIXL_HANDSHAKE', ''
        ).lower() in ('true', '1', 'yes')

    def wrap(self, original_func):
        import functools
        import time
        recorder = self

        @functools.wraps(original_func)
        def wrapped(self_connector, host, port, remote_tp_size, expected_engine_id):
            start = time.monotonic()
            error_msg = None
            result = None
            try:
                result = original_func(
                    self_connector, host, port,
                    remote_tp_size, expected_engine_id
                )
                return result
            except Exception as e:
                error_msg = str(e)
                raise
            finally:
                elapsed = time.monotonic() - start
                record = {
                    "timestamp": time.time(),
                    "pid": os.getpid(),
                    "host": host,
                    "port": port,
                    "remote_tp_size": remote_tp_size,
                    "expected_engine_id": expected_engine_id,
                    "duration_s": round(elapsed, 6),
                    "success": error_msg is None,
                    "error": error_msg,
                    "remote_agents": (
                        list(result.values()) if result else None
                    ),
                }
                recorder.records.append(record)
                recorder._flush()
                print(
                    f"[nixl-handshake] {host}:{port} "
                    f"tp={remote_tp_size} "
                    f"engine={expected_engine_id[:12]}... "
                    f"dur={elapsed:.3f}s "
                    f"{'OK' if error_msg is None else 'FAIL'}",
                    file=sys.stderr
                )

        return wrapped

    def _flush(self):
        import json
        try:
            with open(self.output_path, 'w') as f:
                json.dump(self.records, f, indent=2)
        except Exception as e:
            print(f"[nixl-handshake] flush error: {e}", file=sys.stderr)


_nixl_recorder = NixlHandshakeRecorder()


# ==============================================================================
# Import Hooks
# ==============================================================================

class PostImportLoader(importlib.abc.Loader):
    def __init__(self, loader):
        self.loader = loader

    def create_module(self, spec):
        if hasattr(self.loader, "create_module"):
            return self.loader.create_module(spec)
        return None

    def exec_module(self, module):
        self.loader.exec_module(module)
        if _config.debug:
            print(f"[profiler] {module.__name__} loaded")
        safe_wrap_function(module)
        if _config.debug:
            print(f"[profiler] {module.__name__} wrapped")


class PostImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != _config.target_module:
            return None

        # Prevent recursive lookup
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)

        if spec and spec.loader:
            spec.loader = PostImportLoader(spec.loader)
            return spec
        return None


class NixlPostImportLoader(importlib.abc.Loader):
    def __init__(self, loader, recorder):
        self.loader = loader
        self.recorder = recorder

    def create_module(self, spec):
        if hasattr(self.loader, "create_module"):
            return self.loader.create_module(spec)
        return None

    def exec_module(self, module):
        self.loader.exec_module(module)
        nixl_cls = getattr(module, "NixlConnector", None)
        if nixl_cls is None:
            print(f"[nixl-handshake] NixlConnector not found in {module.__name__}", file=sys.stderr)
            return
        original = getattr(nixl_cls, "_nixl_handshake", None)
        if original is None:
            print(f"[nixl-handshake] _nixl_handshake not found on NixlConnector", file=sys.stderr)
            return
        setattr(nixl_cls, "_nixl_handshake", self.recorder.wrap(original))
        print(f"[nixl-handshake] Wrapped NixlConnector._nixl_handshake", file=sys.stderr)


class NixlPostImportFinder(importlib.abc.MetaPathFinder):
    def __init__(self, recorder):
        self.recorder = recorder

    def find_spec(self, fullname, path, target=None):
        if fullname != _NIXL_CONNECTOR_MODULE:
            return None

        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)

        if spec and spec.loader:
            spec.loader = NixlPostImportLoader(spec.loader, self.recorder)
            return spec
        return None


# Install import hooks
sys.meta_path.insert(0, PostImportFinder())
if _nixl_recorder.enabled:
    sys.meta_path.insert(0, NixlPostImportFinder(_nixl_recorder))


# ==============================================================================
# Profiler Wrapper
# ==============================================================================

def _make_profiler(activities):
    """Create a new torch.profiler.profile instance."""
    from torch.profiler import profile
    return profile(
        activities=activities,
        record_shapes=_config.record_shapes,
        with_stack=_config.with_stack,
        profile_memory=_config.profile_memory,
        with_modules=_config.with_modules
    )


def _stop_and_export(prof, start, end):
    """Stop profiler, print stats, export trace."""
    prof.stop()

    if _config.print_stats:
        print("===== begin profiler output")
        if _config.table_enabled:
            print(prof.key_averages().table(
                sort_by=_config.table_sort_by,
                row_limit=_config.table_row_limit
            ))
        print("===== end profiler output")

    if _config.export_chrome_trace:
        output_file = _config.get_output_filename(range_start=start, range_end=end)
        prof.export_chrome_trace(output_file)
        print(f"[profiler] Exported trace to: {output_file}")
    else:
        print(f"[profiler] Chrome trace export disabled (export_chrome_trace=false)")


def wrap_func_with_profiler(original_func):
    """
    Wraps a function with PyTorch profiler.

    Two modes:
    - Range mode (default): profiles fixed call ranges (e.g., 100-150)
    - Signal mode: waits for signal file, then profiles for N calls
    """
    import functools
    from torch.profiler import ProfilerActivity

    activities = []
    for activity in _config.activities:
        if activity.upper() == "CPU":
            activities.append(ProfilerActivity.CPU)
        elif activity.upper() == "CUDA":
            activities.append(ProfilerActivity.CUDA)

    prof = _make_profiler(activities)
    count = 0
    current_range_idx = 0
    profiling_active = False
    signal_consumed = False
    signal_start = 0
    signal_end = 0

    @functools.wraps(original_func)
    def wrapped_func(*args, **kwargs):
        nonlocal count, current_range_idx, profiling_active, prof
        nonlocal signal_consumed, signal_start, signal_end

        count += 1

        if _config.signal_mode:
            # Signal mode: wait for signal file to start profiling
            if not profiling_active and not signal_consumed:
                if os.path.exists(_config.signal_file):
                    signal_start = count
                    signal_end = count + _config.profile_duration
                    print(f"[profiler] Signal received! Starting profiler for {_config.profile_duration} calls "
                          f"(calls {signal_start}-{signal_end}, call #{count})")
                    prof.start()
                    profiling_active = True

            if profiling_active and count >= signal_end:
                print(f"[profiler] Stopping profiler (call #{count}, range {signal_start}-{signal_end})")
                _stop_and_export(prof, signal_start, signal_end)
                profiling_active = False
                signal_consumed = True
                try:
                    os.remove(_config.signal_file)
                except OSError:
                    pass
        else:
            # Range mode: original behavior
            if not profiling_active and current_range_idx < len(_config.ranges):
                start, end = _config.ranges[current_range_idx]
                if count == start:
                    print(f"[profiler] Starting profiler for range {start}-{end} (call #{count})")
                    prof.start()
                    profiling_active = True

            if profiling_active:
                start, end = _config.ranges[current_range_idx]
                if count == end:
                    print(f"[profiler] Stopping profiler for range {start}-{end} (call #{count})")
                    _stop_and_export(prof, start, end)
                    profiling_active = False
                    current_range_idx += 1

                    if current_range_idx < len(_config.ranges):
                        prof = _make_profiler(activities)

        result = original_func(*args, **kwargs)
        return result

    return wrapped_func


# ==============================================================================
# Helper Functions
# ==============================================================================

def safe_wrap_function(module=None):
    """Safely wrap the target function with error handling."""
    try:
        mod = module or sys.modules.get(_config.target_module)
        if mod is None:
            return
        wrap_function(mod)
    except Exception as e:
        print(f"[profiler] Error wrapping function: {e}")
        if _config.debug:
            import traceback
            traceback.print_exc()


def wrap_function(mod):
    """Wrap the target method with profiler."""
    target_class = getattr(mod, _config.target_class, None)
    if target_class is None:
        print(f"[profiler] Warning: Class '{_config.target_class}' not found in {mod.__name__}")
        return

    original_method = getattr(target_class, _config.target_method, None)
    if original_method is None:
        print(f"[profiler] Warning: Method '{_config.target_method}' not found in {_config.target_class}")
        return

    if _config.debug:
        print(f"[profiler] Wrapping {_config.target_class}.{_config.target_method}")

    setattr(target_class, _config.target_method, wrap_func_with_profiler(original_method))


def unwrap_function():
    """Remove profiler wrapping (for debugging)."""
    import vllm.v1.worker.gpu_worker
    vllm.v1.worker.gpu_worker.Worker.execute_model = \
        vllm.v1.worker.gpu_worker.Worker.execute_model.__wrapped__


# ==============================================================================
# Startup
# ==============================================================================

if _config.signal_mode:
    print(f"[profiler] vLLM profiler installed - signal mode: waiting for {_config.signal_file} (duration={_config.profile_duration} calls)", file=sys.stderr)
else:
    print(f"[profiler] vLLM profiler installed - will profile ranges: {_config.ranges}", file=sys.stderr)
print(f"[profiler] Target: {_config.target_module}.{_config.target_class}.{_config.target_method}", file=sys.stderr)
if _nixl_recorder.enabled:
    print(f"[profiler] NIXL handshake recorder enabled → {_nixl_recorder.output_path}", file=sys.stderr)
