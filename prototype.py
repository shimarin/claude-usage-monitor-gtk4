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

# Polling interval in seconds (settings-configurable later).
POLL_INTERVAL_S = 60


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
        self._reset_label.add_css_class("dim-label")
        self._reset_label.add_css_class("caption")

        self.append(header)
        self.append(self._bar)
        self.append(self._reset_label)

    def update(self, metric: api.Metric) -> None:
        # Only called for available metrics; null ones are hidden by the caller.
        for cls in ("error", "warning"):
            self._bar.remove_css_class(cls)
        pct = max(0.0, min(100.0, metric.utilization))
        self._bar.set_fraction(pct / 100.0)
        self._pct_label.set_label(f"{pct:.0f}%")
        self._reset_label.set_label(_format_reset(metric.resets_at))
        if pct >= 90:
            self._bar.add_css_class("error")
        elif pct >= 70:
            self._bar.add_css_class("warning")


@Gtk.Template(string=_UI_STRING)
class CumWidget(Adw.ApplicationWindow):
    __gtype_name__ = "CumWidget"

    rows_box = Gtk.Template.Child()
    updated_label = Gtk.Template.Child()
    refresh_button = Gtk.Template.Child()
    error_banner = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._rows: dict[str, UsageRow] = {}
        for name in api.METRIC_SOURCES:
            row = UsageRow(name)
            self._rows[name] = row
            self.rows_box.append(row)

        self._fetching = False
        self.refresh_button.connect("clicked", lambda _btn: self.refresh())

        self.refresh()
        self._timeout_id = GLib.timeout_add_seconds(POLL_INTERVAL_S, self._on_tick)

    def _on_tick(self) -> bool:
        self.refresh()
        return GLib.SOURCE_CONTINUE

    def refresh(self) -> None:
        """Kick off a usage fetch on a worker thread (no-op if one is in flight)."""
        if self._fetching:
            return
        self._fetching = True
        self.refresh_button.set_sensitive(False)
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self) -> None:
        result = api.fetch_usage()
        GLib.idle_add(self._apply_result, result)

    def _apply_result(self, result: api.UsageResult) -> bool:
        self._fetching = False
        self.refresh_button.set_sensitive(True)

        if not result.ok:
            self.error_banner.set_title(result.error or "Unknown error")
            self.error_banner.set_revealed(True)
        else:
            self.error_banner.set_revealed(False)
            for metric in result.metrics:
                row = self._rows.get(metric.name)
                if row is None:
                    continue
                # Metrics the API reports as null (e.g. per-model weekly caps
                # that don't apply to the current plan) are hidden entirely.
                row.set_visible(metric.available)
                if metric.available:
                    row.update(metric)
            self.updated_label.set_label("updated: " + time.strftime("%H:%M:%S"))
        return GLib.SOURCE_REMOVE  # idle_add: run once


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
