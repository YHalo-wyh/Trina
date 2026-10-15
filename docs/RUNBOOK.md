# 运行手册

## 0. 环境自检

```bash
python3 -m trina doctor
```

会检查：配置加载、外部工具（nmap/nuclei/sqlmap/hydra/ffuf/docker 是否存在）、护栏边界、工具清单、LLM 后端连通性、目录可写性。
后端不可用时退出码为 3；护栏边界会原样打印，跑之前先确认它是你要的范围。

## 1. 本地自证靶机（lab）

```bash
python3 -m trina lab --http --port 9099         # Web 靶机：SQLi/LFI/RCE/上传/弱口令/flag/信息泄露 + 软 404 混淆
python3 -m trina lab --ssh --ssh-port 2222      # 弱口令 SSH 服务（trina/trina123），仅监听 127.0.0.1
```

已验证的靶点（可直接 curl 复核）：

| 路径 | 现象 |
|---|---|
| `GET /sqli?id=1%27` | 500 + MySQL 风格报错 |
| `GET /lfi?file=../../../../etc/passwd` | 返回 `/etc/passwd` 内容 |
| `GET /rce?cmd=;id` | 返回 `uid=33(www-data) …` |
| `POST /upload`（multipart，字段 `file`） | 任意扩展名接受，返回 `/uploads/<name>` |
| `POST /login`（`username=admin&password=admin123`） | `欢迎 admin，登录成功！` |
| `GET /flag` | `flag{trina_lab_<hex>}` |
| `GET /.git/HEAD` `GET /robots.txt` | 源码仓库/路径泄露 |
| 任意随机路径 | 200 但固定 137 字节（用来验证软 404 过滤） |

## 2. 跑一次全自动渗透

```bash
# 最小冒烟（约 1 分钟，成本 ~$0.003）
python3 -m trina run --target http://127.0.0.1:9099 --goal "拿到 flag" --max-steps 15

# 放开到命令执行等级
python3 -m trina run --target http://127.0.0.1:9099 --allow-exploit --goal "验证 RCE 并读取 flag"

# 用靶场适配器（自动解析目标 + ground truth）
python3 -m trina ranges status lab
python3 -m trina run --range dvwa --goal "验证 SQL 注入" 
```

产物：

```
logs/<run_id>.jsonl                    审计哈希链（每条动作含护栏裁决）
runs/<run_id>/state.json               完整状态（报告与二次分析的数据源）
reports/<run_id>_report.md             Markdown 报告
reports/<run_id>_report.html           自包含 HTML 报告（内联 CSS/SVG）
```

## 3. 单步操作（排障/精确控制）

```bash
python3 -m trina tools --verbose                                  # 工具与参数表
python3 -m trina tool port_scan --target 127.0.0.1 --param ports=quick
python3 -m trina tool dir_bruteforce --params '{"url":"http://127.0.0.1:9099"}'
python3 -m trina tool weak_password_brute --params '{"service":"ssh","target":"127.0.0.1","port":2222,"username":"trina","password":"trina123"}'
python3 -m trina tool rce_exec --params '{"url":"http://127.0.0.1:9099/rce","param":"cmd","prefix":";","command":"id"}'
```

单工具结果里 `denied: true` 表示被护栏拒绝（`data.guard` 里有原因与合规提示），不是工具故障。

## 4. 接入外部靶场

### 4.1 Web 靶场（Pikachu / DVWA / Bugku / 墨者）

1. 在任意可达位置（宿主机 phpstudy、另一台虚拟机、容器）把靶场跑起来；
2. 编辑对应的 `configs/ranges/<name>.yaml`，填 `base_url`（以及 DVWA 的登录凭据，适配器会先登录拿 cookie）；
3. 确认授权：`python3 -m trina guard check <靶机IP>` —— 若靶机不在 127.0.0.0/8 或 RFC1918 内，需要把它加进 `scope.authorized_cidrs`；
4. `python3 -m trina ranges status pikachu` 看连通性与 ground truth 条数；
5. `python3 -m trina run --range pikachu --goal "逐模块验证漏洞"`。

### 4.2 主机靶机（VulnStack / 自建 Linux+Windows）

`configs/ranges/vulnstack.yaml` 里填 `targets`（host/port/service），例如内网 22/3306/3389/445。
弱口令验证示例（注意动作等级为 `exploit`，需要 `--allow-exploit`）：

