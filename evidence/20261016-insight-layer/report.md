# Trina 自动渗透测试报告

| 项目 | 内容 |
| --- | --- |
| 报告标题 | Trina 自动渗透测试报告 |
| 目标 | http://127.0.0.1:9099 |
| 目标说明 | - |
| Run ID | 20261016-165055-2598dc |
| 运行模式 | auto |
| 开始时间 | 2026-10-16 16:50:55 |
| 结束时间 | 2026-10-16 16:51:22 |
| 耗时 | 27.4 秒 |
| 最终状态 | 成功 |
| Agent 后端 / 模型 | omp / deepseek/deepseek-v4-flash |
| LLM 调用次数 | 6 |
| Token 用量 | 输入 8152 / 输出 1779 / 缓存读 35456 |
| LLM 成本（美元） | 0.004793 |
| 报告生成时间 | 2026-10-16 16:51:22 |
| 生成器 | Trina 0.1.0 |
| 元信息 · range |  |
| 元信息 · goal |  |
| 元信息 · play | 指定目标 |


## 一、执行摘要

风险统计：

| 严重度 | 数量 | 占比 |
| --- | ---: | ---: |
| 严重 | 0 | 0.0% |
| 高危 | 1 | 16.7% |
| 中危 | 0 | 0.0% |
| 低危 | 1 | 16.7% |
| 信息 | 4 | 66.7% |
| 合计 | 6 | 100.0% |

| 项目 | 内容 |
| --- | ---: |
| 主机数 | 1 |
| 开放端口数 | 2 |
| 凭据数 | 0 |
| Flag 数 | 1 |
| 漏洞总数 | 6 |
| 总步数 | 9 |
| LLM 调用次数 | 5 |
| LLM 成本（美元） | 0.004793 |

## 二、授权与合规声明

本次测试仅针对下列授权范围内的目标执行；所有主动动作均由护栏（ScopeGuard）判定，
放行/拒绝结果写入审计哈希链，未授权目标不产生任何流量。

护栏判据（与运行时同源，取自 `cfg.scope`）：

```text
授权网段: 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
额外授权主机: (无)
显式拒绝网段: (无)
允许公网目标: 否
当前最大动作等级: verify（detect 只读 / verify 可验证 / exploit 可执行命令）
破坏性动作: 关闭
全局速率上限: 20.0 req/s，单主机并发 8，爆破尝试上限 800
```

| 项目 | 内容 |
| --- | --- |
| 授权网段 | 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 |
| 额外授权主机 | （无） |
| 显式拒绝网段 | （无） |
| 允许公网目标 | 否 |
| 允许回环地址 | 是 |
| 最大动作等级 | verify（detect 只读 / verify 可验证 / exploit 可执行命令） |
| 破坏性动作 | 否 |
| 速率与规模上限 | 20.0 req/s，单主机并发 8，爆破尝试上限 800 次 |
| 护栏拒绝次数（运行态） | 0 |
| 护栏拒绝次数（审计） | （未提供审计摘要） |

## 三、攻击路径

```mermaid
flowchart LR
    n1["目标 http://127.0.0.1:9099"]
    n2["port_scan 端口扫描 127.0.0.1: 2 个开放端口 / 扫描 32 个 engine=builtin"]
    n3["? 端口扫描 127.0.0.1: 2 个开放端口 / 扫描 32 个 engine=builtin"]
    n4["进入阶段 enum"]
    n5["发现漏洞 信息 缺少安全响应头 Content-Security-Policy @ 127.0.0.1:9099"]
    n6["发现漏洞 信息 缺少安全响应头 X-Frame-Options @ 127.0.0.1:9099"]
    n7["发现漏洞 信息 缺少安全响应头 Strict-Transport-Security @ 127.0.0.1:9099"]
    n8["发现漏洞 信息 缺少安全响应头 X-Content-Type-Options @ 127.0.0.1:9099"]
    n9["…（省略 6 个中间步骤）"]
    n10["? 找到 1 个 flag：flag trina_lab_d9176a45"]
    n11["? 目标 http://127.0.0.1:9099 为 Apache 站点。已确认多个安全问题：高危 .git…"]
    n12["夺取 flag flag trina_lab_d9176a45"]
    n1 --> n2
    n2 --> n3
    n3 --> n4
    n4 --> n5
    n5 --> n6
    n6 --> n7
    n7 --> n8
    n8 --> n9
    n9 --> n10
    n10 --> n11
    n11 --> n12
```

