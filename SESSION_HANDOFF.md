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

**Recommended bootstrap pattern: write user-level config into the
VM's `~/.claude/`.**

Key insight: the cloud VM is just an Ubuntu machine running the same
`claude` binary as your laptop. Whatever claude reads from `~/.claude/`
locally, it should read from `~/.claude/` in the cloud VM if the
setup script puts the file there. The CCotW docs that say "your
`~/.claude/CLAUDE.md` doesn't transfer" are about what comes FROM
your local machine — they don't say claude refuses to read VM-local
user-level config.

This means the setup script can write **any** user-level config the
local claude reads:

| Path | What it provides |
|---|---|
| `~/.claude/CLAUDE.md` | Global preferences |
| `~/.claude/skills/` | Global skills |
| `~/.claude/agents/` | Global agents |
| `~/.claude/commands/` | Global slash commands |
| `~/.claude/settings.json` | Hooks block + other settings |

All without touching the repo working dir → zero git pollution, no
`.gitignore` needed.

**Suggested setup script** (extend as `claude/` in dotfiles grows):
```bash
#!/bin/bash
set -e
git clone --depth=1 https://github.com/Gunther-Schulz/dotfiles.git /tmp/dotfiles
mkdir -p ~/.claude
# rsync everything from dotfiles claude/ into ~/.claude/ — future-proof
# as you add skills, agents, MCP, hooks to the dotfiles claude/ dir.
rsync -a /tmp/dotfiles/claude/ ~/.claude/
echo "Bootstrap done at $(date)" > /tmp/bootstrap-marker.txt
```

**Test the pattern works in CCotW:**
1. Add the script above as the setup script for a cloud environment.
2. Start a session in any small repo, ask:
   - *"Read `~/.claude/CLAUDE.md` and tell me the first heading."*
   - *"List all skills you have available"* (once you add skills to
     `dotfiles/claude/skills/`).
   - *"Confirm `/tmp/bootstrap-marker.txt` exists."*
3. **If claude shows the CLAUDE.md content + lists your skills:** the
   pattern works as expected, no fallback needed.
4. **If claude doesn't see the user-level config in the VM** (per the
   docs being silent on this, possible but not the most likely
   outcome): fall back to writing the same files into the *repo*'s
   `.claude/` and `CLAUDE.md` from the setup script, plus a global
   gitignore via the setup script:
   ```bash
   mkdir -p ~/.config/git
   cat > ~/.config/git/ignore <<'EOF'
   .claude/
   .mcp.json
   CLAUDE.md
   EOF
   ```
   This keeps `git status` clean even though the files live in the
   working dir.

**Honest caveat:** I haven't *tested* that claude in the CCotW VM
reads user-level config the setup script created. Plausible from how
claude works generally, but worth verifying with the test above
before committing.

### What CCotW auto-loads from the cloned repo (independent of the bootstrap)

Setup script aside, the repo itself can carry config that CCotW
loads automatically. Per the docs:

| Repo file / dir | Loaded? |
|---|---|
| `CLAUDE.md` | Yes |
| `.claude/settings.json` hooks | Yes |
| `.mcp.json` MCP servers | Yes |
| `.claude/skills/` | Yes |
| `.claude/agents/` | Yes |
| `.claude/commands/` | Yes |
| `.claude/rules/` | Yes |
| Plugins declared in `.claude/settings.json` | Yes |

So a project repo can have its OWN `.claude/` for project-specific
config, alongside whatever the global bootstrap brings. They compose.

**Future direction:** grow the dotfiles `claude/` dir to include:
- `claude/skills/` — global skills (the kind `update-config`,
  `simplify`, `fewer-permission-prompts` provide; you can write your
  own for recurring tasks)
- `claude/.mcp.json` — MCP servers you always want available
- `claude/settings.json` — hooks you always want active

The setup script using `rsync -a` already handles new files added to
the dotfiles `claude/` dir without modification.

### Skill-craft as a reference for skill design

`Gunther-Schulz/skill-craft` is a skill plugin that codifies how to
design effective Claude Code skills (file categories, frontmatter
discipline, "every sentence must change behavior", reflexivity, file
boundary rules). My dotfiles `CLAUDE.md` was vetted against
skill-craft's principles in this session — alignment is documented in
JOURNAL entries (especially the consolidation commit).

If you ever build skills for the dotfiles `claude/skills/` dir, read
skill-craft's PROCEDURE.md first. Key principles: operational vs
maintenance file boundary, imperative voice, no provenance/fluff,
each rule grounded in a real incident (Path 1).

Install with: `claude plugin marketplace add Gunther-Schulz/skill-craft`
then `claude plugin install skill-craft@skill-craft-marketplace`.

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

## Considered and rejected: rc-launcher as a unified UI over CCotW

Briefly considered extending rc-launcher to spawn either local
self-hosted envs OR drive Claude Code on the Web via API (one UI
covers both). Rejected because: CCotW's web UI is already polished
and continuously updated by Anthropic; wrapping it means perpetually
trailing on features and maintaining a thin layer over an evolving
product. Better to keep the two as focused tools — CCotW for the
primary workflow, rc-launcher frozen as the niche self-hosted
fallback. Open question if anyone revisits: does CCotW expose a
public REST API for programmatic session creation, or is `claude
--remote` the only entry point? (Not researched.)

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
