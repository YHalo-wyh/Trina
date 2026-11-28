"""项目内短经验卡：只收录带验收证据的路径，检索结果仍需本题复验。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .brain.prompts import infer_task_route_key
from .context import BRIEF_VERSION
from .state import RunState


_PROOF_READ_BUDGET = 12_000_000


def features(text: str) -> list[str]:
    """英文词与中文双字片段；不加载模型或向量服务。"""
    words = re.findall(r"[a-z][a-z0-9_]{2,}|[\u4e00-\u9fff]+", text.casefold())
    terms: set[str] = set()
    for word in words:
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            terms.update(word[index:index + 2] for index in range(len(word) - 1))
        elif word not in {"the", "and", "with", "for", "task", "test", "verify"}:
            terms.add(word)
    return sorted(terms)[:48]


def conditions(task: Any) -> dict[str, str]:
    """复用已有任务的执行上下文与目标类型，不推测缺失环境。"""
    target = str(task.target)
    scheme = urlsplit(target).scheme if "://" in target else "local_or_host"
    result = {"target_kind": scheme}
    if task.context_key:
        result["context_key"] = task.context_key
    for item in task.capabilities:
        result["capability:" + str(item)] = "required"
    return result


def _text(value: Any, limit: int = 300) -> str:
    """卡片不携带 flag、凭据或完整命令参数。"""
    text = str(value or "")
    text = re.sub(r"(?i)\b(?:flag|ctf)\{[^}]*\}", "<challenge-result>", text)
    text = re.sub(r"(?i)(password|passwd|token|secret|api_key|authorization)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    return text[:limit]


class StrategyMemory:
    """一个 SQLite 文件和关键词索引；无后台线程、外部工具或额外模型调用。"""

    def __init__(self, path: str | Path, *, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly

    @property
    def revision(self) -> int:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return 0

    @staticmethod
    def _proof(item: dict, root: Path) -> dict | None:
        """只接受当前运行目录下仍完整的工件，不把任意状态文本当验收证明。"""
        if item.get("successful") is not True or item.get("synthetic"):
            return None
        ref = str(item.get("id") or item.get("evidence_id") or "")
        path, digest = item.get("path"), item.get("sha256")
        if not ref or not path or not digest:
            return None
        file = Path(path).resolve()
        try:
            if not file.is_relative_to(root.resolve()) or file.stat().st_size > 4_000_000:
                return None
            if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                return None
        except OSError:
            return None
        return {"id": ref, "path": str(file), "sha256": digest}

    def remember(self, task: Any, state: RunState, *, workspace: Path) -> str | None:
        """从已执行步骤和独立验收回执构卡；普通 success/flag 声明不足以入库。"""
        if self.readonly:
            return None
        requested_refs = {str(row.get("evidence_id") or "") for row in state.casebook.get("goal_verifications", [])[:3]
                          if row.get("status") == "passed"}
        for finding in state.findings[:20]:
            if finding.status == "verified" and state.casebook.get("verification", {}).get(finding.id):
                requested_refs.update(str(row.get("id") or "") for row in finding.evidence[:8])
        if not requested_refs:
            return None
        requested_refs = set(sorted(requested_refs)[:8])
        proofs = {str(item.get("id") or ""): proof for item in state.evidence
                  if isinstance(item, dict) and item.get("id") in requested_refs
                  and (proof := self._proof(item, workspace))}
        verifications = []
        for row in state.casebook.get("goal_verifications", []):
            ref = str(row.get("evidence_id") or "")
            if row.get("status") == "passed" and ref in proofs:
                # 复用真实工具的回执判据，再核对归档正文，不能只信状态中的 passed。
                from .tools.workspace import _authority_verifier_facts
                try:
                    receipt = json.loads(Path(proofs[ref]["path"]).read_text(encoding="utf-8"))
                    _, metadata = _authority_verifier_facts(receipt["argv"], receipt.get("stdout", ""),
                        receipt.get("stderr", ""), exit_code=receipt.get("exit_code"), evidence_id=ref)
                except (ValueError, KeyError, TypeError, OSError):
                    continue
                if metadata.get("authority_verifier", {}).get("status") == "passed":
                    verifications.append({"method": "recorded_verifier_receipt", "evidence_refs": [ref]})
        for finding in state.findings:
            verification = state.casebook.get("verification", {}).get(finding.id) or {}
            refs = [str(row.get("id") or "") for row in finding.evidence]
            if (finding.status == "verified" and verification.get("explanation")
                    and len(set(refs)) >= 2 and all(ref in proofs for ref in refs)):
                verifications.append({"method": "independent_finding_check", "evidence_refs": refs[:8]})
        if not verifications:
            return None
        selected_refs = {ref for check in verifications for ref in check["evidence_refs"]}
        observations = [_text(row.get("text")) for key in ("facts", "intelligence")
                        for row in state.casebook.get(key, []) if isinstance(row, dict)
                        and selected_refs.intersection(row.get("evidence_refs") or [])]
        succeeded, failed = [], []
        for step in state.steps:
            if not isinstance(step, dict) or not step.get("action") or step.get("action") == "finish":
                continue
            destination = succeeded if step.get("ok") else failed
            destination.append({"action": _text(step["action"], 60),
                                "summary": _text(step.get("summary") or step.get("error"), 180)})
        terms = features(task.goal)
        if not terms:
            return None
        category = infer_task_route_key(task.goal, task.target, categories=("binary", "browser"))
        card = {"version": 1, "category": category, "features": terms,
                "goal": _text(task.goal), "conditions": conditions(task),
                "verified_clues": observations[:3], "successful_strategy": succeeded[-5:],
                "failed_strategy": failed[-3:], "verification": verifications[:3],
                "proofs": [proofs[ref] for ref in sorted(selected_refs)][:8],
                "source_run": state.run_id, "source_target_digest": hashlib.sha256(task.target.encode()).hexdigest(),
                "elapsed_seconds": state.duration, "tokens": state.ledger.get("total_tokens"),
                "recorded_at": time.time(),
                "qualification": "此运行路径已有验收回执；不代表每步必要，也不证明在当前题适用。"}
        # 验收版本进入身份，重复落盘幂等；不同上下文/证据版本保留独立卡片。
        identity = {key: card[key] for key in ("category", "features", "conditions", "successful_strategy")}
        identity["proof_digests"] = sorted(item["sha256"] for item in card["proofs"])
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=.1)) as db, db:
            db.executescript("CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY, category TEXT NOT NULL, body TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS terms (term TEXT, card_id TEXT, PRIMARY KEY(term, card_id));")
            cursor = db.execute("INSERT OR IGNORE INTO cards VALUES (?, ?, ?)",
                                (key, category, json.dumps(card, ensure_ascii=False)))
            if cursor.rowcount:
                db.executemany("INSERT INTO terms VALUES (?, ?)", [(term, key) for term in terms])
        return key

    def retrieve(self, task: Any, *, limit: int = 3, max_chars: int = 3000) -> list[dict]:
        """条件冲突直接排除；缺失条件标注复验，最多注入三张有界简报。"""
        terms, current = features(task.goal), conditions(task)
        if not self.path.is_file() or not terms or limit <= 0 or max_chars <= 0:
            return []
        category = infer_task_route_key(task.goal, task.target, categories=("binary", "browser"))
        placeholders = ",".join("?" for _ in terms)
        query = ("SELECT c.id, c.body, count(*) AS matches FROM terms t JOIN cards c ON c.id=t.card_id "
                 f"WHERE t.term IN ({placeholders}) AND c.category=? GROUP BY c.id "
                 "ORDER BY matches DESC, c.id LIMIT 30")
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            candidates = db.execute(query, (*terms, category)).fetchall()
        found, used = [], 0
        remaining_bytes = _PROOF_READ_BUDGET
        checked: dict[tuple[str, str], bool] = {}
        for key, body, matches in candidates:
            card = json.loads(body)
            if matches < min(2, len(terms)) or matches / max(1, min(len(terms), len(card["features"]))) < .35:
                continue
            required = card["conditions"]
            if any(name in current and current[name] != value for name, value in required.items()):
                continue
            missing = [name for name in required if name not in current]
            brief = {"version": BRIEF_VERSION, "source_task": "memory:" + key[:16],
                     "source_run_id": card["source_run"], "destination_task": task.id,
                     "similarity": round(matches / max(len(terms), len(card["features"])), 3),
                     "reasons": ["本地已验收经验；必须在当前目标复验"], "intelligence": {},
                     "experience": {name: card[name] for name in ("category", "goal", "conditions", "verified_clues",
                         "successful_strategy", "failed_strategy", "verification", "elapsed_seconds", "tokens", "qualification")},
                     "needs_revalidation": missing, "evidence_refs": [row["path"] + "#sha256=" + row["sha256"] for row in card["proofs"]]}
            size = len(json.dumps(brief, ensure_ascii=False))
            if used + size > max_chars:
                continue
            # 同次查询共享证明只校验一次；限制总读取量，防止大工件拖慢派发。
            valid = bool(card["proofs"])
            for proof in card["proofs"]:
                identity = (proof["path"], proof["sha256"])
                if identity not in checked:
                    try:
                        proof_size = Path(proof["path"]).stat().st_size
                    except OSError:
                        proof_size = remaining_bytes + 1
                    if proof_size > remaining_bytes:
                        checked[identity] = False
                    else:
                        remaining_bytes -= proof_size
                        checked[identity] = bool(self._proof(
                            {**proof, "successful": True}, Path(proof["path"]).parent))
                if not checked[identity]:
                    valid = False
                    break
            if not valid:
                continue
            found.append(brief)
            used += size
            if len(found) >= min(3, limit):
                break
        return found
