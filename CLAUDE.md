# CLAUDE.md — swe-bench-stress

High-concurrency E2B sandbox stress tester that replays real AI agent
trajectories from the SWE-rebench dataset.

---

## Repository layout

```
swe-bench-stress/
├── main.py                  # CLI entry point (4 sub-commands via Click)
├── config.py                # pydantic-settings Config class (reads .env)
├── pyproject.toml           # project metadata + dependencies (managed by uv)
├── uv.lock                  # locked dependency manifest
├── .env.example             # environment variable template
├── .gitignore               # only .env is ignored
└── src/
    ├── __init__.py
    ├── downloader.py        # HuggingFace dataset download & local cache
    ├── template_builder.py  # Dockerfile generation + E2B template build/cache
    ├── trajectory_parser.py # OpenHands trajectory → SandboxOp sequence
    └── stress_tester.py     # asyncio concurrent sandbox runner + metrics
```

Generated at runtime (not committed):

```
data/
  tasks.json               # cached task instances
  trajectories.json        # cached trajectories
  template_cache.json      # fingerprint → E2B template_id
  template_mapping.json    # instance_id → template_id
results/
  report_<timestamp>.json  # stress test result JSON
dockerfiles/
  <fingerprint>.Dockerfile # exported Dockerfiles (debug only)
```

---

## Development environment

