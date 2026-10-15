# Trina · 全自动渗透测试 Agent

在 [omp](https://omp.sh) 之上二开的自动化渗透测试 Agent：**大模型 ReAct 决策 + 全套工具封装 + 多靶场适配 + 硬性安全护栏 + 标准报告 + 评测框架**。

```bash
# 1) 起一个故意脆弱的本地靶机（仅监听 127.0.0.1，用于自证）
python3 -m trina.lab.run --http --port 9099 &

# 2) 全自动渗透（复用 omp 已配置的模型与鉴权，无需额外 API Key）
python3 -m trina run --target http://127.0.0.1:9099 --goal "拿到 flag"

# 3) 看报告
ls reports/            # <run_id>_report.md / .html
python3 -m trina audit verify logs/<run_id>.jsonl   # 校验审计哈希链
```

## 五项能力

| 能力 | 落地位置 | 关键设计 |
|---|---|---|
| 1. 自动决策大脑 | `trina/brain/` | ReAct 循环 + PTES 阶段机（事实驱动推进）+ 停滞检测 + 预算收敛；动作协议是**文本 JSON**，跨模型可移植、可回放 |
| 2. 工具统一封装 | `trina/tools/` | 17 个工具，统一 `ToolSpec/ToolResult` 契约；**外部二进制优先 + 内置 Python 引擎兜底**（本机无 nmap/nuclei/sqlmap/hydra 也能跑） |
| 3. 多靶场适配 | `trina/adapters/` + `configs/ranges/` | `local` / `docker-compose` / `http-range` / `tsecbench` 四类适配器，内置 Pikachu、DVWA、Vulhub、VulnStack、lab、TSecBench 六套注册表（含 ground truth） |
| 4. 安全合规护栏 | `trina/scope.py` + `trina/audit.py` | 授权网段白名单硬校验、动作分级、破坏性闸门、DoS/删库硬禁止、限速与爆破规模上限、SHA-256 哈希链审计 |
| 5. 标准报告 | `trina/report/` | Markdown + 自包含 HTML（内联 CSS/SVG，无外部依赖），含执行摘要、攻击路径、漏洞详情（CVSS/CWE/证据/复现/修复）、凭据与 flag、工具时间线、合规声明 |

## 已封装的工具（17）

| 类别 | 工具 | 动作等级 |
|---|---|---|
| recon | `port_scan` `service_probe` `http_fingerprint` `tls_info` | detect |
| web | `dir_bruteforce` `web_surface` `param_reflect_scan` | detect |
| vuln | `poc_scan`（21 个只读 PoC） `nuclei_scan`（缺 nuclei 自动降级内置引擎） | detect |
| exploit | `sqli_test` `cmd_inject_test` `lfi_test` `file_upload_test` `rce_exec` | verify / exploit |
| creds | `weak_password_brute`（ssh/ftp/mysql/redis/http_basic/http_form） `http_form_login` `flag_hunt` | exploit / verify |

## 评测

两轮评测（详见 `docs/EVAL.md`）：

```bash
python3 -m trina eval run --suite web           # Web 专项（7 场景：sqli/lfi/rce/upload/弱口令/信息泄露/夺旗 + 对抗场景）
python3 -m trina eval run --suite host          # 主机专项（SSH 弱口令/端口覆盖/服务识别）
python3 -m trina eval run --suite integrated    # 综合（蓝队对抗 + TSecBench bundle 回放）
python3 -m trina eval adversarial --port 9100   # 只起对抗代理（WAF/封禁/限速/篡改/蜜罐）
```

- 指标：通过率、平均/P50/P90 耗时、precision、recall、误报率、遗漏率、**范围外发现（不计误报）**、端口覆盖度、服务识别准确率、flag 获取率、被拒动作数。
- **无需手工起靶机**：场景用 `fixtures` 声明依赖，评测器自动拉起/回收本地 lab 靶机。
- **零成本离线回归**：`--set llm.backend=replay --set llm.replay_script=<录制脚本>`（`trina run --record` 录制）。
- 结果落盘：`reports/eval/<suite>-<ts>.md`（中文指标表）与 `.json`（逐场景明细，可机器分析）。

## 常用命令

```bash
python3 -m trina doctor                                   # 环境自检（后端连通性、外部工具、护栏边界）
python3 -m trina tools --verbose                          # 工具清单与参数表
python3 -m trina guard check 8.8.8.8 127.0.0.1            # 目标是否授权（8.8.8.8 会被拒）
python3 -m trina guard argv -- sqlmap -u http://x --os-shell   # 命令是否会被拒
python3 -m trina tool port_scan --target 127.0.0.1        # 单工具执行（带护栏与审计）
python3 -m trina run --target 127.0.0.1:9099 --allow-exploit   # 放开到 exploit 等级
python3 -m trina ranges list                              # 靶场适配器状态
python3 -m trina eval run --suite web                     # 跑评测套件（指标 + 报告）
python3 -m trina eval adversarial --port 9100             # 蓝队对抗代理（WAF/封禁/限速/篡改/蜜罐）
python3 -m trina lab --http --ssh                         # 本地靶机（HTTP + 弱口令 SSH）
python3 -m unittest discover -s tests                   # 128 个单元测试
```

## omp 二开（在本目录里开 omp 会话即生效）

- `.omp/extensions/trina.ts`：把 Trina 暴露成 omp 工具 —— `trina_run` / `trina_tool` / `trina_guard` / `trina_tools` / `trina_report`，并注册 `/pentest` 命令；
  同时对 omp 内建 `bash` 做**第二道防线**拦截（DoS/删库/`--os-shell` 等参数、对公网地址的攻击性工具调用）。
- `.omp/skills/pentest-agent/SKILL.md`：渗透作业技能（何时用、怎么用、护栏规则、报告规范）。
- `.omp/config.yml`：项目级配置覆盖（默认留空，示例见注释）。

## 目录

```
trina/            核心包（brain / tools / adapters / report / eval / lab）
configs/        主配置 + 靶场注册表 + 字典
docs/           方案与文档（PLAN / ARCHITECTURE / GUARDRAILS / EVAL / RUNBOOK）
.omp/           omp 二开产物（扩展 + 技能 + 配置覆盖）
logs/           审计哈希链（每 run 一个 JSONL）
runs/           运行工作区（state.json 等）
reports/        报告（Markdown / HTML / 评测报告）
tests/          单元测试（护栏/审计/注册表/大脑/状态/报告/评测）
```

## 环境依赖

- Python ≥ 3.10，已装：`pyyaml requests paramiko rich`（本机另有 `bs4 aiohttp httpx`）。
  缺失可选依赖时对应能力会**明确降级**而不是崩溃（如无 `pymysql` 则跳过 MySQL 弱口令验证）。
- 外部工具（nmap / nuclei / sqlmap / hydra / ffuf / nmap 等）**非必需**：存在即优先使用，缺失自动降级到内置引擎并在结果里标注 `engine=builtin`。
- Docker（Vulhub/VulnStack 一键起靶场）可选；WSL 需启用 Docker Desktop 集成，或把靶场跑在宿主机后按 `configs/ranges/*.yaml` 填 URL。

## 安全边界（务必阅读 `docs/GUARDRAILS.md`）

- 只允许 `configs/trina.yaml#scope.authorized_cidrs` / `authorized_hosts` 内的目标；公网默认拒绝；回环可控。
- 动作分级 `detect < verify < exploit`，默认上限 `verify`；破坏性动作默认关闭，且需显式主机白名单。
- **永不允许**：DoS/洪泛、删库删表、`--os-shell/--file-write/--dump-all`、写 webshell、格式化/删除系统文件、执行远端下载脚本。
- 每次动作（含被拒绝的）都写入审计哈希链，`trina audit verify` 可检出篡改。

> 仅可用于**已获授权**的实训/竞赛靶场。越界测试是违法行为，本项目的第一道防线就在 `trina/scope.py`。
