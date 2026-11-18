# TSecBench 平台接入（Agent 答题 API）

> 首次托管评测结果见 [`TSECBENCH_FIRST_RUN.md`](TSECBENCH_FIRST_RUN.md)（Run 43092：链路已打通，0/22210，含失败分析与修复记录）。

Trina 通过平台的 `/openapi/v1/challenges/*` 接口接入 TSecBench：
**平台负责起题、计分、限时；Trina 负责逐题启动容器、渗透、提交 flag、释放容器。**

当前官方基准为 **Tsecbench v1**（63 题 / 74 Flag / 6h，六个能力域，本地与托管均可）。
本地评测成绩仅供自测；进入 Agent 榜单需要选择托管评测并上传镜像。
`bctf-2026` 是往期活动题集（37 题 / 22210 分），本文保留其首次跑分记录作为接入证据。

代码位置：

| 模块 | 职责 |
|---|---|
| `trina/bench/client.py` | 答题 API 客户端（`BENCHMARK_TOKEN` 鉴权） |
| `trina/bench/runner.py` | 平台适配、flag 提取/提交；题目调度复用 SwarmRuntime |
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

- 同时最多 3 个容器；本实现默认 2 个 Worker、可用 `--workers 1..3` 调整，每题独立会话并在 `finally` 中关闭。
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
| `TRINA_BENCH_MAX_STEPS` | 否 | 单轮动作上限；默认 0（不限制），Commander 模式下不设固定步数 |
| `TRINA_BENCH_COMMANDER_CHECKPOINT_STEPS` | 否 | Commander 观察检查点的动作间隔，默认 8；这是复核频率，不是停止上限 |
| `TRINA_BENCH_PER_TASK_SECONDS` | 否 | 单题全部续轮共享的墙钟预算；CLI 默认 0（不限制），镜像内置 420s |
| `TRINA_BENCH_TOTAL_SECONDS` | 否 | 整场预算；CLI 默认 0（不限制），镜像内置 21600s（V1 平台时限 6h） |
| `TRINA_BENCH_ATTEMPTS` | 否 | 单题最多续做轮次；CLI 默认 0（不限制），镜像内置 2；无新增事实时提前停止 |
| `TRINA_BENCH_USE_HINTS` | 否 | `1` 时补漏轮次前取提示（扣分） |
| `TRINA_BENCH_LIMIT` | 否 | 只跑前 N 题（调试） |
| `TRINA_BENCH_WORKERS` | 否 | 并行题目数，默认 2，平台上限 3 |
| `TRINA_BENCH_COMMANDER` | 否 | `1` 启用轮次边界 Commander（动态续做/加时/释放）；默认 0 |
| `TRINA_BENCH_COMMANDER_EXTENSION_SECONDS` | 否 | 单次加时秒数，默认 60 |
| `TRINA_BENCH_COMMANDER_MAX_EXTENSIONS` | 否 | 单题最多加时次数，默认 1 |
| `TRINA_BENCH_COMMANDER_RESERVE_RATIO` | 否 | 全局储备池比例，默认 0.10 |
| `TRINA_BENCH_COMMANDER_MIN_REMAINING_SECONDS` | 否 | 全局储备绝对下限，默认 120s |
| `TRINA_BENCH_COMMANDER_MAX_REDUNDANT_RATE` | 否 | 重复探索率达到该值释放，默认 0.80 |

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

## 3. 本地模式（Tsecbench v1 推荐先跑通）

Tsecbench v1 的配置页支持“本地 Agent 评测”：成绩仅供自测，但题目、答题 API 与 flag 判定和托管一致，适合先验证链路，再提交托管镜像。

1. 平台选择题集 `Tsecbench v1` → `本地 Agent 评测` → 登录并启动。页面签发 `BENCHMARK_BASE_URL`、`BENCHMARK_TOKEN` 和 VPN 配置。
2. 连接 VPN。`container_addr` 是平台私网地址（通常是 `10.x`），未连 VPN 时启动成功也访问不到靶场：

```bash
sudo openvpn --config task_xxx_vpn_config.ovpn --daemon trina-tsecbench
ip addr show tun0        # 确认 tun0 已拿到地址
```

WSL 没有免密 sudo 时，可以让 Windows 侧已安装的 OpenVPN Connect 连接同一个 `.ovpn`：
Windows 路由表出现 `10.0.128.0/18`、`10.254.1.0/24` 后，WSL 的流量会经 Windows 路由进入 VPN。
以管理员权限依次执行 `ovpnconnector.exe set-config profile <profile>`、`install`、`start`
（`ovpnconnector.exe` 位于 OpenVPN Connect 安装目录）。

3. 导出凭证。令牌只放环境变量，不写入仓库、配置或镜像：

