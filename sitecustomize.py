"""
sitecustomize.py - Auto-loaded PyTorch profiler for vLLM workers.

Install by putting /tmp/vllm-profiler on PYTHONPATH before starting vLLM
(including Ray worker processes).

Ray backend hot path (compiled DAG):
  RayWorkerWrapper.execute_model_ray -> GPUModelRunner.execute_model

Multiprocess backend hot path:
  Worker.execute_model -> GPUModelRunner.execute_model

Default config profiles GPUModelRunner.execute_model only (works with Ray).
"""
from __future__ import annotations

import functools
import importlib
import importlib.abc
import importlib.util
import os
import sys
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

os.environ.setdefault("VLLM_RPC_TIMEOUT", "1800000")


@dataclass(frozen=True)
class WrapTarget:
    module: str
    class_name: str
    method: str

    def label(self) -> str:
        return f"{self.module}.{self.class_name}.{self.method}"


class ProfilerConfig:
    """Profiler configuration from profiler_config.yaml and env vars."""

    def __init__(self) -> None:
        self.ranges: List[Tuple[int, int]] = []
        self.activities: List[str] = ["CPU", "CUDA"]
        self.record_shapes: bool = True
        self.with_stack: bool = True
        self.profile_memory: bool = False
        self.with_modules: bool = False
        self.export_chrome_trace: bool = True
        self.output_file_pattern: str = (
            "/tmp/vllm_profile/trace_rank{rank}_pid{pid}_range{start}-{end}.json"
        )
        self.table_enabled: bool = True
        self.table_sort_by: str = "self_cuda_time_total"
        self.table_row_limit: int = 50
        self.print_stats: bool = True
        self.targets: List[WrapTarget] = [
            WrapTarget(
                "vllm.v1.worker.gpu_model_runner",
                "GPUModelRunner",
                "execute_model",
            )
        ]
        self.profile_rank: Optional[int] = None
        self.debug: bool = False

        self._load_config()

        if not self.ranges:
            self.ranges = [(100, 150)]

    def _config_path(self) -> str:
        return os.path.join(os.path.dirname(__file__) or "/tmp/vllm-profiler", "profiler_config.yaml")

    def _load_config(self) -> None:
        self._load_from_yaml()
        self._load_from_env()

    def _load_from_yaml(self) -> None:
        config_path = self._config_path()
        if not os.path.exists(config_path):
            return
        try:
            import yaml

            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            if "profiling_ranges" in config:
                self.ranges = self._parse_ranges(str(config["profiling_ranges"]))

            if "activities" in config:
                self.activities = [
                    a.strip() for a in str(config["activities"]).split(",") if a.strip()
                ]

            opts = config.get("options", {})
            self.record_shapes = opts.get("record_shapes", self.record_shapes)
            self.with_stack = opts.get("with_stack", self.with_stack)
            self.profile_memory = opts.get("profile_memory", self.profile_memory)
            self.with_modules = opts.get("with_modules", self.with_modules)

            output = config.get("output", {})
            self.export_chrome_trace = output.get(
                "export_chrome_trace", self.export_chrome_trace
            )
            self.output_file_pattern = output.get(
                "file_pattern", self.output_file_pattern
            )
            self.print_stats = output.get("print_stats", self.print_stats)

            table = output.get("table", {})
            self.table_enabled = table.get("enabled", self.table_enabled)
            self.table_sort_by = table.get("sort_by", self.table_sort_by)
            self.table_row_limit = table.get("row_limit", self.table_row_limit)

            adv = config.get("advanced", {})
            if "targets" in adv and adv["targets"]:
                self.targets = [
                    WrapTarget(
                        t["module"],
                        t["class"],
                        t["method"],
                    )
                    for t in adv["targets"]
                ]
            elif adv.get("target_module"):
                # Backward compatibility with single-target config.
                self.targets = [
                    WrapTarget(
                        adv["target_module"],
                        adv.get("target_class", "Worker"),
                        adv.get("target_method", "execute_model"),
                    )
                ]

            if "profile_rank" in adv and adv["profile_rank"] is not None:
                self.profile_rank = int(adv["profile_rank"])

            self.debug = adv.get("debug", self.debug)

        except ImportError:
            print(
                "[profiler-config] Warning: PyYAML not installed; "
                "using defaults. pip install pyyaml",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"[profiler-config] Warning: Failed to load {config_path}: {e}", file=sys.stderr)

    def _load_from_env(self) -> None:
        if "VLLM_PROFILER_RANGES" in os.environ:
            self.ranges = self._parse_ranges(os.environ["VLLM_PROFILER_RANGES"])
        if "VLLM_PROFILER_ACTIVITIES" in os.environ:
            self.activities = [
                a.strip()
                for a in os.environ["VLLM_PROFILER_ACTIVITIES"].split(",")
                if a.strip()
            ]
        if "VLLM_PROFILER_RECORD_SHAPES" in os.environ:
            self.record_shapes = os.environ["VLLM_PROFILER_RECORD_SHAPES"].lower() in (
                "true",
                "1",
                "yes",
            )
        if "VLLM_PROFILER_WITH_STACK" in os.environ:
            self.with_stack = os.environ["VLLM_PROFILER_WITH_STACK"].lower() in (
                "true",
                "1",
                "yes",
            )
        if "VLLM_PROFILER_MEMORY" in os.environ:
            self.profile_memory = os.environ["VLLM_PROFILER_MEMORY"].lower() in (
                "true",
                "1",
                "yes",
            )
        if "VLLM_PROFILER_OUTPUT" in os.environ:
            self.output_file_pattern = os.environ["VLLM_PROFILER_OUTPUT"]
        if "VLLM_PROFILER_EXPORT_TRACE" in os.environ:
            self.export_chrome_trace = os.environ["VLLM_PROFILER_EXPORT_TRACE"].lower() in (
                "true",
                "1",
                "yes",
            )
        if "VLLM_PROFILER_DEBUG" in os.environ:
            self.debug = os.environ["VLLM_PROFILER_DEBUG"].lower() in (
                "true",
                "1",
                "yes",
            )
        if "VLLM_PROFILE_RANK" in os.environ and os.environ["VLLM_PROFILE_RANK"] != "":
            self.profile_rank = int(os.environ["VLLM_PROFILE_RANK"])

    @staticmethod
    def _parse_ranges(ranges_str: str) -> List[Tuple[int, int]]:
        ranges: List[Tuple[int, int]] = []
        for part in ranges_str.split(","):
            part = part.strip()
            if "-" not in part:
                continue
            start_s, end_s = part.split("-", 1)
            try:
                ranges.append((int(start_s), int(end_s)))
            except ValueError as e:
                print(f"[profiler-config] Warning: Invalid range '{part}': {e}", file=sys.stderr)
        return ranges

    @property
    def target_modules(self) -> set[str]:
        return {t.module for t in self.targets}

    def get_output_filename(
        self,
        *,
        pid: Optional[int] = None,
        rank: Optional[int] = None,
        range_start: Optional[int] = None,
        range_end: Optional[int] = None,
    ) -> str:
        filename = self.output_file_pattern
        filename = filename.replace("{pid}", str(pid or os.getpid()))
        filename = filename.replace("{rank}", str(rank if rank is not None else _global_rank()))
        if range_start is not None:
            filename = filename.replace("{start}", str(range_start))
        if range_end is not None:
            filename = filename.replace("{end}", str(range_end))
        return filename


