# Madeleine Portfolio Tracker — Streamlit UI 容器镜像
#
# 构建 (默认含 UI 依赖):
#   docker build -t madeleine-tracker .
# 附带可选增强 (IBKR / 长桥):
#   docker build --build-arg EXTRAS=ui,ibkr,longport -t madeleine-tracker .
#
# 运行:
#   docker run -p 8501:8501 -v tracker-data:/data madeleine-tracker
# 数据 (portfolio.json / 行情缓存 / K线 HTML) 全部落 /data 卷, 升级镜像不丢。
# 首次使用生成配置模板:
#   docker run --rm --entrypoint tracker -v tracker-data:/data madeleine-tracker init

FROM python:3.12-slim

# 可选依赖组合 (pyproject.toml extras; 默认仅 UI, 镜像最小)
ARG EXTRAS=ui

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 数据目录: 挂卷持久化 (持仓/自选/设置/行情缓存/K线 HTML)
    TRACKER_HOME=/data \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PYTHONUNBUFFERED=1

# 非 root 运行
RUN useradd -r -m -u 10001 appuser \
    && mkdir -p /data /home/appuser/.streamlit \
    && touch /home/appuser/.streamlit/credentials.toml \
    && chown -R appuser:appuser /data /home/appuser

WORKDIR /data

COPY . /src
RUN pip install --no-cache-dir "/src[${EXTRAS}]" \
    && rm -rf /src

USER appuser

EXPOSE 8501

# tracker-ui = 转交 streamlit run (包内 app.py); 参数透传给 streamlit
ENTRYPOINT ["tracker-ui"]
CMD ["--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=5).status==200 else 1)"
