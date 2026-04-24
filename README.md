# rc-launcher

Mobile-first Coolify app to spawn [Claude Code](https://claude.com/claude-code)
sessions on your GitHub repos and surface the resulting Claude Remote
Control URLs as tap-able links. You tap an RC link on your phone →
Claude mobile app opens that session → native mobile UX.

No embedded web terminal. The only typing into a terminal (CLI OAuth for
`claude login` / `gh auth login`) is wrapped in plain HTML forms.

## Status

**Phase 1 (skeleton):** FastAPI + claude + gh preinstalled, hello page, health endpoint, Coolify-template shape.

Planned:
- **Phase 2:** `claude login` OAuth wrap (tap URL, paste code back, no terminal).
- **Phase 3:** `gh auth login` device-code wrap.
- **Phase 4:** repo list, start session (clones repo → `claude --remote-control` in tmux → extract RC URL).
- **Phase 5:** session list, kill, refresh, resume transcript.

## Deploying on Coolify

1. Coolify → **+ New Resource → Public Repository**.
2. Repo: `https://github.com/Gunther-Schulz/rc-launcher`.
3. Build pack: **Docker Compose**.
4. Deploy.
5. Once green, go to the app's **General** tab → **HTTP Basic Auth** → enable, set username + password. All HTTP traffic is now gated at the edge by Traefik.
6. Open the assigned FQDN. Browser prompts for basic auth; the skeleton page loads.

## Credits

- [Claude Code](https://claude.com/claude-code) — Anthropic
- Relies on Claude Code's [Remote Control](https://code.claude.com/docs/en/remote-control) mode for the mobile attach flow.