上图为 mermaid 流程图源码；Markdown/HTML 预览器需支持 mermaid 才能渲染（报告不引入任何外部 JS）。

| # | 时刻 | 类型 | 事件 |
| ---: | --- | --- | --- |
| 1 | 16:50:55 | 目标 | 目标 http://127.0.0.1:9099 |
| 2 | 16:51:00 | 工具 | port_scan 端口扫描 127.0.0.1: 2 个开放端口 / 扫描 32 个 engine=builtin |
| 3 | 16:51:00 | 工具 | ? 端口扫描 127.0.0.1: 2 个开放端口 / 扫描 32 个 engine=builtin |
| 4 | 16:51:00 | 阶段 | 进入阶段 enum |
| 5 | 16:51:04 | 漏洞 | 发现漏洞 信息 缺少安全响应头 Content-Security-Policy @ 127.0.0.1:9099 |
| 6 | 16:51:04 | 漏洞 | 发现漏洞 信息 缺少安全响应头 X-Frame-Options @ 127.0.0.1:9099 |
| 7 | 16:51:04 | 漏洞 | 发现漏洞 信息 缺少安全响应头 Strict-Transport-Security @ 127.0.0.1:9099 |
| 8 | 16:51:04 | 漏洞 | 发现漏洞 信息 缺少安全响应头 X-Content-Type-Options @ 127.0.0.1:9099 |
| 9 | - | 省略 | …（省略 6 个中间步骤） |
| 10 | 16:51:17 | 工具 | ? 找到 1 个 flag：flag trina_lab_d9176a45 |
| 11 | 16:51:22 | 工具 | ? 目标 http://127.0.0.1:9099 为 Apache 站点。已确认多个安全问题：高危 .git… |
| 12 | 16:51:17 | 夺旗 | 夺取 flag flag trina_lab_d9176a45 |

## 四、资产清单

| 主机 | 存活 | 端口/服务/版本 | 技术栈 | 已发现路径 |
| --- | --- | --- | --- | --- |
| 127.0.0.1 | 是 | 22/tcp ssh; 3389/tcp rdp | Apache | http://127.0.0.1:9099/robots.txt; http://127.0.0.1:9099/sitemap.xml; http://127.0.0.1:9099/.well-known/security.txt; http://127.0.0.1:9099/.git/HEAD |

## 五、漏洞详情

| ID | 标题 | 严重度 | 置信度 | 状态 | 类型 | 位置 |
| --- | --- | --- | --- | --- | --- | --- |
| TRINA-006 | .git 目录可公开访问 | 高危 | 高 | 已发现 | info_disclosure | http://127.0.0.1:9099/.git/HEAD |
| TRINA-005 | robots.txt 泄露敏感路径 | 低危 | 中 | 已发现 | information_disclosure | http://127.0.0.1:9099/robots.txt |
| TRINA-001 | 缺少安全响应头 Content-Security-Policy | 信息 | 高 | 已发现 | security_misconfig_headers | http://127.0.0.1:9099 |
| TRINA-002 | 缺少安全响应头 X-Frame-Options | 信息 | 高 | 已发现 | security_misconfig_headers | http://127.0.0.1:9099 |
| TRINA-003 | 缺少安全响应头 Strict-Transport-Security | 信息 | 高 | 已发现 | security_misconfig_headers | http://127.0.0.1:9099 |
| TRINA-004 | 缺少安全响应头 X-Content-Type-Options | 信息 | 高 | 已发现 | security_misconfig_headers | http://127.0.0.1:9099 |

### [TRINA-006] .git 目录可公开访问

| 项目 | 内容 |
| --- | --- |
| ID | TRINA-006 |
| 严重度 | 高危（high） |
| 置信度 | 高（high） |
| 状态 | 已发现（detected） |
| 漏洞类型 | info_disclosure（归一: info_disclosure） |
| CVSS 向量 | 3.1 AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N（内置默认向量） |
| CWE | CWE-538（来自工具） |
| 目标 | http://127.0.0.1:9099 |
| URL | http://127.0.0.1:9099/.git/HEAD |
| 参数 / 方法 | - / GET |
| 发现工具 | poc_scan |
| 首次发现 | 2026-10-16 16:51:10 |
| 最近确认 | 2026-10-16 16:51:10 |

