/** 两个子进程共用相同 kitty VT 引擎、几何尺寸和带时间间隔的输入夹具。 */
import { join, resolve, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import os from "node:os";

const directory = dirname(fileURLToPath(import.meta.url));
const root = resolve(directory, "../..");
const deps = resolve(root, "tmp/omp-output-benchmark/node_modules");
const { KittyTerminal, loadModuleSync } = await import(pathToFileURL(join(deps, "kitty-vt-wasm/dist/index.js")).href);
const wasm = loadModuleSync(join(deps, "kitty-vt-wasm/dist/kitty-vt.wasm"));
const columns = 120, rows = 36;
const trials = Number(process.env.TRINA_BENCH_TRIALS || "3");
const selected = process.env.TRINA_BENCH_SCENARIOS?.split(",") || ["short", "history", "four_streams"];
const outputDir = join(directory, process.env.TRINA_BENCH_OUTPUT || "results");
mkdirSync(outputDir, { recursive: true });
const fixtures: Record<string, any> = {};
const warmup = "Warm up **Markdown**.\n```python\nvalue = {'ready': True}\nprint(value)\n```\n";
function fixture(name: string) {
  let seq = 0;
  const events: any[] = [];
  const push = (row: any) => {
    row.seq = ++seq;
    if (row.type === "delta") { row.marker = `M${String(seq).padStart(5, "0")}`; row.delta += ` ${row.marker}\n`; }
    if (row.type === "key") { row.marker = `K${String(seq).padStart(5, "0")}`; row.text = row.marker + " "; }
    events.push(row);
  };
  const history = [warmup];
  if (name === "history") for (let n = 0; n < 80; n++)
    history.push(`## History ${n}\n` + Array.from({ length: 20 }, (_, line) => `Previous checked fact ${n}:${line}. Stable history, do not change this paragraph.\n`).join(""));
  if (name === "short") {
    for (let n = 0; n < 16; n++) push({ type: "delta", kind: "thinking", delta: "检查工具输入和编译条件，保留真实失败信息。" });
    for (let n = 0; n < 32; n++) {
      push({ type: "delta", kind: "text", delta: n === 0 ? "准备执行测试。\n```python\n# first line" : n === 31 ? "```\n完成编码。" : `value_${n} = {'index': ${n}, 'valid': True} # code` });
      if (n % 8 === 0) push({ type: "key" });
    }
    push({ type: "tool_start", call: "tool", command: "fixture_tool --emit-lines 16" });
    let output = "";
    for (let n = 0; n < 16; n++) {
      const marker = `T${String(seq + 1).padStart(5, "0")}`;
      output += `fixture_output_line_${n}: controlled replay ${marker}\n`;
      push({ type: "tool_output", call: "tool", output, marker });
    }
    push({ type: "tool_end", call: "tool", output });
  } else {
    const count = name === "four_streams" ? 96 : 64;
    for (let n = 0; n < count; n++) {
      const worker = name === "four_streams" ? `worker-${n % 4}` : "main";
      push({ type: "delta", worker, kind: "text", delta: (name === "four_streams" ? n < 4 : n === 0) ? "## Code review\n```python\n# initial" : `result_${n} = {'worker': '${worker}', 'value': ${n}, 'checked': True} # analysis` });
      if (n % 12 === 0) push({ type: "key" });
    }
  }
  push({ type: "done" });
  if (name === "history") for (let n = 0; n < 12; n++) push({ type: "scroll", direction: n % 6 < 3 ? "up" : "down", pause_ms: 140 });
  return { name, columns, rows, interval_ms: 40, history, events };
}
for (const name of selected) fixtures[name] = fixture(name);
const fixtureText = JSON.stringify(fixtures, null, 2);
writeFileSync(join(outputDir, "fixtures.json"), fixtureText);
const fixtureSha = createHash("sha256").update(fixtureText).digest("hex");
function quantile(values: number[], q: number) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return Math.round(sorted[Math.max(0, Math.ceil(q * sorted.length) - 1)] * 1000) / 1000;
}
const sleep = (ms: number) => Bun.sleep(Math.max(0, ms));
async function one(engine: string, name: string, trial: number) {
  const data = fixtures[name];
  const term = KittyTerminal.createSync({ columns, rows, scrollback: 20000, wasm, onEvent: () => {}, onOutput: () => {} });
  const command = engine === "omp" ? [process.execPath, join(directory, "omp_child.ts")] : ["python", "-X", "utf8", join(directory, "trina_child.py")];
  const child = Bun.spawn(command, { cwd: root, stdin: "pipe", stdout: "pipe", stderr: "pipe",
    env: { ...process.env, TERM: "xterm-256color", COLORTERM: "truecolor", TRINA_BENCH_DEPS: deps, NO_COLOR: undefined } });
  const stderr = new Response(child.stderr).text();
  const sent = new Map<number, any>(), paints: any[] = [], observations: any[] = [];
  let initialized = false, result: any, baseline = "", latestCpu = 0, startupStart = performance.now();
  let readyResolve: () => void, readyReject: (error: Error) => void;
  const ready = new Promise<void>((yes, no) => { readyResolve = yes; readyReject = no; });
  let startup_ms = 0, runtimeVersion = "";
  const visible = () => Array.from({ length: rows }, (_, y) => term.viewportLine(y)).join("\n");
  const receive = async () => {
    const decoder = new TextDecoder(); let pending = "";
    for await (const chunk of child.stdout) {
      pending += decoder.decode(chunk, { stream: true });
      let newline: number;
      while ((newline = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, newline); pending = pending.slice(newline + 1);
        if (!line.trim()) continue;
        const row = JSON.parse(line);
        if (row.type === "ready") { initialized = true; runtimeVersion = row.runtime; startup_ms = performance.now() - startupStart; readyResolve(); }
        else if (row.type === "error") readyReject(new Error(JSON.stringify(row)));
        else if (row.type === "summary") result = row;
        else if (row.type === "native_scroll") {
          const request = sent.get(row.seq);
          const start = performance.now();
          const moved = term.scrollViewport(row.direction === "up" ? "page-up" : "page-down");
          observations.push({ seq: row.seq, kind: "scroll", latency_ms: performance.now() - request.sent, moved, terminal_ms: performance.now() - start });
        } else if (row.type === "paint") {
          const terminalStart = performance.now();
          const bytes = Buffer.from(row.ansi, "base64"); term.write(bytes);
          const terminal_ms = performance.now() - terminalStart;
          if (!initialized || result) continue;
          const now = performance.now(), screen = visible();
          latestCpu = row.cpu_ms;
          paints.push({ seq: row.seq, render_ms: row.render_ms, terminal_ms, bytes: bytes.length, rss_mb: row.rss_mb, cpu_ms: row.cpu_ms });
          for (const [seq, request] of sent) {
            if (request.observed || !request.marker || !screen.includes(request.marker)) continue;
            request.observed = true;
            observations.push({ seq, kind: request.type === "key" ? "key" : "output", phase: request.kind || request.type, latency_ms: now - request.sent });
          }
          if (engine === "trina") for (const [seq, request] of sent) {
            if (request.type === "scroll" && !request.observed && row.seq >= seq && screen !== request.before) {
              request.observed = true; observations.push({ seq, kind: "scroll", latency_ms: now - request.sent, moved: true });
            }
          }
          baseline = screen;
        }
      }
    }
  };
  const receiving = receive();
  const send = async (row: any) => { child.stdin.write(JSON.stringify(row) + "\n"); await child.stdin.flush(); };
  await send({ type: "init", columns, rows, history: data.history });
  let timer = setTimeout(() => readyReject(new Error("ready timeout")), 30000);
  try { await ready; } catch (error) { child.kill(); throw error; } finally { clearTimeout(timer); }
  const started = performance.now();
  for (const row of data.events) {
    if (row.type === "scroll" && data.events[row.seq - 2]?.type === "done") await sleep(250);
    sent.set(row.seq, { ...row, sent: performance.now(), before: baseline });
    await send(row);
    await sleep(row.pause_ms || data.interval_ms);
  }
  await sleep(350);
  const finalScreen = visible();
  await send({ type: "finish", seq: data.events.length + 1 });
  const exitCode = await child.exited; await receiving;
  const errorText = await stderr;
  if (exitCode !== 0) throw new Error(`${engine} exited ${exitCode}: ${errorText}`);
  const output = observations.filter(row => row.kind === "output");
  const keys = observations.filter(row => row.kind === "key");
  const scrolls = observations.filter(row => row.kind === "scroll");
  const summary = { engine, scenario: name, trial, runtime: runtimeVersion, startup_ms, duration_ms: performance.now() - started,
    output_observed: output.length, output_expected: data.events.filter((row: any) => row.marker && row.type !== "key").length,
    output_p50_ms: quantile(output.map(row => row.latency_ms), .5), output_p95_ms: quantile(output.map(row => row.latency_ms), .95),
    text_p95_ms: quantile(output.filter(row => row.phase === "text").map(row => row.latency_ms), .95),
    thinking_p95_ms: quantile(output.filter(row => row.phase === "thinking").map(row => row.latency_ms), .95),
    tool_output_p95_ms: quantile(output.filter(row => row.phase === "tool_output").map(row => row.latency_ms), .95),
    first_output_ms: output[0]?.latency_ms ?? null,
    key_p95_ms: quantile(keys.map(row => row.latency_ms), .95), keys_observed: keys.length,
    scroll_p95_ms: quantile(scrolls.map(row => row.latency_ms), .95), scrolls_observed: scrolls.filter(row => row.moved).length,
    frame_p50_ms: quantile(paints.map(row => row.render_ms), .5), frame_p95_ms: quantile(paints.map(row => row.render_ms), .95),
    terminal_p95_ms: quantile(paints.map(row => row.terminal_ms), .95), frames: paints.length,
    total_ansi_bytes: paints.reduce((n, row) => n + row.bytes, 0), cpu_ms: result?.cpu_ms ?? latestCpu,
    peak_rss_mb: Math.max(...paints.map(row => row.rss_mb)), stderr: errorText };
  writeFileSync(join(outputDir, `${engine}-${name}-${trial}.json`), JSON.stringify({ summary, observations, paints,
    missing: [...sent.values()].filter(row => row.marker && !row.observed).map(({seq, type, kind, marker}) => ({seq, type, kind, marker})), final_screen: finalScreen }, null, 2));
  term.dispose();
  console.log(JSON.stringify(summary));
  return summary;
}
const results: any[] = [];
for (let trial = 1; trial <= trials; trial++) for (const name of selected)
  for (const engine of (trial % 2 ? ["trina", "omp"] : ["omp", "trina"])) results.push(await one(engine, name, trial));
writeFileSync(join(outputDir, "summary.json"), JSON.stringify({ generated_at: new Date().toISOString(),
  machine: { platform: process.platform, release: os.release(), cpu: os.cpus()[0]?.model, logical_cpus: os.cpus().length, memory_gb: os.totalmem() / 1073741824 },
  terminal: { engine: "kitty-vt-wasm", version: "0.2.0", columns, rows, scrollback: 20000, color: "truecolor" },
  runtimes: { bun: Bun.version, omp: "18.20.2" }, fixture_sha256: fixtureSha, trials, results }, null, 2));
