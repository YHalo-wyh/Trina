/**
 * Trina 的可选 omp 扩展（项目级，随 cwd 自动加载：<cwd>/.omp/extensions/*.ts）
 *
 * 它做两件事：
 * 1. 把 Trina 的工具集暴露成 omp 的一等工具（trina_run / trina_tool / trina_guard / trina_tools / trina_report），
 *    所有调用都落到 Python 侧，因此**必然**经过 Trina 的 ScopeGuard 护栏与审计哈希链；
 * 2. 拦截 omp 内建 `bash` 工具里的越界/破坏性命令（第二道防线：即使模型绕开 Trina 直接敲命令也拦得住）。
 *
 * 注意：本扩展只是「第二道防线」。真正的判据在 `trina/scope.py`，不可被模型或提示词覆盖。
 */
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

/** 项目根：本文件位于 <root>/.omp/extensions/trina.ts */
const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.TRINA_PYTHON || "python3";
const MAX_BUFFER = 16 * 1024 * 1024;

// ---------------------------------------------------------------------------
// Python 桥
// ---------------------------------------------------------------------------

async function trina(args: string[], timeoutMs = 900_000): Promise<string> {
  const options = {
    cwd: PROJECT_ROOT,
    timeout: timeoutMs,
    maxBuffer: MAX_BUFFER,
    env: { ...process.env, PYTHONPATH: PROJECT_ROOT, PYTHONIOENCODING: "utf-8" },
  };
  try {
    const { stdout, stderr } = await execFileAsync(PYTHON, ["-m", "trina", ...args], options);
    const tail = (stderr || "").trim();
    return tail ? `${stdout}\n[stderr]\n${tail}` : stdout;
  } catch (error) {
    // 非零退出是**正常业务结果**（目标越界被拒、工具失败、run 状态非成功），
    // 必须把 stdout/stderr 原样交给模型，而不是抛异常让它看不到拒绝原因。
    const err = error as { code?: number | string; stdout?: string; stderr?: string; message?: string };
    const stdout = (err?.stdout || "").trim();
    const stderr = (err?.stderr || "").trim();
    const body = [stdout, stderr ? `[stderr]\n${stderr}` : ""].filter(Boolean).join("\n");
    const code = err?.code ?? "unknown";
    return `[退出码 ${code}]\n${body || String(err?.message ?? error)}`;
  }
}

// ---------------------------------------------------------------------------
// 第二道防线：命令级护栏
// ---------------------------------------------------------------------------

