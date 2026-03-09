# swe-bench-stress

使用 [SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench) 真实 AI Agent 轨迹对 [E2B](https://e2b.dev) 沙箱系统进行高并发压测。

## 原理

```
HuggingFace 数据集
  nebius/SWE-rebench                     → 任务实例 (含 install_config)
  nebius/SWE-rebench-openhands-trajectories → OpenHands Agent 执行轨迹

         ↓ 解析轨迹中的工具调用 (bash / 文件读写)

asyncio 并发调度  →  N 个 E2B 沙箱同时回放轨迹  →  采集延迟 / 错误率 / 吞吐量
```

## 目录结构

```
swe-bench-stress/
├── main.py                  # CLI 入口（4 个子命令）
├── config.py                # pydantic-settings 配置（读 .env）
├── requirements.txt
├── .env.example             # 环境变量模板
└── src/
    ├── downloader.py        # HuggingFace 数据集下载 & 缓存
    ├── template_builder.py  # 从 install_config 生成 Dockerfile / 构建 E2B template
    ├── trajectory_parser.py # 解析 OpenHands 轨迹 → SandboxOp 序列
    └── stress_tester.py     # asyncio 并发压测引擎 & 指标聚合
```

## 快速开始

### 1. 安装依赖

需要 [uv](https://docs.astral.sh/uv/)（`pip install uv` 或 `curl -LsSf https://astral.sh/uv/install.sh | sh`）。

```bash
uv sync
```

`uv sync` 会自动创建 `.venv` 并按 `uv.lock` 精确安装所有依赖。

> 运行命令时用 `uv run` 前缀，无需手动激活虚拟环境：
> ```bash
> uv run python main.py --help
> ```
> 也可以先激活再运行：
> ```bash
> source .venv/bin/activate
> python main.py --help
> ```

### 2. 配置环境变量

```bash
cp .env.example .env
```

配置值的优先级（从高到低）：

```
CLI 参数 > 环境变量 (export) > .env 文件 > config.py Field default
```

编辑 `.env`：

```dotenv
E2B_API_KEY=your-api-key
E2B_API_URL=http://localhost:3000          # 自部署 E2B 地址
E2B_BASE_IMAGE=ubuntu:22.04-swe-base
E2B_TEMPLATE_CPU_COUNT=1                   # Template.build(cpu_count)
E2B_TEMPLATE_MEMORY_MB=1024                # Template.build(memory_mb)

MAX_CONCURRENT_SANDBOXES=20          # 最大并发沙箱数
SANDBOX_TIMEOUT=300                  # 单个沙箱生命周期（秒）
COMMAND_TIMEOUT=60                   # 单条命令超时（秒）

N_TASKS=200                          # 下载任务数（0 = 全部）
N_TRAJECTORIES=100                   # 下载轨迹数（0 = 全部）
```

### 3. 下载数据集

```bash
# 从 HuggingFace 远程下载
uv run python main.py download-data
# 指定采样量
uv run python main.py download-data --n-tasks 500 --n-trajectories 200

# 从本地目录加载（跳过网络，适合已在服务器上准备好原始数据集的场景）
uv run python main.py download-data \
  --local-tasks /data/SWE-rebench \
  --local-trajectories /data/SWE-rebench-openhands-trajectories

# 混用：tasks 本地、trajectories 远程
uv run python main.py download-data --local-tasks /data/SWE-rebench
```

数据缓存到 `./data/tasks.json` 和 `./data/trajectories.json`，再次运行直接复用。

### 4. 构建 E2B Template

每个任务的 `install_config` 描述了运行环境（Python 版本、conda/pip 包等），将其转换为 Dockerfile 并在 E2B 注册为 template：

构建前先登录私有镜像仓库：

```bash
docker login 61.47.17.182:89
```

```bash
# 通过 E2B Python SDK 构建（默认）
uv run python main.py build-templates --n-tasks 100 --strategy sdk

# 通过 e2b CLI 工具构建
uv run python main.py build-templates --strategy cli

# 仅导出 Dockerfile，不实际构建（调试）
uv run python main.py build-templates --export-dockerfiles
```

Template ID 缓存在 `./data/template_cache.json`，相同 `install_config` 只构建一次。

> E2B SDK 通过进程环境变量读取 `E2B_API_KEY` / `E2B_API_URL`，
> `main.py` 会在调用 SDK 前从 `Config` 导出到 `os.environ`。
> 默认使用 `Template.build(..., cpu_count=1, memory_mb=1024, on_build_logs=default_build_logger())`，
> 可通过 `.env` 的 `E2B_TEMPLATE_CPU_COUNT`、`E2B_TEMPLATE_MEMORY_MB` 覆盖。

### 5. 执行压测

```bash
# 基础压测：20 路并发，回放 50 条轨迹
uv run python main.py run-stress-test --n-traj 50 --concurrency 20

# 指定 template，并发 50，错峰启动（每 0.1s 启动一个沙箱）
uv run python main.py run-stress-test \
  --n-traj 100 --concurrency 50 \
  --template-id <your-template-id> \
  --ramp-delay 0.1

# 回放所有操作类型（默认只回放 bash 命令）
uv run python main.py run-stress-test --n-traj 50 --all-ops
```

报告自动保存到 `./results/report_<timestamp>.json`。

### 6. 查看报告

```bash
uv run python main.py show-report ./results/report_20240101_120000.json
```

输出示例：

```
╭─────────────────── Stress Test Results ───────────────────╮
│          Trajectories  50                                  │
│        Success/Failed  48 / 2                              │
│        Total commands  1234                                │
│       Failed commands  12 (0%)                             │
│       Avg trajectory   8.43s                               │
│           Throughput   142.5 traj/min                      │
│                                                            │
│  Sandbox create p50/p95/p99   1.23s / 2.10s / 3.45s       │
│  Command latency p50/p95/p99  0.18s / 1.20s / 4.50s       │
╰────────────────────────────────────────────────────────────╯
```

## 各模块说明

### `src/downloader.py`

| 功能 | 说明 |
|------|------|
| 流式下载 | 使用 HuggingFace `streaming=True`，不需要把整个数据集载入内存 |
| 本地加载 | 通过 `--local-tasks` / `--local-trajectories` 指定本地数据集目录，跳过 HuggingFace 网络请求 |
| 本地缓存 | 首次下载后保存为 JSON，后续运行直接读取 |
| `install_config` 清洗 | HF 会把缺失字段填为 `None`，自动移除这些无效键 |
| 任务与轨迹关联 | 通过 `instance_id` 将轨迹与任务的 `install_config` 关联 |

### `src/template_builder.py`

`install_config` 字段映射关系：

| 字段 | Dockerfile 指令 |
|------|----------------|
| `python` | `conda install -y python=<ver>` |
| `env_vars` | `ENV key=value` |
| `pre_install` | `RUN <cmd>`（系统依赖） |
| `packages` | `conda install -y <packages>` |
| `pip_packages` | `pip install --no-cache-dir <packages>` |
| `install` | `RUN <cmd>`（需要源码的跳过，运行时执行） |

支持两种构建策略：
- **`sdk`**：通过 E2B Python SDK（`Template.build`）构建
- **`cli`**：调用 `e2b template build` 命令行工具

### `src/trajectory_parser.py`

兼容所有已知 OpenHands 工具名：

| 操作类型 | 匹配的工具名 |
|----------|-------------|
| `BASH` | `bash`, `execute_bash`, `shell`, `ipython` 等 |
| `FILE_WRITE` | `write_file`, `create_file`, `str_replace_editor(create)` 等 |
| `FILE_READ` | `read_file`, `view_file`, `str_replace_editor(view)` 等 |
| `FILE_STR_REPLACE` | `str_replace_editor`, `str_replace_based_edit_tool` 等 |

### `src/stress_tester.py`

并发控制：`asyncio.Semaphore` 限制最大活跃沙箱数，防止过载。

采集的指标：

| 指标 | 说明 |
|------|------|
| `sandbox_create_s` | 沙箱创建耗时（p50/p95/p99） |
| `command latency` | 单条命令执行耗时（p50/p95/p99） |
| `throughput` | 轨迹吞吐量（条/分钟） |
| `error rate` | 失败轨迹数 & 失败命令数 |

## CLI 参数速查

```
uv run python main.py download-data
  --n-tasks INT                下载任务数（默认读 N_TASKS 配置）
  --n-trajectories INT         下载轨迹数（默认读 N_TRAJECTORIES 配置）
  --data-dir PATH              数据目录
  --local-tasks PATH           本地 SWE-rebench 目录（跳过 HuggingFace）
  --local-trajectories PATH    本地 trajectories 目录（跳过 HuggingFace）

uv run python main.py build-templates
  --n-tasks INT          处理任务数
  --strategy [sdk|cli]   构建策略（默认 sdk）
  --export-dockerfiles   仅导出 Dockerfile，不构建
  --data-dir PATH

uv run python main.py run-stress-test
  --n-traj INT           轨迹数量
  --concurrency INT      最大并发沙箱数
  --template-id STR      覆盖所有沙箱的 template ID
  --all-ops              回放所有操作（默认仅 bash）
  --ramp-delay FLOAT     错峰启动间隔（秒）
  --data-dir PATH
  --results-dir PATH

uv run python main.py show-report REPORT_PATH
```

## 常用 uv 命令

```bash
# 添加新依赖
uv add <package>

# 移除依赖
uv remove <package>

# 更新所有依赖到最新兼容版本
uv lock --upgrade

# 查看依赖树
uv tree

# 在虚拟环境中运行任意命令
uv run <command>
```

## 数据集说明

| 数据集 | 规模 | 说明 |
|--------|------|------|
| [nebius/SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench) | ~21,000 任务 | GitHub Issue 修复任务，含 `install_config` |
| [nebius/SWE-rebench-openhands-trajectories](https://huggingface.co/datasets/nebius/SWE-rebench-openhands-trajectories) | ~67,000 轨迹 | OpenHands Agent 完整执行轨迹 |
