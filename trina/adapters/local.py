"""用户自建主机靶机适配（内网 Linux / Windows 靶机，如 VulnStack 手工搭建版）。

只做连通性检查：对 yaml 里每个 target 的 port 做一次 TCP connect（超时默认 3s）。
不起服务、不发应用层流量——「打靶」是 brain + tools 的事。

yaml 里声明的目标若不在授权网段内，会在 notes 里显式点名（护栏仍会在真正发包时拒绝）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping

from ..scope import is_target_in_scope
from .base import RangeAdapter, RangeInfo, RangeTarget, parse_targets, tcp_probe


class LocalRangeAdapter(RangeAdapter):
    """面向用户自建的 Linux / Windows 靶机。"""

    def __init__(self, config, spec: Mapping[str, Any] | None = None, path=None):
        super().__init__(config, spec, path)
        self.connect_timeout = float(self.spec.get("connect_timeout") or 3.0)
        self.targets_spec: list[RangeTarget] = parse_targets(self.spec.get("targets"))
        self._reachable: dict[str, bool] = {}
        self._info: RangeInfo | None = None

    # ------------------------------------------------------------ 内部
    def _probe(self) -> dict[str, bool]:
        """对每个声明目标做一次 TCP connect（并发执行，单目标超时 connect_timeout）。

        并发是为了让 `list_adapters()` 在大内网靶场（多个不可达 IP）下也能秒回，
        并发度受 `scope.max_host_concurrency` 约束。
        """
        if not self.targets_spec:
            self._reachable = {}
            return {}
        workers = max(1, min(len(self.targets_spec), int(self.config.scope.max_host_concurrency or 8)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(tcp_probe, target.host, target.port, self.connect_timeout): target.authority
                for target in self.targets_spec
            }
            results: dict[str, bool] = {}
            for future, authority in futures.items():
                try:
                    results[authority] = bool(future.result())
                except Exception:  # pragma: no cover - 探测异常按不可达处理
                    results[authority] = False
        self._reachable = results
        return self._reachable

    def _annotated_targets(self) -> list[RangeTarget]:
        out: list[RangeTarget] = []
        for target in self.targets_spec:
            reachable = self._reachable.get(target.authority)
            bits = [target.notes] if target.notes else []
            if not target.port:
                bits.append("未声明端口，无法做 TCP 探测")
            elif reachable:
                bits.append("TCP 可连通")
            else:
                bits.append("TCP 不可达")
            if not is_target_in_scope(self.config.scope, target.authority):
                bits.append("⚠ 不在授权网段内，护栏会拒绝实际发包")
            out.append(
                RangeTarget(
                    host=target.host,
                    port=target.port,
                    service=target.service,
                    url=target.url,
                    notes="；".join(bits),
                )
            )
        return out

    # ------------------------------------------------------------ 生命周期
    def status(self) -> dict:
        if not self.targets_spec:
            return self.status_dict(False, "yaml 未声明 targets 列表", [])
        reachable = self._probe()
        hits = [authority for authority, ok in reachable.items() if ok]
        misses = [authority for authority, ok in reachable.items() if not ok]
        detail = f"TCP 可达 {len(hits)}/{len(reachable)} 个目标"
        if hits:
            detail += f"：{', '.join(hits)}"
        if misses:
            detail += f"；不可达：{', '.join(misses)}（确认靶机已开机、防火墙放行、网段可达）"
        return self.status_dict(bool(hits), detail, self.targets_spec)

    def prepare(self) -> RangeInfo:
        """逐目标 TCP 连通性检查，返回目标与 ground truth（**不抛异常**）。"""
        notes: list[str] = []
        if not self.targets_spec:
            notes.append("yaml 未声明 targets 列表，无法探测任何主机")
        else:
            reachable = self._probe()
            hits = [authority for authority, ok in reachable.items() if ok]
            misses = [authority for authority, ok in reachable.items() if not ok]
            notes.append(f"TCP 连通性检查（超时 {self.connect_timeout:.0f}s）：可达 {len(hits)}/{len(reachable)}")
            if hits:
                notes.append("可达: " + ", ".join(hits))
            if misses:
                notes.append("不可达: " + ", ".join(misses))
            out_of_scope = [
                target.authority
                for target in self.targets_spec
                if not is_target_in_scope(self.config.scope, target.authority)
            ]
            if out_of_scope:
                notes.append("不在授权网段（护栏会拒绝发包）: " + ", ".join(out_of_scope))
        if self.notes:
            notes.append(self.notes)
        info = RangeInfo(
            name=self.name,
            kind=self.kind,
            targets=self._annotated_targets(),
            ground_truth=self.ground_truth(),
            flag_patterns=self.flag_patterns(),
            notes="；".join(n for n in notes if n),
            source=self.source,
        )
        self._info = info
        self._set_detail(info.notes)
        return info

    def info(self) -> RangeInfo:
        return self._info or self._info_snapshot()

    def primary_target(self) -> str:
        for target in self.targets_spec:
            if self._reachable.get(target.authority):
                return target.url or target.authority
        if self.targets_spec:
            first = self.targets_spec[0]
            return first.url or first.authority
        return ""


__all__ = ["LocalRangeAdapter"]