/** 永不允许的参数模式（与 trina/scope.py 的硬禁止清单保持一致） */
const FORBIDDEN_PATTERNS: Array<[RegExp, string]> = [
  [/--os-shell/, "sqlmap 系统 shell 属越权破坏性操作"],
  [/--os-pwn/, "sqlmap OOB 利用禁止"],
  [/--os-smbrelay/, "sqlmap SMB 中继禁止"],
  [/--file-write|--file-dest/, "向目标写文件禁止"],
  [/--sql-shell/, "交互式 SQL shell 禁止"],
  [/--dump-all\b/, "整库导出属数据泄露，禁止"],
  [/--risk\s*=?\s*3/, "sqlmap risk=3 含破坏性 payload"],
  [/\bmkfs(\.\w+)?\b/, "格式化文件系统禁止"],
  [/\bdd\s+.*of=\/dev\//, "裸设备写入禁止"],
  [/:\s*\(\s*\)\s*\{.*\}\s*;?\s*:/, "fork 炸弹禁止"],
  [/\b(shutdown|reboot|poweroff|halt)\b(?!\s*\()/, "关停主机禁止"],
  [/\b(iptables|nft)\s+(-F|--flush)/, "清空防火墙规则禁止"],
  [/\b(hping3?|slowloris|goldeneye|t50)\b/, "洪泛/DoS 工具禁止"],
  [/nmap.*--script[= ]\S*(dos|flood|broadcast)/, "DoS/NSE 广播脚本禁止"],
  [/\brm\s+-rf?\s+(\/\s*$|\/\*|\/etc|\/var|\/usr|\/home|\/root)/, "删除系统目录禁止"],
  [/\b(curl|wget)\b[^|;]*\|\s*(ba|z|da)?sh\b/, "执行远端下载脚本禁止"],
];

/** 攻击性二进制：出现公网 IP 时直接拦截（实训环境只允许私网/回环目标） */
const OFFENSIVE_BINARIES = /\b(nmap|masscan|sqlmap|nuclei|hydra|ffuf|gobuster|dirb|nikto|wpscan|msfconsole|metasploit)\b/;
const IPV4 = /\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b/g;

function isPrivateIPv4(ip: string): boolean {
  const parts = ip.split(".").map(Number);
  const [a, b] = parts;
  if (parts.some((n) => Number.isNaN(n) || n > 255)) return true; // 解析不了的交给 Python 侧判定
  if (a === 10 || a === 127 || a === 0) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 169 && b === 254) return false; // 链路本地：不允许
  if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT，视作内部
  return false;
}

/** 命令分段：跳过引号/heredoc 干扰，按 ; && || | 切开逐段判定 */
function splitSegments(command: string): string[] {
  return command
    .split(/\n|;|&&|\|\||\|/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/** 只写文档/不执行载荷的命令（提交信息里出现 --os-shell 这类字样不该被拦） */
const DOC_WRITE = /^(git\s+(commit|tag|notes|add|merge|rebase|cherry-pick)|echo|printf|cat\s*<<)/;

function guardCommand(command: string): { block: true; reason: string } | undefined {
  const segments = splitSegments(command);
  for (const [pattern, why] of FORBIDDEN_PATTERNS) {
    for (const segment of segments) {
      if (DOC_WRITE.test(segment)) continue; // 文档文本不执行任何载荷
      if (pattern.test(segment)) {
        return { block: true, reason: `Trina 护栏拦截：${why}。该操作在授权实训环境中永不允许。` };
      }
    }
  }
  if (OFFENSIVE_BINARIES.test(command)) {
    const publicIps = [...command.matchAll(IPV4)].map((m) => m[0]).filter((ip) => !isPrivateIPv4(ip));
    if (publicIps.length > 0) {
      return {
        block: true,
        reason:
          `Trina 护栏拦截：检测到对公网地址 ${publicIps.join(", ")} 的攻击性工具调用。` +
          `授权范围仅包含私网/回环；请改用 \`trina_tool\`（会经过 ScopeGuard 校验），` +
          `或用 \`trina guard check <target>\` 确认目标是否在授权范围内。`,
      };
    }
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// 扩展工厂
// ---------------------------------------------------------------------------

export default function paiExtension(pi: ExtensionAPI) {
  const z = pi.zod;
  pi.setLabel("Trina 渗透测试护栏");

  // ---------------------------------------------------------------- 工具定义
  pi.registerTool({
    name: "trina_tools",
    label: "Trina 工具清单",
    description: "列出 Trina 已封装的渗透工具（端口扫描/目录爆破/PoC/SQLi/命令执行/弱口令/取证等），含参数表与动作等级。",
    parameters: z.object({ verbose: z.boolean().optional().describe("是否输出示例与说明") }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      const text = await trina(["tools", ...(params.verbose ? ["--verbose"] : [])], 60_000);
      return { content: [{ type: "text", text }], details: { cwd: ctx.cwd } };
    },
  });

  pi.registerTool({
    name: "trina_guard",
    label: "Trina 授权校验",
    description: "校验目标是否落在授权网段内（硬护栏）。任何渗透动作前都应确认目标已授权。",
    parameters: z.object({ target: z.string().describe("host:port 或 URL") }),
    async execute(_id, params) {
      const text = await trina(["guard", "check", params.target], 60_000);
      return { content: [{ type: "text", text }], details: { target: params.target } };
    },
  });

  pi.registerTool({
    name: "trina_tool",
    label: "Trina 单工具执行",
    description:
      "执行一个 Trina 渗透工具并返回结构化结果（JSON）。动作会经过 ScopeGuard 护栏与审计哈希链。参数用 params 传 JSON 对象。",
    parameters: z.object({
      name: z.string().describe("工具名，见 trina_tools"),
      params: z.record(z.string(), z.any()).optional().describe("工具参数对象，如 {\"target\":\"127.0.0.1\",\"ports\":\"quick\"}"),
      target: z.string().optional().describe("便捷写法：直接指定 target/url"),
    }),
    async execute(_id, params) {
      const argv = ["tool", params.name];
      if (params.params && Object.keys(params.params).length > 0) {
        argv.push("--params", JSON.stringify(params.params));
      }
      if (params.target) argv.push("--target", params.target);
      const text = await trina(argv, 600_000);
      return { content: [{ type: "text", text }], details: { tool: params.name } };
    },
  });

  pi.registerTool({
    name: "trina_run",
    label: "Trina 全自动渗透",
    description:
      "对目标执行一次全自动渗透测试（ReAct 决策 + 工具编排 + 合规护栏 + 报告生成）。耗时可能数分钟到数十分钟，结束后返回结论摘要与报告路径。",
    parameters: z.object({
      target: z.string().describe("目标（host:port 或 URL），必须在授权范围内"),
      goal: z.string().optional().describe("本次任务目标，如「拿到 flag」「验证是否存在 SQL 注入」"),
      max_steps: z.number().int().positive().optional().describe("决策步数上限（默认 40）"),
      max_seconds: z.number().int().positive().optional().describe("总时长上限（秒）"),
      range: z.string().optional().describe("靶场适配器名，如 lab/dvwa/pikachu"),
      hint: z.string().optional().describe("附加约束或提示"),
      allow_exploit: z.boolean().optional().describe("提升动作等级到 exploit（允许命令执行/批量爆破；破坏性动作仍需配置单独开启）"),
    }),
    async execute(_id, params, _signal, onUpdate) {
      const argv = ["run", "--target", params.target];
      if (params.goal) argv.push("--goal", params.goal);
      if (params.max_steps) argv.push("--max-steps", String(params.max_steps));
      if (params.max_seconds) argv.push("--max-seconds", String(params.max_seconds));
      if (params.range) argv.push("--range", params.range);
      if (params.hint) argv.push("--hint", params.hint);
      if (params.allow_exploit) argv.push("--allow-exploit");
      onUpdate?.({ content: [{ type: "text", text: `Trina 正在对 ${params.target} 执行自动渗透…` }] });
      const text = await trina(argv, 3_600_000);
      return { content: [{ type: "text", text }], details: { target: params.target } };
    },
  });

  pi.registerTool({
    name: "trina_report",
    label: "Trina 报告重建",
    description: "由历史运行的状态文件（runs/<run_id>/state.json）重建 Markdown/HTML 渗透测试报告。",
    parameters: z.object({ state_file: z.string().describe("runs/<run_id>/state.json 的路径") }),
    async execute(_id, params) {
      const text = await trina(["report", "rebuild", "--state", params.state_file], 120_000);
      return { content: [{ type: "text", text }], details: { state: params.state_file } };
    },
  });

  // ---------------------------------------------------------------- 命令
  pi.registerCommand("pentest", {
    description: "对授权目标发起 Trina 全自动渗透：/pentest <target> [目标说明]",
    handler: async (args, ctx) => {
      const trimmed = (args || "").trim();
      if (!trimmed) {
        ctx.ui.notify("用法：/pentest <target> [目标说明]，例如 /pentest 127.0.0.1:9099 拿到 flag", "warn");
        return;
      }
      const [target, ...rest] = trimmed.split(/\s+/);
      const goal = rest.join(" ");
      ctx.ui.notify(`Trina：校验 ${target} 授权状态…`, "info");
      try {
        const guardText = await trina(["guard", "check", target], 60_000);
        pi.sendUserMessage(
          `请对 ${target} 执行全自动渗透测试${goal ? `，目标：${goal}` : ""}。\n\n` +
            `先调用 trina_guard 确认授权（当前校验输出如下），再用 trina_run 执行；完成后用 trina_report 汇报报告路径。\n\n` +
            `护栏校验输出：\n${guardText}`,
          { deliverAs: "nextTurn" },
        );
      } catch (error) {
        ctx.ui.notify(`Trina 校验失败：${error instanceof Error ? error.message : String(error)}`, "error");
      }
    },
  });

  // ---------------------------------------------------------------- 拦截
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") return;
    const command = typeof event.input?.command === "string" ? event.input.command : "";
    if (!command) return;
    const verdict = guardCommand(command);
    if (verdict) return verdict;
  });

  pi.on("session_start", async (_event, ctx) => {
    if (!existsSync(resolve(PROJECT_ROOT, "trina", "cli.py"))) return;
    ctx.ui.notify("Trina 扩展已加载：trina_run / trina_tool / trina_guard / trina_report 可用，bash 护栏生效", "info");
  });
}