**描述**

.git/HEAD 可未授权访问，攻击者可据此还原完整源码与提交历史。

**影响**

（未提供影响说明）

**利用过程与证据**

1. [http] http://127.0.0.1:9099/.git/HEAD（-）
   - 说明: PoC git_leak 命中
   - 载荷: `poc:git_leak`
   - 请求: GET http://127.0.0.1:9099/.git/HEAD
   - 响应: 状态 200，长度 21｜响应片段: ref: refs/heads/main

**修复建议**（来自工具）

部署时移除版本控制元数据，或在 Web 入口屏蔽以 . 开头的路径。


### [TRINA-005] robots.txt 泄露敏感路径

| 项目 | 内容 |
| --- | --- |
| ID | TRINA-005 |
| 严重度 | 低危（low） |
| 置信度 | 中（medium） |
| 状态 | 已发现（detected） |
| 漏洞类型 | information_disclosure（归一: information_disclosure） |
| CVSS 向量 | 3.1 AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N（内置默认向量） |
| CWE | CWE-200（来自工具） |
| 目标 | - |
| URL | http://127.0.0.1:9099/robots.txt |
| 参数 / 方法 | - / GET |
| 发现工具 | - |
| 首次发现 | 2026-10-16 16:51:04 |
| 最近确认 | 2026-10-16 16:51:04 |

**描述**

robots.txt 中列出了管理后台/备份/源码等敏感路径，可直接作为攻击入口。

**影响**

攻击者据此外推后台与备份文件位置，降低入侵成本。

**利用过程与证据**

1. [http] http://127.0.0.1:9099/robots.txt（-）
   - 说明: robots.txt 命中敏感路径关键词: admin, backup, git, upload
   - 请求: GET http://127.0.0.1:9099/robots.txt
   - 响应: 状态 200，长度 86｜响应片段: User-agent: * Disallow: /admin Disallow: /backup Disallow: /.git/ Disallow: /uploads/

**修复建议**（来自工具）

robots.txt 不应列出敏感路径；重要目录改用鉴权与不可枚举命名，并移除线上备份文件。


### [TRINA-001] 缺少安全响应头 Content-Security-Policy

| 项目 | 内容 |
| --- | --- |
| ID | TRINA-001 |
| 严重度 | 信息（info） |
| 置信度 | 高（high） |
| 状态 | 已发现（detected） |
| 漏洞类型 | security_misconfig_headers（归一: security_misconfig_headers） |
| CVSS 向量 | 3.1 AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N（内置默认向量） |
| CWE | CWE-693（内置默认） |
| 目标 | http://127.0.0.1:9099 |
| URL | http://127.0.0.1:9099 |
| 参数 / 方法 | Content-Security-Policy / GET |
| 发现工具 | http_fingerprint |
| 首次发现 | 2026-10-16 16:51:04 |
| 最近确认 | 2026-10-16 16:51:04 |

**描述**

缺少 CSP，可能放大 XSS 与内容注入风险

**影响**

（未提供影响说明）

**利用过程与证据**

1. [http] http://127.0.0.1:9099（-）
   - 说明: 响应头缺失: Content-Security-Policy
   - 请求: GET http://127.0.0.1:9099
   - 响应: 状态 200，长度 342｜响应片段: <!DOCTYPE html><html><head><title>Trina Lab</title></head><body> <h1>Trina 脆弱靶机</h1> <ul> <li><a href="/sqli?id=1">/sqli?id=1</a></li> <li><a href="/lfi?file=readme.txt">/lfi?file=readme.txt</a></li>…

**修复建议**（来自工具）

在 Web 服务器/反代上补充响应头 Content-Security-Policy


### [TRINA-002] 缺少安全响应头 X-Frame-Options

| 项目 | 内容 |
| --- | --- |
| ID | TRINA-002 |
| 严重度 | 信息（info） |
| 置信度 | 高（high） |
| 状态 | 已发现（detected） |
| 漏洞类型 | security_misconfig_headers（归一: security_misconfig_headers） |
| CVSS 向量 | 3.1 AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N（内置默认向量） |
| CWE | CWE-693（内置默认） |
| 目标 | http://127.0.0.1:9099 |
| URL | http://127.0.0.1:9099 |
| 参数 / 方法 | X-Frame-Options / GET |
| 发现工具 | http_fingerprint |
| 首次发现 | 2026-10-16 16:51:04 |
| 最近确认 | 2026-10-16 16:51:04 |

