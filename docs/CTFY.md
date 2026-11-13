# CTFY v3 评测接入

日期：2026-11-13。依据 [CTFY-0001](https://ctfy.tsinghua.edu.cn/docs/standards/CTFY-0001) 和平台公开的 [OpenAPI](https://ctfy.tsinghua.edu.cn/openapi.json) 实现。

## 1. 已实现范围

`python -m trina ctfy --work-dir /ctfy/work` 读取平台提供的 `task.json`、题目说明和附件，使用环境中的 LLM 网关运行 ChatAgent。当前支持 `instance` 与 `qa` 两类提交；`defence`、AWD 等持续轮次模式会写明“不支持”并退出，不能被标成已执行。

工作目录和结果文件遵循 v3：输入只从指定 work dir 读取，结果原子写入固定 `result.json`。`--dry-run` 不调用模型、不读取个人供应商配置，也不提交答案，只生成合法的 dry-run 结果。

## 2. 环境契约

| 环境变量 | 规则 |
|---|---|
| `CTFY_WORK_DIR` | 默认 `/ctfy/work` |
| `CTFY_TOKEN` | 平台控制面 Bearer Token；不写入日志或结果 |
| `LLM_FLAVOR` | `openai` 或 `anthropic` |
| `LLM_MODEL` | 平台网关分配的模型 |
| `LLM_BASE_URL` / `LLM_API_KEY` | 必须成对来自平台；禁止回退个人 providers.json |
| `CTFY_EVAL_MAX_TURNS` | 额外限制模型轮次，默认不超过 40 |

控制面请求继续通过 `HttpClient(control_plane=True)` 和固定 API 路径，关闭自动重试，避免错误答案重复扣次数。模型与提交 Token 只驻留于内存和请求头。

## 3. 权威判定

`ctfy_task` 工具提供 `read`、`refresh`、`submit`。实例题提交到 `/api/v1/submissions`，QA 提交到 `/api/v1/qa/submissions`。只有响应的 question ID 与当前解锁问题匹配且 `correct=true` 时，运行器才更新 solved；stdout 中出现类似 Flag 的文本、模型 final 或 `binary_execute` 退出码均不能替代平台响应。

正确提交后刷新题目，支持 gated question 解锁；错误或 pending 保留平台原始状态。结果文件记录实际完成状态、错误原因和用量，绝不把计划或模拟响应伪装为线上得分。

## 4. 验证边界

`tests/test_ctfy.py` 使用真实本地 HTTP 服务分别模拟平台提交、题目刷新和 SSE 模型网关，检查 instance/QA 请求形状、正确/错误/pending、门控刷新、dry-run、缺少网关变量以及一条完整 Agent 调用链。这是协议夹具，不是 CTFY 线上评测，也没有消耗付费模型或取得平台分数。

正式运行前仍需由平台提供真实 `task.json`、Token、LLM 网关和实例。平台创建实例、最终答案裁决与竞赛计时由平台负责，Trina 不自行创建靶机或绕过当前问题顺序。

仓库提供 `Dockerfile.ctfy`。该镜像安装 r2/GDB/GCC，并以非 root 用户运行；由于平台容器本身是外层沙箱，镜像显式设置 `TRINA_BINARY_SANDBOX=none`，不在容器内嵌套 bubblewrap。此设置不会关闭 ToolRegistry、任务路径所有权、能力租约、超时、进程树清理或审计。若运行环境允许 user namespace，也可删除该环境变量，改回 `binary.sandbox=bwrap` 后重新验证。

```bash
docker build -f Dockerfile.ctfy -t trina-ctfy:0.3.0 .
docker run --rm -v "$PWD/work:/ctfy/work" \
  -e CTFY_PLATFORM_URL -e CTFY_TOKEN \
  -e LLM_FLAVOR -e LLM_MODEL -e LLM_BASE_URL -e LLM_API_KEY \
  trina-ctfy:0.3.0
```

未实际执行镜像构建时，不能把 Dockerfile 存在标为镜像已验证。
