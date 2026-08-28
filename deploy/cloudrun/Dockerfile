# The GUI and the MCP service share this image; the entrypoint decides.
#   GUI:  python -m pstb.gui --host 0.0.0.0 --share      (default CMD)
#   MCP:  python -m pstb.mcp_http
# Oracle uses the python-oracledb THIN driver: pure Python, no instant
# client, so the image stays slim and the VPC egress carries plain
# TCP/1521 to the database VM.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pstb ./pstb
COPY scripts/seed_sample_data.py ./scripts/seed_sample_data.py
COPY config.example.yaml ./

# gui is NOT transitive: fastapi lives in that extra, and the image died
# at boot without it (review blocker). The sqlite sample is gitignored,
# so it is seeded at build rather than copied.
RUN pip install --no-cache-dir ".[oracle,llm,gui]" \
 && python scripts/seed_sample_data.py

# State lives OUTSIDE the image. Mount a volume at /data and point
# PSTB_CONFIG at it; every relative path in config.yaml (logs/, the
# catalogs, source_knowledge/) then lands on the mount. Without the
# mount this container is stateless and every approval dies with it.
# PSTB_TRUSTED_PROXY is deliberately NOT baked in: a plain `docker run`
# for local testing must behave like a normal host. deploy.sh sets it on
# the service, where the balancer actually exists.
ENV PSTB_CONFIG=/data/config.yaml

# Cloud Run injects PORT; pstb.gui reads it as its default.
CMD ["python", "-m", "pstb.gui", "--host", "0.0.0.0", "--share"]
