"""真实渲染冒烟：确认隐藏的通知浮层不会拖慢正常输出。

做法：用生产 `EnhancedConsoleInput` + 真实 prompt_toolkit 渲染循环，把 120×36 的
VT100 字节流写进缓冲区，按 40ms 节拍回放固定流式输出。每轮在 `current →
control → visible` 三种布局间切换：

- current：当前实现，`FloatContainer` 包住主布局，通知浮层隐藏；
- control：去掉浮层包装的等价布局（A/B 对照，输出应与 current 逐字节一致）；
- visible：通知可见，用于证明浮层确实在绘制，并给出显示时的成本。

判定依据是应用内直接计量：包裹 `FloatContainer._draw_float`，记录每帧花在
“处理这个浮层”上的时间。隐藏时它只有条件判断与零尺寸计算，不需要排版通知文本；
同时包裹通知条文本回调，确认它一次都没有被调用。

跨进程/跨时段的总帧耗时受 CPU 频率档位影响明显（同机同代码可在 3ms 与 10ms
之间跳变），因此只作为背景数据，不作为“是否拖慢”的判据。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import FloatContainer, Layout
from prompt_toolkit.output.vt100 import Vt100_Output

from trina.console_ui import EnhancedConsoleInput
from trina.terminal import render_answer, streaming_answer

COLUMNS, ROWS = 120, 36
INTERVAL = 0.04
HISTORY_RECORDS = 6
SEQUENCE = ("current", "control", "visible")
ORIGINAL_DRAW_FLOAT = FloatContainer._draw_float


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, int(-(-len(ordered) * q // 1)) - 1))
    return ordered[index]


def median(values: list[float]) -> float:
    return round(statistics.median(values), 4) if values else 0.0


def toast_control(editor: EnhancedConsoleInput):
    """取出浮层里的 FormattedTextControl，用于统计文本回调次数。"""
    return editor.app.layout.container.floats[0].content.content.content


async def run_once(mode: str, trial: int, keep_frames: bool) -> tuple[dict, list | None]:
    capture = io.StringIO()
    output = Vt100_Output(capture, lambda: Size(rows=ROWS, columns=COLUMNS), enable_cpr=False)
    calls = {"toast": 0}
    clock = {"float": 0.0}
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
        editor = EnhancedConsoleInput(
            status=lambda: "fixture", running=lambda: False, dynamic_values=lambda *_: (),
            plain=False, motion="none", output_palette="trina",
        )
        for index in range(HISTORY_RECORDS):
            editor.write(render_answer(
                f"## 历史 {index}\n稳定历史行 {index}：保留视口内容，不参与本次测量。\n"
                "```text\nprevious line with fixed content\n```\n", color=True))

        if mode == "control":
            container = editor.app.layout.container
            editor.app.layout = Layout(container.content, focused_element=editor.input_window.content)
        else:
            control = toast_control(editor)
            original_text = control.text
            def counted(_original=original_text):
                calls["toast"] += 1
                return _original()
            control.text = counted
            def draw_float(self, *args, **kwargs):
                started = time.perf_counter()
                try:
                    return ORIGINAL_DRAW_FLOAT(self, *args, **kwargs)
                finally:
                    clock["float"] += (time.perf_counter() - started) * 1000
            FloatContainer._draw_float = draw_float

        paints: list[dict] = []
        state = {"start": 0.0, "measure": False, "totals": []}

        def before_render(_app) -> None:
            state["start"] = time.perf_counter()
            clock["float"] = 0.0

        def after_render(_app) -> None:
            data = capture.getvalue()
            capture.seek(0)
            capture.truncate(0)
            total_ms = (time.perf_counter() - state["start"]) * 1000
            float_ms = clock["float"] if mode != "control" else 0.0
            if state["measure"]:
                state["totals"].append(total_ms)
            paints.append({
                "total_ms": total_ms,
                "float_ms": float_ms,
                "bytes": len(data),
                "measured": bool(state["measure"]),
                "ansi": base64.b64encode(data.encode("utf-8")).decode("ascii"),
            })

        editor.app.before_render += before_render
        editor.app.after_render += after_render

        async def feed() -> None:
            await asyncio.sleep(0.5)
            if mode == "visible":
                editor.show_notification("Trina", "编译完成 · 3 项工具", seconds=3600)
                await asyncio.sleep(0.15)
            state["measure"] = True
            thought = ""
            for _ in range(16):
                thought += "检查工具输入和编译条件，保留真实失败信息。"
                editor.update_thinking("fixture", thought)
                await asyncio.sleep(INTERVAL)
            editor.update_thinking("fixture", thought, final=True)
            text = ""
            for index in range(32):
                if index == 0:
                    text += "准备执行测试。\n```python\n# first line"
                elif index == 31:
                    text += "```\n完成编码。"
                else:
                    text += f"value_{index} = {{'index': {index}, 'valid': True}} # code"
                editor.update_stream(text)
                await asyncio.sleep(INTERVAL)
            editor.update_stream(text, final=True)
            emitted = ""
            for index in range(12):
                emitted += f"fixture_output_line_{index}: controlled replay\n"
                editor.update_tool("fixture-tool", streaming_answer(emitted, color=True))
                await asyncio.sleep(INTERVAL)
            state["measure"] = False
            await asyncio.sleep(0.25)
            editor.app.exit()

        cpu_start = time.process_time()
        started = time.perf_counter()
        try:
            task = asyncio.create_task(feed())
            await editor.app.run_async()
            await task
        finally:
            FloatContainer._draw_float = ORIGINAL_DRAW_FLOAT
        elapsed_ms = (time.perf_counter() - started) * 1000
        cpu_ms = (time.process_time() - cpu_start) * 1000
        container = type(editor.app.layout.container).__name__

    measured = [paint["total_ms"] for paint in paints if paint["measured"]]
    floats = [paint["float_ms"] for paint in paints if paint["measured"]]
    total_ms = sum(measured)
    result = {
        "mode": mode,
        "trial": trial,
        "container": container,
        "toast_calls": calls["toast"],
        "frames": len(paints),
        "measured_frames": len(measured),
        "frame_p50_ms": round(quantile(measured, 0.50), 3),
        "frame_p95_ms": round(quantile(measured, 0.95), 3),
        "frame_sum_ms": round(total_ms, 3),
        "float_p50_ms": round(quantile(floats, 0.50), 4),
        "float_p95_ms": round(quantile(floats, 0.95), 4),
        "float_max_ms": round(max(floats), 4) if floats else 0.0,
        "float_sum_ms": round(sum(floats), 3),
        "float_share_pct": round(sum(floats) / total_ms * 100, 3) if total_ms else 0.0,
        "ansi_bytes": sum(paint["bytes"] for paint in paints),
        "ansi_sha256": hashlib.sha256(
            b"".join(base64.b64decode(paint["ansi"]) for paint in paints)).hexdigest(),
        "elapsed_ms": round(elapsed_ms, 1),
        "cpu_ms": round(cpu_ms, 1),
    }
    if keep_frames:
        result["frames_file"] = f"frames/{mode}-t{trial}.json"
        last_measured = max(index for index, paint in enumerate(paints) if paint["measured"])
        # 只保留到最后一个测量帧：退出时会离开备用屏幕，继续喂给终端会把画面清空。
        return result, paints[:last_measured + 1]
    return result, None


def summarise(results: list[dict]) -> dict:
    summary = {}
    for mode in SEQUENCE:
        rows = [row for row in results if row["mode"] == mode]
        summary[mode] = {
            "runs": len(rows),
            "frames": median([row["measured_frames"] for row in rows]),
            "frame_p50_ms": median([row["frame_p50_ms"] for row in rows]),
            "frame_p95_ms": median([row["frame_p95_ms"] for row in rows]),
            "frame_sum_ms": median([row["frame_sum_ms"] for row in rows]),
            "float_p50_ms": median([row["float_p50_ms"] for row in rows]),
            "float_p95_ms": median([row["float_p95_ms"] for row in rows]),
            "float_max_ms": median([row["float_max_ms"] for row in rows]),
            "float_sum_ms": median([row["float_sum_ms"] for row in rows]),
            "float_share_pct": median([row["float_share_pct"] for row in rows]),
            "toast_calls": sum(row["toast_calls"] for row in rows),
            "ansi_bytes": median([row["ansi_bytes"] for row in rows]),
            "cpu_ms": median([row["cpu_ms"] for row in rows]),
        }
    hidden, control, visible = summary["current"], summary["control"], summary["visible"]
    hidden_hashes = {row["ansi_sha256"] for row in results if row["mode"] == "current"}
    control_hashes = {row["ansi_sha256"] for row in results if row["mode"] == "control"}
    summary["output"] = {
        "current_hashes": len(hidden_hashes),
        "control_hashes": len(control_hashes),
        "byte_identical": hidden_hashes == control_hashes and len(hidden_hashes) == 1,
        "bytes": hidden["ansi_bytes"],
    }
    summary["delta"] = {
        "frame_p50_ms": round(hidden["frame_p50_ms"] - control["frame_p50_ms"], 3),
        "frame_p95_ms": round(hidden["frame_p95_ms"] - control["frame_p95_ms"], 3),
        "float_over_visible": round(hidden["float_p50_ms"] - visible["float_p50_ms"], 4),
    }
    summary["verdict"] = {
        "toast_not_built_when_hidden": hidden["toast_calls"] == 0,
        "toast_built_when_visible": visible["toast_calls"] > 0,
        "output_byte_identical": summary["output"]["byte_identical"],
        "float_p95_under_0.1ms": hidden["float_p95_ms"] < 0.1,
        "float_share_under_1pct": hidden["float_share_pct"] < 1.0,
    }
    summary["verdict"]["no_hidden_overlay_slowdown"] = all(summary["verdict"].values())
    return summary


async def main_async(trials: int, keep_all: bool, warmup: int) -> dict:
    results: list[dict] = []
    frames_dir = HERE / "frames"
    if frames_dir.exists():
        for path in frames_dir.glob("*.json"):
            path.unlink()
    frames_dir.mkdir(exist_ok=True)
    for round_index in range(warmup):
        for mode in SEQUENCE:
            await run_once(mode, 0, keep_frames=False)
        print(json.dumps({"warmup": round_index + 1, "done": True}, ensure_ascii=False), flush=True)
    saved: set[str] = set()
    for trial in range(1, trials + 1):
        for mode in SEQUENCE:
            keep = keep_all or (trial == 1 and mode not in saved)
            result, paints = await run_once(mode, trial, keep_frames=keep)
            if keep:
                saved.add(mode)
            results.append(result)
            if paints is not None:
                payload = {"mode": mode, "trial": trial, "columns": COLUMNS, "rows": ROWS,
                           "frames": [paint["ansi"] for paint in paints]}
                (frames_dir / f"{mode}-t{trial}.json").write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "python": sys.version.split()[0], "columns": COLUMNS, "rows": ROWS,
            "interval_ms": int(INTERVAL * 1000), "trials": trials, "warmup": warmup,
            "sequence": list(SEQUENCE), "results": results, "summary": summarise(results)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1, help="测量前按同一序列预热，丢弃结果")
    parser.add_argument("--all-frames", action="store_true", help="保存每一轮的帧，便于复核")
    args = parser.parse_args()
    report = asyncio.run(main_async(args.trials, args.all_frames, args.warmup))
    (HERE / "smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
