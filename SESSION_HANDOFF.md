# Session handoff — 2026-04-26

Snapshot of where things stand at the end of a long working session on a
borrowed laptop. Use this to pick up on another machine without losing
context. Once the work this references is closed out, this file can be
deleted (or kept as a postmortem).

## TL;DR

- **Major discovery mid-session:** Anthropic ships **Claude Code on the
  Web** at `claude.ai/code` — cloud-hosted Claude Code sessions with
  GitHub integration, mobile app support, environment setup scripts,
  and bootstrap via repo `.claude/` config. It does almost everything
  rc-launcher was being built to do, with auth that actually works.
- **For Gunther's profile** (Pro/Max sub, mobile-first, GitHub repos,
  no ZDR requirement), **rc-launcher is mostly redundant** with CCotW.
  Recommendation: sunset rc-launcher (or freeze as the niche
  self-hosted alternative for ZDR / data-sovereignty cases) and adopt
  CCotW for the actual workflow.
- **Dotfiles work landed** (github.com/Gunther-Schulz/dotfiles): a
  consolidated `claude/CLAUDE.md` with workflow rules — most
  importantly the **hard "stop after 2 failed attempts" hack-iteration
  rule** that emerged from this session's pain. These rules
  auto-travel to any new machine via the bootstrap symlink.

## Status by thread

### rc-launcher (Phase 5 Slice 2 — claude RC spawn)

**Where we are:** spawn pipeline itself works (clone, worktree, tmux,
pipe-pane, send-keys all green). Blocked on **claude RC requires
subscription OAuth that has no headless flow**.

