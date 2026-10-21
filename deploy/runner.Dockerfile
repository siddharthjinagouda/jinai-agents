# Image each agent run executes in (JINAI_RUNNER=docker).
#   docker build -t jinai-runner:latest -f deploy/runner.Dockerfile deploy/
FROM python:3.12-slim
RUN pip install --no-cache-dir httpx==0.28.1 beautifulsoup4==4.13.4 \
 && useradd --create-home --uid 1000 agent
# Optional JavaScript rendering for fetch(url, browser=True); adds ~400 MB:
# RUN pip install --no-cache-dir playwright==1.56.0 && playwright install --with-deps chromium
USER agent
