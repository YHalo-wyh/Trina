/** 把 smoke.py 录下的真实 ANSI 帧交给 kitty-vt 引擎，核对三种模式的画面一致性与解析成本。 */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const deps = resolve(root, "tmp/omp-output-benchmark/node_modules");
const { KittyTerminal, loadModuleSync } = await import(pathToFileURL(join(deps, "kitty-vt-wasm/dist/index.js")).href);
const wasm = loadModuleSync(join(deps, "kitty-vt-wasm/dist/kitty-vt.wasm"));

const framesDir = join(here, "frames");
const runs = [];
for (const name of readdirSync(framesDir).filter((item) => item.endsWith(".json")).sort()) {
  const run = JSON.parse(readFileSync(join(framesDir, name), "utf8"));
  const term = KittyTerminal.createSync({
    columns: run.columns, rows: run.rows, scrollback: 20000, wasm,
    onEvent: () => {}, onOutput: () => {},
  });
  const started = performance.now();
  for (const frame of run.frames) term.write(Buffer.from(frame, "base64"));
  const parseMs = performance.now() - started;
  const screen = term.text();
  runs.push({
    mode: run.mode, trial: run.trial, frames: run.frames.length,
    parse_ms: Math.round(parseMs * 1000) / 1000,
    sha256: createHash("sha256").update(screen).digest("hex"),
    screen,
  });
  term.dispose();
}

const byMode = Object.fromEntries(runs.map((run) => [run.mode, run]));
const hidden = byMode.current, control = byMode.control, visible = byMode.visible;
const report = {
  engine: "kitty-vt-wasm", version: "0.2.0", columns: 120, rows: 36,
  runs: runs.map(({ mode, trial, frames, parse_ms, sha256 }) => ({ mode, trial, frames, parse_ms, sha256 })),
  checks: {
    hidden_matches_control: hidden?.sha256 === control?.sha256,
    visible_has_toast: Boolean(visible?.screen.includes("Trina · 编译完成")),
    hidden_has_no_toast: Boolean(hidden && !hidden.screen.includes("✦ Trina")),
  },
  screens: { current: hidden?.screen, control: control?.screen, visible: visible?.screen },
};
writeFileSync(join(here, "screens.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify({ runs: report.runs, checks: report.checks }, null, 2));
