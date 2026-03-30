# swe-bench-stress

使用 [SWE-rebench](https://huggingface.co/datasets/nebius/SWE-rebench) 真实 AI Agent 轨迹对 [E2B](https://e2b.dev) 沙箱系统进行高并发压测。

## 原理

```
HuggingFace 数据集
  nebius/SWE-rebench                          → 任务实例 (含 install_config)
  nebius/SWE-rebench-openhands-trajectories   → OpenHands Agent 执行轨迹

         ↓ 解析轨迹中的工具调用 (bash / 文件读写 / str_replace)

build-templates: install_config → Dockerfile → E2B Template（缓存在 template_cache.json）
                                                 ↓
                              template_mapping.json 记录 instance_id → template_id

run-stress-test 执行流程：
  1. 加载 trajectories.json，解析为 SandboxOp 序列
  2. 加载 template_mapping.json，为每条轨迹匹配 template_id
  3. asyncio.Semaphore 限制并发 + _RampUpLimiter 错峰启动
  4. 每条轨迹:
     a. 创建 E2B 沙箱（失败时指数退避重试，最多 3 次）
     b. OpExecutor 按序回放所有操作（bash / file_write / file_read / str_replace）
     c. git diff 提取实际 patch，与轨迹中的 model_patch 语义比对
     d. 销毁沙箱
  5. 聚合指标 → 生成 JSON 报告

  信号处理: SIGINT/SIGTERM → 优雅关停，等待活跃沙箱完成后清理
```

## 目录结构

```
swe-bench-stress/
├── main.py                  # CLI 入口（4 个子命令）
├── config.py                # pydantic-settings 类型定义（默认值统一在 .env）
├── requirements.txt
├── .env.example             # 环境变量模板
└── src/
    ├── downloader.py        # HuggingFace 数据集下载 & 缓存
    ├── template_builder.py  # 从 install_config 生成 Dockerfile / 构建 E2B template
    ├── trajectory_parser.py # 解析 OpenHands 轨迹 → SandboxOp 序列
    ├── stress_tester.py     # asyncio 并发压测引擎 & 指标聚合
    └── patch_compare.py     # 语义化 patch 比对（忽略行号/上下文/空白差异）
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
# 编辑 .env，填入实际值（所有配置项及默认值见 .env.example）
```

配置值的优先级（从高到低）：

```
CLI 参数 > 环境变量 (export) > .env 文件
```

`.env.example` 是所有配置的唯一默认值来源，`config.py` 仅定义类型和变量名。关键配置项：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `E2B_API_KEY` | `your-api-key-here` | E2B API 密钥 |
| `E2B_API_URL` | `http://localhost:3000` | 自部署 E2B 地址 |
| `MAX_CONCURRENT_SANDBOXES` | `10` | 最大并发沙箱数 |
| `SANDBOX_TIMEOUT` | `300` | 单个沙箱生命周期（秒） |
| `COMMAND_TIMEOUT` | `60` | 单条命令超时（秒） |
| `N_TASKS` | `100` | 下载任务数（0 = 全部） |
| `N_TRAJECTORIES` | `50` | 下载轨迹数（0 = 全部） |

完整配置项列表见 [`.env.example`](.env.example)。

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

# 只下载与已有轨迹匹配的 tasks（跳过批量下载，适合单条/少量轨迹回放）
uv run python main.py download-data --n-trajectories 1 --match-tasks
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

# 仅构建与已下载轨迹匹配的 template（避免构建无用 template）
uv run python main.py build-templates --for-trajectories --strategy cli

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
# 基础压测：20 路并发，回放 50 条轨迹（默认回放所有操作类型）
uv run python main.py run-stress-test --n-traj 50 --concurrency 20

# 指定 template，并发 50，错峰启动（每 0.1s 启动一个沙箱）
uv run python main.py run-stress-test \
  --n-traj 100 --concurrency 50 \
  --template-id <your-template-id> \
  --ramp-delay 0.1

# 跳过 patch 提取与比对（加快速度，只关注延迟/吞吐）
uv run python main.py run-stress-test --n-traj 50 --no-patch
```

报告自动保存到 `./results/report_<timestamp>.json`。

### 单条轨迹回放（端到端示例）

只下载 1 条轨迹，自动匹配对应 task，构建 template，然后回放：

```bash
# 1. 下载 1 条轨迹 + 自动匹配 task
uv run python main.py download-data --n-trajectories 1 --match-tasks

# 2. 只为该轨迹构建 template
uv run python main.py build-templates --for-trajectories --strategy cli

# 3. 回放
uv run python main.py run-stress-test --n-traj 1
```

### 6. 查看报告

```bash
uv run python main.py show-report ./results/report_20240101_120000.json
```

输出示例：

```
╭──────────────────── Stress Test Results ────────────────────╮
│           Trajectories  50                                  │
│       Success / Failed  48 / 2                              │
│         Total commands  1234                                │
│        Failed commands  12 (0%)                             │
│        Avg trajectory   8.43s                               │
│            Throughput   142.5 traj/min                      │
│                                                             │
│  Sandbox create p50/p95/p99   1.230s / 2.100s / 3.450s     │
│  Command latency p50/p95/p99  0.180s / 1.200s / 4.500s     │
│                                                             │
│        Patch compared   45                                  │
│  Patch match / mismatch 40 / 5                              │
│       Patch match rate  88.9%                               │
╰─────────────────────────────────────────────────────────────╯

 Per-Op-Type Breakdown
 ┌──────────────────┬───────┬────────┬─────────┬─────────┬─────────┐
 │ Op Type          │ Count │ Failed │ p50 (s) │ p95 (s) │ p99 (s) │
 ├──────────────────┼───────┼────────┼─────────┼─────────┼─────────┤
 │ bash             │  980  │   10   │  0.150  │  1.100  │  4.200  │
 │ file_str_replace │  180  │    1   │  0.030  │  0.080  │  0.120  │
 │ file_write       │   50  │    1   │  0.025  │  0.060  │  0.090  │
 │ file_read        │   24  │    0   │  0.020  │  0.050  │  0.070  │
 └──────────────────┴───────┴────────┴─────────┴─────────┴─────────┘

 Error Categories
 ┌──────────────────┬───────┐
 │ Category         │ Count │
 ├──────────────────┼───────┤
 │ timeout          │     1 │
 │ connection_error │     1 │
 └──────────────────┴───────┘
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

核心组件：

| 组件 | 职责 |
|------|------|
| `StressTester` | 顶层调度：信号处理、并发控制、指标聚合 |
| `SandboxRunner` | 单条轨迹生命周期：创建沙箱（含重试）→ 回放 → 提取 patch → 销毁 |
| `OpExecutor` | 将 `SandboxOp` 分发到 E2B 沙箱 API（bash / files.write / files.read / str_replace） |
| `_RampUpLimiter` | Token-bucket 错峰启动，确保沙箱创建间隔不小于 `ramp_delay` |

并发控制：`asyncio.Semaphore` 限制最大活跃沙箱数；沙箱创建失败时指数退避重试（最多 3 次，基础延迟 2s）。

采集的指标：

| 指标 | 说明 |
|------|------|
| `sandbox_create_s` | 沙箱创建耗时（p50/p95/p99） |
| `command_latency` | 单条命令执行耗时（p50/p95/p99） |
| `per_op_type_stats` | 按操作类型分组的 count / failed / p50 / p95 / p99 |
| `throughput` | 轨迹吞吐量（条/分钟） |
| `error_categories` | 按类别（timeout / connection / rate_limited / server_error 等）统计的错误数 |
| `patch_compared/matched/mismatched` | Patch 语义比对结果（通过 `src/patch_compare.py`） |
| `timeline` | 每条轨迹的启动/完成时间偏移，用于可视化并发分布 |

### `src/patch_compare.py`

语义化 patch 比对：将 unified diff 解析为 `(文件路径, 删除行, 新增行)` 多重集合，忽略行号、上下文行数、尾部空白和 hunk 顺序差异。用于验证沙箱回放是否产生与原始 Agent 执行相同的代码变更。

## CLI 参数速查

```
uv run python main.py download-data
  --n-tasks INT                下载任务数（默认读 N_TASKS 配置）
  --n-trajectories INT         下载轨迹数（默认读 N_TRAJECTORIES 配置）
  --data-dir PATH              数据目录
  --local-tasks PATH           本地 SWE-rebench 目录（跳过 HuggingFace）
  --local-trajectories PATH    本地 trajectories 目录（跳过 HuggingFace）
  --match-tasks                跳过批量下载，只获取与已下载轨迹匹配的 tasks

uv run python main.py build-templates
  --n-tasks INT          处理任务数
  --strategy [sdk|cli]   构建策略（默认 sdk）
  --export-dockerfiles   仅导出 Dockerfile，不构建
  --for-trajectories     只构建与已下载轨迹匹配的 template
  --data-dir PATH

uv run python main.py run-stress-test
  --n-traj INT           轨迹数量（0 = 全部已下载）
  --concurrency INT      最大并发沙箱数（默认读配置）
  --template-id STR      覆盖所有沙箱的 template ID
  --no-patch             跳过 patch 提取与比对
  --ramp-delay FLOAT     错峰启动间隔（秒，默认 0）
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
