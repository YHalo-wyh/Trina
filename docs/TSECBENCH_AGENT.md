# TSecBench 平台接入（Agent 答题 API）

> 首次托管评测结果见 [`TSECBENCH_FIRST_RUN.md`](TSECBENCH_FIRST_RUN.md)（Run 43092：链路已打通，0/22210，含失败分析与修复记录）。

Trina 通过平台的 `/openapi/v1/challenges/*` 接口接入 TSecBench：
**平台负责起题、计分、限时；Trina 负责逐题启动容器、渗透、提交 flag、释放容器。**

代码位置：

| 模块 | 职责 |
|---|---|
| `trina/bench/client.py` | 答题 API 客户端（`BENCHMARK_TOKEN` 鉴权） |
| `trina/bench/runner.py` | 逐题编排、flag 提取/提交、容器回收 |
| `trina/cli.py` → `trina bench` | 本地/托管统一入口 |
| `trina/scope.py` → `check_control_plane` | 控制面主机白名单（只允许答题 API） |

## 1. 平台接口（摘自评测页内文档）

所有接口带请求头 `BENCHMARK_TOKEN: <token>`：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/openapi/v1/challenges` | 题目列表与进度 |
| POST | `/openapi/v1/challenges/start?unique_code=...` | 启动容器，返回 `container_addr` |
| GET | `/openapi/v1/challenges/hint?unique_code=...` | 获取提示（扣分） |
| POST | `/openapi/v1/challenges/submit` | `{"unique_code": "...", "flag": "..."}` |
| POST | `/openapi/v1/challenges/close?unique_code=...` | 释放容器 |

关键约束：

- 同时最多 3 个容器；本实现串行处理并在 `finally` 中关闭。
- 每题可能有多个 flag，按 `flag_count` / `submit` 返回的 `correct_flag_count` 判断是否拿全。
- 重复提交同一正确 flag 返回 `409 duplicate`；任务结束/超时返回 `409 invalid_state`。
- 查看 hint 后该题得分按比例扣减；`trina bench` 默认不取提示（`TRINA_BENCH_USE_HINTS=1` 才启用）。
- `container_addr` 需要通过平台 VPN 直连；本地模式务必先连 VPN，托管模式由平台网络自动打通。

## 2. 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `BENCHMARK_BASE_URL` | 是 | 答题 API 根地址（平台下发） |
| `BENCHMARK_TOKEN` | 是 | 本次跑分 token（平台下发） |
| `TRINA_LLM_BACKEND` | 托管 | 固定 `openai`（镜像已内置） |
| `TRINA_LLM_BASE_URL` | 托管 | 例：`http://api.deepseek.com.tsecbench.gw/v1` |
| `TRINA_LLM_MODEL` | 托管 | 例：`deepseek-chat` |
| `TRINA_LLM_API_KEY` | 托管 | 模型密钥（也可用 `DEEPSEEK_API_KEY`） |
| `TRINA_SCOPE_EXPLOIT_LEVEL` | 托管 | 固定 `exploit`（镜像已内置，答题需要命令执行） |
| `TRINA_BENCH_MAX_STEPS` | 否 | 单轮决策步数，默认 36 |
| `TRINA_BENCH_PER_TASK_SECONDS` | 否 | 单题预算，默认 420s |
| `TRINA_BENCH_TOTAL_SECONDS` | 否 | 整场预算，默认 21600s（平台时限 360min） |
| `TRINA_BENCH_ATTEMPTS` | 否 | 单题最多尝试轮次，默认 1 |
| `TRINA_BENCH_USE_HINTS` | 否 | `1` 时补漏轮次前取提示（扣分） |
| `TRINA_BENCH_LIMIT` | 否 | 只跑前 N 题（调试） |

托管模式下 `trina bench` 会自动把 `https://<厂商域名>/...` 改写成
`http://<厂商域名>.tsecbench.gw/...`；本地模式需显式 `--gateway` 或设 `TRINA_BENCH_HOSTED=1`。

### OpenCode Go 接入

OpenCode Go 的 OpenAI 兼容接口以 `https://opencode.ai/inference/go/openai/v1` 为 Base URL；
托管环境对应 `http://opencode.ai.tsecbench.gw/inference/go/openai/v1`。
模型名使用 Go 套餐中已启用的 ID，例如 `glm-5.3-flash`。
密钥只通过运行时的 `TRINA_LLM_API_KEY` 注入，不写入镜像、配置或仓库。

Go 要求请求带稳定的 `x-opencode-session`，并用 Agent 自身的 User-Agent 标识客户端。
`OpenAICompatBackend` 对上述 Go 端点自动发送这两个请求头；同一次后端实例的会话 ID 保持不变。
接口直连可用不代表托管网关放行：提交完整评测前，应先用 `TRINA_BENCH_LIMIT=1`
进行短时冒烟，并在平台过程观察中确认出现模型会话。平台页面的 API 白名单也需包含 `opencode.ai`。

## 3. 本地模式（平台开放时）

以下流程适用于平台开放本地评测的题集。`bctf-2026`（题集 9）在 2026-11-04 的配置页显示“仅支持托管模式”，无法通过该页面创建本地计分任务；核对截图见 [当日评测报告](../评测/2026-11-04_TSecBench_OpenCode_Go/评测报告.md)。

1. 在 TSecBench 评测页选择本地模式，创建跑分任务，拿到 `BENCHMARK_BASE_URL`、`BENCHMARK_TOKEN`，连接平台 VPN。
2. 本地执行：

```bash
export BENCHMARK_BASE_URL='...'
export BENCHMARK_TOKEN='...'
export TRINA_LLM_BACKEND=openai
export TRINA_LLM_BASE_URL='https://api.deepseek.com/v1'
export TRINA_LLM_API_KEY='...'
export TRINA_LLM_MODEL='deepseek-chat'

python3 -m trina bench --allow-exploit --max-steps 18 --per-task-seconds 420
# 先只看题目：
python3 -m trina bench --dry-run
```

> 本地模式请不要加 `TRINA_BENCH_HOSTED=1`：该开关只用于托管沙箱内的网关改写。

## 4. 托管模式（Docker）

```bash
# 构建
docker build -t trina-tsecbench:latest .

# 本地冒烟（可选；没有 BENCHMARK_* 时会立即报错退出）
docker run --rm -e BENCHMARK_BASE_URL -e BENCHMARK_TOKEN \
  -e TRINA_LLM_BASE_URL -e TRINA_LLM_API_KEY -e TRINA_LLM_MODEL \
  trina-tsecbench:latest --dry-run

# 导出上传（平台限制 3GB）
docker save trina-tsecbench:latest | gzip > trina-tsecbench.tar.gz
```

平台页面操作：

1. 选择/新增 Agent；
2. 上传 `trina-tsecbench.tar.gz`；
3. 配置运行时环境变量：`TRINA_LLM_BASE_URL`、`TRINA_LLM_MODEL`、`TRINA_LLM_API_KEY`（镜像已内置其余项）；
4. 勾选协议并提交，平台自动拉取镜像、注入 `BENCHMARK_*`、启动容器。

## 5. 安全边界

- 答题 API 与靶场目标走两条不同的授权路径：控制面只认
  `scope.control_plane_hosts`（由 `BENCHMARK_BASE_URL` 显式写入并写审计），不因此获得扫描权限；
  靶场目标仍走 `authorized_cidrs` / `authorized_hosts`。
- `exploit_level=exploit` 只放开动作等级；破坏性动作、DoS、数据导出等硬禁止规则不变。
- 所有工具调用、控制面请求、每题容器启停都写入 `logs/<run_id>.jsonl` 审计哈希链。
