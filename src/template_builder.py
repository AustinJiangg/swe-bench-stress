"""
Build E2B sandbox templates from SWE-rebench install_config.

Strategy
--------
1. Generate a Dockerfile for each unique install_config fingerprint.
2. Build and register templates via the E2B Python SDK or E2B CLI.
3. Cache template IDs locally so rebuilds are skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from e2b import Template, default_build_logger

load_dotenv()

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  Dockerfile generation                                                        #
# --------------------------------------------------------------------------- #

_DOCKERFILE_HEADER = """\
# syntax=docker/dockerfile:1
# Auto-generated from SWE-rebench install_config
"""


def _list_val(v) -> list[str]:
    """Normalise a field that can be None, str, or list[str]."""
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [x for x in v if x]


def generate_dockerfile(install_config: dict, base_image: str) -> str:
    """
    Translate an install_config dict into a Dockerfile string.

    install_config fields handled:
      - python        : target Python version for conda
      - env_vars      : ENV directives
      - pre_install   : RUN commands before package installation
      - packages      : conda packages
      - pip_packages  : pip packages
      - reqs_path     : requirements files (skipped – source not available)
      - env_yml_path  : conda env yml files (skipped – source not available)
      - install       : final install command (e.g. "pip install -e .")
      - no_use_env    : if True, skip conda env activation
      - test_cmd      : stored as a label for reference
    """
    lines: list[str] = [
        _DOCKERFILE_HEADER,
        f"FROM {base_image}",
        "",
        "ENV DEBIAN_FRONTEND=noninteractive",
        'SHELL ["/bin/bash", "-c"]',
        "",
    ]

    env_vars = install_config.get("env_vars") or {}
    if isinstance(env_vars, dict):
        for k, v in env_vars.items():
            if k and v is not None:
                lines.append(f"ENV {k}={v}")
        if env_vars:
            lines.append("")

    for cmd in _list_val(install_config.get("pre_install")):
        lines.append(f"RUN {cmd}")
    if _list_val(install_config.get("pre_install")):
        lines.append("")

    python_ver = install_config.get("python")
    if python_ver:
        safe_ver = str(python_ver).strip()
        lines += [
            f"RUN conda install -y python={safe_ver} -q && conda clean -ay || \\",
            f"    echo 'conda python={safe_ver} failed, using system python'",
            "",
        ]

    packages = (install_config.get("packages") or "").strip()
    if packages:
        lines += [
            f"RUN conda install -y {packages} -q && conda clean -ay || \\",
            f"    pip install --no-cache-dir {packages}",
            "",
        ]

    pip_pkgs = _list_val(install_config.get("pip_packages"))
    if pip_pkgs:
        joined = " ".join(pip_pkgs)
        lines += [
            f"RUN pip install --no-cache-dir {joined}",
            "",
        ]

    install_cmd = (install_config.get("install") or "").strip()
    if install_cmd:
        if any(x in install_cmd for x in ["pip install -e", "python setup.py"]):
            lines += [
                "# NOTE: editable install deferred to runtime (requires repo source)",
                f"# RUN {install_cmd}",
                "",
            ]
        else:
            lines += [f"RUN {install_cmd} || true", ""]

    test_cmd = install_config.get("test_cmd", "")
    if test_cmd:
        lines.append(f'LABEL swe.test_cmd="{test_cmd}"')

    lines.append("")
    return "\n".join(lines)


def fingerprint_install_config(install_config: dict) -> str:
    """Return a short hash that uniquely identifies an install_config."""
    canonical = json.dumps(install_config, sort_keys=True, default=str)
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
    """Group tasks by install_config fingerprint.

    Returns::

        {fingerprint: {"config": {…}, "tasks": [instance_id, …], "python": "3.x"}}
    """
    groups: dict[str, dict] = {}
    for task in tasks:
        ic = task.get("install_config") or {}
        fp = fingerprint_install_config(ic)
        if fp not in groups:
            groups[fp] = {
                "config": ic,
                "tasks": [],
                "python": str(ic.get("python", "")),
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

    def get_or_build(self, install_config: dict, name_prefix: str = "swe") -> str:
        fp = fingerprint_install_config(install_config)
        cached = self.cache.get(fp)
        if cached:
            logger.info("Template cache hit: %s -> %s", fp, cached)
            return cached

        dockerfile = generate_dockerfile(install_config, self.base_image)
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
            "Grouped %d tasks into %d unique install_configs",
            len(tasks), len(groups),
        )

        result: dict[str, str] = {}
        for fp, group in groups.items():
            try:
                tid = self.get_or_build(group["config"], name_prefix)
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
                "--dockerfile", str(df_path),
                "--yes",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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

    def export_dockerfile(self, install_config: dict, output_path: str) -> str:
        dockerfile = generate_dockerfile(install_config, self.base_image)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dockerfile)
        logger.info("Dockerfile written to %s", path)
        return str(path)
