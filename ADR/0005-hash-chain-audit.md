# ADR-0005：审计用 SHA-256 哈希链

- 状态：已采纳（M0）

## 背景
课程与实训都要求"全程留痕可追溯"，更进一步的要求是：**能证明日志没被事后修改**。
普通 JSONL 日志做不到这一点——删掉一条越界记录，没人看得出来。

## 决定
每次运行写一个 JSONL 审计文件，每条记录包含：

```
seq · ts · run_id · event · tool · decision · target · params_digest · output_digest
prev_hash · hash          # hash = sha256(canonical_json(record 去掉 hash))
```

首条 `prev_hash` 为 64 个 0；写入即 `flush + fsync`；`AuditLog.open(resume=True)` 在续写前先校验整链。
敏感字段（password/token/cookie/authorization…）递归脱敏为 `<redacted:前8位>`。

## 后果
- 正面：`trina audit verify` 能定位篡改/插入/删除到具体序号（测试里有对应断言）；报告里可直接引用链末端哈希。
- 负面：写入有 fsync 开销（实测可忽略）；不能防"运行期被替换的伪造实现"（已在 `docs/GUARDRAILS.md` 诚实声明）。
- 组合效果：哈希链 + `ToolRegistry` 单一收口 + `state.json` 三方交叉，任何"少记一条"都会在复核时暴露。

## 被否决的方案
- 普通日志 + 口头保证：无法对抗"事后修改"。
- 外部不可篡改存储（如远程 syslog）：引入环境依赖，实训环境不可用（WSL 无 docker/外网受限）。
