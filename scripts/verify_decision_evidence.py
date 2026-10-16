"""用全新 lab 与固定决策脚本复核证据链；不消耗模型调用或修改授权配置。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from trina.audit import AuditLog
from trina.brain.agent import PentestAgent
from trina.config import LLMConfig, TrinaConfig
from trina.lab.http_target import create_server
from trina.llm.replay_backend import ReplayBackend
from trina.policy import AuthorizationController, PolicyRegistry, RegisteredTarget, RegisteredTest
from trina.report import build_report
from trina.review import review_state
from trina.tools import build_registry


def verify(output: Path) -> dict:
    """每轮使用新端口、新靶机、新运行目录，保存状态、审计、报告和复核记录。"""
    output.mkdir(parents=True, exist_ok=False)
    server, thread = create_server(port=0)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        url = base + "/sqli"
        actions = [
            {"action": "sqli_test", "test_id": "sqli_recheck", "params": {"url": url, "param": "id", "technique": "error"},
             "hypothesis": {"statement": "id 参数可能存在错误型 SQL 注入", "vuln_class": "sqli", "target": url, "parameter": "id"}},
            {"final": {"summary": "固定脚本执行结束，结果由工具证据判定"}},
        ]
        script = output / "replay.json"
        script.write_text(json.dumps({"steps": [{"response": json.dumps(a, ensure_ascii=False)} for a in actions]},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
        config = TrinaConfig.load()
        config.run.workspace = str(output / "runs")
        config.audit.dir = str(output / "logs")
        policy = PolicyRegistry()
        policy.targets["fresh-lab"] = RegisteredTarget("fresh-lab", (base,))
        policy.tests["sqli_recheck"] = RegisteredTest("sqli_recheck", "fresh-lab", "sqli_test", "verify")
        controller = AuthorizationController(policy, mode="strict")
        backend = ReplayBackend(LLMConfig(backend="replay", replay_script=str(script)))
        agent = PentestAgent(config, registry=build_registry(), backend=backend, controller=controller)
        state = agent.run(base, goal="确认 SQL 注入并给出证据", max_steps=3, max_seconds=60)
        audit = AuditLog(Path(state.artifacts["audit_path"]), state.run_id)
        chain_ok, chain_detail = audit.verify()
        reports = build_report(state, config).save(output / "reports")
        review = review_state(state.to_dict(), audit_path=audit.path, registry=policy)
        (output / "review.md").write_text(review.to_markdown(), encoding="utf-8")
        ok = (state.status == "success" and state.verdict == "CONFIRMED" and chain_ok
              and state.hypotheses[0]["status"] == "confirmed" and not review.verdict_conflicts
              and not review.claims_without_evidence)
        summary = {"ok": ok, "status": state.status, "verdict": state.verdict,
                   "hypotheses": state.hypotheses, "findings": len(state.findings),
                   "audit_chain_ok": chain_ok, "audit_detail": chain_detail,
                   "reports": [str(path) for path in reports], "artifacts": state.artifacts,
                   "limits": "固定决策 + 内置模拟靶机，仅验证执行机制；不衡量模型规划能力或公开基准成绩"}
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evidence") / ("decision-evidence-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]))
    args = parser.parse_args()
    results = [verify(args.output.resolve() / f"round-{index}") for index in (1, 2)]
    print(json.dumps({"passed": sum(bool(result["ok"]) for result in results), "rounds": len(results),
                      "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
