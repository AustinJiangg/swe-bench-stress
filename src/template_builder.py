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

from e2b import Template, default_build_logger
from packaging.version import parse as parse_version

from config import config

logger = logging.getLogger(__name__)

ENV = "testbed"
RAW_URL = "https://raw.githubusercontent.com"
CONDA_TOS = (
    "RUN conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main "
    "&& conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r"
)

# --------------------------------------------------------------------------- #
#  Dockerfile generation                                                        #
# --------------------------------------------------------------------------- #


def _printf_file(content: str, dest: str) -> str:
    """Generate a ``RUN printf '%s\\n' ... > dest`` instruction from *content*."""
    content_lines = [l for l in content.splitlines() if l.strip()]
    if not content_lines:
        return ""
    escaped = [f"    '{l.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for l in content_lines]
    body = " \\\n".join(escaped)
    return f"RUN printf '%s\\n' \\\n{body} \\\n    > {dest}"


def _strip_apt_update(cmd: str) -> str:
    """Remove ``apt-get update`` from *cmd* while keeping other chained commands."""
    # "apt-get update && apt-get install ..." → "apt-get install ..."
    cleaned = re.sub(r"apt-get\s+update\s*&&\s*", "", cmd)
    # standalone "apt-get update"
    cleaned = re.sub(r"^\s*apt-get\s+update\s*$", "", cleaned)
    return cleaned.strip()


def generate_dockerfile(
    instance: dict[str, Any],
    base_image: str = "ubuntu:22.04-swe-base",
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
    act = "" if no_use_env else f". activate {ENV} && "

    lines = [f"FROM {base_image}", ""]

    # ── PROXY ENV ─────────────────────────────────────────────
    if config.http_proxy:
        lines.append(f"ENV http_proxy={config.http_proxy}")
    if config.https_proxy:
        lines.append(f"ENV https_proxy={config.https_proxy}")
    if config.no_proxy:
        lines.append(f"ENV no_proxy={config.no_proxy}")
    if config.http_proxy or config.https_proxy or config.no_proxy:
        lines.append("")

    for k, v in env_vars.items():
        lines.append(f"ENV {k}={v}")
    if env_vars:
        lines.append("")

    # ── ENV LAYER ──────────────────────────────────────────────
    if no_use_env:
        if reqs_clean:
            lines += [_printf_file(reqs_clean, "/tmp/reqs.txt"),
                       "RUN pip install --no-cache-dir -r /tmp/reqs.txt && rm /tmp/reqs.txt", ""]
        if pip_extra:
            lines += [f"RUN pip install --no-cache-dir {' '.join(pip_extra)}", ""]
    elif pkgs == "environment.yml":
        lines += [CONDA_TOS, ""]
        if env_yml:
            yml = re.sub(r"^name\s*:.*", f"name: {ENV}", env_yml, count=1, flags=re.M)
            yml = re.sub(r"^prefix\s*:.*\n?", "", yml, flags=re.M)
            lines += [_printf_file(yml.strip(), "/tmp/env.yml"),
                       f"RUN conda env create -f /tmp/env.yml && "
                       f"conda clean -afy && rm /tmp/env.yml", ""]
        else:
            for p in (specs.get("env_yml_path") or ["environment.yml"]):
                url = f"{RAW_URL}/{repo}/{env_commit}/{p}"
                lines += [f"RUN curl -fsSL {url} -o /tmp/env.yml && "
                           f"sed -i 's/^name:.*/name: {ENV}/' /tmp/env.yml && "
                           f"conda env create -f /tmp/env.yml && "
                           f"conda clean -afy && rm /tmp/env.yml || true"]
            lines.append("")
        if pip_extra:
            lines += [f"RUN {act}pip install --no-cache-dir {' '.join(pip_extra)}", ""]
    elif pkgs == "requirements.txt":
        lines += [CONDA_TOS,
                   f"RUN conda create -n {ENV} python={py} -y && "
                   f"conda clean -afy", ""]
        if reqs_clean:
            lines += [_printf_file(reqs_clean, "/tmp/reqs.txt"),
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
        lines += [CONDA_TOS,
                   f"RUN conda create -n {ENV} python={py} -y && "
                   f"conda clean -afy", ""]
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
        cleaned = _strip_apt_update(cmd)
        if cleaned:
            lines.append(f"RUN {act}{cleaned}")
    if pre_install:
        lines.append("")
    if install:
        lines += [f"RUN {act}{install}", ""]
    lines.append("RUN git diff --name-only | xargs -r git checkout --")
    # Ensure non-root users (e.g. E2B sandbox default 'user') can access the repo
    lines.append(f"RUN chmod -R a+rwX /{ENV}")
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
        registry_username: str = "",
        registry_password: str = "",
        docker_registry_url: str = "localhost:5000",
        docker_registry_repo: str = "e2b",
    ):
        self.base_image = base_image
        self.cache = TemplateCache(cache_file)
        self.strategy = strategy
        self.cpu_count = cpu_count
        self.memory_mb = memory_mb
        self.registry_username = registry_username
        self.registry_password = registry_password
        self.docker_registry_url = docker_registry_url
        self.docker_registry_repo = docker_registry_repo

    def _make_instance(self, group: dict) -> dict[str, Any]:
        """Build the instance dict that generate_dockerfile expects from a group entry."""
        return {
            "install_config": group["config"],
            "repo": group["repo"],
            "base_commit": group["base_commit"],
            "environment_setup_commit": group.get("environment_setup_commit", group["base_commit"]),
            "environment": group.get("environment", ""),
            "requirements": group.get("requirements", ""),
        }

    @staticmethod
    def _make_image_name(name_prefix: str, repo: str, fp: str) -> str:
        """Build a descriptive Docker image name from repo and fingerprint.

        Example: ``swe-django__django-976d053c`` (prefix-repo-fingerprint8).
        """
        if repo:
            repo_slug = repo.replace("/", "__").replace(".", "-").lower()
            return f"{name_prefix}-{repo_slug}-{fp[:8]}"
        return f"{name_prefix}-{fp}"

    def get_or_build(self, group: dict, name_prefix: str = "swe") -> str:
        # Generate Dockerfile first so we can fingerprint the actual build content.
        # This ensures any config change (base_image, proxy, etc.) invalidates the cache.
        dockerfile = generate_dockerfile(self._make_instance(group), self.base_image)
        fp = hashlib.sha256(dockerfile.encode()).hexdigest()[:16]

        cached = self.cache.get(fp)
        if cached:
            logger.info("Template cache hit: %s -> %s", fp, cached)
            return cached

        # Use repo-based prefix for a readable image name
        repo_fp = fingerprint_install_config(group["config"], group["repo"], group["base_commit"])
        template_name = self._make_image_name(name_prefix, group["repo"], repo_fp)
        logger.info("Building new template: %s (dockerfile fingerprint: %s)", template_name, fp)

        # Step 1: Build Docker image and push to private registry
        image_ref = self._build_and_push_image(dockerfile, template_name)

        # Step 2: Create E2B template from the pushed image
        if self.strategy == "cli":
            template_id = self._build_via_cli(image_ref, template_name)
        else:
            template_id = self._build_via_sdk(image_ref, template_name)

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

    def _build_and_push_image(self, dockerfile: str, image_name: str) -> str:
        """Build a Docker image from *dockerfile* and push it to the private registry.

        Returns the full image reference (e.g. ``registry:5000/e2b/swe-django__django-abc123:latest``).
        """
        # Build local image with short name, then tag with full registry path for push
        local_tag = f"{image_name}:latest"
        image_ref = f"{self.docker_registry_url}/{self.docker_registry_repo}/{image_name}:latest"
        logger.info("Building Docker image: %s", image_ref)

        with tempfile.TemporaryDirectory() as tmpdir:
            df_path = Path(tmpdir) / "Dockerfile"
            df_path.write_text(dockerfile)

            # Build with local tag
            build_cmd = ["docker", "build", "-t", local_tag, "-f", str(df_path), tmpdir]
            logger.info("Running: %s", " ".join(build_cmd))
            proc = subprocess.Popen(
                build_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in proc.stdout:
                logger.info("[docker build] %s", line.rstrip())
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"docker build failed (exit {proc.returncode})"
                )
            logger.info("Docker image built successfully: %s", local_tag)

        # Tag with full registry path for push
        tag_cmd = ["docker", "tag", local_tag, image_ref]
        logger.info("Tagging image: %s -> %s", local_tag, image_ref)
        proc = subprocess.run(tag_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker tag failed (exit {proc.returncode}):\n{proc.stderr}"
            )

        # Login to registry if credentials are provided
        if self.registry_username and self.registry_password:
            login_cmd = [
                "docker", "login", self.docker_registry_url,
                "-u", self.registry_username,
                "--password-stdin",
            ]
            login_proc = subprocess.run(
                login_cmd, input=self.registry_password,
                capture_output=True, text=True,
            )
            if login_proc.returncode != 0:
                raise RuntimeError(
                    f"docker login failed (exit {login_proc.returncode}):\n{login_proc.stderr}"
                )

        # Push
        push_cmd = ["docker", "push", image_ref]
        logger.info("Pushing image: %s", image_ref)
        proc = subprocess.Popen(
            push_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout:
            logger.info("[docker push] %s", line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker push failed (exit {proc.returncode})"
            )
        logger.info("Image pushed successfully: %s", image_ref)

        return image_ref

    def _build_via_cli(self, image_ref: str, template_name: str) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal Dockerfile that pulls from the pre-built image
            df_path = Path(tmpdir) / "e2b.Dockerfile"
            df_path.write_text(f"FROM {image_ref}\n")

            cmd = [
                "e2b",
                "template",
                "build",
                "--name", template_name,
                "--dockerfile", "e2b.Dockerfile",
                "--cpu-count", str(self.cpu_count),
                "--memory-mb", str(self.memory_mb),
            ]
            logger.info("Running: %s", " ".join(cmd))
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=tmpdir,
            )
            output_lines: list[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                output_lines.append(line)
                print(line, flush=True)
            returncode = proc.wait()
            if returncode != 0:
                raise RuntimeError(
                    f"e2b template build failed (exit {returncode}):\n"
                    + "\n".join(output_lines)
                )

            for line in output_lines:
                if "template" in line.lower() and "id" in line.lower():
                    parts = line.split(":")
                    if len(parts) >= 2:
                        return parts[-1].strip()

            raise RuntimeError(
                f"Could not parse template ID from output:\n"
                + "\n".join(output_lines)
            )

    def _build_via_sdk(self, image_ref: str, template_name: str) -> str:
        # Use from_image() to properly pass registry credentials alongside the
        # image reference.  The previous from_dockerfile("FROM <ref>") approach
        # did not propagate credentials, causing E2B's server to fail with
        # "invalid UUID length: 0" when trying to resolve the private image.
        tpl = Template()
        template = tpl.from_image(
            image_ref,
            username=self.registry_username or None,
            password=self.registry_password or None,
        )

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
        dockerfile = generate_dockerfile(self._make_instance(group), self.base_image)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dockerfile)
        logger.info("Dockerfile written to %s", path)
        return str(path)
