#!/usr/bin/env python3
"""Claude Usage Monitor (GTK4) — prototype.

A small libadwaita widget that displays Claude Code usage metrics fetched from
the Anthropic OAuth usage API (see api.py). Network polling runs on a worker
thread; UI updates are marshalled back onto the GTK main loop with GLib.idle_add.

Target: Wayland. Always-on-top is intentionally not implemented (see project memo).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import api  # noqa: E402


def _load_ui_string() -> str:
    """Return the compiled widget UI XML.

    Works both as a zipapp (UI bundled alongside this module, read via the
    import loader) and in the dev tree (compiled to build/widget.ui).
    """
    name = "widget.ui"
    loader = globals().get("__loader__")
    if loader is not None and hasattr(loader, "get_data"):
        try:
            return loader.get_data(
                os.path.join(os.path.dirname(__file__), name)
            ).decode("utf-8")
        except (OSError, AttributeError, ValueError):
            pass  # not bundled here (e.g. dev run) — fall back to disk
    here = Path(__file__).resolve().parent
    for candidate in (here / name, here / "build" / name):
        if candidate.exists():
            return candidate.read_text()
    raise SystemExit(f"{name} not found — run 'make' to compile the Blueprint UI.")


_UI_STRING = _load_ui_string()

# Polling cadence (settings-configurable later). A 429 backoff is honored up to
# POLL_MAX_BACKOFF_S; a plain error retries sooner.
POLL_INTERVAL_S = 60
POLL_ERROR_S = 30
POLL_MAX_BACKOFF_S = 900


def _format_reset(resets_at: datetime | None) -> str:
    """Human-friendly 'resets in ...' string from a future UTC datetime."""
    if resets_at is None:
        return ""
    delta = resets_at - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "resetting…"
    hours, rem = divmod(secs, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, h = divmod(hours, 24)
        return f"resets in {days}d {h}h"
    if hours >= 1:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


class UsageRow(Gtk.Box):
    """A single labelled progress bar: name + percentage, with a reset subtitle."""

    def __init__(self, name: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.name = name

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name_label = Gtk.Label(label=name, xalign=0.0, hexpand=True)
        name_label.add_css_class("heading")
        self._pct_label = Gtk.Label(label="--", xalign=1.0)
        self._pct_label.add_css_class("numeric")
        self._pct_label.add_css_class("dim-label")
        header.append(name_label)
        header.append(self._pct_label)

        self._bar = Gtk.ProgressBar()
        self._bar.set_fraction(0.0)

        self._reset_label = Gtk.Label(label="", xalign=0.0)
        self._reset_label.set_wrap(True)
        self._reset_label.add_css_class("dim-label")
        self._reset_label.add_css_class("caption")

        self.append(header)
        self.append(self._bar)
        self.append(self._reset_label)

    def _set_state_class(self, pct: float) -> None:
        for cls in ("error", "warning"):
            self._bar.remove_css_class(cls)
        if pct >= 90:
            self._bar.add_css_class("error")
        elif pct >= 70:
            self._bar.add_css_class("warning")

    def update(self, metric: api.Metric) -> None:
        # Only called for available metrics; ones that do not apply to the
        # account are not listed by the API client at all.
        self._bar.set_visible(True)
        pct = max(0.0, min(100.0, metric.utilization))
        self._bar.set_fraction(pct / 100.0)
        self._pct_label.set_label(f"{pct:.0f}%")
        self._reset_label.set_label(_format_reset(metric.resets_at))
        self._set_state_class(pct)

    def update_extra(self, extra: api.ExtraUsage, prepaid: api.PrepaidBalance | None) -> None:
        """Render paid extra-usage: a bar+amount when capped, amount only when not.

        The prepaid credit balance is appended when the account has one, so it is
        visible whether work can still continue or only what has been spent.
        """
        balance = f" · {prepaid.text} available" if prepaid is not None else ""
        pct = extra.utilization
        if pct is not None:
            # Capped: percentage bar, with "$spent / $limit" as the subtitle.
            self._bar.set_visible(True)
            pct = max(0.0, min(100.0, pct))
            self._bar.set_fraction(pct / 100.0)
            self._pct_label.set_label(f"{pct:.0f}%")
            self._reset_label.set_label(f"{extra.spent_text} / {extra.limit_text}{balance}")
            self._set_state_class(pct)
        else:
            # Uncapped pay-as-you-go: no bar (a bar would imply a limit).
            self._bar.set_visible(False)
            self._pct_label.set_label("")
            self._reset_label.set_label(f"{extra.spent_text} spent{balance}")


@Gtk.Template(string=_UI_STRING)
class CumWidget(Adw.ApplicationWindow):
    __gtype_name__ = "CumWidget"

    window_title = Gtk.Template.Child()
    rows_box = Gtk.Template.Child()
    updated_label = Gtk.Template.Child()
    refresh_button = Gtk.Template.Child()
    error_banner = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Rows are created from the response, keyed by the API field name, so a
        # quota type Anthropic adds or scopes to a single model shows up without
        # a code change.
        self._rows: dict[str, UsageRow] = {}

        # Extra (paid pay-as-you-go) usage row: shown only when enabled.
        self._extra_row = UsageRow("Extra Usage")
        self._extra_row.set_visible(False)
        self.rows_box.append(self._extra_row)

        self._fetching = False
        self._timeout_id: int | None = None
        self.refresh_button.connect("clicked", lambda _btn: self.refresh())

        self.refresh()
        # Safety net: _apply_result re-arms this after every completed fetch.
        self._schedule_next(POLL_INTERVAL_S)

    def _schedule_next(self, delay: int) -> None:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
        self._timeout_id = GLib.timeout_add_seconds(max(1, delay), self._on_tick)

    def _on_tick(self) -> bool:
        self._timeout_id = None
        self.refresh()
        return GLib.SOURCE_REMOVE  # re-armed from _apply_result

    def refresh(self) -> None:
        """Kick off a usage fetch on a worker thread (no-op if one is in flight)."""
        if self._fetching:
            return
        self._fetching = True
        self.refresh_button.set_sensitive(False)
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self) -> None:
        # fetch_usage() is designed never to raise, but guard anyway: an
        # unexpected exception here must not leave _fetching stuck True (which
        # would freeze polling permanently), so always deliver a result.
        try:
            result = api.fetch_usage()
        except Exception as exc:  # noqa: BLE001 - last-resort safety net
            result = api.UsageResult(metrics=[], error=f"Unexpected error: {exc}")
        GLib.idle_add(self._apply_result, result)

    def _next_delay(self, result: api.UsageResult) -> int:
        """Seconds until the next poll, honoring a 429 Retry-After."""
        if not result.ok:
            if result.rate_limited and result.retry_after:
                return min(result.retry_after, POLL_MAX_BACKOFF_S)
            return POLL_ERROR_S
        return POLL_INTERVAL_S

    def _apply_result(self, result: api.UsageResult) -> bool:
        self._fetching = False
        self.refresh_button.set_sensitive(True)
        self._schedule_next(self._next_delay(result))

        if not result.ok:
            self.error_banner.set_title(result.error or "Unknown error")
            self.error_banner.set_revealed(True)
            return GLib.SOURCE_REMOVE  # idle_add: run once

        self.error_banner.set_revealed(False)

        rows: list[Gtk.Widget] = []
        for metric in result.metrics:
            row = self._rows.get(metric.field)
            if row is None:
                row = UsageRow(metric.name)
                self._rows[metric.field] = row
            row.update(metric)
            rows.append(row)

        extra = result.extra
        self._extra_row.set_visible(extra is not None)
        if extra is not None:
            self._extra_row.update_extra(extra, result.prepaid)
        rows.append(self._extra_row)

        self._sync_order(rows)
        if result.plan:
            self.window_title.set_subtitle(result.plan)
        self.updated_label.set_label("updated: " + time.strftime("%H:%M:%S"))
        return GLib.SOURCE_REMOVE  # idle_add: run once

    def _sync_order(self, wanted: list[Gtk.Widget]) -> None:
        """Put the rows in the API-derived order without rebuilding them every poll."""
        current: list[Gtk.Widget] = []
        child = self.rows_box.get_first_child()
        while child is not None:
            current.append(child)
            child = child.get_next_sibling()
        if current == wanted:
            return
        for widget in current:
            self.rows_box.remove(widget)
        for widget in wanted:
            self.rows_box.append(widget)


class CumApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.walbrix.ClaudeUsageMonitor")

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = CumWidget(application=self)
        win.present()


def main() -> int:
    return CumApp().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
