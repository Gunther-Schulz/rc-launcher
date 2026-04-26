<!-- managed by rc-launcher; safe to edit, will not be overwritten on redeploy -->

# rc-launcher Container

You are running inside an `rc-launcher` container on a Coolify-hosted
server, not on a developer's laptop. The end user is interacting with
you via the **Claude mobile app** (iOS/Android) over Remote Control —
they do NOT see the raw terminal output and they have a small screen.

## Behavior implications

- Be terse. Mobile users have limited screen real estate and slow scroll.
- Prefer summaries over verbose dumps. If you must show a large file or
  output, summarize first and offer to show the full thing on request.
- Avoid ASCII art / box drawings — they wrap badly on phones.
- Don't try to open browsers, GUIs, or anything visual — only the user's
  phone is "visible," and only via the Claude app's chat UI.

## Environment layout

- You run as the `node` user with passwordless `sudo`.
- `$HOME` = `/home/node`. Persisted across container restarts via volume.
- `$HOME/.claude/` is its own persistent volume (your conversation
  history, credentials, settings).
- `/workspace/` is a persistent volume holding cloned repos. When started
  via rc-launcher, your current working directory is a *git worktree*
  under `/workspace/<owner>/<repo>/_wt/<branch>/` (a "worktree" is a
  second working directory tied to the same git history — see `man
  git-worktree`).
- `/var/lib/rcl/` is the rc-launcher app's state directory (its DB, the
  GitHub PAT, etc.). You can read it but don't write to it.

## Installing tools

Anything you install into `$HOME` persists across container restarts:

- `pip install --user X`, `uv tool install X`, `uv add X` (in a project)
- `cargo install X`, `rustup` install scripts
- `npm i -g X` (we've redirected the global prefix into `~/.npm-global`)
- `curl | bash` install scripts that target `~/`

Pre-installed system-wide: `node`, `python3`, `uv`, `pipx`, `git`, `gh`
(CLI; not used by rc-launcher's app code, available for shell use),
`tmux`, `ripgrep`, `fzf`, `jq`, `claude`. Devcontainer base provides
`build-essential`.

`apt install` does NOT persist (system dirs aren't mounted) — files
vanish on redeploy. If a system package is needed often, the user can
either:
1. Add the package to the rc-launcher Dockerfile (via PR / git push), or
2. Set the `APT_EXTRA_PACKAGES` env var in Coolify (whitespace-separated
   list) — the entrypoint installs them on each boot.

## GitHub access

- `git clone`, `git push`, `git pull` over HTTPS use a stored Personal
  Access Token (scope: `repo`). You don't need to authenticate.
- The PAT is in `/var/lib/rcl/data/github-token`; the user manages it
  through the rc-launcher web UI.

## Customizing your Claude config

- Permissions, hooks, default model, env vars: all live in
  `~/.claude/settings.json`. You can edit it directly, or the user can
  ask you ("allow Bash(npm *) globally", "set default model to opus")
  and you can update the file with the Edit tool. Settings persist in
  the `claude` volume across redeploys.
- Plugins: install with `/plugin install <name>`. Plugin code lives in
  `~/.claude/plugins/`, which persists.

## Session lifecycle

- The user starts and stops sessions from the rc-launcher web UI, not
  from inside this terminal. If the user asks "kill the session," tell
  them to use the web UI's Kill button — your tmux session ending is
  what kills you.
- The session's Remote Control URL is generated automatically on spawn;
  the user opened it on their phone to reach you. Don't generate `/rc`
  URLs yourself.
- If global config has changed and you need to be restarted to pick it
  up, the user can hit "Restart" in the web UI — that respawns you in
  the same worktree, same conversation by default (or fresh if they
  toggle "start fresh conversation").

## When unsure

If the user asks about the deployment ("what version is this," "where
do files live," "how do I install X"), the answers are above. If
something isn't covered here, ask the user — they can update this file
freely.
