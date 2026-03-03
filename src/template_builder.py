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
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from e2b import Template

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

    # -- environment variables
    env_vars = install_config.get("env_vars") or {}
    if isinstance(env_vars, dict):
        for k, v in env_vars.items():
            if k and v is not None:
                lines.append(f"ENV {k}={v}")
        if env_vars:
            lines.append("")

    # -- pre-install commands (system deps, apt packages, etc.)
    for cmd in _list_val(install_config.get("pre_install")):
        lines.append(f"RUN {cmd}")
    if _list_val(install_config.get("pre_install")):
        lines.append("")

    # -- Python version via conda (base env)
    python_ver = install_config.get("python")
    if python_ver:
        safe_ver = str(python_ver).strip()
        lines += [
            f"RUN conda install -y python={safe_ver} -q && conda clean -ay || \\",
            f"    echo 'conda python={safe_ver} failed, using system python'",
            "",
        ]

    # -- conda packages
    packages = (install_config.get("packages") or "").strip()
    if packages:
        lines += [
            f"RUN conda install -y {packages} -q && conda clean -ay || \\",
            f"    pip install --no-cache-dir {packages}",
            "",
        ]

    # -- pip packages
    pip_pkgs = _list_val(install_config.get("pip_packages"))
    if pip_pkgs:
        joined = " ".join(pip_pkgs)
        lines += [
            f"RUN pip install --no-cache-dir {joined}",
            "",
        ]

    # -- final install (skip editable installs that require source code)
    install_cmd = (install_config.get("install") or "").strip()
    if install_cmd:
        # editable installs require the actual repo; skip or make conditional
        if any(x in install_cmd for x in ["pip install -e", "python setup.py"]):
            lines += [
                "# NOTE: editable install deferred to runtime (requires repo source)",
                f"# RUN {install_cmd}",
                "",
            ]
        else:
            lines += [f"RUN {install_cmd} || true", ""]

    # -- label for traceability
    test_cmd = install_config.get("test_cmd", "")
    if test_cmd:
        lines.append(f'LABEL swe.test_cmd="{test_cmd}"')

    lines.append("")
    return "\n".join(lines)


def fingerprint_install_config(install_config: dict) -> str:
    """Return a short hash that uniquely identifies an install_config."""
    canonical = json.dumps(install_config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
#  E2B Template Registry (local cache)                                         #
# --------------------------------------------------------------------------- #

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

    def __len__(self):
        return len(self._data)


# --------------------------------------------------------------------------- #
#  E2B Template Builder                                                         #
# --------------------------------------------------------------------------- #

class E2BTemplateBuilder:
    """
    Build and register E2B templates from install_config dicts.

    Two build strategies are supported:

    1. **e2b-cli** (default): writes an e2b.Dockerfile to a temp dir and
       calls `e2b template build` as a subprocess.  Requires the `e2b` CLI
       tool to be installed and configured with E2B_DOMAIN / E2B_API_KEY.

    2. **sdk**: uses the E2B Python SDK (`Template.build`).
       This is the recommended programmatic strategy.
    """

    def __init__(
        self,
        api_key: str,
        domain: str,
        base_image: str,
        cache_file: str = "./data/template_cache.json",
        strategy: str = "sdk",  # "cli" | "sdk"
    ):
        self.api_key = api_key
        self.domain = domain
        self.base_image = base_image
        self.cache = TemplateCache(cache_file)
        self.strategy = strategy

        # Ensure SDK env vars are set for any subprocess calls
        os.environ.setdefault("E2B_API_KEY", api_key)
        os.environ.setdefault("E2B_DOMAIN", domain)

    # --------------------------------------------------------- public API

    def get_or_build(self, install_config: dict, name_prefix: str = "swe") -> str:
        """
        Return a template_id for the given install_config.
        Builds the template if it is not already cached.
        """
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

    def get_or_build_batch(
        self, tasks: list[dict], name_prefix: str = "swe"
    ) -> dict[str, str]:
        """
        Build templates for a list of tasks.
        Returns a mapping of instance_id -> template_id.
        """
        result: dict[str, str] = {}
        seen_fps: dict[str, str] = {}  # fingerprint -> template_id

        for task in tasks:
            iid = task.get("instance_id", "unknown")
            ic = task.get("install_config") or {}
            fp = fingerprint_install_config(ic)

            if fp in seen_fps:
                result[iid] = seen_fps[fp]
                continue

            try:
                tid = self.get_or_build(ic, name_prefix)
                seen_fps[fp] = tid
                result[iid] = tid
            except Exception as exc:
                logger.warning("Failed to build template for %s: %s", iid, exc)
                result[iid] = ""

        return result

    # --------------------------------------------------------- CLI strategy

    def _build_via_cli(self, dockerfile: str, template_name: str) -> str:
        """Build a template by shelling out to `e2b template build`."""
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
            env = {**os.environ, "E2B_API_KEY": self.api_key, "E2B_DOMAIN": self.domain}
            result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)

            if result.returncode != 0:
                raise RuntimeError(
                    f"e2b template build failed:\n{result.stdout}\n{result.stderr}"
                )

            # Parse template ID from CLI output (format: "Template ID: <id>")
            for line in result.stdout.splitlines():
                if "template" in line.lower() and "id" in line.lower():
                    parts = line.split(":")
                    if len(parts) >= 2:
                        return parts[-1].strip()

            raise RuntimeError(f"Could not parse template ID from output:\n{result.stdout}")

    # --------------------------------------------------------- SDK strategy

    def _build_via_sdk(self, dockerfile: str, template_name: str) -> str:
        """
        Build a template via the E2B Python SDK.

        The SDK encapsulates backend API details and returns typed build info.
        """
        template = Template().from_dockerfile(dockerfile)
        build_info = Template.build(
            template,
            alias=template_name,
            api_key=self.api_key,
            domain=self.domain,
        )
        template_id = getattr(build_info, "template_id", "")
        if not template_id:
            raise RuntimeError(f"No template_id in SDK response: {build_info}")
        return template_id

    # --------------------------------------------------------- Dockerfile export

    def export_dockerfile(
        self, install_config: dict, output_path: str
    ) -> str:
        """Write a Dockerfile for the given install_config and return its path."""
        dockerfile = generate_dockerfile(install_config, self.base_image)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dockerfile)
        logger.info("Dockerfile written to %s", path)
        return str(path)
