"""证据关系图：把资产、路径、发现、证据、检查项、假设连成可追溯的关系图。

解决的问题
----------
报告与状态里证据是**列表**：用户想知道"这条结论凭什么成立"时只能翻日志。
本模块把同一批事实组织成图，支持从**结论反向追溯每一条证据来源**：

    主机 → 端口/服务 → 路径 → 发现 → 证据条目
                        ↘ 注册检查项（哪个 test_id 产出的）
                        ↘ 假设（被哪条假设驱动、假设是否被确认/否定）

输出三种形态：`to_dict()`（进 state.json / 工具消费）、`to_mermaid()`（进报告与控制台）、
`summary()`（一句人话统计）。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

NODE_KINDS = ("target", "host", "port", "path", "finding", "evidence", "check", "hypothesis", "flag", "credential")


@dataclass
class GraphNode:
    id: str
    label: str
    kind: str = "host"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphEdge:
    src: str
    dst: str
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)

    # ------------------------------------------------------------------ 构建辅助
    def add_node(self, node_id: str, label: str, kind: str, **meta: Any) -> str:
        for existing in self.nodes:
            if existing.id == node_id:
                return node_id
        self.nodes.append(GraphNode(id=node_id, label=_clean(label), kind=kind, meta=meta))
        return node_id

    def add_edge(self, src: str, dst: str, label: str = "") -> None:
        if src and dst and src != dst:
            if not any(e.src == src and e.dst == dst and e.label == label for e in self.edges):
                self.edges.append(GraphEdge(src=src, dst=dst, label=label))

    def node_kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.nodes:
            counts[node.kind] = counts.get(node.kind, 0) + 1
        return counts

    def summary(self) -> str:
        kinds = self.node_kinds()
        parts = "，".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
        return f"证据图：{len(self.nodes)} 个节点（{parts}）、{len(self.edges)} 条关系"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "summary": self.summary(),
        }

    # ------------------------------------------------------------------ 渲染
    def to_mermaid(self, *, max_nodes: int = 80, direction: str = "LR") -> str:
        """渲染成 mermaid flowchart（超量时按"发现优先"裁剪并标注）。"""
        if not self.nodes:
            return "```mermaid\nflowchart LR\n  empty[暂无证据]\n```"
        priority = {"finding": 0, "evidence": 1, "hypothesis": 2, "check": 3, "path": 4, "port": 5, "host": 6, "target": 7, "flag": 1, "credential": 2}
        ordered = sorted(self.nodes, key=lambda n: (priority.get(n.kind, 9), n.id))
        kept = ordered[:max_nodes]
        kept_ids = {node.id for node in kept}
        dropped = len(self.nodes) - len(kept)

        lines = [f"flowchart {direction}"]
        for node in kept:
            lines.append(f"  {_node_id(node.id)}{_node_shape(node)}")
        for edge in self.edges:
            if edge.src in kept_ids and edge.dst in kept_ids:
                label = f"|{edge.label}|" if edge.label else ""
                lines.append(f"  {_node_id(edge.src)} -->{label} {_node_id(edge.dst)}")
        if dropped > 0:
            lines.append(f"  dropped[另有 {dropped} 个节点未显示]")
        return "```mermaid\n" + "\n".join(lines) + "\n```"


def _clean(text: str, limit: int = 42) -> str:
    """mermaid 标签清洗：去掉会破坏语法的字符。"""
    out = re.sub(r'[\[\]{}()<>"|`]', "", str(text or ""))
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit] or "?"


def _node_id(node_id: str) -> str:
    return "n" + re.sub(r"[^0-9a-zA-Z]", "_", node_id)


def _node_shape(node: GraphNode) -> str:
    label = node.label
    shapes = {
        "target": f'([{label}])',
        "host": f'([{label}])',
        "port": f'[{label}]',
        "path": f'[/{label}/]',
        "finding": f'{{{label}}}',
        "evidence": f'[({label})]',
        "check": f'[[{label}]]',
        "hypothesis": f'>{label}]',
        "flag": f'(({label}))',
        "credential": f'[{label}]',
    }
    return shapes.get(node.kind, f'[{label}]')


def build(
    state: Any,
    *,
    registry: Any | None = None,
    hypotheses: Iterable[dict[str, Any]] | None = None,
) -> EvidenceGraph:
    """从 `RunState` 构建证据图。`registry`（PolicyRegistry）用于把 test_id 关联到工具。"""
    graph = EvidenceGraph()
    target = str(getattr(state, "target", "") or "")
    if target:
        graph.add_node(f"target:{target}", target, "target")

    # 主机 / 端口 / 路径
    host_ids: dict[str, str] = {}
    for host, host_state in (getattr(state, "hosts", None) or {}).items():
        host_id = graph.add_node(f"host:{host}", host, "host", alive=getattr(host_state, "alive", None))
        host_ids[host] = host_id
        if target:
            graph.add_edge(f"target:{target}", host_id, "测试对象")
        for port, info in sorted((getattr(host_state, "ports", None) or {}).items()):
            info = info if isinstance(info, dict) else {}
            label = f"{port}/{info.get('service') or '?'}" + (f" {info['version']}" if info.get("version") else "")
            port_id = graph.add_node(f"port:{host}:{port}", label, "port")
            graph.add_edge(host_id, port_id, "开放")
        for tech in getattr(host_state, "technologies", []) or []:
            tech_id = graph.add_node(f"tech:{host}:{tech}", str(tech), "path", technology=True)
            graph.add_edge(host_id, tech_id, "技术栈")
        for url in getattr(host_state, "web_paths", []) or []:
            path_id = graph.add_node(f"path:{url}", str(url), "path")
            graph.add_edge(host_id, path_id, "发现")

    # 发现 → 证据
    findings = getattr(state, "findings", None) or []
    for finding in findings:
        fid = getattr(finding, "id", "") or f"F{len(graph.nodes)}"
        label = f"[{getattr(finding, 'severity', '?')}] {getattr(finding, 'vuln_class', '?')} {getattr(finding, 'status', '')}"
        finding_id = graph.add_node(f"finding:{fid}", label, "finding",
                                   url=getattr(finding, "url", ""), target=getattr(finding, "target", ""))
        # 挂到最贴近的父节点：同 URL 的路径节点 → 否则主机节点
        parent = ""
        url = str(getattr(finding, "url", "") or "")
        for node in graph.nodes:
            if node.kind == "path" and url and url.rstrip("/").endswith(node.label.rstrip("/")):
                parent = node.id
                break
        if not parent:
            raw_host = str(getattr(finding, "target", "") or "").split(":")[0]
            parent = host_ids.get(raw_host, host_ids.get(next(iter(host_ids), ""), ""))
        if parent:
            graph.add_edge(parent, finding_id, "命中")
        for index, evidence in enumerate(getattr(finding, "evidence", []) or [], 1):
            ev_label = str(evidence.get("kind") or "evidence") + (f" · {evidence.get('note')}" if evidence.get("note") else "")
            ev_id = graph.add_node(f"evi:{fid}:{index}", ev_label, "evidence")
            graph.add_edge(finding_id, ev_id, "证据")
        tool = str(getattr(finding, "tool", "") or "")
        if tool:
            check = _check_id_for_tool(tool, registry)
            check_node = graph.add_node(f"check:{check}", f"{check}（{tool}）", "check")
            graph.add_edge(check_node, finding_id, "产出")

    # 注册检查项的覆盖情况（已测）
    coverage = getattr(state, "coverage", None) or {}
    for test_id in coverage.get("executed") or []:
        check_node = graph.add_node(f"check:{test_id}", str(test_id), "check")
        graph.add_edge(f"target:{target}" if target else check_node, check_node, "已执行")

    # 假设（由台账传入）
    for item in hypotheses or []:
        hid = str(item.get("id") or "")
        if not hid:
            continue
        status_cn = item.get("status_cn") or item.get("status", "")
        h_node = graph.add_node(f"hypo:{hid}", f"{hid} {status_cn} {str(item.get('statement', ''))[:30]}", "hypothesis")
        for evidence in item.get("evidence") or []:
            fid = evidence.get("finding_id")
            if fid:
                graph.add_edge(h_node, f"finding:{fid}", "验证为")

    # 凭据 / flag
    for cred in getattr(state, "credentials", None) or []:
        cid = graph.add_node(f"cred:{getattr(cred, 'target', '')}:{getattr(cred, 'username', '')}",
                             f"{getattr(cred, 'service', '')} {getattr(cred, 'username', '')}", "credential")
        raw_host = str(getattr(cred, "target", "") or "").split(":")[0]
        graph.add_edge(host_ids.get(raw_host, f"target:{target}"), cid, "获得凭据")
    for flag in getattr(state, "flags", None) or []:
        value = str(flag.get("value", ""))[:24]
        flag_id = graph.add_node(f"flag:{value}", value, "flag")
        raw_host = str(flag.get("source", "") or "")
        parent = ""
        for node in graph.nodes:
            if node.kind == "path" and raw_host and raw_host.rstrip("/").endswith(node.label.rstrip("/")):
                parent = node.id
                break
        graph.add_edge(parent or (f"target:{target}" if target else flag_id), flag_id, "夺取")
    return graph


def _check_id_for_tool(tool: str, registry: Any | None) -> str:
    """把工具名映射回注册检查项；没有注册表就直接用工具名。"""
    if registry is not None:
        for test in getattr(registry, "tests", {}).values():
            if getattr(test, "tool", "") == tool:
                return str(getattr(test, "test_id", tool))
    return tool


__all__ = ["EvidenceGraph", "GraphEdge", "GraphNode", "NODE_KINDS", "build"]
