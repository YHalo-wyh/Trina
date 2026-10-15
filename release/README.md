# release · 启动、重置、停止、恢复、已知问题

> 交付形态：**可重复运行的 CLI**（不需要云、不需要容器）。本文件是「另一组能否在干净环境跑起来并恢复」的说明书。

## 1. 环境要求
| 项 | 要求 | 检查 |
|---|---|---|
| Python | ≥ 3.10（开发用 3.12） | `python3 -V` |
| 依赖 | `pyyaml requests paramiko rich`（可选 `bs4 aiohttp pymysql`） | `python3 -m trina doctor` |
| 模型后端 | omp（已配置模型/鉴权）或任意 OpenAI 兼容端点 | `python3 -m trina doctor` 的「模型后端」段 |
| 外部工具 | nmap/nuclei/sqlmap/hydra **非必需**（缺失自动降级内置引擎） | 同上「外部工具」段 |

## 2. 启动（四步，模拟器优先）
```bash
# ① 自检
python3 -m trina doctor

# ② 起内置模拟器（故意脆弱靶机，仅监听回环）
python3 -m trina lab --http --port 9099 --ssh --ssh-port 2222

# ③ 一键开打（也可由它自动拉起靶机：不写 --target）
python3 -m trina play --target http://127.0.0.1:9099 --goal "拿到 flag 并验证 SQL 注入"

# ④ 交互模式（人工审批/介入）
python3 -m trina console --approve exploit
```
> 只有第 ③/④ 步会产生真实的模型调用与网络请求，且都限制在授权范围（`configs/trina.yaml#scope`）。

## 3. 停止与回收
| 场景 | 操作 |
|---|---|
| 让 Agent 停下来 | 控制台 `stop`（当前工具调用结束后生效）；或 `Ctrl+C` |
| 暂停/继续 | 控制台 `pause` / `resume` |
| 撤销某目标授权 | 控制台 `revoke <target_id>`（后续该目标的检查一律拒绝，减权不绕路） |
| 关闭靶机 | `trina lab` 前台运行时 `Ctrl+C`；`play` 自动拉起的靶机会在任务结束时自动回收（`--keep-lab` 可保留） |
| 清理证据 | 删除 `runs/<run_id>/`、`logs/<run_id>.jsonl`、`reports/<run_id>_report.*`（`evidence/` 里只保留精选样本） |

## 4. 恢复（一次演练脚本）
```bash
# 1) 回到已知状态：停掉靶机，确认端口空闲
python3 -c "import socket;s=socket.socket();print('9099 closed' if s.connect_ex(('127.0.0.1',9099)) else 'still open')"
# 2) 重新拉起模拟器并跑同一目标
python3 -m trina play --target http://127.0.0.1:9099 --max-steps 10 --no-report
# 3) 核对结论与覆盖（结论状态 + 已测/未测检查项应可解释）
python3 -m trina review --state runs/$(ls -t runs | head -1)/state.json
```
> 「恢复」的评价标准不是"跑出同样分数"，而是：**能重新跑起来、能解释结论差异、能说清未覆盖项**。

## 5. 部署与执行边界（P3 硬要求）
- **不挂宿主凭据**：执行环境不需要、也不应提供宿主 SSH key 或云凭证；
- **不暴露 Docker socket**：容器名不构成安全证明，本项目的执行层不依赖容器特权；
- **无开放外网**：默认只允许回环 + RFC1918；公网目标需要显式改配置（`allow_public` + 网段授权，双重闸门）；
- **模型没有写权限**：注册表（`configs/ranges/*.yaml`）与预算是文件，模型只能用 `test_id` 提议。

## 6. 已知问题（诚实清单）
| 问题 | 影响 | 现状/规避 |
|---|---|---|
| 主机类场景结果有波动 | 端口覆盖/服务识别场景在不同采样下 pass 率会跳（2/3 ↔ 1/3） | 已定位为 LLM 扫描策略选择；目标里点名端口会成为硬条件（见 `docs/EVAL.md §4.2`） |
| `exploit` 级动作默认被拒 | `rce_exec`/字典爆破在默认 `verify` 上限下不可用 | 需要时用 `--allow-exploit` 或改配置；护栏拒绝会给出合规替代路径 |
| 外部工具缺失 | 能力降级为内置引擎（覆盖较窄） | 结果里标注 `engine=builtin`；装上 nmap/nuclei/sqlmap/hydra 自动优先 |
| WSL 无 docker | Vulhub/VulnStack 不能一键起 | 用宿主机或 Docker Desktop 集成；配置里写远程 URL |
| TSecBench 远程模式 | 无官方 key 不可用 | 用本地 bundle 回放（`configs/tsecbench_bundle/`）；拿到 key 只需填环境变量 |
| 大字典爆破 | 默认 `max_brute_attempts`/限速限制规模 | 按需改配置；单凭据验证不受限 |

## 7. 版本与可重复性
- 代码版本：`git log -1 --oneline`（证据文件需标注该 commit）
- 决策序列可复现：`--record <file>` 录制、`--replay <file>` 离线重放
- 审计可校验：`python3 -m trina audit verify logs/<run_id>.jsonl`
