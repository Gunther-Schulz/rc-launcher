# syntax=docker/dockerfile:1.6
#
# rc-launcher: small Coolify app that spawns `claude --remote-control`
# sessions on a git repo/branch and surfaces the resulting Claude Remote
# Control URLs as tap-able links. Intended for mobile-first use with the
# native Claude app (no embedded web terminal).
#
# Phase 1: skeleton — Python + claude + gh preinstalled, FastAPI hello page.
# Phase 2+ will add the claude/gh OAuth wrap flows and session management.

FROM mcr.microsoft.com/devcontainers/javascript-node:20

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    TERM=xterm-256color \
    HOME=/home/node \
    PYTHONUNBUFFERED=1

# tmux for session backing; socat as loopback proxy (rate-limit hygiene,
# same reason as aoe-coolify); python venv tooling; Claude Code CLI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux ripgrep fzf jq socat python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

# GitHub CLI from the official package (needed for device-code auth wrap
# and repo listing later — not used in Phase 1 but preinstalled to keep
# image layers stable across phases).
RUN /usr/bin/install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod 644 /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Passwordless sudo for node (idempotent).
RUN echo 'node ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/node \
    && chmod 0440 /etc/sudoers.d/node

# Python venv with everything the app needs.
RUN python3 -m venv /opt/rcl \
    && /opt/rcl/bin/pip install --no-cache-dir \
        fastapi 'uvicorn[standard]' python-multipart jinja2 httpx

# App code.
COPY app /opt/rcl/app

# Build-time sanity: app imports cleanly (catches missing deps / syntax
# errors at BUILD time instead of crash-loop at runtime).
RUN /opt/rcl/bin/python -c "import sys; sys.path.insert(0,'/opt/rcl'); from app.main import app; print('import OK')"

# State dir for sqlite etc.
RUN /usr/bin/install -o node -g node -m 0755 -d /var/lib/rcl

COPY <<'BASH' /entrypoint.sh
#!/bin/bash
echo "=== rc-launcher boot ==="
id
locale 2>/dev/null | head
echo "TERM=$TERM LANG=$LANG HOME=$HOME"
which bash python3 claude gh tmux git node npm
claude --version 2>&1 || echo "claude: missing"
gh --version 2>&1 | head -1 || echo "gh: missing"
/opt/rcl/bin/python3 -c "import fastapi,uvicorn; print('fastapi', fastapi.__version__, 'uvicorn', uvicorn.__version__)"
echo "=== end boot ==="

# Repair volume ownership.
sudo chown -R node:node /home/node /workspace /var/lib/rcl 2>/dev/null || true

# socat loopback, same pattern as aoe-coolify. Keeps traefik X-Forwarded-For
# trustable for any per-IP features we add (rate limiting, audit logs).
socat TCP-LISTEN:8080,fork,reuseaddr TCP:127.0.0.1:8081 &

exec sudo -u node -E HOME=/home/node \
    /opt/rcl/bin/uvicorn --app-dir /opt/rcl app.main:app \
    --host 127.0.0.1 --port 8081
BASH

RUN chmod +x /entrypoint.sh

EXPOSE 8080
CMD ["/entrypoint.sh"]