_config = ProfilerConfig()
_wrapped_labels: set[str] = set()


def _global_rank() -> int:
    try:
        from vllm.distributed.parallel_state import get_world_group

        world_group = get_world_group()
        if world_group is not None:
            return int(world_group.rank)
    except Exception:
        pass
    try:
        import torch.distributed as dist

        if dist.is_initialized():
            return dist.get_rank()
    except Exception:
        pass
    return -1


def _instance_rank(instance: Any) -> int:
    if hasattr(instance, "rank"):
        return int(instance.rank)
    if hasattr(instance, "worker") and getattr(instance.worker, "rank", None) is not None:
        return int(instance.worker.rank)
    return _global_rank()


def _should_profile_instance(instance: Any) -> bool:
    if _config.profile_rank is None:
        return True
    rank = _instance_rank(instance)
    return rank == _config.profile_rank


def wrap_func_with_profiler(original_func, target: WrapTarget):
    import torch
    from torch.profiler import ProfilerActivity, profile

    activities = []
    for activity in _config.activities:
        name = activity.upper()
        if name == "CPU":
            activities.append(ProfilerActivity.CPU)
        elif name == "CUDA":
            activities.append(ProfilerActivity.CUDA)

    count = 0
    range_idx = 0
    profiling_active = False
    prof = profile(
        activities=activities,
        record_shapes=_config.record_shapes,
        with_stack=_config.with_stack,
        profile_memory=_config.profile_memory,
        with_modules=_config.with_modules,
    )

    @functools.wraps(original_func)
    def wrapped_func(instance, *args, **kwargs):
        nonlocal count, range_idx, profiling_active, prof

        if not _should_profile_instance(instance):
            return original_func(instance, *args, **kwargs)

        count += 1
        in_range = False
        if range_idx < len(_config.ranges):
            start, end = _config.ranges[range_idx]
            in_range = start <= count <= end

        if in_range and not profiling_active:
            rank = _instance_rank(instance)
            start, end = _config.ranges[range_idx]
            print(
                f"[profiler] rank={rank} starting {target.label()} "
                f"range {start}-{end} (call #{count})",
                flush=True,
            )
            prof.start()
            profiling_active = True

        try:
            return original_func(instance, *args, **kwargs)
        finally:
            if profiling_active and range_idx < len(_config.ranges):
                start, end = _config.ranges[range_idx]
                if count == end:
                    rank = _instance_rank(instance)
                    print(
                        f"[profiler] rank={rank} stopping {target.label()} "
                        f"range {start}-{end} (call #{count})",
                        flush=True,
                    )
                    prof.stop()
                    profiling_active = False

                    if _config.print_stats:
                        print("===== begin profiler output", flush=True)
                        if _config.table_enabled:
                            print(
                                prof.key_averages().table(
                                    sort_by=_config.table_sort_by,
                                    row_limit=_config.table_row_limit,
                                ),
                                flush=True,
                            )
                        print("===== end profiler output", flush=True)

                    if _config.export_chrome_trace:
                        output_file = _config.get_output_filename(
                            rank=rank,
                            range_start=start,
                            range_end=end,
                        )
                        os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
                        prof.export_chrome_trace(output_file)
                        print(f"[profiler] Exported trace to: {output_file}", flush=True)

                    range_idx += 1
                    if range_idx < len(_config.ranges):
                        prof = profile(
                            activities=activities,
                            record_shapes=_config.record_shapes,
                            with_stack=_config.with_stack,
                            profile_memory=_config.profile_memory,
                            with_modules=_config.with_modules,
                        )

    wrapped_func._vllm_profiler_wrapped = True  # type: ignore[attr-defined]
    return wrapped_func