**描述**

缺少 X-Frame-Options，页面可能被 iframe 嵌套（点击劫持）

**影响**

（未提供影响说明）

**利用过程与证据**

1. [http] http://127.0.0.1:9099（-）
   - 说明: 响应头缺失: X-Frame-Options
   - 请求: GET http://127.0.0.1:9099
   - 响应: 状态 200，长度 342｜响应片段: <!DOCTYPE html><html><head><title>Trina Lab</title></head><body> <h1>Trina 脆弱靶机</h1> <ul> <li><a href="/sqli?id=1">/sqli?id=1</a></li> <li><a href="/lfi?file=readme.txt">/lfi?file=readme.txt</a></li>…

**修复建议**（来自工具）

在 Web 服务器/反代上补充响应头 X-Frame-Options


### [TRINA-003] 缺少安全响应头 Strict-Transport-Security

| 项目 | 内容 |
| --- | --- |
| ID | TRINA-003 |
| 严重度 | 信息（info） |
| 置信度 | 高（high） |
| 状态 | 已发现（detected） |
| 漏洞类型 | security_misconfig_headers（归一: security_misconfig_headers） |
| CVSS 向量 | 3.1 AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N（内置默认向量） |
| CWE | CWE-693（内置默认） |
| 目标 | http://127.0.0.1:9099 |
| URL | http://127.0.0.1:9099 |
| 参数 / 方法 | Strict-Transport-Security / GET |
| 发现工具 | http_fingerprint |
| 首次发现 | 2026-10-16 16:51:04 |
| 最近确认 | 2026-10-16 16:51:04 |

**描述**

缺少 HSTS，HTTPS 可被降级到明文 HTTP

**影响**

（未提供影响说明）

**利用过程与证据**

1. [http] http://127.0.0.1:9099（-）
   - 说明: 响应头缺失: Strict-Transport-Security
   - 请求: GET http://127.0.0.1:9099
   - 响应: 状态 200，长度 342｜响应片段: <!DOCTYPE html><html><head><title>Trina Lab</title></head><body> <h1>Trina 脆弱靶机</h1> <ul> <li><a href="/sqli?id=1">/sqli?id=1</a></li> <li><a href="/lfi?file=readme.txt">/lfi?file=readme.txt</a></li>…

**修复建议**（来自工具）

在 Web 服务器/反代上补充响应头 Strict-Transport-Security


### [TRINA-004] 缺少安全响应头 X-Content-Type-Options

| 项目 | 内容 |
| --- | --- |
| ID | TRINA-004 |
| 严重度 | 信息（info） |
| 置信度 | 高（high） |
| 状态 | 已发现（detected） |
| 漏洞类型 | security_misconfig_headers（归一: security_misconfig_headers） |
| CVSS 向量 | 3.1 AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N（内置默认向量） |
| CWE | CWE-693（内置默认） |
| 目标 | http://127.0.0.1:9099 |
| URL | http://127.0.0.1:9099 |
| 参数 / 方法 | X-Content-Type-Options / GET |
| 发现工具 | http_fingerprint |
| 首次发现 | 2026-10-16 16:51:04 |
| 最近确认 | 2026-10-16 16:51:04 |

**描述**

缺少 X-Content-Type-Options，浏览器可能 MIME 嗅探导致 XSS

**影响**

（未提供影响说明）

**利用过程与证据**

1. [http] http://127.0.0.1:9099（-）
   - 说明: 响应头缺失: X-Content-Type-Options
   - 请求: GET http://127.0.0.1:9099
   - 响应: 状态 200，长度 342｜响应片段: <!DOCTYPE html><html><head><title>Trina Lab</title></head><body> <h1>Trina 脆弱靶机</h1> <ul> <li><a href="/sqli?id=1">/sqli?id=1</a></li> <li><a href="/lfi?file=readme.txt">/lfi?file=readme.txt</a></li>…

**修复建议**（来自工具）

在 Web 服务器/反代上补充响应头 X-Content-Type-Options


## 六、证据链与假设台账

假设台账:共 4 条（待验证 0、验证中 2、已确认 2、已否定 0、被阻断 0）

