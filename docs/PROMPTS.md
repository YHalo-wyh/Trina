# 比赛 Agent 提示词

2026-11-13：Trina 的稳定 system 定位为通用 CTF / 攻防比赛 Agent，涵盖 Web / API、Pwn、逆向、密码、取证、隐写、Misc、移动端、AI / ML 和综合题。题型决定验证方法，已注册工具决定可用能力。存在二进制工具只追加使用说明，不把对话和其他题型改成 Binary Worker。

系统保留 Engineering、Tool Policy、Exploration、Delegation、Workflow、Delivery、Completeness 七个模块。共同原则包括先读题和已有事实、验证一条端到端路径、按实际预算推进、停滞时改变方法、区分候选答案与权威判定，以及保存可复现工件和交接信息。跨题线索保留来源，并在当前题目上重新验证。

对话模式允许自然语言直接回答，普通问候不要求工具动作；分层提示词也不附加任务专用的“只允许工具或结束”协议。原生工具模式和 JSON 回退各自使用相符的运行合同。任务模式继续按工具动作和 final 协议执行。范围、工具 schema、预算、平台合同和动态事实由现有运行层提供。

## sapper-v2 参考

核对的是官方仓库 `N1nEmAn/sapper-v2` 的公开内容：

- [Commander standing orders](https://github.com/N1nEmAn/sapper-v2/blob/master/docker/commander/SKILL.md)：检查当前事实、按预算分配工作、复用已验证线索、停滞换方向、保存交接信息，以及核对 Flag 提交状态。
- [worker.sh 的 default_mission / general_mission](https://github.com/N1nEmAn/sapper-v2/blob/master/docker/commander/worker.sh)：自主推进、把发现记录为事实、及时验证与提交、明确报告阻塞，并继续可独立完成的替代路径。

这些原则经过改写后适配 Trina。sapper 的硬编码路径、实例和 Worker 数量、模型档位、密钥轮换、提交命令及固定时间表属于其部署，不进入 Trina system。提示词不承诺自动扩大工具、共享跨题状态或恢复旧调试进程。

## 生效与验证

新提示词在新建 Agent / 聊天初始化时加载，进程内已有会话与 `/resume` 保留保存的消息前缀。更新代码后重启 Trina 并使用 `/new`，可使新聊天采用新提示词。

`tests/test_brain.py`、`tests/test_binary.py` 和 `tests/test_tool_protocol.py` 验证任务与聊天定位、能力说明、两种提示词路径和动作协议；`tests/test_interactive.py` 验证控制台调用与错误回显。测试使用回放和本地夹具，不代表多题型实战得分或真实模型的响应时延测量。
