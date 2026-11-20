# trina-native

Rust 基础工具层，当前提供文件读取、批量读取、固定字符串检索、原子写入和精确编辑。Python 的 `ToolRegistry` 先完成路径范围、动作等级、审计和超时判断，再把已经裁决过的路径交给该二进制。

```bash
cargo build --release
```

Trina 自动查找 `native/target/release/trina-native`。也可以用 `TRINA_NATIVE_BIN` 指定路径。没有可用二进制时自动回退到原有 Python 实现，工具 Schema 与调用入口保持不变。

终端命令和网络请求继续由 Python 执行链负责，Rust 层不拥有独立的授权边界。写入与编辑也必须先由 Python 完成 `for_write` 路径裁决和审计字段准备。