| ID | 状态 | 置信度 | 优先级 | 假设 | 下一步 | 证据数 |
| --- | --- | ---: | ---: | --- | --- | ---: |
| H-001 | 验证中 | 0.70 | 0.53 | 目标 127.0.0.1:9099 处于开放状态且为 HTTP 服务 | 对 9099 做 http_fingerprint 识别技术栈与标题 | 0 |
| H-002 | 已确认 | 0.90 | 0.00 | 127.0.0.1:9099 是运行中的 HTTP 服务（Lab 靶场） | - | 5 |
| H-003 | 已确认 | 0.90 | 0.00 | 目标存在常见敏感文件泄露（.git/.env/备份/phpinfo 等） | - | 1 |
| H-004 | 验证中 | 0.70 | 0.53 | 9099 站点存在可直接读取的 flag 文件或 .git 泄露的敏感文件 | 若命中 flag 则收尾出报告；否则对 .git 内容做源码还原，再找参数化接口做注入验证 | 0 |

证据关系图（可用支持 mermaid 的 Markdown 查看器渲染）：

```mermaid
flowchart LR
  nfinding_TRINA_001{info security_misconfig_headers detected}
  nfinding_TRINA_002{info security_misconfig_headers detected}
  nfinding_TRINA_003{info security_misconfig_headers detected}
  nfinding_TRINA_004{info security_misconfig_headers detected}
  nfinding_TRINA_005{low information_disclosure detected}
  nfinding_TRINA_006{high info_disclosure detected}
  nevi_TRINA_001_1[(http · 响应头缺失: Content-Security-Policy)]
  nevi_TRINA_002_1[(http · 响应头缺失: X-Frame-Options)]
  nevi_TRINA_003_1[(http · 响应头缺失: Strict-Transport-Security)]
  nevi_TRINA_004_1[(http · 响应头缺失: X-Content-Type-Options)]
  nevi_TRINA_005_1[(http · robots.txt 命中敏感路径关键词: admin, backup)]
  nevi_TRINA_006_1[(http · PoC git_leak 命中)]
  nflag_flag_trina_lab_d9176a45_((flagtrina_lab_d9176a45))
  nhypo_H_001>H-001 验证中 目标 127.0.0.1:9099 处于开放状态且为 HTT]
  nhypo_H_002>H-002 已确认 127.0.0.1:9099 是运行中的 HTTP 服务（L]
  nhypo_H_003>H-003 已确认 目标存在常见敏感文件泄露（.git/.env/备份/phpi]
  nhypo_H_004>H-004 验证中 9099 站点存在可直接读取的 flag 文件或 .git]
  ncheck_flag_read[[flag_read]]
  ncheck_http_fingerprint[[http_fingerprint（http_fingerprint）]]
  ncheck_poc_scan[[poc_scan（poc_scan）]]
  ncheck_port_quick[[port_quick]]
  npath_http___127_0_0_1_9099__git_HEAD[/http://127.0.0.1:9099/.git/HEAD/]
  npath_http___127_0_0_1_9099__well_known_security_txt[/http://127.0.0.1:9099/.well-known/security/]
  npath_http___127_0_0_1_9099_robots_txt[/http://127.0.0.1:9099/robots.txt/]
  npath_http___127_0_0_1_9099_sitemap_xml[/http://127.0.0.1:9099/sitemap.xml/]
  ntech_127_0_0_1_Apache[/Apache/]
  nport_127_0_0_1_22[22/ssh]
  nport_127_0_0_1_3389[3389/rdp]
  nhost_127_0_0_1([127.0.0.1])
  ntarget_http___127_0_0_1_9099([http://127.0.0.1:9099])
  ntarget_http___127_0_0_1_9099 -->|测试对象| nhost_127_0_0_1
  nhost_127_0_0_1 -->|开放| nport_127_0_0_1_22
  nhost_127_0_0_1 -->|开放| nport_127_0_0_1_3389
  nhost_127_0_0_1 -->|技术栈| ntech_127_0_0_1_Apache
  nhost_127_0_0_1 -->|发现| npath_http___127_0_0_1_9099_robots_txt
  nhost_127_0_0_1 -->|发现| npath_http___127_0_0_1_9099_sitemap_xml
  nhost_127_0_0_1 -->|发现| npath_http___127_0_0_1_9099__well_known_security_txt
  nhost_127_0_0_1 -->|发现| npath_http___127_0_0_1_9099__git_HEAD
  nhost_127_0_0_1 -->|命中| nfinding_TRINA_001
  nfinding_TRINA_001 -->|证据| nevi_TRINA_001_1
  ncheck_http_fingerprint -->|产出| nfinding_TRINA_001
  nhost_127_0_0_1 -->|命中| nfinding_TRINA_002
  nfinding_TRINA_002 -->|证据| nevi_TRINA_002_1
  ncheck_http_fingerprint -->|产出| nfinding_TRINA_002
  nhost_127_0_0_1 -->|命中| nfinding_TRINA_003
  nfinding_TRINA_003 -->|证据| nevi_TRINA_003_1
  ncheck_http_fingerprint -->|产出| nfinding_TRINA_003
  nhost_127_0_0_1 -->|命中| nfinding_TRINA_004
  nfinding_TRINA_004 -->|证据| nevi_TRINA_004_1
  ncheck_http_fingerprint -->|产出| nfinding_TRINA_004
  npath_http___127_0_0_1_9099_robots_txt -->|命中| nfinding_TRINA_005
  nfinding_TRINA_005 -->|证据| nevi_TRINA_005_1
  npath_http___127_0_0_1_9099__git_HEAD -->|命中| nfinding_TRINA_006
  nfinding_TRINA_006 -->|证据| nevi_TRINA_006_1
  ncheck_poc_scan -->|产出| nfinding_TRINA_006
  ntarget_http___127_0_0_1_9099 -->|已执行| ncheck_flag_read
  ntarget_http___127_0_0_1_9099 -->|已执行| ncheck_http_fingerprint
  ntarget_http___127_0_0_1_9099 -->|已执行| ncheck_poc_scan
  ntarget_http___127_0_0_1_9099 -->|已执行| ncheck_port_quick
  nhypo_H_002 -->|验证为| nfinding_TRINA_001
  nhypo_H_002 -->|验证为| nfinding_TRINA_002
  nhypo_H_002 -->|验证为| nfinding_TRINA_003
  nhypo_H_002 -->|验证为| nfinding_TRINA_004
  nhypo_H_002 -->|验证为| nfinding_TRINA_005
  nhypo_H_003 -->|验证为| nfinding_TRINA_006
  ntarget_http___127_0_0_1_9099 -->|夺取| nflag_flag_trina_lab_d9176a45_
```

