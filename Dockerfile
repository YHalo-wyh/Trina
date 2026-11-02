# Trina · TSecBench 托管模式镜像
#
# 平台会注入 BENCHMARK_BASE_URL / BENCHMARK_TOKEN，并限制外网；因此入口直接进入
# `trina bench`，由它轮询答题 API、启动靶场容器、逐题解题并提交 flag。
#
# 基础镜像使用本机已有的 Python 3.14 环境（weblate/weblate），避免在国内网络下拉取
# python:3.12-slim 失败；Trina 只依赖标准库 + 六个纯 Python/带 wheel 的包。
FROM weblate/weblate:2026.6.1.0

USER root
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/trina

RUN /app/venv/bin/python3 -m ensurepip --upgrade \
    && /app/venv/bin/python3 -m pip install --no-cache-dir \
        --retries 10 --timeout 60 \
        --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
        "paramiko>=3.4" \
        "rich>=13.0" \
        "beautifulsoup4>=4.12" \
        "aiohttp>=3.9"

WORKDIR /opt/trina
COPY pyproject.toml README.md ./
COPY trina ./trina
COPY configs ./configs

# 容器内不保留运行产物；平台只读取 stdout 与答题 API 结果。
ENV TRINA_LLM_BACKEND=openai \
    TRINA_SCOPE_EXPLOIT_LEVEL=exploit \
    TRINA_BENCH_HOSTED=1 \
    TRINA_RUN_WORKSPACE=/tmp/trina/runs \
    TRINA_AUDIT_DIR=/tmp/trina/logs \
    TRINA_REPORT_OUTPUT_DIR=/tmp/trina/reports

ENTRYPOINT ["/app/venv/bin/python3", "-m", "trina", "bench"]
CMD []
