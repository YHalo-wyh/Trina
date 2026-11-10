# 托管评测使用专用 Python 镜像；依赖仅由 pyproject.toml 定义。
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /opt/trina
COPY pyproject.toml README.md ./
COPY trina ./trina
COPY configs/trina.yaml ./configs/trina.yaml
RUN python -m pip install --no-cache-dir .
ENV TRINA_LLM_BACKEND=openai \
    TRINA_SCOPE_EXPLOIT_LEVEL=exploit \
    TRINA_BENCH_HOSTED=1 \
    TRINA_RUN_WORKSPACE=/tmp/trina/runs \
    TRINA_AUDIT_DIR=/tmp/trina/logs \
    TRINA_REPORT_OUTPUT_DIR=/tmp/trina/reports
ENTRYPOINT ["python", "-m", "trina", "bench"]
