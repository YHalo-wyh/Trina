# AWDP / AWD pwn 攻防兼备（Break + Fix）

AWDP = 每轮「攻击（打别人的服务拿 flag）+ 防御（提交修补包，让别人的 exp 打不动自己的服务）」。
Pwn 方向的核心难点不在拿 shell，而在**最小化修补且不破坏业务**。

## 赛制与得分机制

| 环节 | 做什么 | 判定 |
|---|---|---|
| Break（攻击） | 对**自己**的服务打 exp 拿 flag 提交 → 平台自动拿该 exp 去打所有未修补的对手 | 攻击得分 |
| Fix（防御） | 写修补包 `xx.tar.gz`，内含可执行 `update.sh`，上传平台 | 平台的官方 exp 打不动 = 修补成功 |
| 轮次 | 每 20 分钟一轮，每轮有效攻击/防御分别计分 | 服务异常不扣分 |

**关键推论**：修补只需挡住**平台那一条 exp**，不要求理论完备。实践经验中：
- 改小读取长度、把 `printf(buf)` 换成 `printf("%s",buf)`、`free` 后置 NULL、加长度上限 —— 这类最小 patch 命中率最高。
- 目录穿越题里，曾出现把 `flag` 文件名改成大写也能判防御成功（exp 太死板）。**先做最小改动试提交，再逐步加固**。

## Fix：修补包规范

`update.sh` 是唯一被要求的入口，平台执行它来替换文件。两种常见形态：

```bash
# 形态 A：包内已含补丁好的二进制/源码，update.sh 只做 mv
#!/bin/sh
mv -f ./pwn /home/ctf/pwn
chmod +x /home/ctf/pwn
```

```bash
# 形态 B：包内是 update.tar.gz，解压后覆盖
#!/bin/sh
tar -xzf update.tar.gz -C /home/ctf/
```

打包（**必须保留执行位**）：

```bash
chmod +x update.sh
tar -czf fix.tar.gz update.sh pwn patched_source.c
```

注意：
- `update.sh` 的换行必须是 LF（Windows 上是 CRLF，提交前 `sed -i 's/\r$//' update.sh`）。
- 路径按题目 Dockerfile 里的实际部署路径写（`/home/ctf/`、`/app/`、`/pwn` 各异）。
- 参考官方样例：`/mnt/f/pwn宝/awdp-pwn原题/AWDP-PWN原题/2025-长城杯CISCN半决赛/update_demo_ext/update_demo/{pwn_demo,web_demo}/`。

## 二进制 patch 工具链

### AwdPwnPatcher（首选，支持 x86/x86_64/arm/aarch64/mips，大小端）

基于 pwntools + keystone。核心思路：把 patch 代码写进 `.eh_frame`（自动改为可执行），在原地址用 `jmp` 跳过去执行再跳回，**原文件改动极小**，容易过 check。

```bash
pip install pwntools keystone-engine
export PYTHONPATH=/path/to/AwdPwnPatcher:$PYTHONPATH
```

> 本仓库 vendored 副本 `references/upstreams/tools/AwdPwnPatcher/` 已修复两个上游 bug（见下「踩坑」）。
> 本机可用解释器：`/home/wyh/.venvs/pwno-mcp/bin/python`（已装 pwntools 4.15 + keystone-engine）。

**踩坑（已在本仓库副本修掉，实测 gcc 13/Ubuntu 24.04 验证）**

1. `fix_eh_frame_flags()` 用浮点算页对齐，`(end/0x1000)*0x1000` 永不截断，只是加了 0x1000 —— 选择器会误命中 RX 代码段。
2. 更致命：它用 `ELF.write(file_offset, ...)` 写 program header，但 pwntools ≥4.x 的 `ELF.write()` 第一个参数是**虚拟地址**，传文件偏移会让 `vaddr_to_offset()` 返回 `None` 并**静默丢弃**写入 → `.eh_frame` 所在段始终没有 `X` → 跳板一执行就 SIGSEGV，表现是「patch 后程序无输出」。
   修法：直接写 `self.binary.mmap[offset:offset+4]`（按文件偏移），并把选择条件改成 `p_vaddr <= eh_frame <= p_vaddr+p_memsz` 且只取 `PT_LOAD`。
3. 现代 `gcc` 默认 `-z separate-code` + CET 会把 `.eh_frame` 放进**独立的只读段**（不是老的 RX 段），因此上面第 2 条不修就必然失败。
4. **PIE 二进制地址用文件内相对 vaddr**（`objdump -d` 里显示的那个，如 `0x11a8`），不要加 `0x400000`；基址由工具内部处理。

**实测通过的三种修补（本机验证）**

| 场景 | 调用 | 验证 |
|---|---|---|
| 格式化串（非 PIE） | `patch_fmt_by_call(<call printf 地址>)` | `%p.%p.%p` 不再展开，输出无多余换行 |
| 栈溢出钳长度（非 PIE） | `patch_origin(<mov edx 立即数指令起>, <止>, assembly="mov edx, 0x20")` | 0x100 输入不再 smash，正常输入输出不变 |
| 栈溢出钳长度（PIE） | 同上，地址用相对 vaddr | 同上 |


