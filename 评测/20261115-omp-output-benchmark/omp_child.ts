/** OMP 生产 Composer/TUI 适配器；仅替换终端 I/O，不替换渲染器。 */
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import { createInterface } from "node:readline";

const deps = process.env.TRINA_BENCH_DEPS!;
const moduleAt = (path: string) => import(pathToFileURL(join(deps, "@oh-my-pi/pi-tui/src", path)).href);
const { initTheme } = await moduleAt("theme/index.ts");
const { Composer } = await moduleAt("prompt/composer.ts");
const { TranscriptContainer } = await moduleAt("chrome/transcript-container.ts");
const { AssistantMessageComponent } = await moduleAt("chat/assistant-message.ts");
const { ToolExecutionComponent } = await moduleAt("chat/tool-execution.ts");
await initTheme(false);
const send = (value: object) => process.stdout.write(JSON.stringify(value) + "\n");
let sequence = 0, frameStart = 0, ansi = "", ready = false;
let cpuBase = process.cpuUsage();
let composer: any, transcript: any, active: any;
const messages = new Map<string, any>();
const tools = new Map<string, any>();
const texts = new Map<string, { thinking: string; text: string }>();
const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const message = (thinking = "", text = "") => ({ role: "assistant",
  content: [...(thinking ? [{ type: "thinking", thinking }] : []), ...(text ? [{ type: "text", text }] : [])],
  api: "openai-completions", provider: "openai", model: "fixture", usage, stopReason: "stop", timestamp: 0 });
class Terminal {
  columns = 120; rows = 36; input = (_data: string) => {};
  kittyProtocolActive = false; kittyEnableSequence = null;
  appearance = undefined;
  start(input: (data: string) => void) { this.input = input; }
  async drainInput() {}
  stop() {}
  write(data: string) { ansi += data; }
  moveBy(lines: number) { this.write(`\x1b[${Math.abs(lines)}${lines > 0 ? "B" : "A"}`); }
  hideCursor() { this.write("\x1b[?25l"); }
  showCursor() { this.write("\x1b[?25h"); }
  clearLine() { this.write("\x1b[K"); }
  clearFromCursor() { this.write("\x1b[J"); }
  clearScreen() { this.write("\x1b[H\x1b[2J"); }
  setTitle(_title: string) {}
  setProgress(_active: boolean) {}
  onAppearanceChange(_fn: unknown) {}
}
const terminal = new Terminal();
const scheduler = {
  now: () => performance.now(),
  scheduleImmediate: (fn: () => void) => setImmediate(() => { frameStart = performance.now(); fn(); }),
  scheduleRender: (fn: () => void, delay: number) => {
    const id = setTimeout(() => { frameStart = performance.now(); fn(); }, delay);
    return { cancel: () => clearTimeout(id) };
  },
};
const painted = () => {
  const cpu = process.cpuUsage(cpuBase);
  send({ type: "paint", seq: sequence, render_ms: performance.now() - frameStart,
    cpu_ms: (cpu.user + cpu.system) / 1000, rss_mb: process.memoryUsage().rss / 1048576,
    ansi: Buffer.from(ansi).toString("base64") });
  ansi = "";
};
async function handle(row: any) {
  sequence = row.seq || 0;
  if (row.type === "init") {
    terminal.columns = row.columns; terminal.rows = row.rows;
    composer = new Composer({ terminal, tuiOptions: { renderScheduler: scheduler, onPaint: painted },
      preferences: { quiet: true, showHardwareCursor: false, spellingTypoDetection: false, spellingAutocomplete: false } });
    transcript = new TranscriptContainer();
    for (const text of row.history) transcript.addChild(new AssistantMessageComponent(message("", text), false));
    composer.setRuntimeChildren([transcript, composer.editor]);
    composer.ui.setFocus(composer.editor);
    composer.start({ playWelcomeIntro: false });
    await Bun.sleep(700);
    cpuBase = process.cpuUsage(); ready = true;
    send({ type: "ready", runtime: Bun.version });
    return;
  }
  if (row.type === "delta") {
    const id = row.worker || "main";
    if (!messages.has(id)) {
      active = new AssistantMessageComponent(undefined, false, () => composer.ui.requestRender(), [], undefined, false, undefined, true);
      messages.set(id, active); texts.set(id, { thinking: "", text: "" }); transcript.addChild(active);
    }
    const state = texts.get(id)!;
    state[row.kind === "thinking" ? "thinking" : "text"] += row.delta;
    messages.get(id).updateContent(message(state.thinking, state.text), { transient: true });
    composer.ui.requestRender();
  } else if (row.type === "tool_start") {
    for (const [id, component] of messages) {
      const state = texts.get(id)!;
      component.updateContent(message(state.thinking, state.text)); component.markTranscriptBlockFinalized();
    }
    const tool = new ToolExecutionComponent("bash", { command: row.command }, {}, undefined, composer.ui, process.cwd());
    tool.setArgsComplete(); tool.setExpanded(true);
    tools.set(row.call, tool); transcript.addChild(tool); composer.ui.requestRender();
  } else if (row.type === "tool_output" || row.type === "tool_end") {
    const tool = tools.get(row.call);
    tool.updateResult({ content: [{ type: "text", text: row.output }], details: { exitCode: row.type === "tool_end" ? 0 : undefined }, isError: false }, row.type !== "tool_end");
    composer.ui.requestRender();
  } else if (row.type === "key") {
    terminal.input(row.text);
  } else if (row.type === "scroll") {
    // OMP 使用主屏原生历史滚动；同一 kitty 终端引擎执行该动作。
    send({ type: "native_scroll", seq: sequence, direction: row.direction });
  } else if (row.type === "done") {
    for (const [id, component] of messages) {
      if (component.isTranscriptBlockFinalized()) continue;
      const state = texts.get(id)!;
      component.updateContent(message(state.thinking, state.text)); component.markTranscriptBlockFinalized();
    }
    composer.ui.requestRender();
  } else if (row.type === "finish") {
    await Bun.sleep(300);
    const cpu = process.cpuUsage(cpuBase);
    send({ type: "summary", cpu_ms: (cpu.user + cpu.system) / 1000, rss_mb: process.memoryUsage().rss / 1048576 });
    composer.stop(); for (const tool of tools.values()) tool.stopAnimation();
    process.exit(0);
  }
}
for await (const line of createInterface({ input: process.stdin })) {
  try { await handle(JSON.parse(line)); }
  catch (error) { send({ type: "error", error: String(error), stack: (error as Error).stack }); process.exit(1); }
}
