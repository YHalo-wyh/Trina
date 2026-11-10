# 执行信任模式

Trina 0.3.0 不再默认把模型的工具申请交给目标白名单和风险等级逐项裁决。默认配置为：

```yaml
scope:
  trust_mode: full
```

这表示当前任务中的已注册工具默认可信，不弹人工审批，也不因目标、公网属性、动作等级、破坏性方法、载荷内容或尝试规模被 `ScopeGuard` 拒绝。

## 1. 两种模式

| 模式 | 默认 | 行为 |
|---|---:|---|
| `full` | 是 | 合法格式的目标和已注册工具直接执行；授权类规则只作为兼容配置保留 |
| `restricted` | 否 | 恢复 0.2.0 的目标白名单、动作等级、破坏性开关、禁止模式、同源重定向和爆破规模限制 |

临时切换：

```bash
trina run --target <目标> --set scope.trust_mode=restricted
```

永久切换：修改 `configs/trina.yaml` 中的 `scope.trust_mode`。

## 2. full 模式的实际裁决

`full` 不是只改提示文字，而是改变 `trina/scope.py` 的真实执行判据：

- 任意格式合法的 IP、域名、URL、CIDR 均放行；公网和特殊地址不再查授权网段。
- DNS 预解析失败不拒绝，真实连接结果由工具返回。
- `detect`、`verify`、`exploit` 均放行，`exploit_level` 不作为上限。
- `destructive` 和 `destructive_hosts` 不作为闸门。
- 旧的参数与载荷禁止模式不参与拒绝，包括 `--os-shell`、写文件、数据修改等模式。
- `max_brute_attempts` 不作为拒绝上限。
- HTTP 跨源重定向可继续执行，每一跳仍记录执行信息。
- `.omp/extensions/trina.ts` 不再拦截 omp 内建 bash，也不要求 `/pentest` 先确认授权。

## 3. 始终保留的执行机制

完全信任不等于删除工具封装。以下机制在两种模式下都保留：

- `ToolRegistry` 的工具名、参数类型、必填项、枚举值和可用性校验。
- `trina/scope.py` 作为 HTTP 与外部进程的统一执行入口。
- 工具超时、取消信号和执行租约；超时后的晚到线程不能提交状态或发起新的受控 I/O。
- `max_qps` 与单主机并发控制，避免实现层失控；它们只节流，不要求人工确认。
- `tool.start/tool.end/guard.allow` 等审计事件及 SHA-256 哈希链。
- URL/目标的基本格式校验，以及 HTTP 出口只接受 `http(s)`。
- TSecBench 控制面独立白名单。控制面涉及题目领取与提交，不继承渗透目标的 full trust。
- `AuthorizationController` 的预算、停止合同和显式撤权；默认 `policy.mode=free`，没有预装注册表限制。

## 4. restricted 兼容模式

切换到 `restricted` 后，下列字段重新参与拒绝：

- `authorized_cidrs`、`authorized_hosts`、`deny_cidrs`
- `allow_public`、`allow_loopback`、`resolve_dns`
- `exploit_level`
- `destructive`、`destructive_hosts`
- `max_brute_attempts`
- 参数和载荷禁止模式

受限模式用于需要复现旧评测、课堂演示边界或对特定任务主动收紧的场景。它不再是默认行为。

## 5. 人工审批

`scope.trust_mode` 与人工审批是两个独立开关：

- 默认 `approve=off`：控制台、`play`、裸 `run` 和定时任务都不等待确认。
- 只有显式使用 `--approve exploit|verify|all` 或控制台 `/approve` 才会出现审批。
- full 模式下显式开启审批仍然有效，因为这是使用者主动选择的人机闸门。

## 6. 验证

```bash
python -m trina guard check 8.8.8.8
python -m unittest tests.test_scope tests.test_registry tests.test_brain -v
python -m unittest discover -s tests
```

测试必须同时覆盖默认 full 的直接放行和显式 restricted 的旧拒绝路径。
