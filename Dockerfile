# syntax=docker/dockerfile:1.6
#
# rc-launcher — small Coolify app that spawns `claude --remote-control`
# sessions on a git repo/branch and surfaces the resulting Claude Remote
# Control URLs as tap-able links. Intended for mobile-first use with the
# native Claude app (no embedded web terminal).
#
# Image layers: devcontainer JS-Node-20 base + apt deps + Claude Code CLI
# + GitHub CLI (kept for shell-into convenience; not used in the app's
# running flow — we use the GitHub REST API via httpx) + Python venv with
# FastAPI/uvicorn/jinja2/httpx.

FROM mcr.microsoft.com/devcontainers/javascript-node:20

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    TERM=xterm-256color \
    HOME=/home/node \
    PYTHONUNBUFFERED=1 \
    PATH="/home/node/.npm-global/bin:/home/node/.local/bin:/usr/local/share/npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# tmux for session backing; python venv tooling; pipx for user-scoped Python
# tools; Claude Code CLI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux ripgrep fzf jq python3-venv pipx ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

# uv — fast Python package/tool installer. Single static binary, system-wide.
# UV_UNMANAGED_INSTALL: skip updater + PATH-edit, install binary into the
# given dir directly (we want plain /usr/local/bin, not the per-user default).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=/usr/local/bin sh

# GitHub CLI — installed for shell-into convenience (running `gh` from
# Coolify's web terminal, etc). Our application code uses the GitHub
# REST API via httpx with a stored PAT — gh is NOT in the running flow.
RUN /usr/bin/install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod 644 /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Passwordless sudo for node. Also override sudo's secure_path so that
# `claude`, `gh`, `uv`, and other tools (system-wide + user-scoped under
# ~/.npm-global, ~/.local) are on PATH for the node user — otherwise sudo's
# default secure_path strips them out even with `-E`.
RUN printf '%s\n' \
    'node ALL=(ALL) NOPASSWD: ALL' \
    'Defaults:node secure_path="/home/node/.npm-global/bin:/home/node/.local/bin:/usr/local/share/npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' \
    > /etc/sudoers.d/node \
    && chmod 0440 /etc/sudoers.d/node

# Profile snippet: prepend per-user bin dirs to PATH for interactive shells.
# /etc/profile reads everything in profile.d, so this fires for both
# `bash -l` (sudo -i, ssh, web terminal) and zsh's compat path.
RUN printf '%s\n' \
    '# rc-launcher: prepend per-user bin dirs (npm i -g X, pipx, uv tool, cargo)' \
    'export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"' \
    > /etc/profile.d/rcl-user-paths.sh \
    && chmod 0644 /etc/profile.d/rcl-user-paths.sh

# Python venv with everything the app needs.
RUN python3 -m venv /opt/rcl \
    && /opt/rcl/bin/pip install --no-cache-dir \
        fastapi 'uvicorn[standard]' python-multipart jinja2 httpx

# App code + seed files (CLAUDE.md template, etc) the entrypoint copies into
# the persistent home volume on first boot.
COPY app /opt/rcl/app
COPY seed /opt/rcl/seed

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
which bash python3 claude gh tmux git node npm uv pipx
claude --version 2>&1 || echo "claude: missing"
gh --version 2>&1 | head -1 || echo "gh: missing"
uv --version 2>&1 || echo "uv: missing"
pipx --version 2>&1 || echo "pipx: missing"
/opt/rcl/bin/python3 -c "import fastapi,uvicorn; print('fastapi', fastapi.__version__, 'uvicorn', uvicorn.__version__)"

# Optional extra apt packages — install on every boot since /var/lib/dpkg
# isn't on a persistent volume. Whitespace-separated list.
if [ -n "$APT_EXTRA_PACKAGES" ]; then
  echo "--- installing APT_EXTRA_PACKAGES: $APT_EXTRA_PACKAGES ---"
  sudo apt-get update \
    && sudo apt-get install -y --no-install-recommends $APT_EXTRA_PACKAGES \
    && sudo rm -rf /var/lib/apt/lists/* \
    || echo "APT_EXTRA_PACKAGES install failed (continuing)"
fi

# Repair volume ownership FIRST so subsequent `cp`/`mkdir` as node can write
# (volumes start root-owned on first mount).
sudo chown -R node:node /home/node /workspace /var/lib/rcl 2>/dev/null || true

# Seed /home/node from /etc/skel on first boot. The home volume starts empty
# on a fresh deploy; without skel the node user has no .bashrc/.profile.
# Detect "empty" via missing .bashrc; never overwrite an existing home.
if [ ! -f /home/node/.bashrc ]; then
  echo "--- seeding /home/node from /etc/skel (first boot) ---"
  cp -an /etc/skel/. /home/node/ 2>/dev/null || true
fi

# Redirect npm's global prefix into the home volume so `npm i -g X` at
# runtime persists across redeploys (system /usr/local prefix vanishes).
if [ ! -f /home/node/.npmrc ] || ! grep -q '^prefix=' /home/node/.npmrc 2>/dev/null; then
  echo "prefix=/home/node/.npm-global" >> /home/node/.npmrc
fi
mkdir -p /home/node/.npm-global

# Seed ~/.claude/CLAUDE.md from the bundled template on first boot. Never
# overwrite — the template header tells the user it's safe to edit and won't
# be touched on redeploy.
mkdir -p /home/node/.claude
if [ ! -f /home/node/.claude/CLAUDE.md ]; then
  echo "--- seeding ~/.claude/CLAUDE.md from template ---"
  cp /opt/rcl/seed/CLAUDE.md /home/node/.claude/CLAUDE.md
fi

# Restore ~/.claude.json from the most recent backup if it's missing.
# Background: an earlier rc-launcher revision purged this file on every
# boot. We removed the purge in Round 1, but on existing deployments the
# file was already gone — leaving claude unable to determine the user's
# organization (Remote Control needs that). claude itself keeps timestamped
# backups under ~/.claude/backups/, so a missing-but-backed-up state is
# losslessly recoverable. Idempotent: only restores when the file is gone.
if [ ! -f /home/node/.claude.json ]; then
  LATEST_BACKUP=$(ls -1t /home/node/.claude/backups/.claude.json.backup.* 2>/dev/null | head -1)
  if [ -n "$LATEST_BACKUP" ]; then
    echo "--- restoring ~/.claude.json from $LATEST_BACKUP ---"
    cp "$LATEST_BACKUP" /home/node/.claude.json
    chown node:node /home/node/.claude.json
  fi
fi

# Refresh org-info cache. claude RC needs oauthAccount.organizationUuid
# in ~/.claude.json. The OAuth tokens from the legacy `claude login` flow
# don't always populate this. `claude auth status` may fetch+cache it on
# first call when valid OAuth tokens exist; if so, this seeds the cache
# without requiring a full re-login. Output is logged for diagnosis.
if [ -f /home/node/.claude.json ]; then
  echo "--- claude auth status (refresh org-info cache) ---"
  # Full path to claude — sudoers secure_path applies to *node* invoking
  # sudo, not to root invoking sudo here, so PATH lookup of `claude` fails.
  sudo -u node -E HOME=/home/node /usr/local/share/npm-global/bin/claude auth status 2>&1 \
    || echo "(status check failed — non-fatal)"
fi
echo "=== end boot ==="

exec sudo -u node -E HOME=/home/node \
    /opt/rcl/bin/uvicorn --app-dir /opt/rcl app.main:app \
    --host 0.0.0.0 --port 8080
BASH

RUN chmod +x /entrypoint.sh

EXPOSE 8080
CMD ["/entrypoint.sh"]
