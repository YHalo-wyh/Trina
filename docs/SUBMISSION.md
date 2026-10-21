# 提交清单与边界（什么该提交、什么不该提交）

**2026-10-21 章节 02 检查入口：[开题报告与第一次汇报](../开题报告与第一次汇报.md) → [完整课程材料](course/README.md) → [本轮证据](../evidence/20261021-course-pack/README.md)。** 下文保留早期 P3 六类目录说明；历史快照不代替本轮规格和验证。

> 对应课程《AI原生软件工程实践》P3「授权安全测试Agent」的交付要求：六类内容类别（brief / design / eval / evidence / release / review）、
> 证据与复核分离、「机器生成结果不要复制进报告」。本文件是**映射索引**：说清每类内容在仓库里的位置、提交边界，以及为什么。

## 1. 六类交付物与映射（索引）

| 类别 | 该放什么（课程口径） | 本仓库位置 | 备注 |
|---|---|---|---|
| **brief** | 问题、范围、验收ID、责任 | [`brief/README.md`](../brief/README.md) → `docs/PLAN.md` | 目标与验收标准写在这里；范围以 `configs/trina.yaml#scope` 为准 |
| **design** | 关键 ADR、AI 规则、状态与权限 | [`design/README.md`](../design/README.md)、`design/adr/*.md`、`design/ai-rules.md`、`docs/ARCHITECTURE.md`、`docs/GUARDRAILS.md` | ADR 记录「为什么这么做」，不是功能清单 |
| **eval** | 用例与判定 | `trina/eval/scenarios/*.yaml`、[`eval/README.md`](../eval/README.md)、`docs/EVAL.md` | 用例 = 场景 YAML；判定 = ground truth + 指标口径；**验收ID** 见 `eval/README.md` |
| **evidence** | 版本结果、Trace、成本 | [`evidence/README.md`](../evidence/README.md) + `evidence/<版本>-<场景>/` 精选样本 | 只提交**小而关键**的运行事实；全量产物不入库 |
| **release** | 启动、重置、已知问题、恢复 | [`release/README.md`](../release/README.md)、`docs/RUNBOOK.md` | 含「模拟器优先」与停用/回收说明 |
| **review** | 实验、Change、Finding、互评和签署 | [`review/README.md`](../review/README.md)（模板 + 索引） | 存**解释与复核**，不存运行事实 |

## 2. 提交边界（红线）

### 该提交
- 源代码与配置：`trina/`、`configs/`、`tests/`、`.omp/`（可选 omp 集成）、`pyproject.toml`
- 文档与索引：`README.md`、`docs/`、六类目录下的 README/模板/ADR
- **精选证据**：一次有代表性的运行的审计哈希链、状态、报告与评测指标（小体积、可复核）
- 靶场注册表（含 ground truth）：`configs/ranges/*.yaml`（**虚构靶机**，不含真实业务数据）

### 不该提交（已在 `.gitignore` 里排除）
| 类别 | 例子 | 为什么 |
|---|---|---|
| 凭据 / 令牌 | Gitee token、`TRINA_LLM_API_KEY`、SSH 私钥、cookie | 泄露即事故；本仓库任何文件都不应有真实密钥 |
| 批量运行产物 | `runs/`（工作区与 `state.json`）、`logs/`（全量审计）、`reports/`（每次报告） | 体积大、可重复生成；课程口径也要求「机器生成结果别复制进报告」 |
| 缓存与虚拟环境 | `__pycache__/`、`.venv/`、`*.pyc` | 与实现无关的机器产物 |
| 真实目标数据 | 真实站点响应、真实凭据、真实学生/业务数据 | P3 硬边界：只用授权靶场与虚构数据 |
| 大二进制 | 模型权重、靶场镜像、工具安装包 | 仓库不是分发渠道；用 `release/README.md` 说明如何获取 |
| 他人成果 | 第三方源码副本、未授权模板 | 归属与合规问题 |

