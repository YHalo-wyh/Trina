"""当前产品机器规约的自检与防回归测试。"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from scripts.verify_contract import ALLOWED_COMMANDS, check, load_contract


ROOT = Path(__file__).resolve().parents[1]


class MachineContractTests(unittest.TestCase):
    def test_current_contract_is_machine_verifiable(self) -> None:
        report = check(ROOT)
        self.assertTrue(report.ok, "\n".join(report.errors))
        self.assertEqual(report.deliverables, 6)
        self.assertEqual(report.requirements, 21)

    def test_contract_cannot_replace_whitelisted_commands(self) -> None:
        data = copy.deepcopy(load_contract(ROOT))
        data["verification_commands"]["V-TESTS"]["argv"] = ["sh", "arbitrary.sh"]
        report = check(ROOT, data=data)
        self.assertTrue(any("V-TESTS argv" in error for error in report.errors))
        self.assertEqual(ALLOWED_COMMANDS["V-TESTS"][1:4], ("-X", "utf8", "-m"))

    def test_unknown_test_reference_is_rejected(self) -> None:
        data = copy.deepcopy(load_contract(ROOT))
        data["requirements"][0]["rules"][0]["tests"] = ["tests.missing.Unknown.test_absent"]
        report = check(ROOT, data=data)
        self.assertTrue(any("引用不存在的测试" in error for error in report.errors))

    def test_ctf_skill_inventory_is_complete(self) -> None:
        names = {
            "solve-challenge", "ctf-web", "ctf-pwn", "ctf-crypto", "ctf-reverse",
            "ctf-forensics", "ctf-misc", "ctf-osint", "ctf-malware", "ctf-ai-ml",
            "ctf-similar-search", "ctf-writeup",
        }
        missing = [name for name in sorted(names) if not (ROOT / ".omp" / "skills" / name / "SKILL.md").is_file()]
        self.assertEqual(missing, [])
        self.assertTrue((ROOT / ".omp" / "skills" / "CTF_SKILLS_SOURCE.md").is_file())
        self.assertTrue((ROOT / ".omp" / "skills" / "ctf-skills.LICENSE").is_file())


if __name__ == "__main__":
    unittest.main()