This project uses **[uv](https://docs.astral.sh/uv/)** exclusively.

```bash
# Install dependencies (creates .venv automatically)
uv sync

# Run any command inside the venv
uv run python main.py --help

# Add / remove dependencies
uv add <package>
uv remove <package>

# Update lock file
uv lock --upgrade
```

Python ≥ 3.11 is required (`requires-python = ">=3.11"` in `pyproject.toml`).

There are no test suites, linters, or formatters configured in this repo.

---

## Configuration

All runtime configuration lives in `config.py` as a single `Config` (pydantic-settings `BaseSettings`).

**Priority (high → low):**
1. CLI flags passed to the sub-command
2. Shell environment variables (`export VAR=value`)
3. `.env` file in the project root
4. `config.py` field defaults

Copy `.env.example` → `.env` and fill in values before running:

| Variable | Default | Purpose |
|---|---|---|
| `E2B_API_KEY` | `test-key` | API key for E2B instance |
| `E2B_API_URL` | `http://localhost:3000` | E2B self-hosted endpoint |
| `E2B_BASE_IMAGE` | `61.47.17.182:89/e2b/ubuntu:22.04-custom` | Base Docker image for templates |
| `E2B_TEMPLATE_CPU_COUNT` | `1` | vCPUs per template |
| `E2B_TEMPLATE_MEMORY_MB` | `1024` | Memory per template (MB) |
| `MAX_CONCURRENT_SANDBOXES` | `10` | Semaphore limit for stress test |
| `SANDBOX_TIMEOUT` | `300` | Sandbox lifetime (seconds) |
| `COMMAND_TIMEOUT` | `60` | Per-command timeout (seconds) |
| `DATA_DIR` | `./data` | Cache directory |
| `RESULTS_DIR` | `./results` | Report output directory |
| `HF_TOKEN` | `` | HuggingFace token (private datasets) |
| `N_TASKS` | `100` | Default number of tasks to download |
| `N_TRAJECTORIES` | `50` | Default number of trajectories to download |

`main.py` explicitly sets `os.environ["E2B_API_KEY"]` and
`os.environ["E2B_API_URL"]` before calling into the E2B SDK, because the SDK
reads those variables directly from the process environment.

---

## CLI sub-commands

All commands are invoked via:

```bash
uv run python main.py [--debug] <sub-command> [options]
```

### `download-data`

Downloads and caches the two HuggingFace datasets.

```bash
uv run python main.py download-data [--n-tasks INT] [--n-trajectories INT]
    [--data-dir PATH]
    [--local-tasks PATH]          # skip HF, load from local dir
    [--local-trajectories PATH]   # skip HF, load from local dir
```

- Streams data with `streaming=True` so the whole dataset is never loaded into memory.
- On subsequent runs, loads from `data/tasks.json` / `data/trajectories.json` (no network call).
- `n_tasks=0` / `n_trajectories=0` means "download everything".
- `_clean_task()` strips `None`-valued keys that HuggingFace injects into `install_config`.

### `build-templates`

Groups tasks by `(install_config, repo, base_commit)` fingerprint, generates
Dockerfiles, and builds E2B templates. Each unique fingerprint maps to exactly
one template (deduplication is handled by `TemplateCache`).

```bash
uv run python main.py build-templates [--n-tasks INT]
    [--strategy sdk|cli]      # sdk (default): E2B Python SDK; cli: e2b CLI tool
    [--export-dockerfiles]    # write Dockerfiles to ./dockerfiles/, no build
    [--data-dir PATH]
```

Before running with `--strategy cli`, authenticate with the private registry:

```bash
docker login 61.47.17.182:89
```

### `run-stress-test`

Parses trajectories and replays them concurrently across E2B sandboxes.

```bash
uv run python main.py run-stress-test [--n-traj INT]
    [--concurrency INT]
    [--template-id STR]   # override template for all sandboxes
    [--all-ops]           # replay all op types (default: bash only)
    [--ramp-delay FLOAT]  # seconds between successive sandbox launches
    [--data-dir PATH] [--results-dir PATH]
```

Report is saved to `results/report_<timestamp>.json`.

### `show-report`

Pretty-prints a saved report with a Rich panel and per-sandbox table.

```bash
uv run python main.py show-report ./results/report_20240101_120000.json
```

---

## Module details

### `config.py`

Single `Config` class, instantiated fresh in each sub-command (`cfg = Config()`).
A module-level `config = Config()` singleton is also exported but not used by
`main.py` (which always creates its own instance to pick up any monkey-patching
of environment variables done at import time).

### `src/downloader.py`

**Important:** This module monkey-patches `httpx.Client.__init__` and
`httpx.AsyncClient.__init__` at import time to force `verify=False` (SSL
disabled) and sets `HF_ENDPOINT=https://hf-mirror.com`. This is intentional
for environments where the official HuggingFace CDN is unreachable.

Key methods:
- `download_tasks(n_samples)` → `list[dict]` — cached in `data/tasks.json`
- `download_trajectories(n_samples)` → `list[dict]` — cached in `data/trajectories.json`
- `join(tasks, trajectories)` → merged list keyed by `instance_id`

### `src/template_builder.py`

**`generate_dockerfile(instance, base_image)`** — pure function that converts
one task's `install_config` into a Dockerfile string. Handles three `packages`
modes:
- `"environment.yml"` — conda env from YAML
- `"requirements.txt"` — pip from requirements file
- inline string — direct pip install

`install_config` field → Dockerfile instruction mapping:

| Field | Instruction |
|---|---|
| `python` | `conda create -n testbed python=<ver>` |
| `env_vars` | `ENV key=value` |
| `pre_install` | `RUN <cmd>` before main install |
| `packages` | conda install or requirements dispatch (see above) |
| `pip_packages` | `pip install --no-cache-dir` |
| `install` | `RUN <cmd>` at end (skip `pip install -e .` style, run at runtime) |
| `no_use_env` | skip conda activation prefix |

**`fingerprint_install_config(config, repo, commit)`** — SHA-256 (16 chars) of
the canonical JSON of `{config, repo, commit}`. Used as dedup key and cache key.

**`TemplateCache`** — JSON-backed `{fingerprint: template_id}` store at
`data/template_cache.json`.

**`E2BTemplateBuilder`** — orchestrates the build loop. Two strategies:
- `sdk` — calls `Template().from_dockerfile(df)` then `Template.build(…)`
- `cli` — writes Dockerfile to a temp dir and shells out to `e2b template build`

### `src/trajectory_parser.py`

Converts raw HuggingFace trajectory rows into `ParsedTrajectory` objects
containing ordered `SandboxOp` lists.

**`OpType` enum:** `BASH`, `FILE_WRITE`, `FILE_READ`, `FILE_STR_REPLACE`, `UNKNOWN`

Tool name → OpType mapping (covers all known OpenHands versions):

| OpType | Tool names |
|---|---|
| `BASH` | `bash`, `execute_bash`, `run_bash`, `shell`, `terminal`, `ipython` |
| `FILE_WRITE` | `write_file`, `create_file`, `file_write`, `write` |
| `FILE_READ` | `read_file`, `view_file`, `file_read`, `read`, `open` |
| `FILE_STR_REPLACE` | `str_replace_editor`, `str_replace_based_edit_tool`, `edit_file`, `replace`, `str_replace` |

`str_replace_editor` is polymorphic: dispatches to `FILE_READ` (command=`view`),
`FILE_WRITE` (command=`create`), or `FILE_STR_REPLACE` otherwise.

HuggingFace stores `tool_calls[].function.arguments` as a JSON string;
`_deserialize_arguments()` handles both string and already-decoded dict forms.

### `src/stress_tester.py`

Pure asyncio engine. Key design decisions:

- **`asyncio.Semaphore(max_concurrent)`** caps live sandboxes.
- **Ramp-up delay**: `await asyncio.sleep(idx * ramp_up_delay_s)` staggers launches.
- **`asyncio.gather(*tasks)`**: all trajectory coroutines run concurrently.
- `FILE_STR_REPLACE` is implemented as read → python `.replace(old, new, 1)` → write.
- Sandbox is always killed in a `finally` block even on error.

Metrics collected per run:

| Metric | Description |
|---|---|
| `sandbox_create_s` | p50/p95/p99 sandbox creation latency |
| command latency | p50/p95/p99 per-command duration |
| `avg_trajectory_s` | mean total time per trajectory |
| `throughput_traj_per_min` | trajectories / (elapsed_s / 60) |
| `failed_commands` | commands with non-zero exit code or exception |

---

## Data flow

```
HuggingFace (or local dir)
        │
        ▼
  DatasetDownloader
        │  tasks.json  /  trajectories.json
        ▼
  template_builder.group_tasks_by_config()
        │  dedup by fingerprint
        ▼
  E2BTemplateBuilder.get_or_build_batch()
        │  template_cache.json  /  template_mapping.json
        ▼
  TrajectoryParser.parse_many()
        │  list[ParsedTrajectory]
        ▼
  StressTester.run()
        │  asyncio.gather, Semaphore, E2B AsyncSandbox
        ▼
  StressTestReport  →  results/report_<timestamp>.json
```

---

## Key conventions

1. **No `load_dotenv()` in `main.py`** — pydantic-settings handles `.env` reading
   automatically via `model_config = {"env_file": ".env"}` in `Config`.

2. **Lazy imports** — `src.*` modules are imported inside each CLI handler, not
   at module top-level. This keeps startup fast and avoids side effects (e.g.,
   the httpx monkey-patch in `downloader.py`) until the relevant command runs.

3. **Cache-first pattern** — both `download_tasks()` and `download_trajectories()`
   return immediately from JSON cache if it already exists. Delete `data/*.json`
   to force a fresh download.

4. **`n=0` means "all"** throughout (dataset sampling, template build, replay count).

5. **Config is always freshly instantiated** (`cfg = Config()`) inside each
   sub-command function, not shared as a global. This ensures environment
   variable mutations made before the SDK call are picked up.

6. **`from __future__ import annotations`** is present in all source files for
   forward-reference compatibility.

---

## Git workflow

Active development branch: `claude/claude-md-mmihz0js910bfm6c-HhlHw`

```bash
git push -u origin claude/claude-md-mmihz0js910bfm6c-HhlHw
```

Commit messages follow an imperative / conventional style (e.g., `feat:`,
`fix:`, `refactor:`, `docs:`). Chinese is used in some commit messages — that
is fine and expected.
