"""Anthropic OAuth usage API client.

The only module that touches credentials. It reads Claude Code's OAuth access
token from the local credentials file and queries the (undocumented) Anthropic
OAuth usage endpoint. The token is refreshed by Claude Code itself, so we simply
re-read the file on every poll rather than refreshing it ourselves.

Network access is exclusively to api.anthropic.com; the token is used only in the
Authorization header.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API_URL_USAGE = "https://api.anthropic.com/api/oauth/usage"
API_URL_PROFILE = "https://api.anthropic.com/api/oauth/profile"

CLAUDE_CONFIG_DIR = (
    Path(os.environ["CLAUDE_CONFIG_DIR"])
    if os.environ.get("CLAUDE_CONFIG_DIR")
    else Path.home() / ".claude"
)
CLAUDE_CREDENTIALS = CLAUDE_CONFIG_DIR / ".credentials.json"

_OAUTH_BETA = "oauth-2025-04-20"
_FALLBACK_USER_AGENT = "claude-code/2.1.85"

# Maps a display name -> the API response key it is sourced from.
# Order here defines display order in the widget.
METRIC_SOURCES: dict[str, str] = {
    "Session": "five_hour",
    "Weekly": "seven_day",
    "Sonnet": "seven_day_sonnet",
    "Opus": "seven_day_opus",
}

# Known ISO 4217 -> display symbol for extra-usage amounts. Unknown codes fall
# back to the code itself (e.g. "AUD 1.23").
_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
}


def format_credits(
    minor_units: float,
    currency: str | None = None,
    decimal_places: int | None = None,
) -> str:
    """Format a minor-unit money amount (e.g. 658 -> "$6.58")."""
    places = decimal_places if isinstance(decimal_places, int) and decimal_places >= 0 else 2
    amount = minor_units / (10 ** places)
    text = f"{amount:,.{places}f}"
    if not currency:
        return text
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())
    # Single-char symbols prefix the amount ("$6.58"); ISO codes suffix it.
    return f"{symbol}{text}" if len(symbol) == 1 else f"{text} {symbol}"


@dataclass(frozen=True)
class Metric:
    """A single usage metric. ``utilization`` is a percentage 0..100."""

    name: str
    utilization: float | None
    resets_at: datetime | None

    @property
    def available(self) -> bool:
        return self.utilization is not None


@dataclass(frozen=True)
class ExtraUsage:
    """Paid extra-usage (pay-as-you-go) state, when enabled on the account.

    ``used_credits``/``monthly_limit`` are amounts in the currency's minor units
    (e.g. cents). ``monthly_limit`` is ``None`` for uncapped pay-as-you-go, where
    there is no percentage to show — only the amount spent.
    """

    used_credits: float
    monthly_limit: float | None
    currency: str | None
    decimal_places: int | None

    @property
    def has_limit(self) -> bool:
        return self.monthly_limit is not None and self.monthly_limit > 0

    @property
    def utilization(self) -> float | None:
        """Percentage 0..100, or ``None`` when uncapped (no limit to divide by)."""
        if not self.has_limit:
            return None
        return self.used_credits / self.monthly_limit * 100.0

    @property
    def spent_text(self) -> str:
        return format_credits(self.used_credits, self.currency, self.decimal_places)

    @property
    def limit_text(self) -> str | None:
        if not self.has_limit:
            return None
        return format_credits(self.monthly_limit, self.currency, self.decimal_places)


@dataclass(frozen=True)
class UsageResult:
    """Outcome of a usage poll: either ``metrics`` populated, or ``error`` set."""

    metrics: list[Metric]
    extra: ExtraUsage | None = None
    error: str | None = None
    auth_error: bool = False
    rate_limited: bool = False
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def read_access_token() -> str | None:
    """Read the current OAuth access token, or None if unavailable."""
    if not CLAUDE_CREDENTIALS.exists():
        return None
    try:
        creds = json.loads(CLAUDE_CREDENTIALS.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return creds.get("claudeAiOauth", {}).get("accessToken") or None


def _cli_version() -> str | None:
    """Best-effort lookup of the installed Claude Code version for the UA string."""
    try:
        out = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # e.g. "2.1.144 (Claude Code)"
    token = out.stdout.strip().split()
    return token[0] if token else None


def _user_agent() -> str:
    version = _cli_version()
    return f"claude-code/{version}" if version else _FALLBACK_USER_AGENT


def api_headers() -> dict[str, str] | None:
    token = read_access_token()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": _user_agent(),
        "anthropic-beta": _OAUTH_BETA,
    }


def _parse_resets_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_metrics(payload: dict[str, Any]) -> list[Metric]:
    metrics: list[Metric] = []
    for name, key in METRIC_SOURCES.items():
        block = payload.get(key)
        if isinstance(block, dict):
            util = block.get("utilization")
            metrics.append(
                Metric(
                    name=name,
                    utilization=float(util) if isinstance(util, (int, float)) else None,
                    resets_at=_parse_resets_at(block.get("resets_at")),
                )
            )
        else:
            # null / missing -> metric exists but has no data (e.g. no Opus usage)
            metrics.append(Metric(name=name, utilization=None, resets_at=None))
    return metrics


def _parse_extra_usage(payload: dict[str, Any]) -> ExtraUsage | None:
    extra = payload.get("extra_usage")
    if not isinstance(extra, dict) or not extra.get("is_enabled"):
        return None
    used = extra.get("used_credits")
    if not isinstance(used, (int, float)):
        return None
    limit = extra.get("monthly_limit")
    places = extra.get("decimal_places")
    currency = extra.get("currency")
    return ExtraUsage(
        used_credits=float(used),
        monthly_limit=float(limit) if isinstance(limit, (int, float)) else None,
        currency=currency if isinstance(currency, str) else None,
        decimal_places=places if isinstance(places, int) else None,
    )


def _model_slug(display_name: str) -> str:
    """Convert a model display name into a field-name suffix ("Opus" -> "opus")."""
    cleaned = "".join(c if c.isalnum() else " " for c in display_name.lower())
    return "_".join(cleaned.split())


def _merge_scoped_limits(payload: dict[str, Any]) -> dict[str, Any]:
    """Surface model-scoped weekly limits from the ``limits`` array as fields.

    Newer usage responses carry per-model limits only inside the ``limits``
    array (via ``scope.model``), no longer as top-level fields like
    ``seven_day_sonnet``. Each active scoped limit is mapped onto a synthetic
    quota field so the existing top-level field names keep working. The period
    prefix is derived from the response — the non-scoped limit of the same
    ``group`` shares its ``resets_at`` with an existing top-level quota field,
    whose name supplies the prefix. An existing top-level field is never
    overwritten (it carries higher-precision data). Ported from
    jens-duttke/usage-monitor-for-claude.
    """
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return payload

    # resets_at -> existing top-level quota field name (the prefix source)
    reset_to_field: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, dict) and value.get("utilization") is not None:
            resets_at = value.get("resets_at")
            if resets_at:
                reset_to_field.setdefault(resets_at, key)

    # group -> period prefix, via the non-scoped limit's shared reset time
    group_prefix: dict[str, str] = {}
    for limit in limits:
        if not isinstance(limit, dict) or limit.get("scope"):
            continue
        group = limit.get("group")
        resets_at = limit.get("resets_at")
        if group and resets_at and resets_at in reset_to_field:
            group_prefix.setdefault(group, reset_to_field[resets_at])

    merged = dict(payload)
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        model = (limit.get("scope") or {}).get("model") or {}
        display_name = model.get("display_name")
        prefix = group_prefix.get(limit.get("group"))
        if not display_name or not prefix:
            continue
        field = f"{prefix}_{_model_slug(display_name)}"
        if merged.get(field) is not None:
            continue
        merged[field] = {
            "utilization": float(limit.get("percent") or 0),
            "resets_at": limit.get("resets_at"),
        }
    return merged


def _server_message(resp: requests.Response | None) -> str | None:
    if resp is None:
        return None
    try:
        msg = resp.json().get("error", {}).get("message")
    except (ValueError, AttributeError):
        return None
    if not msg:
        return None
    return msg.removesuffix(" Please try again later.").removesuffix(
        " Please try again later"
    ).strip() or None


def _retry_after(resp: requests.Response | None) -> int | None:
    if resp is None:
        return None
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return None


def fetch_usage() -> UsageResult:
    """Fetch and normalize usage data. Never raises; errors are returned in-band."""
    headers = api_headers()
    if not headers:
        return UsageResult(metrics=[], error="No Claude Code credentials found")

    try:
        resp = requests.get(API_URL_USAGE, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.ConnectionError:
        return UsageResult(metrics=[], error="Connection error")
    except requests.Timeout:
        return UsageResult(metrics=[], error="Request timed out")
    except requests.HTTPError as exc:
        resp = exc.response
        code = resp.status_code if resp is not None else 0
        msg = _server_message(resp)
        if code == 401:
            return UsageResult(
                metrics=[], error=msg or "Authentication expired", auth_error=True
            )
        if code == 429:
            return UsageResult(
                metrics=[],
                error=msg or "Rate limited (429)",
                rate_limited=True,
                retry_after=_retry_after(resp),
            )
        return UsageResult(metrics=[], error=msg or f"HTTP error {code or '?'}")
    except requests.RequestException:
        return UsageResult(metrics=[], error="Connection error")

    try:
        payload = resp.json()
    except ValueError:
        return UsageResult(metrics=[], error="Invalid response from server")

    payload = _merge_scoped_limits(payload)
    return UsageResult(
        metrics=_parse_metrics(payload),
        extra=_parse_extra_usage(payload),
    )


if __name__ == "__main__":
    # Manual smoke test: python3 api.py
    result = fetch_usage()
    if not result.ok:
        print(f"ERROR: {result.error} (auth_error={result.auth_error})")
    else:
        for m in result.metrics:
            if m.available:
                resets = m.resets_at.astimezone().strftime("%Y-%m-%d %H:%M") if m.resets_at else "?"
                print(f"{m.name:8} {m.utilization:5.1f}%  resets {resets}")
            else:
                print(f"{m.name:8}   n/a")
        extra = result.extra
        if extra is not None:
            if extra.has_limit:
                print(f"{'Extra':8} {extra.utilization:5.1f}%  {extra.spent_text} / {extra.limit_text}")
            else:
                print(f"{'Extra':8}   {extra.spent_text} spent (uncapped)")