### 灰区（要写清来源才提交）
- 单个靶机的 `state.json`：可提交**裁剪版**（去掉大段响应体，保留结论与证据索引）；
- 录制的回放脚本（`*.replay.yaml`）：可提交，它是「可复现」的证据，但必须是自建靶场的录制；
- 评测 JSON：可提交**汇总**（指标 + 逐场景计数），不要提交每次运行的完整明细。

## 3. 证据与复核分离（课程强调的点）

- **`evidence/`** 只放**运行事实**：审计 JSONL（哈希链可校验）、`state.json`、工具产出摘要、评测指标 JSON。
  事实必须能追到「版本 + 命令 + 输入」。
- **`review/`** 放**对事实的解释与复核**：实验卡、Change 记录、Finding、互评与签署。
  目标组自己写的「已通过」属于自述，放在 `review/` 并标注复核状态，不因为放在 `evidence/` 就变成独立证据。
- 目录名不是真实性保证：`evidence/` 里的每份材料都要写清 **版本（git commit）＋复现命令＋预期输出**。

## 4. 本次提交包含什么（快照）

| 内容 | 位置 | 版本定位 |
|---|---|---|
| 实现 | `trina/`（含 policy/session/interactive/play）、`configs/`、`tests/` | `git log -1` |
| 计划与验收 | `docs/PLAN.md`、`brief/README.md` | — |
| 设计 | `design/adr/*.md`（14 篇：8 架构 + 6 实证）、`design/ai-rules.md`、`docs/ARCHITECTURE.md`、`docs/GUARDRAILS.md` | — |
| 实验与复核 | `review/experiments/E1–E3`（实验卡含逐次数据与结论边界）、`review/README.md` 模板、`review/*-review.md` 复核记录 | 见文件内标注 |
| 评测 | `trina/eval/scenarios/*.yaml`、`docs/EVAL.md`、`evidence/` 中的评测摘要 | 见文件内 commit |
| 发布/交接 | `release/README.md`、`docs/RUNBOOK.md` | — |
| 复核 | `review/README.md`（模板，待填） | 需本组与结对组签署 |

## 5. 提交邮箱与贡献归属（Gitee 贡献墙）

贡献墙按「**提交邮箱 ↔ 账号已绑定邮箱**」匹配，两点必须同时满足，否则墙上是空的：

1. Gitee → 设置 → 邮箱管理 里**已绑定并验证**邮箱（开启「不公开邮箱」时会给你一个提交专用地址，
   形如 `<用户ID>+<登录名>@user.noreply.gitee.com`；用 `GET /api/v5/emails` 可查到该地址）；
2. 本地 `git config user.email` 与上面那个地址一致：

```bash
git config user.email "15640673+YHalo@user.noreply.gitee.com"   # 换成本账号的地址
git config user.name  "YHalo"
```

> **注意**：邮箱绑定**之前**的历史提交不会被回填。旧提交必须重写作者邮箱后重新推送：

```bash
bash scripts/fix-commit-email.sh --email "<你的提交专用地址>" --dry-run   # 先看改什么
bash scripts/fix-commit-email.sh --email "<你的提交专用地址>"            # 改写本地历史（自动建备份分支）
git push --force-with-lease origin HEAD                                  # 确认内容未变后再强推
```

脚本的安全保障：改前自动建 `backup/pre-email-rewrite-<时间戳>` 分支；断言**内容树哈希**改前改后一致
（只有元数据允许变化），不一致立即回滚；推送建议用 `--force-with-lease`，避免覆盖别人的提交。

## 6. 发布前自检（三条命令）

```bash
git status --short                      # 只应有源码/文档/精选证据
grep -rIn "<你的令牌片段>" . --exclude-dir=.git    # 密钥零命中
git ls-files | xargs -I{} du -h {} | sort -h | tail   # 确认没有大文件混入
python3 -m unittest discover -s tests   # 全绿
```

> 令牌/密钥一律走环境变量（`TRINA_LLM_API_KEY` 等），**任何时候都不要写进仓库**；
> 一旦误提交，除了删除还要在平台侧吊销该令牌。