def wrap_module(mod, targets: List[WrapTarget]) -> None:
    mod_name = getattr(mod, "__name__", "")
    for target in targets:
        if target.module != mod_name:
            continue
        label = target.label()
        if label in _wrapped_labels:
            continue

        cls = getattr(mod, target.class_name, None)
        if cls is None:
            print(
                f"[profiler] Warning: class '{target.class_name}' not found in {mod_name}",
                file=sys.stderr,
            )
            continue

        original = getattr(cls, target.method, None)
        if original is None:
            print(
                f"[profiler] Warning: method '{target.method}' not found on "
                f"{target.class_name}",
                file=sys.stderr,
            )
            continue

        if getattr(original, "_vllm_profiler_wrapped", False):
            _wrapped_labels.add(label)
            continue

        wrapped = wrap_func_with_profiler(original, target)
        setattr(cls, target.method, wrapped)
        _wrapped_labels.add(label)
        print(f"[profiler] Wrapped {label}", file=sys.stderr)


class PostImportLoader(importlib.abc.Loader):
    def __init__(self, loader, module_name: str):
        self.loader = loader
        self.module_name = module_name

    def create_module(self, spec):
        if hasattr(self.loader, "create_module"):
            return self.loader.create_module(spec)
        return None

    def exec_module(self, module):
        self.loader.exec_module(module)
        if _config.debug:
            print(f"[profiler] imported {self.module_name}", file=sys.stderr)
        wrap_module(module, _config.targets)


class PostImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname not in _config.target_modules:
            return None

        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)

        if spec and spec.loader:
            spec.loader = PostImportLoader(spec.loader, fullname)
            return spec
        return None


def _install_hooks() -> None:
    sys.meta_path.insert(0, PostImportFinder())
    for module_name in _config.target_modules:
        mod = sys.modules.get(module_name)
        if mod is not None:
            wrap_module(mod, _config.targets)


_install_hooks()

print(
    f"[profiler] vLLM profiler installed - ranges: {_config.ranges}",
    file=sys.stderr,
)
if _config.profile_rank is not None:
    print(
        f"[profiler] Single-rank mode: global rank {_config.profile_rank} only",
        file=sys.stderr,
    )
for target in _config.targets:
    print(f"[profiler] Target: {target.label()}", file=sys.stderr)
