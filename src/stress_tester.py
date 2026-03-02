"""
Concurrent E2B sandbox stress tester.

Replays parsed OpenHands trajectories (SandboxOp sequences) in real E2B
sandboxes, measuring sandbox creation latency, per-command latency, error
rates, and overall throughput.

Architecture
------------
- asyncio event loop drives all I/O.
- asyncio.Semaphore limits maximum concurrent live sandboxes.
- Each trajectory is an independent coroutine: create → replay → destroy.
- Results are aggregated into a StressTestReport with percentile statistics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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
    commands: list[CommandResult] = field(default_factory=list)
    error: Optional[str] = None

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
    sandbox_results: list[SandboxResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # sandbox_results can be large; summarise per sandbox
        d["sandbox_results"] = [
            {
                "instance_id": r.instance_id,
                "template_id": r.template_id,
                "sandbox_id": r.sandbox_id,
                "sandbox_create_s": r.sandbox_create_s,
                "total_duration_s": r.total_duration_s,
                "n_commands": r.n_commands,
                "n_failed_commands": r.n_failed_commands,
                "error": r.error,
            }
            for r in self.sandbox_results
        ]
        return d

    def save(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info("Report saved to %s", path)


# --------------------------------------------------------------------------- #
#  Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _pct(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_d = sorted(data)
    idx = int(len(sorted_d) * p / 100)
    idx = min(idx, len(sorted_d) - 1)
    return sorted_d[idx]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
#  Single sandbox runner                                                        #
# --------------------------------------------------------------------------- #

async def _run_trajectory_in_sandbox(
    instance_id: str,
    ops,                    # list[SandboxOp]
    template_id: str,
    api_key: str,
    domain: str,
    sandbox_timeout: int,
    command_timeout: int,
    bash_only: bool,
) -> SandboxResult:
    """
    Create one E2B sandbox, replay ops, collect metrics, destroy the sandbox.
    """
    from e2b import AsyncSandbox
    from src.trajectory_parser import OpType

    t0 = time.monotonic()
    sandbox = None
    sandbox_id = None
    create_time = 0.0
    commands: list[CommandResult] = []

    try:
        # ---- create sandbox
        t_create = time.monotonic()
        sandbox = await AsyncSandbox.create(
            template_id,
            timeout=sandbox_timeout,
        )
        sandbox_id = getattr(sandbox, "sandbox_id", None) or getattr(sandbox, "id", None)
        create_time = time.monotonic() - t_create
        logger.debug("Sandbox %s created in %.2fs", sandbox_id, create_time)

        # ---- replay ops
        ops_to_run = [op for op in ops if op.op_type == OpType.BASH] if bash_only else ops

        for op in ops_to_run:
            t_cmd = time.monotonic()
            try:
                if op.op_type == OpType.BASH:
                    result = await asyncio.wait_for(
                        sandbox.commands.run(op.command),
                        timeout=command_timeout,
                    )
                    cmd_res = CommandResult(
                        op_type=op.op_type.value,
                        command_preview=op.command[:120],
                        stdout_bytes=len((result.stdout or "").encode()),
                        stderr_bytes=len((result.stderr or "").encode()),
                        exit_code=result.exit_code,
                        duration_s=time.monotonic() - t_cmd,
                    )

                elif op.op_type == OpType.FILE_WRITE:
                    await sandbox.files.write(op.path, op.content)
                    cmd_res = CommandResult(
                        op_type=op.op_type.value,
                        command_preview=f"write {op.path}",
                        stdout_bytes=0,
                        stderr_bytes=0,
                        exit_code=0,
                        duration_s=time.monotonic() - t_cmd,
                    )

                elif op.op_type == OpType.FILE_READ:
                    content = await sandbox.files.read(op.path)
                    cmd_res = CommandResult(
                        op_type=op.op_type.value,
                        command_preview=f"read {op.path}",
                        stdout_bytes=len((content or "").encode()),
                        stderr_bytes=0,
                        exit_code=0,
                        duration_s=time.monotonic() - t_cmd,
                    )

                elif op.op_type == OpType.FILE_STR_REPLACE:
                    # read → replace → write
                    existing = await sandbox.files.read(op.path)
                    updated = (existing or "").replace(op.old_str, op.new_str, 1)
                    await sandbox.files.write(op.path, updated)
                    cmd_res = CommandResult(
                        op_type=op.op_type.value,
                        command_preview=f"str_replace {op.path}",
                        stdout_bytes=0,
                        stderr_bytes=0,
                        exit_code=0,
                        duration_s=time.monotonic() - t_cmd,
                    )

                else:
                    continue  # skip unknown ops

                commands.append(cmd_res)

            except asyncio.TimeoutError:
                cmd_res = CommandResult(
                    op_type=op.op_type.value,
                    command_preview=op.command[:120] if hasattr(op, "command") else str(op.path),
                    stdout_bytes=0,
                    stderr_bytes=0,
                    exit_code=-1,
                    duration_s=time.monotonic() - t_cmd,
                    error="timeout",
                )
                commands.append(cmd_res)
                logger.warning("Command timeout in sandbox %s", sandbox_id)

            except Exception as exc:
                cmd_res = CommandResult(
                    op_type=op.op_type.value,
                    command_preview=getattr(op, "command", "")[:120] or getattr(op, "path", ""),
                    stdout_bytes=0,
                    stderr_bytes=0,
                    exit_code=-1,
                    duration_s=time.monotonic() - t_cmd,
                    error=str(exc),
                )
                commands.append(cmd_res)
                logger.warning("Command error in sandbox %s: %s", sandbox_id, exc)

        return SandboxResult(
            instance_id=instance_id,
            template_id=template_id,
            sandbox_id=sandbox_id,
            sandbox_create_s=create_time,
            total_duration_s=time.monotonic() - t0,
            commands=commands,
        )

    except Exception as exc:
        logger.error("Sandbox error for %s: %s", instance_id, exc)
        return SandboxResult(
            instance_id=instance_id,
            template_id=template_id,
            sandbox_id=sandbox_id,
            sandbox_create_s=create_time,
            total_duration_s=time.monotonic() - t0,
            commands=commands,
            error=str(exc),
        )

    finally:
        if sandbox is not None:
            try:
                await sandbox.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  Stress tester                                                                #
# --------------------------------------------------------------------------- #

@dataclass
class StressTestConfig:
    template_id: str                    # default template when not per-task
    api_key: str
    domain: str
    max_concurrent: int = 10
    sandbox_timeout: int = 300
    command_timeout: int = 60
    bash_only: bool = True              # only replay bash commands
    ramp_up_delay_s: float = 0.0       # stagger sandbox creation
    results_dir: str = "./results"


class StressTester:
    """
    Orchestrates concurrent trajectory replay across E2B sandboxes.

    Usage
    -----
    tester = StressTester(cfg)
    report = await tester.run(parsed_trajectories, template_mapping)
    report.save("./results/report.json")
    """

    def __init__(self, cfg: StressTestConfig):
        self.cfg = cfg

    async def run(
        self,
        parsed_trajectories,            # list[ParsedTrajectory]
        template_mapping: dict[str, str] | None = None,
    ) -> StressTestReport:
        """
        Run the stress test.

        Parameters
        ----------
        parsed_trajectories : list[ParsedTrajectory]
        template_mapping    : {instance_id: template_id}
                              Falls back to cfg.template_id when missing.
        """
        cfg = self.cfg
        sem = asyncio.Semaphore(cfg.max_concurrent)
        started_at = _now_iso()
        t_start = time.monotonic()

        async def run_one(pt, idx: int) -> SandboxResult:
            # stagger launches to avoid thundering herd
            if cfg.ramp_up_delay_s > 0:
                await asyncio.sleep(idx * cfg.ramp_up_delay_s)
            async with sem:
                tid = (template_mapping or {}).get(pt.instance_id) or cfg.template_id
                return await _run_trajectory_in_sandbox(
                    instance_id=pt.instance_id,
                    ops=pt.ops,
                    template_id=tid,
                    api_key=cfg.api_key,
                    domain=cfg.domain,
                    sandbox_timeout=cfg.sandbox_timeout,
                    command_timeout=cfg.command_timeout,
                    bash_only=cfg.bash_only,
                )

        tasks = [run_one(pt, i) for i, pt in enumerate(parsed_trajectories)]
        results: list[SandboxResult] = await asyncio.gather(*tasks)

        elapsed = time.monotonic() - t_start
        return self._build_report(results, elapsed, started_at)

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

        all_cmd_durations: list[float] = []
        total_cmds = 0
        failed_cmds = 0
        for r in results:
            for c in r.commands:
                all_cmd_durations.append(c.duration_s)
                total_cmds += 1
                if c.exit_code != 0 or c.error:
                    failed_cmds += 1

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
                "n_trajectories": len(results),
            },
            total_trajectories=len(results),
            successful_trajectories=len(successful),
            failed_trajectories=len(failed),
            total_commands=total_cmds,
            failed_commands=failed_cmds,
            p50_create_s=_pct(create_times, 50),
            p95_create_s=_pct(create_times, 95),
            p99_create_s=_pct(create_times, 99),
            p50_cmd_s=_pct(all_cmd_durations, 50),
            p95_cmd_s=_pct(all_cmd_durations, 95),
            p99_cmd_s=_pct(all_cmd_durations, 99),
            avg_trajectory_s=statistics.mean(traj_times) if traj_times else 0.0,
            throughput_traj_per_min=throughput,
            sandbox_results=results,
        )
