# ADR-0022：标准消息、直接 API 与流式输出

日期：2026-11-12

状态：Accepted

2026-11-13 补充：正文和工具窗口的展示方式、统一运行事件及续流由 [ADR-0027](0027-unified-runtime-events.md) 更新；下文保留标准消息与 API 流协议的原决策。

适用版本：0.3.0 开发分支

## 问题

对话入口原先把近期 user/assistant 历史压成带角色标签的大字符串，再作为一条 user 消息发送。真实角色边界因此丢失，模型服务无法按原生 Chat Template 处理多轮历史。每轮重新生成摘要及裁剪文本还会改变已有前缀。在线后端使用 `stream: false`，终端只能等待完整响应。

OMP 进程后端引入第二套模型目录、凭据来源与持久化字段。供应商管理已经能够保存地址、Key、协议、模型和思考设置，继续保留该后端会增加配置分支与错误回退路径。

## 决定

1. 后端只保留标准 API 与离线 Replay。删除 `omp_backend.py`、OMP 模型数据库读取、`/backend` 入口及 `omp_model` 持久化。`.omp` 项目技能与工具扩展继续作为外部集成存在。
2. Agent 调用 `complete_messages()`，保留 `role/content`、assistant 回复与原生工具回执。Chat 使用 `messages`；Messages 使用 `system + messages`；Responses 使用 `instructions + input` 数组。禁止以角色标签字符串模拟多轮对话。
3. system 和初始运行合同固定在前缀；动态状态、观察和人工补充只追加。模型切换保留消息历史，不修改旧消息。缓存命中只采用供应商报告，不把本地请求字节一致直接等同于缓存命中。
4. 在线 Agent 使用 SSE；方言转换后产生 start、text_delta、thinking_delta、done、error。健康检查与模型表单短验证继续使用非流式接口。
5. 终端原位刷新正文卡片，解析前隐藏 JSON/DSML 后缀；思考增量不作为正文。流中断、无终止事件、非法 SSE、输出截断或供应商错误均返回失败，未完成卡片撤回。最终答复只保留一次。

## 代价与限制

历史改为追加消息后，较长会话会消耗更多输入 Token；当前没有自动 Token 压缩。上下文容量不足由供应商返回错误，可通过 `/new` 开新会话。稳定前缀只能提供缓存复用条件，各供应商的最小缓存长度、保留时间与计量方式仍不同。

正文包含代码和括号时，部分后缀可能先缓冲、完成后再显示，以免工具协议短暂出现在终端。原生工具调用的供应商支持范围由当前协议实现决定，兼容失败仍须明确记录。流式输出不改变工具审计和执行租约。

## 验证

`tests/test_llm_streaming.py` 检查真实回环 HTTP 在响应结束前交付增量、三种 API 的结构化请求、多轮前缀继承、终止事件校验、协议隐藏和终端草稿保留。`tests/test_llm_openai.py`、`test_providers.py` 检查用量及移除 OMP 后的配置生命周期。当前产品机器规则为 TR-03-R03、TR-04-R03。

本地协议测试证明请求与解析机制，不替代所有付费供应商的真实账户兼容性测试。

## 参考

- [Oh My Pi 流事件传递](https://github.com/can1357/oh-my-pi/blob/main/docs/provider-streaming-internals.md)
- [Oh My Pi 接口方言约束](https://github.com/can1357/oh-my-pi/blob/main/docs/provider-endpoint-constraints.md)