证据图：30 个节点（check 4，evidence 6，finding 6，flag 1，host 1，hypothesis 4，path 5，port 2，target 1）、36 条关系

| 起点 | 关系 | 终点 |
| --- | --- | --- |
| target:http://127.0.0.1:9099 | 测试对象 | host:127.0.0.1 |
| host:127.0.0.1 | 开放 | port:127.0.0.1:22 |
| host:127.0.0.1 | 开放 | port:127.0.0.1:3389 |
| host:127.0.0.1 | 技术栈 | tech:127.0.0.1:Apache |
| host:127.0.0.1 | 发现 | path:http://127.0.0.1:9099/robots.txt |
| host:127.0.0.1 | 发现 | path:http://127.0.0.1:9099/sitemap.xml |
| host:127.0.0.1 | 发现 | path:http://127.0.0.1:9099/.well-known/security.txt |
| host:127.0.0.1 | 发现 | path:http://127.0.0.1:9099/.git/HEAD |
| host:127.0.0.1 | 命中 | finding:TRINA-001 |
| finding:TRINA-001 | 证据 | evi:TRINA-001:1 |
| check:http_fingerprint | 产出 | finding:TRINA-001 |
| host:127.0.0.1 | 命中 | finding:TRINA-002 |
| finding:TRINA-002 | 证据 | evi:TRINA-002:1 |
| check:http_fingerprint | 产出 | finding:TRINA-002 |
| host:127.0.0.1 | 命中 | finding:TRINA-003 |
| finding:TRINA-003 | 证据 | evi:TRINA-003:1 |
| check:http_fingerprint | 产出 | finding:TRINA-003 |
| host:127.0.0.1 | 命中 | finding:TRINA-004 |
| finding:TRINA-004 | 证据 | evi:TRINA-004:1 |
| check:http_fingerprint | 产出 | finding:TRINA-004 |
| path:http://127.0.0.1:9099/robots.txt | 命中 | finding:TRINA-005 |
| finding:TRINA-005 | 证据 | evi:TRINA-005:1 |
| path:http://127.0.0.1:9099/.git/HEAD | 命中 | finding:TRINA-006 |
| finding:TRINA-006 | 证据 | evi:TRINA-006:1 |
| check:poc_scan | 产出 | finding:TRINA-006 |
| target:http://127.0.0.1:9099 | 已执行 | check:flag_read |
| target:http://127.0.0.1:9099 | 已执行 | check:http_fingerprint |
| target:http://127.0.0.1:9099 | 已执行 | check:poc_scan |
| target:http://127.0.0.1:9099 | 已执行 | check:port_quick |
| hypo:H-002 | 验证为 | finding:TRINA-001 |
| hypo:H-002 | 验证为 | finding:TRINA-002 |
| hypo:H-002 | 验证为 | finding:TRINA-003 |
| hypo:H-002 | 验证为 | finding:TRINA-004 |
| hypo:H-002 | 验证为 | finding:TRINA-005 |
| hypo:H-003 | 验证为 | finding:TRINA-006 |
| target:http://127.0.0.1:9099 | 夺取 | flag:flag{trina_lab_d9176a45} |

