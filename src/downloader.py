"""
Download SWE-rebench datasets from HuggingFace.

Datasets:
  - nebius/SWE-rebench: task instances with install_config
  - nebius/SWE-rebench-openhands-trajectories: agent trajectories
"""

from __future__ import annotations

import os
import ssl
import httpx

os.environ["HF_HUB_DISABLE_SSL_VERIFICATION"] = "1"

# monkey-patch httpx Client，强制 verify=False
_original_init = httpx.Client.__init__
def _patched_init(self, *args, **kwargs):
    kwargs["verify"] = False
    _original_init(self, *args, **kwargs)
httpx.Client.__init__ = _patched_init

_original_async_init = httpx.AsyncClient.__init__
def _patched_async_init(self, *args, **kwargs):
    kwargs["verify"] = False
    _original_async_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_init

import json
import logging
from pathlib import Path
from typing import Iterator

from datasets import load_dataset, DownloadConfig
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

logger = logging.getLogger(__name__)


class DatasetDownloader:
    TASKS_DATASET = "nebius/SWE-rebench"
    TRAJECTORIES_DATASET = "nebius/SWE-rebench-openhands-trajectories"

    def __init__(self, data_dir: str = "./data", hf_token: str = ""):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.hf_token = hf_token or None

    def _load_dataset(self, name: str, streaming: bool = True, n_samples: int = 0):
        kwargs = dict(
            path=name,
            split="train",
            streaming=streaming,
        )
        if self.hf_token:
            kwargs["token"] = self.hf_token
        ds = load_dataset(**kwargs)
        if n_samples > 0:
            ds = ds.take(n_samples)
        return ds

    # ------------------------------------------------------------------ tasks

    def stream_tasks(self, n_samples: int = 0) -> Iterator[dict]:
        """Stream task instances from SWE-rebench."""
        logger.info("Streaming tasks from %s (n=%s)", self.TASKS_DATASET, n_samples or "all")
        yield from self._load_dataset(self.TASKS_DATASET, streaming=True, n_samples=n_samples)

    def download_tasks(self, n_samples: int = 0) -> list[dict]:
        """Download and cache task instances as a JSON file."""
        cache_path = self.data_dir / "tasks.json"
        if cache_path.exists():
            logger.info("Loading cached tasks from %s", cache_path)
            with open(cache_path) as f:
                tasks = json.load(f)
            if n_samples > 0:
                tasks = tasks[:n_samples]
            return tasks

        tasks = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            transient=True,
        ) as progress:
            task_id = progress.add_task("Downloading tasks...", total=None)
            for row in self.stream_tasks(n_samples):
                tasks.append(self._clean_task(row))
                progress.advance(task_id)

        with open(cache_path, "w") as f:
            json.dump(tasks, f, indent=2)
        logger.info("Saved %d tasks to %s", len(tasks), cache_path)
        return tasks

    @staticmethod
    def _clean_task(row: dict) -> dict:
        """Normalise install_config: remove None-valued keys inserted by HF."""
        task = dict(row)
        ic = task.get("install_config")
        if isinstance(ic, dict):
            task["install_config"] = {k: v for k, v in ic.items() if v is not None}
        return task

    # ------------------------------------------------------------- trajectories

    def stream_trajectories(self, n_samples: int = 0) -> Iterator[dict]:
        """Stream agent trajectories."""
        logger.info(
            "Streaming trajectories from %s (n=%s)",
            self.TRAJECTORIES_DATASET,
            n_samples or "all",
        )
        yield from self._load_dataset(
            self.TRAJECTORIES_DATASET, streaming=True, n_samples=n_samples
        )

    def download_trajectories(self, n_samples: int = 0) -> list[dict]:
        """Download and cache trajectories as a JSON file."""
        cache_path = self.data_dir / "trajectories.json"
        if cache_path.exists():
            logger.info("Loading cached trajectories from %s", cache_path)
            with open(cache_path) as f:
                trajs = json.load(f)
            if n_samples > 0:
                trajs = trajs[:n_samples]
            return trajs

        trajs = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            transient=True,
        ) as progress:
            task_id = progress.add_task("Downloading trajectories...", total=None)
            for row in self.stream_trajectories(n_samples):
                trajs.append(row)
                progress.advance(task_id)

        with open(cache_path, "w") as f:
            json.dump(trajs, f, indent=2)
        logger.info("Saved %d trajectories to %s", len(trajs), cache_path)
        return trajs

    # ------------------------------------------------------------------ join

    def build_task_index(self, tasks: list[dict]) -> dict[str, dict]:
        """Build an index of tasks keyed by instance_id."""
        return {t["instance_id"]: t for t in tasks if "instance_id" in t}

    def join(
        self,
        tasks: list[dict],
        trajectories: list[dict],
    ) -> list[dict]:
        """
        Join trajectories with their corresponding task (install_config etc.).
        Returns a list of dicts containing both trajectory and task fields.
        """
        index = self.build_task_index(tasks)
        joined = []
        for traj in trajectories:
            iid = traj.get("instance_id")
            task = index.get(iid, {})
            joined.append({**traj, "task": task})
        return joined
