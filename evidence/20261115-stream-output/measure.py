"""可重跑的本地输出链路压测；不访问模型或网络。"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from trina.events import EventBus, ModelEventStream
from trina.event_view import EventTranscript
from trina.llm import LLMResponse, LLMStreamEvent


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="trina-output-measure-") as tmp:
        bus = EventBus(Path(tmp) / "events.sqlite3", "measure")
        bus.subscribe(EventTranscript(lambda text: None).consume)
        started = time.perf_counter()
        stream = ModelEventStream(bus.publish)
        for _ in range(300):
            stream.on_event(LLMStreamEvent("thinking_delta", delta="思考"))
        stream.finish(LLMResponse(ok=True, text="完成"))
        elapsed = time.perf_counter() - started
        recovered = "".join(event.payload["delta"] for event in bus.replay() if event.kind == "model.thinking_delta")
        print(json.dumps({"chunks": 300, "events": bus.last_seq, "seconds": round(elapsed, 3),
                          "text_exact": recovered == "思考" * 300}, ensure_ascii=False))


if __name__ == "__main__":
    main()