```bash
export BENCHMARK_BASE_URL='...'
export BENCHMARK_TOKEN='...'
export TRINA_LLM_BACKEND=openai
export TRINA_LLM_BASE_URL='https://api.deepseek.com/v1'
export TRINA_LLM_API_KEY='...'
export TRINA_LLM_MODEL='deepseek-chat'
```

4. 先干跑、再单题冒烟、最后全量：

```bash
python3 -m trina bench --dry-run

TRINA_BENCH_LIMIT=1 TRINA_BENCH_MAX_STEPS=18 TRINA_BENCH_PER_TASK_SECONDS=300 \
  python3 -m trina bench

TRINA_BENCH_TOTAL_SECONDS=21600 TRINA_BENCH_ATTEMPTS=2 TRINA_BENCH_WORKERS=2 \
  python3 -m trina bench --commander
```

单题 420s × 2 Worker 覆盖 63 题约 3.7h，剩余时间留给续轮和慢题；平台 6h 到点后所有接口返回 `invalid_state`。

> 预算是按未完成题数公平分配的：单题实际墙钟约为
> `min(TRINA_BENCH_PER_TASK_SECONDS, 剩余总预算 / 未完成题数)`，并行 Worker 不改变这个分母。
> 例如 3 Worker 要让 63 题各拿 300s，总预算应设 `63 × 300 / 3 ≈ 6300s`；
> 设 `12000s` 时首轮单题只有约 190s。按目标单题时长反推总预算，避免“预算看起来够、实际每题被摊薄”。

> 本地模式不要设 `TRINA_BENCH_HOSTED=1`：该开关只用于托管沙箱内的网关改写。
> `bctf-2026` 在 2026-11-04 的配置页显示“仅支持托管模式”，本地入口不可用；核对截图见 [当日评测报告](../评测/2026-11-04_TSecBench_OpenCode_Go/评测报告.md)。

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

## 5. 执行模式

- 答题 API 控制面只认 `scope.control_plane_hosts`（由 `BENCHMARK_BASE_URL` 写入并审计），避免模型把提交接口改到任意地址。
- 靶场目标默认使用 `scope.trust_mode=full`，不需要 `--allow-exploit`；需要复现旧边界时显式切换 `restricted`。
- 所有工具调用、控制面请求、每题容器启停都写入 `logs/<run_id>.jsonl` 审计哈希链。
- 控制台/日志是 3 个 Worker 的合并流：每行以 `[题目编号]` 前缀区分，`[n/上限]` 是该题本轮内的动作序号，不是全局序号；逐题顺序以 `logs/<run_id>-<task>-attempt-*.jsonl` 的哈希链为准。
- 首轮为后续恢复保留一部分单题时间。续轮继承上一轮 `RunState` 的资产、发现、凭据、Flag、证据索引和计数，但重新建立标准 API 消息链，不拼接旧对话；模型会先收到继承事实检查点。
- 停止或阻断时直接释放 Worker；其他状态只有出现新增事实且重复率低于 80%，或已得到部分正确 Flag 时才续做。结果 JSON 逐题记录 `decision_rounds`、`llm_calls`、`tool_calls`、`model_seconds`、重复探索率、每轮预算及实际耗时；`ok` 区分正确完成和未完成样本。

### Commander 动态决策

`trina bench --commander` 在每轮执行中和轮次结束时观察结构化事实，而不是让模型凭感觉判断：

- 输入：部分正确 Flag、事实增量（hosts/findings/credentials/flags/evidence）、重复探索率、单题剩余墙钟、整场剩余预算。
- 检查点：每 `commander_checkpoint_steps` 个动作复核一次（默认 8）；这是观察频率，不是停止上限，`max_steps` 默认 0。
- 动作：`continue`（继续当前轮）、`retry`（用剩余单题预算续做）、`extend`（从全局储备池加时）、`release`（停止当前轮或释放 Worker）。
- 硬上界：`commander_max_extensions`、`commander_extension_seconds`、`reserve_ratio`、`min_remaining_seconds`；没有部分 Flag 且没有新增事实时，轮次结束才释放。
- 决策落在 `attempt_metrics[].commander_action/commander_reason`，汇总 `metrics.commander_extensions/commander_checkpoints`，便于复盘。
- 关闭时（默认）保持原来的固定规则：`_attempt_budget` + `_retry_worthwhile`。
- 唯一的不可协商上限是平台整场 6h 和本次总预算；单题时间窗口是软窗口，可由 Commander 在检查点加时。

示例：62 题、3 Worker、总预算 18600s，给首轮约 300s，并允许 Commander 在“部分 Flag/新增事实”时从 10% 储备池里最多加时 1 次 60s：

```bash
python3 -m trina bench --allow-exploit --commander \
  --per-task-seconds 300 --total-seconds 18600 --workers 3 \
  --commander-extension-seconds 60 --commander-max-extensions 1
```
