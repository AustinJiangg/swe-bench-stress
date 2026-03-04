"""
Build E2B sandbox templates from SWE-rebench install_config.

Strategy
--------
1. Group tasks by (install_config + repo + base_commit) fingerprint.
2. Generate a Dockerfile for each unique combination.
3. Build and register templates via the E2B Python SDK or E2B CLI.
4. Cache template IDs locally so rebuilds are skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from e2b import Template, default_build_logger
from packaging.version import parse as parse_version

load_dotenv()

logger = logging.getLogger(__name__)

HEREDOC = "EOF"
ENV = "testbed"
RAW_URL = "https://raw.githubusercontent.com"

# --------------------------------------------------------------------------- #
#  Dockerfile generation                                                        #
# --------------------------------------------------------------------------- #


def generate_dockerfile(
    instance: dict[str, Any],
) -> str:
    specs = {k: v for k, v in (instance.get("install_config") or {}).items() if v is not None}
    repo = instance["repo"]
    commit = instance["base_commit"]
    env_commit = instance.get("environment_setup_commit", commit)
    py = str(specs.get("python", "3.9"))
    if parse_version(py) < parse_version("3.6"):
        py = "3.6"
    pkgs = specs.get("packages", "requirements.txt")
    pip_extra = specs.get("pip_packages") or []
    pre_install = specs.get("pre_install") or []
    install = specs.get("install", "")
    env_vars = specs.get("env_vars") or {}
    no_use_env = specs.get("no_use_env", False)
    env_yml = instance.get("environment", "")
    reqs = instance.get("requirements", "")
    reqs_clean = "\n".join(l for l in reqs.splitlines() if l.strip() and not l.strip().startswith("-e "))
    act = "" if no_use_env else f". /opt/miniconda3/bin/activate {ENV} && "

    base_image = instance.get("base_image", "")
    lines = [f"FROM {base_image}", ""]

    for k, v in env_vars.items():
        lines.append(f"ENV {k}={v}")
    if env_vars:
        lines.append("")

    # ── ENV LAYER ──────────────────────────────────────────────
    if no_use_env:
        if reqs_clean:
            lines += [f"COPY <<'{HEREDOC}' /tmp/reqs.txt", reqs_clean, HEREDOC,
                       "RUN pip install --no-cache-dir -r /tmp/reqs.txt && rm /tmp/reqs.txt", ""]
        if pip_extra:
            lines += [f"RUN pip install --no-cache-dir {' '.join(pip_extra)}", ""]
    elif pkgs == "environment.yml":
        if env_yml:
            yml = re.sub(r"^name\s*:.*", f"name: {ENV}", env_yml, count=1, flags=re.M)
            yml = re.sub(r"^prefix\s*:.*\n?", "", yml, flags=re.M)
            lines += [f"COPY <<'{HEREDOC}' /tmp/env.yml", yml.strip(), HEREDOC,
                       f"RUN /opt/miniconda3/bin/conda env create -f /tmp/env.yml && "
                       f"/opt/miniconda3/bin/conda clean -afy && rm /tmp/env.yml", ""]
        else:
            for p in (specs.get("env_yml_path") or ["environment.yml"]):
                url = f"{RAW_URL}/{repo}/{env_commit}/{p}"
                lines += [f"RUN curl -fsSL {url} -o /tmp/env.yml && "
                           f"sed -i 's/^name:.*/name: {ENV}/' /tmp/env.yml && "
                           f"/opt/miniconda3/bin/conda env create -f /tmp/env.yml && "
                           f"/opt/miniconda3/bin/conda clean -afy && rm /tmp/env.yml || true"]
            lines.append("")
        if pip_extra:
            lines += [f"RUN {act}pip install --no-cache-dir {' '.join(pip_extra)}", ""]
    elif pkgs == "requirements.txt":
        lines += [f"RUN /opt/miniconda3/bin/conda create -n {ENV} python={py} -y && "
                   f"/opt/miniconda3/bin/conda clean -afy", ""]
        if reqs_clean:
            lines += [f"COPY <<'{HEREDOC}' /tmp/reqs.txt", reqs_clean, HEREDOC,
                       f"RUN {act}pip install --no-cache-dir -r /tmp/reqs.txt && rm /tmp/reqs.txt", ""]
        else:
            for rp in (specs.get("reqs_path") or ["requirements.txt"]):
                url = f"{RAW_URL}/{repo}/{env_commit}/{rp}"
                lines += [f"RUN {act}curl -fsSL {url} -o /tmp/reqs.txt && "
                           f"pip install --no-cache-dir -r /tmp/reqs.txt && rm /tmp/reqs.txt || true"]
            lines.append("")
        if pip_extra:
            lines += [f"RUN {act}pip install --no-cache-dir {' '.join(pip_extra)}", ""]
    else:  # inline pip packages string
        lines += [f"RUN /opt/miniconda3/bin/conda create -n {ENV} python={py} -y && "
                   f"/opt/miniconda3/bin/conda clean -afy", ""]
        if pkgs.strip():
            lines += [f"RUN {act}pip install --no-cache-dir {pkgs}", ""]
        if pip_extra:
            lines += [f"RUN {act}pip install --no-cache-dir {' '.join(pip_extra)}", ""]

    # ── INSTANCE LAYER ─────────────────────────────────────────
    repo_alt = repo.replace("/", "__")
    lines += [
        f"RUN git clone https://github.com/{repo}.git /{ENV} 2>/dev/null || "
        f"git clone https://github.com/SWE-bench-repos/{repo_alt}.git /{ENV}",
        f"WORKDIR /{ENV}",
        f"RUN git -c advice.detachedHead=false checkout {commit}", "",
    ]
    for cmd in pre_install:
        lines.append(f"RUN {act}{cmd}")
    if pre_install:
        lines.append("")
    if install:
        lines += [f"RUN {act}{install}", ""]
    lines.append("RUN git diff --name-only | xargs -r git checkout --")
    return "\n".join(lines)


def fingerprint_install_config(
    install_config: dict, repo: str = "", base_commit: str = ""
) -> str:
    """Return a short hash identifying a (install_config, repo, base_commit) combination."""
    canonical = json.dumps(
        {"config": install_config, "repo": repo, "commit": base_commit},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class TemplateCache:
    """Persist fingerprint → template_id mappings in a JSON file."""

    def __init__(self, cache_file: str = "./data/template_cache.json"):
        self.path = Path(cache_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, str] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path) as f:
                self._data = json.load(f)

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, fp: str) -> Optional[str]:
        return self._data.get(fp)

    def set(self, fp: str, template_id: str):
        self._data[fp] = template_id
        self._save()


def group_tasks_by_config(tasks: list[dict]) -> dict[str, dict]:
    """Group tasks by (install_config, repo, base_commit) fingerprint.

    Returns::

        {fingerprint: {"config": {…}, "tasks": [instance_id, …],
                       "python": "3.x", "repo": "owner/repo",
                       "base_commit": "abc123",
                       "environment_setup_commit": "...",
                       "environment": "...", "requirements": "..."}}
    """
    groups: dict[str, dict] = {}
    for task in tasks:
        ic = task.get("install_config") or {}
        repo = task.get("repo", "")
        base_commit = task.get("base_commit", "")
        fp = fingerprint_install_config(ic, repo, base_commit)
        if fp not in groups:
            groups[fp] = {
                "config": ic,
                "tasks": [],
                "python": str(ic.get("python", "")),
                "repo": repo,
                "base_commit": base_commit,
                "environment_setup_commit": task.get("environment_setup_commit", base_commit),
                "environment": task.get("environment", ""),
                "requirements": task.get("requirements", ""),
            }
        groups[fp]["tasks"].append(task.get("instance_id", "unknown"))
    return groups


class E2BTemplateBuilder:
    """Build and register E2B templates from install_config dicts."""

    def __init__(
        self,
        base_image: str,
        cache_file: str = "./data/template_cache.json",
        strategy: str = "sdk",  # "cli" | "sdk"
        cpu_count: int = 1,
        memory_mb: int = 1024,
    ):
        self.base_image = base_image
        self.cache = TemplateCache(cache_file)
        self.strategy = strategy
        self.cpu_count = cpu_count
        self.memory_mb = memory_mb

    def _make_instance(self, group: dict) -> dict[str, Any]:
        """Build the instance dict that generate_dockerfile expects from a group entry."""
        return {
            "install_config": group["config"],
            "repo": group["repo"],
            "base_commit": group["base_commit"],
            "environment_setup_commit": group.get("environment_setup_commit", group["base_commit"]),
            "environment": group.get("environment", ""),
            "requirements": group.get("requirements", ""),
            "base_image": self.base_image,
        }

    def get_or_build(self, group: dict, name_prefix: str = "swe") -> str:
        fp = fingerprint_install_config(group["config"], group["repo"], group["base_commit"])
        cached = self.cache.get(fp)
        if cached:
            logger.info("Template cache hit: %s -> %s", fp, cached)
            return cached

        dockerfile = generate_dockerfile(self._make_instance(group))
        template_name = f"{name_prefix}-{fp}"
        logger.info("Building new template: %s", template_name)

        if self.strategy == "cli":
            template_id = self._build_via_cli(dockerfile, template_name)
        else:
            template_id = self._build_via_sdk(dockerfile, template_name)

        self.cache.set(fp, template_id)
        logger.info("Template built: %s -> %s", template_name, template_id)
        return template_id

    def get_or_build_batch(self, tasks: list[dict], name_prefix: str = "swe") -> dict[str, str]:
        groups = group_tasks_by_config(tasks)
        logger.info(
            "Grouped %d tasks into %d unique configs",
            len(tasks), len(groups),
        )

        result: dict[str, str] = {}
        for fp, group in groups.items():
            try:
                tid = self.get_or_build(group, name_prefix)
            except Exception as exc:
                logger.warning("Failed to build template for %s: %s", fp, exc)
                tid = ""
            for iid in group["tasks"]:
                result[iid] = tid

        return result

    def _build_via_cli(self, dockerfile: str, template_name: str) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            df_path = Path(tmpdir) / "e2b.Dockerfile"
            df_path.write_text(dockerfile)

            cmd = [
                "e2b",
                "template",
                "build",
                "--name", template_name,
                "--dockerfile", "e2b.Dockerfile",
                "--cpu-count", str(self.cpu_count),
                "--memory-mb", str(self.memory_mb),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpdir, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(
                    f"e2b template build failed:\n{result.stdout}\n{result.stderr}"
                )

            for line in result.stdout.splitlines():
                if "template" in line.lower() and "id" in line.lower():
                    parts = line.split(":")
                    if len(parts) >= 2:
                        return parts[-1].strip()

            raise RuntimeError(f"Could not parse template ID from output:\n{result.stdout}")

    def _build_via_sdk(self, dockerfile: str, template_name: str) -> str:
        template = Template().from_dockerfile(dockerfile)

        build_info = Template.build(
            template,
            alias=template_name,
            cpu_count=self.cpu_count,
            memory_mb=self.memory_mb,
            on_build_logs=default_build_logger(),
        )

        template_id = getattr(build_info, "template_id", "")
        if not template_id:
            raise RuntimeError(f"No template_id in SDK response: {build_info}")
        return template_id

    def export_dockerfile(self, group: dict, output_path: str) -> str:
        dockerfile = generate_dockerfile(self._make_instance(group))
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dockerfile)
        logger.info("Dockerfile written to %s", path)
        return str(path)
