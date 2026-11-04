# TSecBench × OpenCode Go 接入与冒烟记录

> 记录日期：2026-11-04（UTC+08:00）
> 题集：`bctf-2026`，托管 Agent 模式，37 题 / 22210 分
> 结论：OpenCode Go 密钥与 Chat Completions 直连可用；托管网关未观察到任何转发到 OpenCode 的请求。完整 37 题评测暂未启动。

## 接入参数

| 项目 | 值 |
|---|---|
| 原始 Base URL | `https://opencode.ai/inference/go/openai/v1` |
| 平台改写 Base URL | `http://opencode.ai.tsecbench.gw/inference/go/openai/v1` |
| 模型 | `glm-5.3-flash`（Go 套餐中已启用） |
| 接口 | `POST /chat/completions` |
| 密钥 | 仅通过平台运行时 `TRINA_LLM_API_KEY` 注入；不写入仓库和镜像 |
| 必需请求头 | `x-opencode-session`：同一 Agent 对话保持稳定；`User-Agent: Trina/0.2.0` |

TSecBench 托管页要求模型 API 先进入白名单，然后才按域名添加 `.tsecbench.gw` 并将 HTTPS 改为 HTTP。2026-11-04 页面展示的 24 项白名单中没有 `opencode.ai`。因此改写后的地址格式正确，并不等于该网关允许转发。

## 验证过程与证据

1. 原版 `OpenAICompatBackend` 未发送 `x-opencode-session`。对 OpenCode Go 直连的最小 Chat Completions 请求返回 HTTP 400，提示缺少该会话头。
2. 加入稳定会话头后，同一密钥、模型与接口的直连请求返回 HTTP 200，包含 1 个 `choices`。这验证了密钥、模型 ID 和接口路径。
3. [Run 43784](https://tsecbench.zc.tencent.com/agent/43784)：复用 2026-11-02 上传的 v2 镜像；设置 `TRINA_BENCH_LIMIT=1`、`TRINA_BENCH_MAX_STEPS=3`。平台启动并关闭 1 道题，模型会话 0，得分 0，运行状态 `timeout`。
4. 为排除缺少会话头这一变量，将当前 Trina 0.2.0 源码及请求头修复覆盖到已验证的 OCI 镜像。新镜像 `trina-tsecbench-go-v020.tar.gz` 的 SHA-256 为 `8656bbbbc25cec4a89b4c5b1c3a96d6b7ae717a1f3be6a5474fb191a409cdf16`；OCI index、manifest、config、镜像层摘要和层数均完成校验，镜像层不包含密钥。
5. [Run 43799](https://tsecbench.zc.tencent.com/benchmark/running/43799)：同样只跑 1 道题、最多 3 步。平台启动并关闭容器，最终 `timeout`，得分 0，模型使用记录仍为空。OpenCode 控制台的最近请求日志没有出现这轮运行的请求；最近的 `glm-5.3-flash` 记录是步骤 2 的直连测试。
6. 本地 `python -m unittest discover -s tests`：304 项通过。Docker Desktop Engine 在当前 Windows 会话不可用，所以新镜像从已有 OCI 归档叠加源码层构建；没有把旧归档或运行产物提交到 Git。

## 判断与后续条件

两轮托管运行都成功经过平台答题 API 的题目启动/关闭，但没有模型会话；加入必需的会话头后结果不变。结合平台白名单缺少 `opencode.ai`、OpenCode 侧没有对应请求记录，**最有依据的判断是平台网关没有把请求转发到 OpenCode Go**。平台运行页未提供可回看的容器 stdout，因此目前没有网关返回码的直接证据。

继续完整评测需要平台将 `https://opencode.ai/*` 加入大模型 API 白名单，或改用现有白名单服务商的密钥。放行后先用本页的单题冒烟配置确认平台出现模型会话，再取消 `TRINA_BENCH_LIMIT` 和 `TRINA_BENCH_MAX_STEPS` 覆盖，启动完整 37 题评测。
