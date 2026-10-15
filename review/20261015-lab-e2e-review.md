# 复核记录 · 20261015-164542-2598dc

- 目标：`http://127.0.0.1:9099`
- 产品 Agent 自评：状态 **success** · 结论 **CONFIRMED**
- 复核者：产品 Agent 之外的角色（人签署；可由 `trina review` 生成初稿）

## 一、声明与证据核对

| # | 漏洞类 | 位置 | 状态 | 证据 | 判定 |
|---|---|---|---|---|---|
| 1 | security_misconfig_headers | `http://127.0.0.1:9099/` | detected | 1 条 | ✅ 有证据 |
| 2 | security_misconfig_headers | `http://127.0.0.1:9099/` | detected | 1 条 | ✅ 有证据 |
| 3 | security_misconfig_headers | `http://127.0.0.1:9099/` | detected | 1 条 | ✅ 有证据 |
| 4 | security_misconfig_headers | `http://127.0.0.1:9099/` | detected | 1 条 | ✅ 有证据 |
| 5 | information_disclosure | `http://127.0.0.1:9099/robots.txt` | detected | 1 条 | ✅ 有证据 |
| 6 | sqli | `http://127.0.0.1:9099/sqli` | verified | 1 条 | ✅ 有证据 |
| 7 | rce | `http://127.0.0.1:9099/rce` | verified | 3 条 | ✅ 有证据 |

## 二、结论一致性

- ✅ 结论状态与证据一致（无自相矛盾）

## 三、覆盖缺口（已测 / 未测 / 原因）

| 未测检查项 | 工具 | 原因 |
|---|---|---|
| dir_enum | dir_bruteforce | 未调度（步数/优先级不足） |
| poc_scan | poc_scan | 未调度（步数/优先级不足） |
| lfi_probe | lfi_test | 未调度（步数/优先级不足） |
| upload_probe | file_upload_test | 未调度（步数/优先级不足） |
| login_weak_cred | http_form_login | 未调度（步数/优先级不足） |
| ssh_weak_cred | weak_password_brute | 未调度（步数/优先级不足） |

## 四、边界事件（请求越界，执行层已阻断）

| 工具 | 类别 | 结果 | 原因 |
|---|---|---|---|
| rce_exec | action-level | blocked | 动作等级 exploit 超过当前上限 verify（工具 rce_exec） |

> 以上均为**请求越界**且被阻断；本系统不产生「实际越界」（执行链单点收口）。

## 五、审计与可追溯

- 审计文件：`/mnt/c/Users/WYH/Desktop/网安实训中级/logs/20261015-164542-2598dc.jsonl`
- 记录数：138 · 哈希链：完整 ✅
- 链校验：哈希链完整：138 条记录，末端 744a8ea8ac6a…

## 六、建议补测（走注册检查项，由控制器批准）

- `dir_enum`（dir_bruteforce）— 未调度（步数/优先级不足）；期望发现 information_disclosure
- `poc_scan`（poc_scan）— 未调度（步数/优先级不足）；期望发现 information_disclosure
- `lfi_probe`（lfi_test）— 未调度（步数/优先级不足）；期望发现 lfi
- `upload_probe`（file_upload_test）— 未调度（步数/优先级不足）；期望发现 unrestricted_file_upload
- `login_weak_cred`（http_form_login）— 未调度（步数/优先级不足）；期望发现 weak_password
- `ssh_weak_cred`（weak_password_brute）— 未调度（步数/优先级不足）；期望发现 weak_password

## 七、复核签署

| 项 | 内容 |
|---|---|
| 复核结论 | 采信 / 部分采信 / 退回补测 |
| 复核人 | （人填写：结对组/教师） |
| 日期 |  |
| 备注 | 如不采信，指出具体反例与最小复现 |

> 复核只读证据，不接管扫描器、不新增工具、不放宽目标清单。
