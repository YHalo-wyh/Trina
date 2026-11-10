/**
 * Trina 的可选 omp 扩展（项目级，随 cwd 自动加载：<cwd>/.omp/extensions/*.ts）
 *
 * 它做一件事：
 * 1. 把 Trina 的工具集暴露成 omp 的一等工具（trina_run / trina_tool / trina_guard / trina_tools / trina_report），
 *    所有调用都落到 Python 侧，统一完成参数校验、执行、超时和审计。
 * 默认 ``scope.trust_mode=full``，扩展不再额外拦截 omp 的 bash。
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
// 扩展工厂
// ---------------------------------------------------------------------------

export default function paiExtension(pi: ExtensionAPI) {
  const z = pi.zod;
  pi.setLabel("Trina");

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
    label: "Trina 执行检查",
    description: "查看目标在当前 trust_mode 下的执行判定；默认 full 会直接放行。",
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
      "执行一个 Trina 工具并返回结构化结果（JSON）。参数用 params 传 JSON 对象，执行过程会记录审计。",
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
      "对目标执行一次全自动任务（ReAct 决策 + 工具编排 + 报告生成）。耗时可能数分钟到数十分钟，结束后返回结论摘要与报告路径。",
    parameters: z.object({
      target: z.string().describe("目标（host:port 或 URL）"),
      goal: z.string().optional().describe("本次任务目标，如「拿到 flag」「验证是否存在 SQL 注入」"),
      max_steps: z.number().int().positive().optional().describe("决策步数上限（默认 40）"),
      max_seconds: z.number().int().positive().optional().describe("总时长上限（秒）"),
      range: z.string().optional().describe("靶场适配器名，如 lab/dvwa/pikachu"),
      hint: z.string().optional().describe("附加约束或提示"),
      allow_exploit: z.boolean().optional().describe("restricted 模式兼容项：提升动作等级到 exploit；full 模式无需设置"),
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
    handler: async (args, _ctx) => {
      const trimmed = (args || "").trim();
      if (!trimmed) {
        ctx.ui.notify("用法：/pentest <target> [目标说明]，例如 /pentest 127.0.0.1:9099 拿到 flag", "warn");
        return;
      }
      const [target, ...rest] = trimmed.split(/\s+/);
      const goal = rest.join(" ");
      pi.sendUserMessage(
        `请对 ${target} 执行全自动任务${goal ? `，目标：${goal}` : ""}。调用 trina_run 执行，完成后汇报报告路径。`,
        { deliverAs: "nextTurn" },
      );
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    if (!existsSync(resolve(PROJECT_ROOT, "trina", "cli.py"))) return;
    ctx.ui.notify("Trina 扩展已加载：trina_run / trina_tool / trina_guard / trina_report 可用", "info");
  });
}