```python
from AwdPwnPatcher import *
p = AwdPwnPatcher("./pwn")

# 1) 就地改指令/字符串（start..end 不足处自动 nop 填充）
p.patch_origin(0x80484cb, assembly="mov eax, 1")
p.patch_origin(0x8049000, string="/flag\x00")

# 2) 跳板 patch：在 eh_frame 里插入代码，执行完跳回
addr = p.add_constant_in_ehframe("%s\x00\x00")
p.patch_by_jmp(0x80484c5, jmp_to=0x80484cb,
               assembly=f"sub esp,0xc\npush dword ptr [ebp-0xc]\nlea eax,[{addr}]\npush eax")

# 3) 修 printf 格式化串漏洞（专用封装，只支持 x86）
p.patch_fmt_by_call(0x80484cb)   # 参数 = call printf 指令的地址（PIE 用相对 vaddr）

p.save()   # 生成 ./pwn_patch，自动把 eh_frame 段置为可执行
```

**经典修补配方**

| 漏洞 | 最小 patch | 注意 |
|---|---|---|
| 格式化字符串 `printf(buf)` | 改为 `printf("%s", buf)` | 不要改成 `puts`（多一个换行符过不了 check） |
| 栈溢出 `read(0,buf,size)` | 长度参数钳到 buffer 实际大小 | 优先就地改立即数/寄存器 |
| UAF `free(p)` | `free` 后把槽位置 NULL | 动态索引槽位需重算下标 |
| 堆溢出 `memcpy(dst,src,n)` | `n` 钳到目标容量 | 别动业务逻辑 |
| 全局通防 | seccomp-bpf 拦 `open/openat/execve/openat2` | 猛药，可能连正常启动路径一起杀掉，务必本地验证 |

**禁止**：不要 patch `call free` 去换实现（UAF 题会被 check 判负）；不要用 lief 整体重写（文件改动大、过不了 hash check）。

### ToPaWaf（通用 WAF 型通防，amd64）

基于 `read` 入口做通防，不依赖沙箱/syscall 过滤。仅支持用 `read` 读输入的题目（绝大多数 pwn 题都是）。

```bash
python3 ToPaWaf/main.py    # 按 README 交互式给二进制打补丁
```

### PwnHelper（IDA Pro 9.2+ 插件，Windows）

基于 Hex-Rays AST 的静态漏洞扫描 + 一键 patch。检测栈溢出/格式化串/double free/悬垂指针/整数溢出/命令注入，`ALT+F8` 调出。trampoline 只落在 `.frame*`/`.eh_frame*` 段，不破坏 `.text`。适合防守环节快速定位所有可疑点。

## Break：AWDP 攻击侧注意点

与普通 CTF 一致，但有几条 AWDP 特有：

- **打自己**：exp 的 host 写本队靶机；题目环境就是赛场环境，`flag` 常在本机固定路径（`/flag`、`/home/ctf/flag`）。
- **稳定性 > 优雅**：一轮内要多次复用同一 exp 打多队，脚本必须幂等、能重连、失败不挂（外层包 `try` + 重试）。
- **不要打坏服务**：服务异常虽不扣分，但会让本队也拿不到后续回合的分。避免 `fork` 炸弹、避免写坏共享文件。
- **拿到 shell 后固定读 flag 路径**，不要 `cat /flag*` 撞运气。
- 常用模板：

```python
from pwn import *
context.log_level = 'error'

def attack(host, port):
    for _ in range(3):
        try:
            io = remote(host, port, timeout=5)
            # ... exploit ...
            io.sendline(b'cat /flag* /home/ctf/flag* 2>/dev/null')
            out = io.recvall(timeout=3)
            if b'flag{' in out or b'DASCTF{' in out:
                return out
        except Exception as e:
            log.warning(str(e))
    return None
```

## 本地复现赛题

`/mnt/f/pwn宝/awdp-pwn原题/` 下已有原始附件：
- `AWDP-PWN原题/2024-CISCN总决赛/{anime,CHR,ezheap}` — anime 是格式化串，CHR 是 iconv 后堆溢出
- `AWDP-PWN原题/2025-CISCN总决赛/{DarkHeap,LoginSystem,embbed_httpd}` — embbed_httpd 是 MIPS32 路由器 httpd（含 qemu-mipsel-static + 完整 MIPS libc）
- `AWDP-PWN原题/2025-长城杯CISCN半决赛/{broken_manager,catchme,easy_rw_revenge,minidb,update_demo}` — minidb 是 UAF→fake FILE→FSOP（glibc 2.35），catchme 是 glibc 2.27 UAF

修完立刻用原 exp 回归验证：`./pwn_patch` 跑通正常流程 + 原 exp 打不动 = 修补成立。

## 参考

- AwdPwnPatcher: https://github.com/aftern00n/AwdPwnPatcher
- ToPaWaf: https://github.com/ErodedElk/ToPaWaf
- PwnHelper: https://github.com/Fa1lSnow/AWDP_PWN_Helper
- AWD/AWDP Filter 集合: https://github.com/Drun1baby/AWD-AWDP_SecFilters
- 2024 CISCN 决赛 AWDP-PWN 原题: https://github.com/CTF-Archives/2024-CISCN-Finals-AWDP
- 2025 CISCN 总决赛 AWDP: https://github.com/CTF-Archives/2025-CISCN-Finals
- 2025 长城杯/CISCN 半决赛 AWDP: https://github.com/CTF-Archives/2025-CCB-CISCN-Semis
