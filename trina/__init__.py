"""Trina · 全自动渗透测试 Agent。

模块地图：
    trina.config    配置模型（同时是合规护栏的数据源）
    trina.scope     授权范围护栏：所有动作的唯一放行判据
    trina.audit     审计哈希链：全程留痕、可校验
    trina.llm       大模型后端：omp / openai 兼容 / 离线回放
    trina.tools     工具集：recon / web / vuln / exploit
    trina.brain     ReAct 决策大脑 + PTES 阶段机 + 事实记忆
    trina.adapters  靶场适配：local / docker-compose / pikachu / dvwa / vulhub / vulnstack / tsecbench
    trina.report    报告生成：Markdown + 自包含 HTML
    trina.eval      评测框架：场景库、指标、蓝队对抗
    trina.lab       故意脆弱的本地靶机（端到端验证用）
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
