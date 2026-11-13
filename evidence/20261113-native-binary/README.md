# 2026-11-13 · 原生二进制、CTFY 协议与模型窗口验证

执行状态：**本人实际执行**。平台线上评测状态：**未运行**。本目录不包含模型 API Key、CTFY Token 或编译后的 ELF。

## 结论

- WSL Ubuntu 24.04 全量回归：519 项通过，0 跳过；包含 10 个真实 Linux ELF/r2/GDB 场景。
- Windows 全量回归：519 项通过，10 项按“需要 Linux r2/GDB/gcc/bwrap”跳过；其余协议、配置和错误语义通过。
- 纵向 native 用例真实完成 `binary_open → strings → xrefs → function → debug_start → PIE 断点 → registers/frames/memory → 恢复候选 → binary_execute`；正确候选被本地程序接受，错误候选被拒绝。
- 原生审计链校验通过；r2/GDB 会话均已关闭。CTFY 的 instance/QA、门控刷新、pending 和模型网关通过本地 HTTP 协议夹具，不代表 CTFY 线上得分。
- DeepSeek 自动上下文为 500,000；代码明确识别的百万窗口模型为 1,000,000；手工设置、供应商切换和重启恢复有测试。

## 环境

| 组件 | 实际版本 |
|---|---|
| radare2 | 5.5.0 |
| GDB | 15.1 |
| GCC | 13.3.0 |
| bubblewrap | 0.9.0 |
| Linux Python | 3.12.3 |
| Windows Python | 3.10 |

精确版本字符串、耗时、调用参数和实际输出在 [`native-e2e.json`](native-e2e.json)。

## 文件索引

| 文件 | 内容 |
|---|---|
| [`native-e2e.json`](native-e2e.json) | 实际工具调用、参数、结构化返回、版本、耗时、负对照、关闭后的会话快照和 raw 路径映射 |
| [`raw/`](raw/) | 每一步 r2/GDB/执行器完整协议工件；`native-e2e.json.raw_index` 将临时源路径映射到本目录相对路径 |
| [`audit.jsonl`](audit.jsonl) | 纵向用例的真实哈希链审计 |
| [`encoded.h`](encoded.h) | 当轮随机夹具的编码数组；候选不预先交给求解逻辑 |
| [`linux-unittest.txt`](linux-unittest.txt) | WSL 全量 519 项原始测试输出 |
| [`windows-unittest.txt`](windows-unittest.txt) | Windows 全量 519 项原始测试输出及 10 项平台跳过 |
| [`doctor.txt`](doctor.txt) | replay 模式环境检查：31 个工具、r2/GDB/bwrap 路径和 500K 默认窗口 |
| [`tools.json`](tools.json) | 31 个 ToolRegistry 工具及参数 Schema |
| [`contract-execute.json`](contract-execute.json) | 机器规约执行结果：21 项需求、40 条规则、90 个测试引用；实际运行 519 项 Windows 回归 |

## 复现

```bash
# Ubuntu / WSL，需安装 r2、gdb、gcc、bwrap
python3 -X utf8 -m unittest tests.test_binary -v
python3 -X utf8 -m scripts.verify_binary_stack \
  --output evidence/20261113-native-binary

# 全量
python3 -X utf8 -m unittest discover -s tests -v
python3 -X utf8 -m scripts.verify_contract --execute --json
```

夹具源码为 `tests/fixtures/binary/challenge.c`。每次运行随机生成 12 字符候选并编译 PIE，因此 ELF 哈希和候选会变化；验收应比较行为、审计和正确/错误分支，不要求字节相同。

## 能证明与不能证明的内容

本证据能证明当前机器上的 r2 stdio、GDB/MI、PIE/ASLR、会话租约、进程清理、地址模型、上下文设置和 CTFY 请求契约按用例工作。它不能证明所有 ISA/文件格式、复杂 argv、长周期资源压力、所有模型质量、Dockerfile 已构建，或 CTFY 线上题目已经解出。完整限制见 [`docs/BINARY_TOOLS.md`](../../docs/BINARY_TOOLS.md)和 [`docs/CTFY.md`](../../docs/CTFY.md)。