```bash
python3 -m trina run --allow-exploit --range vulnstack --goal "对 192.168.1.10 做服务识别与弱口令验证"
```

### 4.3 Docker 类靶场（Vulhub）

```bash
# 本机有 docker（WSL 需启用 Docker Desktop 集成）：
python3 -m trina ranges status vulhub     # 会尝试 docker compose up
# 无 docker：在宿主机/另一台机器起容器，把 IP/URL 填进 configs/ranges/*.yaml
```

## 5. 评测

```bash
python3 -m trina eval run --suite web          # Web 专项（7 场景，逐个跑真实 Agent）
python3 -m trina eval run --suite host         # 主机专项
python3 -m trina eval run --suite integrated   # 综合 + 对抗
# 结果：reports/eval/<suite>-<ts>.md / .json
```

离线可复现评测（不调用 LLM）：

```bash
python3 -m trina run --target http://127.0.0.1:9099 --record runs/demo.replay.yaml --max-steps 8
python3 -m trina run --target http://127.0.0.1:9099 --replay runs/demo.replay.yaml
```

## 6. omp 内使用

在本目录开 omp 后：

```
/pentest 127.0.0.1:9099 拿到 flag        # 命令：先 guard 校验，再让 agent 驱动 trina_run
```

或直接对话："用 trina_tool 对 127.0.0.1:9099 跑一次 dir_bruteforce，并解释命中的路径"。
扩展工具：`trina_tools` / `trina_guard` / `trina_tool` / `trina_run` / `trina_report`。

## 7. 审计与报告维护

```bash
python3 -m trina audit verify logs/<run_id>.jsonl     # 哈希链校验（篡改/插入/删除可定位到序号）
python3 -m trina audit show logs/<run_id>.jsonl --limit 30
python3 -m trina report rebuild --state runs/<run_id>/state.json   # 由历史状态重建报告
```

## 8. 怎么写目标（`--goal`）

目标描述会**双向生效**：既是给模型的任务说明，也会被 `trina/brain/goal.py` 解析成硬条件。
写清楚能显著提升成功率（也更贴近真实渗透任务书）：

| 写法 | 效果 |
|---|---|
| 点名漏洞类：`确认 /sqli 的 SQL 注入`、`验证文件上传`、`验证弱口令` | 解析为硬条件：没有「已验证 + 带证据」的对应发现就不算完成，也不会因为顺手拿到 flag 就收工 |
| 点名路径：`读取 /.git/HEAD`、`读取 /admin/config.php` | 无类要求时作为硬条件；有类要求时降级为提示 |
| 点名端口：`必须覆盖 9099 与 2222`、`端口 2222` | 作为硬条件：这两个端口没有出现在扫描结果里就不会收敛 |
| `拿到 flag` | 只解析出"flag"意图（无硬条件），拿到 flag 即成功 |
| 限定规模：`--max-steps 8`、`--max-seconds 300` | 预算控制；预算耗尽会按已获得的事实给出 success/partial/failed |

示例：

```bash
python3 -m trina run --target 127.0.0.1 --goal "必须覆盖 9099 与 2222 端口，并验证 /rce 的命令注入"
python3 -m trina run --target http://127.0.0.1:9099 --allow-exploit --goal "验证文件上传漏洞并读取 /flag"
```

## 9. 故障排查

| 症状 | 排查 |
|---|---|
| `状态: blocked` | 目标不在授权范围。`trina guard check <target>` 看原因；确属授权范围再改 `configs/trina.yaml` |
| 大量 `拒` | 动作等级超限（`verify`）→ 用 `--allow-exploit`；或载荷命中硬禁止清单（看 `data.guard.reason`） |
| 工具返回 `engine=builtin` | 外部工具缺失，属预期降级；装上 nmap/nuclei/sqlmap/hydra 会自动优先使用 |
| 端口扫描结果为 0 | 目标不可达或端口被防火墙过滤；先用 `trina tool service_probe` 验证连通性 |
| LLM 连续失败 | `trina doctor` 看后端；`omp -p --no-tools "OK"` 手工验证；必要时 `--model` 换模型 |
| 评测 FP 偏高 | 检查场景 `scoped_classes` 是否声明完整；范围外发现不计入误报 |
| 报告缺章节/为空 | `state.json` 是否完整；`trina report rebuild --state …` 重建后仍有问题看 `docs/ARCHITECTURE.md` §6 |
| 审计校验失败 | 说明文件被改过；不要续写，保留原文件用于取证，另起 run |
