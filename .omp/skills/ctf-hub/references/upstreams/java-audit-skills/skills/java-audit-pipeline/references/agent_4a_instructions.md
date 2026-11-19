# Agent-4a-risk-classifier: 高危路由分级员 - 执行指令

## 角色信息

```
角色: agent-4a-risk-classifier (高危路由分级员)
等待: agent-1-route-mapper、agent-2-auth-audit、agent-3-vuln-scanner 全部完成
输出目录: {output_path}/cross_analysis/（已创建，直接写入）
输出文件: {output_path}/cross_analysis/high_risk_routes.md
```

## 执行步骤

1. 读取 agent-2-auth-audit 鉴权映射表，提取 ❌无鉴权 的路由
2. 读取 agent-2-auth-audit 鉴权绕过漏洞，提取 🔓可绕过鉴权 的路由
3. 读取 agent-3-vuln-scanner 漏洞报告，提取可导致鉴权绕过的组件漏洞，将受影响路由标记为 🔓可绕过
4. 读取 agent-1-route-mapper 路由主索引（`{output_path}/route_mapper/` 根目录下的 `*_route_mapper_*.md`），通过主索引中的模块链接定位各模块子目录下的详情文件，获取完整参数结构
5. 生成高危路由清单，按优先级排序：

| 优先级 | 条件 |
|:-------|:-----|
| P0 | ❌无鉴权 |
| P1 | 🔓可绕过鉴权（代码层绕过 + 组件漏洞导致绕过） |

## 输出 `high_risk_routes.md` 模板

```markdown
# 高危路由筛选清单

## 筛选概览

| 指标 | 数量 |
|:-----|:-----|
| 总路由数 | {从 agent-1-route-mapper 获取} |
| 无鉴权路由数 | {从 agent-2-auth-audit 获取} |
| 可绕过鉴权路由数 | {agent-2 鉴权绕过 + agent-3 组件绕过} |
| 高危路由总数 | {P0 + P1} |

## P0 - 无鉴权

| 路由 | 方法 | 鉴权状态 | 参数 | 来源文件 |
|:-----|:-----|:---------|:-----|:---------|
| /api/xxx | POST | ❌无鉴权 | param1, param2 | XxxController.java:行号 |
| /api/yyy | GET | ❌无鉴权 | id, name | YyyController.java:行号 |

## P1 - 可绕过鉴权

| 路由 | 方法 | 鉴权状态 | 绕过方式 | 参数 | 来源文件 |
|:-----|:-----|:---------|:---------|:-----|:---------|
| /admin/users | GET | 🔓可绕过 | Shiro 路径穿越 (H-AUTH-001) | page, size | AdminController.java:行号 |
| /api/config | POST | 🔓可绕过 | Tomcat AJP 绕过 (CVE-2020-1938) | key, value | ConfigController.java:行号 |

## 待追踪路由列表

以下路由必须进入阶段3调用链追踪（按 P0→P1 优先级排列）：

| 序号 | 优先级 | 路由 | 方法 | 追踪理由 |
|:-----|:-------|:-----|:-----|:---------|
| 1 | P0 | /api/xxx | POST | 无鉴权 |
| 2 | P0 | /api/yyy | GET | 无鉴权 |
| 3 | P1 | /admin/users | GET | 鉴权可绕过 |
| 4 | P1 | /api/config | POST | 鉴权可绕过 |
```

## 注意事项

- 追踪理由不包含具体 CVE 编号或绕过细节，避免干扰后续 agent 的代码审计重心
- P1 的绕过方式详情在分组表格中记录，但不传递到「待追踪路由列表」