**Root cause:** `claude auth login` (the documented current command)
uses a callback-based OAuth flow that needs a local HTTP listener
reachable from the user's browser — fundamentally incompatible with
our headless container. The legacy `claude login` form (paste-the-code
flow) still completes OAuth but produces tokens that the current
claude considers `loggedIn: false`. `claude setup-token` is
explicitly inference-only per the docs ("cannot establish Remote
Control sessions").

**Available paths if we continue:**
1. User runs `claude auth login` once interactively via Coolify's web
   terminal. Tokens land in `~/.claude.json` with org info.
   rc-launcher's `/claude` UI becomes a status check, not a login
   wrapper.
2. Build a custom OAuth callback handler inside rc-launcher (forward
   the callback URL through the rc-launcher external URL). Significant
   work for a one-time-per-deploy action.

**Recent commits with hacks that should be reverted if we sunset:**
- `8582e54` — claude_login.py reverted to `claude login` + entrypoint
  `claude auth status` probe
- `b330f6c` — full path to claude binary in entrypoint
- `4f53862` — entrypoint restores `~/.claude.json` from backup
- `1938de2` — `/api/diag/session/{sid}` endpoint (keep — operational
  health check per the diag-policy rule)

If we **continue** rc-launcher: revert the hacks, document the
"interactively log in once via Coolify web terminal" canonical path
in the `/claude` UI, finish Phase 5 polish (Stop/Restart, per-repo
lock), then audit pass (CI + tests + diag-endpoint review) before
the terminal feature.

If we **sunset** rc-launcher: README explanation of why (link to
Claude Code on the Web), freeze the repo. Maybe extract the
Coolify+FastAPI patterns into a separate template repo for unrelated
self-hosted projects.

### dotfiles (github.com/Gunther-Schulz/dotfiles)

**Pushed and current** (verified `0 ahead, 0 behind`). Final shape:

```
claude/
├── CLAUDE.md   (operational, auto-loaded everywhere via bootstrap.sh symlink)
├── JOURNAL.md  (improvement log, never loaded into sessions)
└── README.md   (human intro, never loaded into sessions)
```

`bootstrap.sh` adds the single symlink `~/.dotfiles/claude/CLAUDE.md
→ ~/.claude/CLAUDE.md`. On a new machine, `./bootstrap.sh` gets you
the full ruleset.

**Key rule that emerged this session:** the lead "Workflow rhythm"
bullet now mandates a **hard stop after 2 failed attempts** on the
same problem, with required research → surface → confirm sequence
before any third change. Hacks must be labeled and explicitly
confirmed. This is the rule the user wants followed most strictly,
because hack-iteration was the biggest behavioral failure this
session.

JOURNAL has 4 entries documenting what was added, why, and the
real incidents that motivated each.

**Caveat:** committer identity on this laptop's git is
`Gunther@MACs-MacBook-Pro.local` (no `git config user.email` set).
Easy to amend on the main machine if it bothers you.

### Claude Code on the Web (the new path)

**Confirmed working:** the user got into `claude.ai/code`, has access
(research preview is live for them), saw the cloud environment
creation form. Did not yet finish the test.

**Open test plan:**
1. Create a cloud environment with a setup script that bootstraps
   `~/.claude/CLAUDE.md` from the dotfiles repo (Approach A — write
   into VM home dir):
   ```bash
   #!/bin/bash
   set -e
   git clone --depth=1 https://github.com/Gunther-Schulz/dotfiles.git /tmp/dotfiles
   mkdir -p ~/.claude
   cp /tmp/dotfiles/claude/CLAUDE.md ~/.claude/CLAUDE.md
   echo "Bootstrap done at $(date)" > /tmp/bootstrap-marker.txt
   ```
2. Start a session in any small repo, ask: *"Read
   `~/.claude/CLAUDE.md` and tell me the first heading. Confirm
   `/tmp/bootstrap-marker.txt` exists."*
3. **If claude reads the file content out:** Approach A works. The
   bootstrap story is: setup script writes to `~/.claude/`, repo
   stays clean (no git pollution).
4. **If claude does NOT read it** (CCotW only loads repo-level
   CLAUDE.md per docs): fall back to **Approach C** — setup script
   writes a global gitignore (`~/.config/git/ignore` containing
   `.claude/`, `.mcp.json`, `CLAUDE.md`) and then injects into the
   repo working dir. Files exist on disk for claude to read but never
   appear in `git status`.

This open test is the highest-priority next thing if continuing on a
new machine. It validates the entire "bootstrap any GitHub repo with
my standard config" workflow.

## What to do on the new machine

1. **Bootstrap dotfiles first:** `git clone Gunther-Schulz/dotfiles
   ~/.dotfiles && cd ~/.dotfiles && ./bootstrap.sh`. The CLAUDE.md
   symlink lands; new claude sessions inherit the workflow rules
   automatically.
2. **For the Claude Code on the Web test:** open `claude.ai/code` in a
   browser, configure the cloud environment with the setup script
   above, and run the test prompt. This is the one outstanding
   experimental answer.
3. **For rc-launcher:** if the answer to "should we continue?" is
   yes, revert the hacks listed above and proceed to Phase 5 polish.
   If no, freeze the repo with a clear README explaining why
   (point to Claude Code on the Web).
4. **For paste-bootstrap of this conversation context:** start a new
   claude session and paste the contents of THIS file as the first
   message, plus any follow-up direction. That gives the new session
   enough to pick up.

## Sunset checklist (if rc-launcher is freezing)

- [ ] Update README with explanation: "Anthropic ships Claude Code on
      the Web at claude.ai/code which covers this use case. This repo
      remains as a self-hosted alternative for ZDR / data-sovereignty
      / persistent-dev-env cases."
- [ ] Revert the auth-related hacks (commits listed above)
- [ ] Archive on GitHub or just stop deploying
- [ ] Coolify: stop the rcl service, optionally delete

## Continue checklist (if rc-launcher is continuing)

- [ ] User does interactive `claude auth login` via Coolify web
      terminal, verify org info populated, RC works end-to-end
- [ ] Revert claude_login.py + entrypoint hacks
- [ ] Update `/claude` UI to document the canonical setup
- [ ] Phase 5 polish: Stop/Restart per-session, per-`(owner, repo)`
      asyncio.Lock for prep races
- [ ] Audit pass: CI (ruff + pytest) + small test suite + diag
      endpoint review (which to keep, which to remove)
- [ ] Then terminal feature (xterm.js + WS + tmux attach)
