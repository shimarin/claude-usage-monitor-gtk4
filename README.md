# Claude Usage Monitor (GTK4)

A small GTK4 / libadwaita widget that shows your Claude Code rate-limit usage
at a glance on the Linux desktop. Usage data is read from the Anthropic OAuth
usage API using the OAuth token that Claude Code already stores locally — no
extra login required.

> Target environment is **Wayland**. Always-on-top is intentionally not
> implemented (Wayland gives clients no standard way to force keep-above).

## What it shows

- One bar per rate limit that applies to your account. The limits are read from
  the API response rather than hardcoded, so model-scoped weekly limits and
  quota types Anthropic adds later appear on their own (`Session (5hr)`,
  `Weekly (7 day)`, `Weekly (Sonnet)`, …), while limits your plan does not have
  and quota types the API lists before activating them stay hidden.
- The paid extra-usage section (spend vs. monthly cap), plus the prepaid
  usage-credit balance left to pay for it when the account has one.
- Your plan under the window title, using the name claude.ai shows rather than
  the internal plan identifier.

Polling runs once a minute and backs off to the `Retry-After` the API reports on
a 429; a refresh button fetches on demand.

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
make test      # run the API-response handling tests
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
follows the design of the projects above. The upstream `docs/api-reference.md`
is kept as the reference for the (undocumented) response shape, and its
improvements are ported across when they apply to this widget — so far:

- quota field names parsed from the response, including the code names used for
  quota types that are not activated yet (`is_active_quota()`)
- model-scoped limits surfaced from the `limits` array, marked
  `from_account_limits` so an inactive one is still shown at 0%
- the plan name from `organization.plan_display_name`, with the internal
  `organization_type` only as a fallback
- the prepaid usage-credit balance next to the extra-usage spend

## License

[MIT](LICENSE). The upstream copyright notices (Jens Duttke and omi-last-stand)
are retained alongside this project's own.
