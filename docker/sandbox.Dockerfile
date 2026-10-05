# Sandbox image for AI-generated research code. Built once, locally, at no cost.
#   docker build -f docker/sandbox.Dockerfile -t oracle-sandbox:1 .
# Runtime isolation (no network, read-only root, dropped capabilities, resource limits) is
# applied by oracle.research_agent.sandbox when containers are started, not here.
FROM python:3.13-slim
RUN pip install --no-cache-dir "numpy==2.4.6" && useradd --create-home --uid 10001 sandbox
USER sandbox
WORKDIR /home/sandbox
