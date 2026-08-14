# Optional container build. NOTE: the whole point of this server is to run on
# your own machine with your own IP/fingerprint. A datacenter IP re-introduces
# the logout risk. If you containerize, run it on your own host and consider a
# residential/home exit node.

FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml README.md ./
COPY sales_nav_mcp ./sales_nav_mcp

RUN uv venv && uv pip install --no-cache . \
    && uv run patchright install-deps chromium \
    && uv run patchright install chromium

ENV PATH="/app/.venv/bin:$PATH"
# Headed capture needs a display; a virtual one (Xvfb) is required for a
# non-headless run in a container. Set HEADLESS=true to skip that, at the cost
# of higher detectability.
ENV HEADLESS=true

ENTRYPOINT ["python", "-m", "sales_nav_mcp"]
