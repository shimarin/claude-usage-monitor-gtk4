# Claude Usage Monitor (GTK4)

A small GTK4 / libadwaita widget that shows your Claude Code rate-limit usage
(Session / Weekly / Sonnet / Opus) at a glance on the Linux desktop. Usage data
is read from the Anthropic OAuth usage API using the OAuth token that Claude Code
already stores locally — no extra login required.

> Target environment is **Wayland**. Always-on-top is intentionally not
> implemented (Wayland gives clients no standard way to force keep-above).

## Requirements

System packages (installed via your distro; on Gentoo these come from Portage):

- Python 3.11+
- PyGObject (GTK 4.0 + libadwaita / Adw 1)
- `requests`
- `blueprint-compiler` (build-time only, to compile the UI)

The token is read from `~/.claude/.credentials.json` (or
`$CLAUDE_CONFIG_DIR/.credentials.json`).

## Build & install

```sh
make           # build a single self-contained executable (Python zipapp) in build/
make install   # install the executable, .desktop file and icon
```

Install location:

- As a regular user, `PREFIX` defaults to `~/.local` (no root needed).
- As root, `PREFIX` defaults to `/usr/local`.
- Override with `make install PREFIX=/usr`.
- `DESTDIR` is honored for staged installs (e.g. ebuild):
  `make install DESTDIR="${D}" PREFIX=/usr`. When `DESTDIR` is set, the
  desktop/icon cache updates are skipped (handled in `pkg_postinst`).

Uninstall with `make uninstall` (same `PREFIX`/`DESTDIR` rules).

## Acknowledgements

This is an independent **GTK4 reimplementation** in Python. It was written by
referencing two upstream projects, with sincere gratitude to their authors:

- [Usage Monitor for Claude](https://github.com/jens-duttke/usage-monitor-for-claude)
  by **Jens Duttke** — the original work.
- [Claude Usage Monitor](https://github.com/omi-last-stand/claude-usage-monitor)
  by **omi-last-stand** — a fork that this project's API client and the documented
  API response shape were modeled on.

This project shares no code copied verbatim, but its credentials/API handling
follows the design of the projects above.

## License

[MIT](LICENSE). The upstream copyright notices (Jens Duttke and omi-last-stand)
are retained alongside this project's own.
