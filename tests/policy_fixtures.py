"""仅供拒绝分支和调度单测使用的最小内存夹具，不随产品提供预置靶场。"""
from pathlib import Path
from trina.policy import PolicyRegistry


def sample_policy():
    registry = PolicyRegistry()
    registry._absorb({"name": "fixture", "targets": [
        {"id": "lab-http", "url": "http://127.0.0.1:9099"},
        {"id": "lab-ssh", "url": "ssh://127.0.0.1:2222"}],
        "tests": [
            {"id": name, "target_id": "lab-http", "tool": tool, "level": level,
             "params": params, "expect_finding": finding}
            for name, tool, level, params, finding in (
                ("port_quick", "port_scan", "detect", {"target": "127.0.0.1", "ports": "quick"}, ""),
                ("sqli_probe", "sqli_test", "verify", {"url": "http://127.0.0.1:9099/sqli?id=1"}, "sqli"),
                ("lfi_probe", "lfi_test", "verify", {"url": "http://127.0.0.1:9099/lfi?file=hello.txt"}, "lfi"),
                ("cmd_probe", "cmd_inject_test", "verify", {"url": "http://127.0.0.1:9099/rce?host=127.0.0.1"}, "cmd_injection"),
                ("dir_enum", "dir_bruteforce", "detect", {"url": "http://127.0.0.1:9099"}, "information_disclosure"),
                ("poc_scan", "poc_scan", "detect", {"url": "http://127.0.0.1:9099"}, "information_disclosure"),
                ("flag_read", "flag_hunt", "verify", {"target": "http://127.0.0.1:9099"}, "flag"),
            )]}, Path("test-fixture.yaml"))
    return registry
