"""
Concurrent E2B sandbox stress tester.

Replays parsed OpenHands trajectories (SandboxOp sequences) in real E2B
sandboxes, measuring sandbox creation latency, per-command latency, error
rates, and overall throughput.

Architecture
------------
- asyncio event loop drives all I/O.
- asyncio.Semaphore limits maximum concurrent live sandboxes.
- _RampUpLimiter ensures staggered sandbox creation (token-bucket pattern).
- Each trajectory runs in a SandboxRunner: create (with retry) -> replay -> destroy.
- OpExecutor dispatches individual ops to the E2B sandbox API.
- ProgressEvent callbacks drive real-time Rich Live display.
- Signal handlers enable graceful shutdown with sandbox cleanup.
- Results are aggregated into a StressTestReport with per-op-type statistics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Progress events                                                              #
# --------------------------------------------------------------------------- #

class EventType(str, Enum):
    SANDBOX_CREATING = "sandbox_creating"
    SANDBOX_CREATED = "sandbox_created"
    OP_DONE = "op_done"
    TRAJECTORY_DONE = "trajectory_done"
    TRAJECTORY_ERROR = "trajectory_error"
    RETRY = "retry"
    SHUTDOWN = "shutdown"


@dataclass
class ProgressEvent:
    """Emitted at key lifecycle points for real-time progress display."""
    event_type: EventType
    instance_id: str
    detail: str = ""
    sandbox_id: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


# --------------------------------------------------------------------------- #
#  Result data models                                                           #
# --------------------------------------------------------------------------- #

@dataclass
class CommandResult:
    op_type: str
    command_preview: str        # first 120 chars of command / path
    stdout_bytes: int
    stderr_bytes: int
    exit_code: int
    duration_s: float
    error: Optional[str] = None


@dataclass
class SandboxResult:
    instance_id: str
    template_id: str
    sandbox_id: Optional[str]
    sandbox_create_s: float     # time to create the sandbox
    total_duration_s: float
    started_at_s: float = 0.0   # monotonic offset from test start (for timeline)
    commands: list[CommandResult] = field(default_factory=list)
    error: Optional[str] = None
    model_patch: str = ""       # expected patch from trajectory
    actual_patch: str = ""      # git diff captured after replay
    patch_match: bool | None = None  # None=skipped, True/False=compared

    @property
    def success(self) -> bool:
        return self.error is None

    @property
    def n_commands(self) -> int:
        return len(self.commands)

    @property
    def n_failed_commands(self) -> int:
        return sum(1 for c in self.commands if c.exit_code != 0 or c.error)


@dataclass
class StressTestReport:
    started_at: str
    finished_at: str
    config: dict
    total_trajectories: int
    successful_trajectories: int
    failed_trajectories: int
    total_commands: int
    failed_commands: int
    # latency stats (seconds)
    p50_create_s: float
    p95_create_s: float
    p99_create_s: float
    p50_cmd_s: float
    p95_cmd_s: float
    p99_cmd_s: float
    avg_trajectory_s: float
    throughput_traj_per_min: float
    # enhanced stats
    per_op_type_stats: dict[str, dict] = field(default_factory=dict)
    error_categories: dict[str, int] = field(default_factory=dict)
    timeline: list[dict] = field(default_factory=list)
    # patch validation
    patch_compared: int = 0
    patch_matched: int = 0
    patch_mismatched: int = 0
    sandbox_results: list[SandboxResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # summarise per sandbox (strip full command lists for compactness)
        d["sandbox_results"] = [
            {
                "instance_id": r.instance_id,
                "template_id": r.template_id,
                "sandbox_id": r.sandbox_id,
                "sandbox_create_s": r.sandbox_create_s,
                "total_duration_s": r.total_duration_s,
                "started_at_s": r.started_at_s,
                "n_commands": r.n_commands,
                "n_failed_commands": r.n_failed_commands,
                "error": r.error,
                "patch_match": r.patch_match,
            }
            for r in self.sandbox_results
        ]
        return d

    def save(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info("Report saved to %s", path)

    @classmethod
    def from_dict(cls, data: dict) -> StressTestReport:
        """Reconstruct a report from saved JSON data."""
        sr_list = data.pop("sandbox_results", [])
        # Provide defaults for fields that may be missing in older reports
        data.setdefault("per_op_type_stats", {})
        data.setdefault("error_categories", {})
        data.setdefault("timeline", [])
        data.setdefault("patch_compared", 0)
        data.setdefault("patch_matched", 0)
        data.setdefault("patch_mismatched", 0)
        # sandbox_results in saved JSON are summaries (dicts), not full SandboxResult
        report = cls(**data, sandbox_results=[])
        # Attach raw summary dicts as lightweight objects for display
        report._sandbox_summaries = sr_list
        return report


# --------------------------------------------------------------------------- #
#  Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _percentiles(data: list[float]) -> dict[str, float]:
    """Compute p50, p95, p99 using numpy."""
    if not data:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    arr = np.array(data)
    return {
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _categorize_error(error: str) -> str:
    """Classify an error string into a human-readable category."""
    e = error.lower()
    if "timeout" in e:
        return "timeout"
    if any(k in e for k in ("connection", "connect", "unreachable")):
        return "connection_error"
    if any(k in e for k in ("429", "rate")):
        return "rate_limited"
    if any(k in e for k in ("503", "502", "500")):
        return "server_error"
    if any(k in e for k in ("permission", "denied", "forbidden", "403")):
        return "permission_denied"
    if any(k in e for k in ("not found", "404")):
        return "not_found"
    return "other"


# --------------------------------------------------------------------------- #
#  Ramp-up rate limiter                                                         #
# --------------------------------------------------------------------------- #

class _RampUpLimiter:
    """
    Token-bucket rate limiter for staggering sandbox creation.

    Ensures at most one sandbox launch per ``delay_s`` seconds.
    Used INSIDE the concurrency semaphore so tasks don't pre-schedule
    massive sleep timers.
    """

    def __init__(self, delay_s: float):
        self._delay_s = delay_s
        self._lock = asyncio.Lock()
        self._last_launch = 0.0

    async def wait(self):
        if self._delay_s <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait_until = self._last_launch + self._delay_s
            if now < wait_until:
                await asyncio.sleep(wait_until - now)
            self._last_launch = time.monotonic()


# --------------------------------------------------------------------------- #
#  Op executor                                                                  #
# --------------------------------------------------------------------------- #

class OpExecutor:
    """
    Executes individual SandboxOp against an AsyncSandbox instance.

    Each method handles its own timing and wraps the result into a
    CommandResult, including error handling for timeouts and exceptions.
    """

    def __init__(self, sandbox, command_timeout: int):
        self._sandbox = sandbox
        self._command_timeout = command_timeout

    async def execute(self, op) -> CommandResult | None:
        """Dispatch to the appropriate handler based on op_type."""
        from src.trajectory_parser import OpType

        handler = {
            OpType.BASH: self._exec_bash,
            OpType.FILE_WRITE: self._exec_file_write,
            OpType.FILE_READ: self._exec_file_read,
            OpType.FILE_STR_REPLACE: self._exec_str_replace,
        }.get(op.op_type)

        if handler is None:
            return None  # skip unknown ops

        t_cmd = time.monotonic()
        try:
            return await handler(op, t_cmd)
        except asyncio.TimeoutError:
            preview = (op.command[:120] if op.command else op.path) or "?"
            return CommandResult(
                op_type=op.op_type.value,
                command_preview=preview,
                stdout_bytes=0, stderr_bytes=0,
                exit_code=-1,
                duration_s=time.monotonic() - t_cmd,
                error="timeout",
            )
        except Exception as exc:
            preview = (getattr(op, "command", "") or "")[:120] or getattr(op, "path", "?")
            return CommandResult(
                op_type=op.op_type.value,
                command_preview=preview,
                stdout_bytes=0, stderr_bytes=0,
                exit_code=-1,
                duration_s=time.monotonic() - t_cmd,
                error=str(exc),
            )

    async def _exec_bash(self, op, t_start: float) -> CommandResult:
        result = await asyncio.wait_for(
            self._sandbox.commands.run(op.command),
            timeout=self._command_timeout,
        )
        return CommandResult(
            op_type=op.op_type.value,
            command_preview=op.command[:120],
            stdout_bytes=len((result.stdout or "").encode()),
            stderr_bytes=len((result.stderr or "").encode()),
            exit_code=result.exit_code,
            duration_s=time.monotonic() - t_start,
        )

    async def _exec_file_write(self, op, t_start: float) -> CommandResult:
        await self._sandbox.files.write(op.path, op.content)
        return CommandResult(
            op_type=op.op_type.value,
            command_preview=f"write {op.path}",
            stdout_bytes=0, stderr_bytes=0,
            exit_code=0,
            duration_s=time.monotonic() - t_start,
        )

    async def _exec_file_read(self, op, t_start: float) -> CommandResult:
        content = await self._sandbox.files.read(op.path)
        return CommandResult(
            op_type=op.op_type.value,
            command_preview=f"read {op.path}",
            stdout_bytes=len((content or "").encode()),
            stderr_bytes=0,
            exit_code=0,
            duration_s=time.monotonic() - t_start,
        )

    async def _exec_str_replace(self, op, t_start: float) -> CommandResult:
        existing = await self._sandbox.files.read(op.path)
        updated = (existing or "").replace(op.old_str, op.new_str, 1)
        await self._sandbox.files.write(op.path, updated)
        return CommandResult(
            op_type=op.op_type.value,
            command_preview=f"str_replace {op.path}",
            stdout_bytes=0, stderr_bytes=0,
            exit_code=0,
            duration_s=time.monotonic() - t_start,
        )


# --------------------------------------------------------------------------- #
#  Sandbox runner (single trajectory lifecycle)                                 #
# --------------------------------------------------------------------------- #

class SandboxRunner:
    """
    Manages the full lifecycle of one sandbox: create (with retry) -> replay -> destroy.

    Tracks active sandboxes in a shared set for graceful shutdown.
    """

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2.0

    def __init__(
        self,
        config: "StressTestConfig",
        active_sandboxes: set,
        shutdown_event: asyncio.Event,
        progress_cb: ProgressCallback | None = None,
    ):
        self._cfg = config
        self._active_sandboxes = active_sandboxes
        self._shutdown_event = shutdown_event
        self._progress_cb = progress_cb

    def _emit(self, event_type: EventType, instance_id: str, **kwargs):
        if self._progress_cb:
            self._progress_cb(ProgressEvent(
                event_type=event_type, instance_id=instance_id, **kwargs,
            ))

    async def run(
        self,
        instance_id: str,
        ops: list,
        template_id: str,
        test_start_mono: float,
        model_patch: str = "",
    ) -> SandboxResult:
        """Create one sandbox, replay ops, extract patch, compare, destroy."""
        from src.trajectory_parser import OpType

        t0 = time.monotonic()
        started_at_s = t0 - test_start_mono
        sandbox = None
        sandbox_id = None
        create_time = 0.0
        commands: list[CommandResult] = []
        actual_patch = ""
        patch_match_result: bool | None = None

        try:
            # ---- create sandbox with retry
            self._emit(EventType.SANDBOX_CREATING, instance_id)
            sandbox, create_time = await self._create_with_retry(
                template_id, instance_id,
            )
            sandbox_id = getattr(sandbox, "sandbox_id", None) or getattr(sandbox, "id", None)
            self._active_sandboxes.add(sandbox)
            self._emit(
                EventType.SANDBOX_CREATED, instance_id,
                sandbox_id=sandbox_id,
                detail=f"created in {create_time:.2f}s",
            )

            # ---- replay ops
            executor = OpExecutor(sandbox, self._cfg.command_timeout)
            bash_only = self._cfg.bash_only
            for op in ops:
                # cooperative shutdown check
                if self._shutdown_event.is_set():
                    break
                if bash_only and op.op_type != OpType.BASH:
                    continue
                cmd_result = await executor.execute(op)
                if cmd_result is not None:
                    commands.append(cmd_result)
                    if cmd_result.error:
                        logger.warning(
                            "Command error in sandbox %s: %s",
                            sandbox_id, cmd_result.error,
                        )

            # ---- extract actual patch via git diff
            if self._cfg.extract_patch and not self._shutdown_event.is_set():
                try:
                    diff_result = await asyncio.wait_for(
                        sandbox.commands.run(
                            "cd /testbed && git add -A && git diff --cached HEAD"
                        ),
                        timeout=30,
                    )
                    actual_patch = (diff_result.stdout or "").strip()
                except Exception as exc:
                    logger.warning(
                        "Failed to extract patch from %s: %s", sandbox_id, exc,
                    )

            # ---- compare patches
            if model_patch and actual_patch:
                from src.patch_compare import patches_match
                patch_match_result = patches_match(model_patch, actual_patch)
            elif model_patch and not actual_patch:
                patch_match_result = False  # expected changes but got none

            self._emit(
                EventType.TRAJECTORY_DONE, instance_id,
                sandbox_id=sandbox_id,
                detail=f"{len(commands)} ops in {time.monotonic() - t0:.1f}s"
                       + (f" patch={'match' if patch_match_result else 'mismatch'}"
                          if patch_match_result is not None else ""),
            )

            return SandboxResult(
                instance_id=instance_id,
                template_id=template_id,
                sandbox_id=sandbox_id,
                sandbox_create_s=create_time,
                total_duration_s=time.monotonic() - t0,
                started_at_s=started_at_s,
                commands=commands,
                model_patch=model_patch,
                actual_patch=actual_patch,
                patch_match=patch_match_result,
            )

        except Exception as exc:
            logger.error("Sandbox error for %s: %s", instance_id, exc)
            self._emit(
                EventType.TRAJECTORY_ERROR, instance_id,
                detail=str(exc),
            )
            return SandboxResult(
                instance_id=instance_id,
                template_id=template_id,
                sandbox_id=sandbox_id,
                sandbox_create_s=create_time,
                total_duration_s=time.monotonic() - t0,
                started_at_s=started_at_s,
                commands=commands,
                error=str(exc),
                model_patch=model_patch,
                actual_patch=actual_patch,
                patch_match=patch_match_result,
            )

        finally:
            if sandbox is not None:
                self._active_sandboxes.discard(sandbox)
                try:
                    await sandbox.kill()
                except Exception:
                    pass

    async def _create_with_retry(
        self, template_id: str, instance_id: str,
    ) -> tuple[Any, float]:
        """Create sandbox with exponential-backoff retry on transient errors."""
        from e2b import AsyncSandbox

        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            t_create = time.monotonic()
            try:
                sandbox = await AsyncSandbox.create(
                    template_id,
                    timeout=self._cfg.sandbox_timeout,
                )
                return sandbox, time.monotonic() - t_create
            except Exception as exc:
                last_exc = exc
                if attempt == self.MAX_RETRIES or not self._is_retryable(exc):
                    raise
                delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Sandbox creation attempt %d/%d for %s failed: %s  (retry in %.1fs)",
                    attempt + 1, self.MAX_RETRIES, instance_id, exc, delay,
                )
                self._emit(
                    EventType.RETRY, instance_id,
                    detail=f"attempt {attempt + 1}, retrying in {delay:.0f}s",
                )
                await asyncio.sleep(delay)

        raise last_exc  # unreachable, but satisfies type checker

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(k in msg for k in (
            "connection", "timeout", "503", "502", "429", "rate", "unavailable",
        ))


# --------------------------------------------------------------------------- #
#  Stress tester config                                                         #
# --------------------------------------------------------------------------- #

@dataclass
class StressTestConfig:
    template_id: str                    # default template when not per-task
    api_key: str
    api_url: str
    max_concurrent: int = 10
    sandbox_timeout: int = 300
    command_timeout: int = 60
    bash_only: bool = False             # replay all op types by default for correct patches
    extract_patch: bool = True          # run git diff after replay to capture changes
    ramp_up_delay_s: float = 0.0        # minimum seconds between sandbox launches
    results_dir: str = "./results"


# --------------------------------------------------------------------------- #
#  Stress tester orchestrator                                                   #
# --------------------------------------------------------------------------- #

class StressTester:
    """
    Orchestrates concurrent trajectory replay across E2B sandboxes.

    Features:
    - Semaphore-based concurrency limiting
    - Token-bucket ramp-up to avoid thundering herd
    - Retry on transient sandbox creation failures
    - Graceful shutdown via signal handlers (SIGINT/SIGTERM)
    - Real-time progress via ProgressEvent callbacks
    - Enhanced reporting with per-op-type stats and error categorization

    Usage::

        tester = StressTester(cfg)
        tester.set_progress_callback(my_callback)
        report = await tester.run(parsed_trajectories, template_mapping)
        report.save("./results/report.json")
    """

    def __init__(self, cfg: StressTestConfig):
        self.cfg = cfg
        self._active_sandboxes: set = set()
        self._shutdown_event = asyncio.Event()
        self._progress_cb: ProgressCallback | None = None

    def set_progress_callback(self, cb: ProgressCallback):
        self._progress_cb = cb

    async def run(
        self,
        parsed_trajectories: list,          # list[ParsedTrajectory]
        template_mapping: dict[str, str] | None = None,
    ) -> StressTestReport:
        """Run the stress test with concurrency control, ramp-up, and shutdown handling."""
        cfg = self.cfg
        os.environ.setdefault("E2B_API_KEY", cfg.api_key)
        os.environ.setdefault("E2B_API_URL", cfg.api_url)

        sem = asyncio.Semaphore(cfg.max_concurrent)
        limiter = _RampUpLimiter(cfg.ramp_up_delay_s)
        started_at = _now_iso()
        t_start = time.monotonic()

        # Install signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        original_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                original_handlers[sig] = loop.add_signal_handler(
                    sig, self._handle_shutdown_signal,
                )
            except (NotImplementedError, OSError):
                pass  # Windows or non-main-thread

        runner = SandboxRunner(
            config=cfg,
            active_sandboxes=self._active_sandboxes,
            shutdown_event=self._shutdown_event,
            progress_cb=self._progress_cb,
        )

        async def run_one(pt) -> SandboxResult:
            async with sem:
                if self._shutdown_event.is_set():
                    return SandboxResult(
                        instance_id=pt.instance_id,
                        template_id=cfg.template_id,
                        sandbox_id=None,
                        sandbox_create_s=0.0,
                        total_duration_s=0.0,
                        error="shutdown_before_start",
                    )
                await limiter.wait()
                tid = (template_mapping or {}).get(pt.instance_id) or cfg.template_id
                return await runner.run(
                    instance_id=pt.instance_id,
                    ops=pt.ops,
                    template_id=tid,
                    test_start_mono=t_start,
                    model_patch=getattr(pt, "model_patch", ""),
                )

        tasks = [asyncio.create_task(run_one(pt)) for pt in parsed_trajectories]

        try:
            results: list[SandboxResult] = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # Collect whatever finished
            results = []
            for t in tasks:
                if t.done() and not t.cancelled():
                    results.append(t.result())
                else:
                    t.cancel()

        # Restore signal handlers
        for sig in original_handlers:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, OSError):
                pass

        # Ensure all sandboxes are killed on exit
        await self.shutdown()

        elapsed = time.monotonic() - t_start
        return self._build_report(results, elapsed, started_at)

    def _handle_shutdown_signal(self):
        if not self._shutdown_event.is_set():
            logger.warning("Shutdown signal received, finishing active sandboxes...")
            self._shutdown_event.set()
            if self._progress_cb:
                self._progress_cb(ProgressEvent(
                    event_type=EventType.SHUTDOWN,
                    instance_id="*",
                    detail="graceful shutdown initiated",
                ))

    async def shutdown(self):
        """Kill all tracked active sandboxes."""
        if not self._active_sandboxes:
            return
        logger.info("Cleaning up %d active sandbox(es)...", len(self._active_sandboxes))
        coros = []
        for sb in list(self._active_sandboxes):
            async def _kill(s=sb):
                try:
                    await s.kill()
                except Exception:
                    pass
            coros.append(_kill())
        await asyncio.gather(*coros)
        self._active_sandboxes.clear()

    def _build_report(
        self,
        results: list[SandboxResult],
        elapsed_s: float,
        started_at: str,
    ) -> StressTestReport:
        cfg = self.cfg
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        create_times = [r.sandbox_create_s for r in results if r.sandbox_create_s > 0]
        traj_times = [r.total_duration_s for r in results]

        # Per-command aggregation
        all_cmd_durations: list[float] = []
        total_cmds = 0
        failed_cmds = 0
        op_type_durations: dict[str, list[float]] = defaultdict(list)
        op_type_errors: dict[str, int] = defaultdict(int)
        error_categories: dict[str, int] = defaultdict(int)

        for r in results:
            if r.error:
                error_categories[_categorize_error(r.error)] += 1
            for c in r.commands:
                all_cmd_durations.append(c.duration_s)
                op_type_durations[c.op_type].append(c.duration_s)
                total_cmds += 1
                if c.exit_code != 0 or c.error:
                    failed_cmds += 1
                    op_type_errors[c.op_type] += 1
                    if c.error:
                        error_categories[_categorize_error(c.error)] += 1

        # Per-op-type stats
        per_op_type_stats = {}
        for op_type, durations in op_type_durations.items():
            pcts = _percentiles(durations)
            per_op_type_stats[op_type] = {
                "count": len(durations),
                "failed": op_type_errors.get(op_type, 0),
                "p50_s": pcts["p50"],
                "p95_s": pcts["p95"],
                "p99_s": pcts["p99"],
            }

        # Timeline (for visualization / debugging)
        timeline = [
            {
                "instance_id": r.instance_id,
                "started_at_s": r.started_at_s,
                "finished_at_s": r.started_at_s + r.total_duration_s,
                "success": r.success,
            }
            for r in results
        ]

        # Patch validation stats
        patch_compared = sum(1 for r in results if r.patch_match is not None)
        patch_matched = sum(1 for r in results if r.patch_match is True)
        patch_mismatched = sum(1 for r in results if r.patch_match is False)

        create_pcts = _percentiles(create_times)
        cmd_pcts = _percentiles(all_cmd_durations)
        throughput = len(results) / (elapsed_s / 60) if elapsed_s > 0 else 0.0

        return StressTestReport(
            started_at=started_at,
            finished_at=_now_iso(),
            config={
                "template_id": cfg.template_id,
                "max_concurrent": cfg.max_concurrent,
                "sandbox_timeout": cfg.sandbox_timeout,
                "command_timeout": cfg.command_timeout,
                "bash_only": cfg.bash_only,
                "ramp_up_delay_s": cfg.ramp_up_delay_s,
                "n_trajectories": len(results),
            },
            total_trajectories=len(results),
            successful_trajectories=len(successful),
            failed_trajectories=len(failed),
            total_commands=total_cmds,
            failed_commands=failed_cmds,
            p50_create_s=create_pcts["p50"],
            p95_create_s=create_pcts["p95"],
            p99_create_s=create_pcts["p99"],
            p50_cmd_s=cmd_pcts["p50"],
            p95_cmd_s=cmd_pcts["p95"],
            p99_cmd_s=cmd_pcts["p99"],
            avg_trajectory_s=float(np.mean(traj_times)) if traj_times else 0.0,
            throughput_traj_per_min=throughput,
            per_op_type_stats=per_op_type_stats,
            error_categories=dict(error_categories),
            timeline=timeline,
            patch_compared=patch_compared,
            patch_matched=patch_matched,
            patch_mismatched=patch_mismatched,
            sandbox_results=results,
        )
