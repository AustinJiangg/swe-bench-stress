#!/usr/bin/env python3
"""
SWE-bench E2B Sandbox Stress Tester
====================================

CLI commands:

  download-data        Download SWE-rebench tasks and trajectories
  build-templates      Build E2B templates from install_configs
  run-stress-test      Execute concurrent trajectory replay in E2B sandboxes
  show-report          Pretty-print a saved JSON report

Usage examples:

  python main.py download-data --n-tasks 200 --n-trajectories 100
  python main.py build-templates --n-tasks 50
  python main.py run-stress-test --n-traj 20 --concurrency 5
  python main.py show-report ./results/report.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

# ------------------------------------------------------------------ setup

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger("stress")
console = Console()


def _load_config():
    from config import Config
    return Config()


# ------------------------------------------------------------------ helpers

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ===========================================================================
#  CLI root
# ===========================================================================

@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
def cli(debug: bool):
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)


# ===========================================================================
#  download-data
# ===========================================================================

@cli.command("download-data")
@click.option("--n-tasks", default=None, type=int,
              help="Number of task instances to download (0 = all, default from config).")
@click.option("--n-trajectories", default=None, type=int,
              help="Number of trajectories to download (0 = all, default from config).")
@click.option("--data-dir", default=None, help="Override data directory.")
@click.option("--local-tasks", default=None, metavar="PATH",
              help="Local directory of SWE-rebench dataset (skip HuggingFace).")
@click.option("--local-trajectories", default=None, metavar="PATH",
              help="Local directory of SWE-rebench-openhands-trajectories dataset (skip HuggingFace).")
@click.option("--match-tasks", is_flag=True,
              help="Skip bulk task download; instead fetch only tasks matching downloaded trajectories.")
def download_data(n_tasks: int | None, n_trajectories: int | None, data_dir: str | None,
                  local_tasks: str | None, local_trajectories: str | None,
                  match_tasks: bool):
    """Download SWE-rebench tasks and trajectories from HuggingFace (or local path)."""
    from config import Config
    from src.downloader import DatasetDownloader

    cfg = Config()
    data_dir = data_dir or cfg.data_dir
    if n_tasks is None:
        n_tasks = cfg.n_tasks
    if n_trajectories is None:
        n_trajectories = cfg.n_trajectories

    downloader = DatasetDownloader(
        data_dir=data_dir,
        hf_token=cfg.hf_token,
        local_tasks_dir=local_tasks or "",
        local_trajs_dir=local_trajectories or "",
    )

    # ---- download trajectories first (needed if --match-tasks)
    with console.status("[bold green]Downloading trajectories..."):
        trajs = downloader.download_trajectories(n_samples=n_trajectories)
    console.print(
        f"[green]✓[/] Downloaded {len(trajs)} trajectories → {data_dir}/trajectories.json"
    )

    # ---- download tasks
    if match_tasks:
        # Only fetch tasks whose instance_id appears in the downloaded trajectories
        traj_ids = {t.get("instance_id") for t in trajs if t.get("instance_id")}

        # Load existing cached tasks (if any)
        tasks_path = Path(data_dir) / "tasks.json"
        tasks: list[dict] = []
        if tasks_path.exists():
            with open(tasks_path) as f:
                tasks = json.load(f)

        existing_ids = {t.get("instance_id") for t in tasks}
        missing_ids = traj_ids - existing_ids

        if missing_ids:
            console.print(f"[yellow]Scanning tasks dataset for {len(missing_ids)} trajectory-matched task(s)...[/]")
            with console.status("[bold green]Fetching matched tasks..."):
                matched = downloader.download_tasks_by_ids(missing_ids)
            if matched:
                tasks.extend(matched)
                with open(tasks_path, "w") as f:
                    json.dump(tasks, f, indent=2)
                console.print(f"[green]✓[/] Found {len(matched)}/{len(missing_ids)} matched tasks → {data_dir}/tasks.json")
            else:
                console.print(f"[red]Could not find any matching tasks in the dataset[/]")
        else:
            console.print(f"[green]✓[/] All {len(traj_ids)} trajectory instance_ids already have matching tasks")
    else:
        with console.status("[bold green]Downloading tasks..."):
            tasks = downloader.download_tasks(n_samples=n_tasks)
        console.print(f"[green]✓[/] Downloaded {len(tasks)} task instances → {data_dir}/tasks.json")

    # Quick stats
    with_config = sum(1 for t in tasks if t.get("install_config"))
    console.print(f"  Tasks with install_config: {with_config}/{len(tasks)}")


# ===========================================================================
#  build-templates
# ===========================================================================

@cli.command("build-templates")
@click.option("--n-tasks", default=0, show_default=True,
              help="Number of tasks to process (0 = all cached tasks).")
@click.option("--strategy", default="sdk", type=click.Choice(["sdk", "cli"]),
              show_default=True, help="Template build strategy.")
@click.option("--export-dockerfiles", is_flag=True,
              help="Export Dockerfiles to ./dockerfiles/ without building.")
@click.option("--for-trajectories", is_flag=True,
              help="Only build templates for tasks that have matching trajectories.")
@click.option("--data-dir", default=None)
def build_templates(n_tasks: int, strategy: str, export_dockerfiles: bool,
                    for_trajectories: bool, data_dir: str | None):
    """Build E2B templates from task install_configs."""
    from config import Config
    from src.template_builder import (
        E2BTemplateBuilder,
        generate_dockerfile,
        group_tasks_by_config,
    )

    cfg = Config()
    data_dir = data_dir or cfg.data_dir
    tasks_path = Path(data_dir) / "tasks.json"

    if not tasks_path.exists():
        console.print("[red]tasks.json not found. Run `download-data` first.[/]")
        sys.exit(1)

    with open(tasks_path) as f:
        tasks: list[dict] = json.load(f)

    if n_tasks > 0:
        tasks = tasks[:n_tasks]

    if for_trajectories:
        trajs_path = Path(data_dir) / "trajectories.json"
        if trajs_path.exists():
            with open(trajs_path) as f:
                trajs = json.load(f)
            traj_ids = {t.get("instance_id") for t in trajs}
            tasks = [t for t in tasks if t.get("instance_id") in traj_ids]
            console.print(f"Filtered to {len(tasks)} tasks matching trajectories")
        else:
            console.print("[yellow]No trajectories.json found, building for all tasks[/]")

    # ---- group by install_config
    groups = group_tasks_by_config(tasks)
    console.print(
        f"Processing {len(tasks)} tasks → "
        f"[bold]{len(groups)}[/] unique install_configs, "
        f"strategy=[bold]{strategy}[/]"
    )
    for fp, g in groups.items():
        console.print(
            f"  {fp}  python={g['python'] or '?':6s}  "
            f"repo={g['repo'] or '(none)':30s}  "
            f"commit={g['base_commit'][:8] if g['base_commit'] else '?':8s}  "
            f"tasks={len(g['tasks'])}"
        )

    if export_dockerfiles:
        df_dir = Path("./dockerfiles")
        df_dir.mkdir(exist_ok=True)
        for fp, g in groups.items():
            instance = {
                "install_config": g["config"],
                "repo": g["repo"],
                "base_commit": g["base_commit"],
                "environment_setup_commit": g.get("environment_setup_commit", g["base_commit"]),
                "environment": g.get("environment", ""),
                "requirements": g.get("requirements", ""),
            }
            df = generate_dockerfile(instance, cfg.e2b_base_image)
            (df_dir / f"{fp}.Dockerfile").write_text(df)
        console.print(f"[green]✓[/] Exported {len(groups)} unique Dockerfiles to ./dockerfiles/")
        return

    # Expose config values as env vars for the E2B SDK
    os.environ.setdefault("E2B_API_KEY", cfg.e2b_api_key)
    os.environ.setdefault("E2B_API_URL", cfg.e2b_api_url)

    builder = E2BTemplateBuilder(
        base_image=cfg.e2b_base_image,
        cache_file=str(Path(data_dir) / "template_cache.json"),
        strategy=strategy,
        cpu_count=cfg.e2b_template_cpu_count,
        memory_mb=cfg.e2b_template_memory_mb,
        registry_username=cfg.e2b_registry_username,
        registry_password=cfg.e2b_registry_password,
        docker_registry_url=cfg.docker_registry_url,
        docker_registry_repo=cfg.docker_registry_repo,
    )

    mapping = builder.get_or_build_batch(tasks, name_prefix="swe")

    built = sum(1 for v in mapping.values() if v)
    console.print(f"[green]✓[/] Templates ready: {built}/{len(tasks)} ({len(groups)} unique images)")

    # Save mapping
    mapping_path = Path(data_dir) / "template_mapping.json"
    with open(mapping_path, "w") as f:
        json.dump(mapping, f, indent=2)
    console.print(f"  Mapping saved to {mapping_path}")


# ===========================================================================
#  run-stress-test
# ===========================================================================

@cli.command("run-stress-test")
@click.option("--n-traj", default=0, show_default=True,
              help="Number of trajectories to replay (0 = all downloaded).")
@click.option("--concurrency", default=None, type=int,
              help="Max concurrent sandboxes (default from config).")
@click.option("--template-id", default=None,
              help="Override template ID for all sandboxes.")
@click.option("--all-ops", is_flag=True,
              help="Replay all op types (default: bash only).")
@click.option("--ramp-delay", default=0.0, show_default=True,
              help="Seconds between successive sandbox launches (ramp-up).")
@click.option("--data-dir", default=None)
@click.option("--results-dir", default=None)
def run_stress_test(
    n_traj: int,
    concurrency: int | None,
    template_id: str | None,
    all_ops: bool,
    ramp_delay: float,
    data_dir: str | None,
    results_dir: str | None,
):
    """Replay trajectories concurrently in E2B sandboxes and collect metrics."""
    from config import Config
    from src.trajectory_parser import TrajectoryParser
    from src.stress_tester import (
        StressTester, StressTestConfig, EventType, ProgressEvent,
    )

    cfg = Config()
    data_dir = data_dir or cfg.data_dir
    results_dir = results_dir or cfg.results_dir
    max_concurrent = concurrency or cfg.max_concurrent_sandboxes

    # ---- load trajectories
    trajs_path = Path(data_dir) / "trajectories.json"
    if not trajs_path.exists():
        console.print("[red]trajectories.json not found. Run `download-data` first.[/]")
        sys.exit(1)

    with open(trajs_path) as f:
        raw_trajs: list[dict] = json.load(f)

    if n_traj > 0:
        raw_trajs = raw_trajs[:n_traj]

    console.print(f"Loaded {len(raw_trajs)} trajectories from {trajs_path}")

    # ---- parse trajectories
    parser = TrajectoryParser()
    with console.status("Parsing trajectories..."):
        parsed = parser.parse_many(raw_trajs)

    bash_counts = [len(parser.bash_ops_only(p)) for p in parsed]
    console.print(
        f"Parsed {len(parsed)} trajectories  "
        f"| bash ops/traj: min={min(bash_counts, default=0)} "
        f"avg={sum(bash_counts)//max(len(bash_counts),1)} "
        f"max={max(bash_counts, default=0)}"
    )

    # ---- load template mapping (if available)
    mapping_path = Path(data_dir) / "template_mapping.json"
    template_mapping: dict[str, str] = {}
    if mapping_path.exists():
        with open(mapping_path) as f:
            template_mapping = json.load(f)
        console.print(f"Loaded template mapping with {len(template_mapping)} entries")

    if not template_mapping and not template_id:
        console.print(
            "[yellow]No template mapping and no --template-id. "
            "Falling back to 'base'. Run `build-templates` for proper isolation.[/]"
        )

    # ---- set E2B env vars before entering asyncio
    os.environ.setdefault("E2B_API_KEY", cfg.e2b_api_key)
    os.environ.setdefault("E2B_API_URL", cfg.e2b_api_url)

    # ---- configure stress test
    stress_cfg = StressTestConfig(
        template_id=template_id or "base",
        api_key=cfg.e2b_api_key,
        api_url=cfg.e2b_api_url,
        max_concurrent=max_concurrent,
        sandbox_timeout=cfg.sandbox_timeout,
        command_timeout=cfg.command_timeout,
        bash_only=not all_ops,
        ramp_up_delay_s=ramp_delay,
        results_dir=results_dir,
    )

    # ---- set up real-time progress display
    from rich.live import Live
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

    total = len(parsed)
    counters = {"done": 0, "ok": 0, "fail": 0, "active": 0}

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("[green]{task.fields[ok]}[/] ok  [red]{task.fields[fail]}[/] fail  [cyan]{task.fields[active]}[/] active"),
        TimeElapsedColumn(),
        console=console,
    )
    task_id = progress.add_task(
        f"Stress Test ({total} traj, c={max_concurrent})",
        total=total, ok=0, fail=0, active=0,
    )

    def on_progress(event: ProgressEvent):
        if event.event_type == EventType.SANDBOX_CREATING:
            counters["active"] += 1
        elif event.event_type == EventType.TRAJECTORY_DONE:
            counters["done"] += 1
            counters["ok"] += 1
            counters["active"] = max(0, counters["active"] - 1)
        elif event.event_type == EventType.TRAJECTORY_ERROR:
            counters["done"] += 1
            counters["fail"] += 1
            counters["active"] = max(0, counters["active"] - 1)
        elif event.event_type == EventType.SHUTDOWN:
            progress.update(task_id, description="[bold yellow]Shutting down...")
            return
        progress.update(
            task_id,
            completed=counters["done"],
            ok=counters["ok"],
            fail=counters["fail"],
            active=counters["active"],
        )

    tester = StressTester(stress_cfg)
    tester.set_progress_callback(on_progress)

    console.print(Panel(
        f"[bold]Stress Test Starting[/bold]\n"
        f"  Trajectories : {total}\n"
        f"  Concurrency  : {max_concurrent}\n"
        f"  Bash only    : {not all_ops}\n"
        f"  Ramp delay   : {ramp_delay}s\n"
        f"  E2B API URL  : {cfg.e2b_api_url}",
        title="Config",
        border_style="cyan",
    ))

    # ---- run with live progress
    async def _run():
        return await tester.run(parsed, template_mapping or None)

    try:
        with Live(progress, console=console, refresh_per_second=4):
            report = asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted. Cleaning up sandboxes...[/]")
        asyncio.run(tester.shutdown())
        sys.exit(130)

    # ---- save report
    Path(results_dir).mkdir(parents=True, exist_ok=True)
    report_path = str(Path(results_dir) / f"report_{_timestamp()}.json")
    report.save(report_path)

    # ---- print summary
    _print_report(report)
    console.print(f"\n[green]✓[/] Full report saved to {report_path}")


def _print_report(report):
    """Print a comprehensive stress test summary."""
    grid = Table.grid(expand=False, padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column()

    grid.add_row("Trajectories", f"{report.total_trajectories}")
    grid.add_row(
        "Success / Failed",
        f"[green]{report.successful_trajectories}[/] / [red]{report.failed_trajectories}[/]",
    )
    grid.add_row("Total commands", str(report.total_commands))
    total_cmds = max(report.total_commands, 1)
    grid.add_row(
        "Failed commands",
        f"[red]{report.failed_commands}[/] ({100 * report.failed_commands // total_cmds}%)",
    )
    grid.add_row("Avg trajectory", f"{report.avg_trajectory_s:.2f}s")
    grid.add_row("Throughput", f"{report.throughput_traj_per_min:.1f} traj/min")
    grid.add_row("", "")
    grid.add_row("Sandbox create p50/p95/p99",
                 f"{report.p50_create_s:.3f}s / {report.p95_create_s:.3f}s / {report.p99_create_s:.3f}s")
    grid.add_row("Command latency p50/p95/p99",
                 f"{report.p50_cmd_s:.3f}s / {report.p95_cmd_s:.3f}s / {report.p99_cmd_s:.3f}s")

    console.print(Panel(grid, title="[bold]Stress Test Results[/bold]", border_style="green"))

    # ---- per-op-type breakdown
    op_stats = getattr(report, "per_op_type_stats", None) or {}
    if op_stats:
        t = Table(title="Per-Op-Type Breakdown", show_lines=False, padding=(0, 1))
        t.add_column("Op Type", style="bold")
        t.add_column("Count", justify="right")
        t.add_column("Failed", justify="right", style="red")
        t.add_column("p50 (s)", justify="right")
        t.add_column("p95 (s)", justify="right")
        t.add_column("p99 (s)", justify="right")
        for op_type, stats in sorted(op_stats.items()):
            t.add_row(
                op_type,
                str(stats.get("count", 0)),
                str(stats.get("failed", 0)),
                f"{stats.get('p50_s', 0):.3f}",
                f"{stats.get('p95_s', 0):.3f}",
                f"{stats.get('p99_s', 0):.3f}",
            )
        console.print(t)

    # ---- error categories
    err_cats = getattr(report, "error_categories", None) or {}
    if err_cats:
        t = Table(title="Error Categories", show_lines=False, padding=(0, 1))
        t.add_column("Category", style="bold red")
        t.add_column("Count", justify="right")
        for cat, count in sorted(err_cats.items(), key=lambda x: -x[1]):
            t.add_row(cat, str(count))
        console.print(t)


# ===========================================================================
#  show-report
# ===========================================================================

@cli.command("show-report")
@click.argument("report_path")
def show_report(report_path: str):
    """Pretty-print a saved JSON stress test report."""
    with open(report_path) as f:
        data = json.load(f)

    from src.stress_tester import StressTestReport
    report = StressTestReport.from_dict(data)
    _print_report(report)

    # Per-sandbox table from raw summary dicts
    rows = getattr(report, "_sandbox_summaries", [])
    if rows:
        t = Table(title="Per-Sandbox Summary", show_lines=False)
        t.add_column("instance_id", style="dim", max_width=40)
        t.add_column("template_id", max_width=20)
        t.add_column("create_s", justify="right")
        t.add_column("total_s", justify="right")
        t.add_column("cmds", justify="right")
        t.add_column("failed", justify="right")
        t.add_column("error", max_width=40, style="red")

        for row in rows[:50]:  # cap display
            t.add_row(
                row.get("instance_id", ""),
                (row.get("template_id") or "")[:20],
                f"{row.get('sandbox_create_s', 0):.2f}",
                f"{row.get('total_duration_s', 0):.2f}",
                str(row.get("n_commands", 0)),
                str(row.get("n_failed_commands", 0)),
                (row.get("error") or "")[:40],
            )
        console.print(t)
        if len(rows) > 50:
            console.print(f"  [dim]... and {len(rows) - 50} more (see full report JSON)[/]")


# ===========================================================================
#  entry
# ===========================================================================

if __name__ == "__main__":
    cli()
