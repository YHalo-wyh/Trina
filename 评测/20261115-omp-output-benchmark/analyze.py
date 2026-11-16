"""复算全部样本，导出聚合指标、图和 VT 文本画面。"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / os.environ.get("TRINA_BENCH_ANALYZE", "results")
ARTIFACT_ROOT = ROOT if RESULTS.name == "results" else RESULTS


def quantile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)


def aggregate():
    summary = json.loads((RESULTS / "summary.json").read_text(encoding="utf-8"))
    fixture_bytes = (RESULTS / "fixtures.json").read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == summary["fixture_sha256"]
    assert len(summary["results"]) == 18
    output = []
    for scenario in ("short", "history", "four_streams"):
        for engine in ("trina", "omp"):
            trials = [json.loads((RESULTS / f"{engine}-{scenario}-{trial}.json").read_text(encoding="utf-8")) for trial in (1, 2, 3)]
            observations = [row for trial in trials for row in trial["observations"]]
            paints = [row for trial in trials for row in trial["paints"]]
            for trial in trials:
                assert not trial["summary"]["stderr"]
                assert trial["summary"]["keys_observed"] == {"short": 4, "history": 6, "four_streams": 8}[scenario]
                assert trial["final_screen"].strip()
                if scenario != "four_streams":
                    assert trial["summary"]["output_observed"] == trial["summary"]["output_expected"]
            row = {"engine": engine, "scenario": scenario, "trials": 3,
                   "output_seen": sum(trial["summary"]["output_observed"] for trial in trials),
                   "output_expected": sum(trial["summary"]["output_expected"] for trial in trials),
                   "frames": len(paints), "frame_p50_ms": quantile([p["render_ms"] for p in paints], .5),
                   "frame_p95_ms": quantile([p["render_ms"] for p in paints], .95),
                   "frame_max_ms": round(max(p["render_ms"] for p in paints), 3),
                   "frame_over_33ms": sum(p["render_ms"] > 1000 / 30 for p in paints),
                   "terminal_p95_ms": quantile([p["terminal_ms"] for p in paints], .95),
                   "first_output_median_ms": round(statistics.median(trial["summary"]["first_output_ms"] for trial in trials), 3),
                   "cpu_median_ms": statistics.median(trial["summary"]["cpu_ms"] for trial in trials),
                   "peak_rss_median_mb": statistics.median(trial["summary"]["peak_rss_mb"] for trial in trials),
                   "ansi_bytes_median": statistics.median(trial["summary"]["total_ansi_bytes"] for trial in trials)}
            for name, selector in (
                ("output", lambda p: p["kind"] == "output"),
                ("thinking", lambda p: p.get("phase") == "thinking"),
                ("text", lambda p: p.get("phase") == "text"),
                ("tool", lambda p: p.get("phase") == "tool_output"),
                ("key", lambda p: p["kind"] == "key"),
                ("scroll", lambda p: p["kind"] == "scroll" and p["moved"]),
            ):
                values = [p["latency_ms"] for p in observations if selector(p)]
                row[f"{name}_p50_ms"] = quantile(values, .5)
                row[f"{name}_p95_ms"] = quantile(values, .95)
                row[f"{name}_n"] = len(values)
            output.append(row)
    (RESULTS / "aggregated.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary, output


def screenshots():
    from PIL import Image, ImageDraw, ImageFont
    image_dir = ARTIFACT_ROOT / "screens"
    image_dir.mkdir(exist_ok=True)
    mono = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 15)
    chinese = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
    symbol = ImageFont.truetype("C:/Windows/Fonts/seguisym.ttf", 15)
    for scenario in ("short", "history", "four_streams"):
        for engine in ("trina", "omp"):
            data = json.loads((RESULTS / f"{engine}-{scenario}-1.json").read_text(encoding="utf-8"))
            screen = data["final_screen"]
            image = Image.new("RGB", (1120, 810), "#131019")
            draw = ImageDraw.Draw(image)
            draw.text((20, 15), f"{engine.upper()} / {scenario} / 120 x 36 / trial 1", font=mono, fill="#e0b6ec")
            draw.text((20, 40), "VT text capture; derived from terminal cells, not a desktop screenshot", font=mono, fill="#a995b5")
            for y, line in enumerate(screen.splitlines()[:36]):
                x = 20
                for char in line:
                    font = chinese if ord(char) > 0x2fff else symbol if ord(char) > 0x2000 else mono
                    draw.text((x, 75 + y * 20), char, font=font, fill="#e6deed")
                    x += 18 if unicodedata.east_asian_width(char) in "WF" else 0 if unicodedata.combining(char) else 9
            image.save(image_dir / f"{engine}-{scenario}.png")
            (image_dir / f"{engine}-{scenario}.txt").write_text(screen + "\n", encoding="utf-8")


def chart(rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
    figure, axes = plt.subplots(1, 2, figsize=(12, 4.4), layout="constrained")
    scenarios = ("short", "history", "four_streams")
    for ax, metric, title in ((axes[0], "output_p95_ms", "Input to visible output (P95)"),
                              (axes[1], "frame_p95_ms", "Application paint work (P95)")):
        for offset, engine, color in ((-.18, "trina", "#b66fc9"), (.18, "omp", "#429a9c")):
            values = [next(row[metric] for row in rows if row["engine"] == engine and row["scenario"] == scenario) for scenario in scenarios]
            bars = ax.bar([i + offset for i in range(3)], values, .35, label=engine.upper(), color=color)
            ax.bar_label(bars, labels=[str(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) for value in values], padding=3)
        ax.set_xticks(range(3), ["Short + tool", "Long history", "4 active streams*"])
        ax.set_ylabel("Milliseconds (lower is better)")
        ax.set_title(title)
        ax.legend(frameon=False)
        ax.set_ylim(0, ax.get_ylim()[1] * 1.13)
    figure.suptitle("Same Windows machine / Kitty VT 120x36 / fixed 40ms replay / 3 trials")
    figure.text(.02, -.04, "* Four-stream visibility is partial; see coverage counts. No model/network/GPU latency is measured.", fontsize=10)
    figure.savefig(ARTIFACT_ROOT / "latency-comparison.png", dpi=160, bbox_inches="tight")
    plt.close(figure)


def provenance():
    project = ROOT.parents[1]
    if ARTIFACT_ROOT == ROOT:
        patterns = ("*.py", "*.ts", "*.ps1", "*.json", "*.lock", "*.log", "*.png", "results/*.json", "screens/*")
        paths = {path for pattern in patterns for path in ROOT.glob(pattern) if path.is_file()}
    else:
        paths = {path for path in ARTIFACT_ROOT.rglob("*") if path.is_file()}
        paths.update(path for path in ROOT.iterdir() if path.is_file() and path.suffix in {".py", ".ts", ".ps1", ".json", ".lock"})
    paths.discard(ARTIFACT_ROOT / "manifest.json")
    paths.discard(ROOT / "manifest.json")
    paths.update(project / name for name in ("trina/console_ui.py", "trina/transcript.py", "trina/terminal.py",
                                             "trina/markdown_view.py", "trina/event_view.py", "trina/events.py",
                                             "trina/provider_form.py", "trina/subworker_ui.py"))
    manifest = {}
    for path in sorted(paths):
        value = path.read_bytes()
        # 新评测的源码按仓库 LF 规范取指纹；运行原件保持采集字节。
        if ARTIFACT_ROOT != ROOT and path.suffix in {".py", ".ts", ".ps1"} and not path.is_relative_to(ARTIFACT_ROOT):
            value = value.replace(b"\r\n", b"\n")
        manifest[str(path.relative_to(project)).replace("\\", "/")] = hashlib.sha256(value).hexdigest()
    (ARTIFACT_ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    summary, rows = aggregate()
    screenshots()
    chart(rows)
    provenance()
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