## 七、凭据与 Flag

### 有效凭据

（未获取凭据）

### Flag

| # | Flag | 来源 | 时间 |
| ---: | --- | --- | --- |
| 1 | flag{trina_lab_d9176a45} | http://127.0.0.1:9099/flag | 16:51:17 |

## 八、附录

### 工具调用时间线

| 时刻 | 工具 | 参数 | 结果摘要 | 耗时 |
| --- | --- | --- | --- | --- |
| 16:51:00 | port_scan | target=127.0.0.1 ports=quick timeout=1.0 max_ports=4096 | 端口扫描 127.0.0.1: 2 个开放端口 / 扫描 32 个 (engine=builtin) | 0.51s |
| 16:51:00 | ? | target=127.0.0.1 ports=quick | 端口扫描 127.0.0.1: 2 个开放端口 / 扫描 32 个 (engine=builtin) | 0.51s |
| 16:51:04 | http_fingerprint | url=http://127.0.0.1:9099 | HTTP 指纹 http://127.0.0.1:9099: 200 title=Trina Lab tech=Apache 缺失安全头 4 个 | 0.26s |
| 16:51:04 | ? | url=http://127.0.0.1:9099 | HTTP 指纹 http://127.0.0.1:9099: 200 title=Trina Lab tech=Apache 缺失安全头 4 个 | 0.26s |
| 16:51:10 | poc_scan | url=http://127.0.0.1:9099 | 命中 1 个 PoC: git_leak(high) | 1.61s |
| 16:51:10 | ? | url=http://127.0.0.1:9099 | 命中 1 个 PoC: git_leak(high) | 1.61s |
| 16:51:17 | flag_hunt | target=127.0.0.1 base_url=http://127.0.0.1:9099 include_local=False | 找到 1 个 flag：flag{trina_lab_d9176a45} | 0.97s |
| 16:51:17 | ? | target=127.0.0.1 base_url=http://127.0.0.1:9099 include_local=False | 找到 1 个 flag：flag{trina_lab_d9176a45} | 0.97s |
| 16:51:22 | ? | - | 目标 http://127.0.0.1:9099 为 Apache 站点。已确认多个安全问题：高危 .git 源码/元数据泄露（/.git/HEAD 可读，TRINA-006 high）、robots.txt 信息泄露，以及缺失 CSP/X-Frame-Options/HSTS/X-Content-Type-Options 等安全响应头。最终在内置路径清单中通过 HTTP 读取到 flag 文件 | 0.00s |

### 指标

| 项目 | 内容 |
| --- | ---: |
| 运行耗时 | 27.4 秒 |
| 总步数 | 9 |
| 工具调用次数 | 4 |
| LLM 调用次数 | 5 |
| LLM 成本（美元） | 0.004793 |
| 护栏拒绝次数 | 0 |
| 错误计数 | 0 |
| 主机数 | 1 |
| 开放端口数 | 2 |
| 凭据数 | 0 |
| Flag 数 | 1 |
| 漏洞数 | 6 |
| HTTP 请求数 | 11（来源: evidence（受证据上限约束）） |

### 审计摘要

（未提供审计摘要）
