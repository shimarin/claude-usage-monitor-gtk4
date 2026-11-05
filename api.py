"""Anthropic OAuth usage API client.

The only module that touches credentials. It reads Claude Code's OAuth access
token from the local credentials file and queries the (undocumented) Anthropic
OAuth usage endpoint. The token is refreshed by Claude Code itself, so we simply
re-read the file on every poll rather than refreshing it ourselves.

Network access is exclusively to api.anthropic.com; the token is used only in the
Authorization header.

Several parts of the response handling (quota field-name parsing, model-scoped
limit merging, plan naming, prepaid credit reading) follow the design of
jens-duttke/usage-monitor-for-claude — see the README acknowledgements.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API_URL_USAGE = "https://api.anthropic.com/api/oauth/usage"
API_URL_PROFILE = "https://api.anthropic.com/api/oauth/profile"
API_URL_PREPAID_CREDITS = (
    "https://api.anthropic.com/api/oauth/organizations/{org_uuid}/prepaid/credits"
)

CLAUDE_CONFIG_DIR = (
    Path(os.environ["CLAUDE_CONFIG_DIR"])
    if os.environ.get("CLAUDE_CONFIG_DIR")
    else Path.home() / ".claude"
)
CLAUDE_CREDENTIALS = CLAUDE_CONFIG_DIR / ".credentials.json"

_OAUTH_BETA = "oauth-2025-04-20"
_FALLBACK_USER_AGENT = "claude-code/2.1.85"
_ORG_UUID_PATTERN = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Supplementary data (profile / prepaid credits) changes rarely. The profile is
# re-read only when the access token changes (account switch); the prepaid
# balance is re-read at most every _PREPAID_TTL_S seconds.
_PREPAID_TTL_S = 300

# Quota field names are read from the response instead of being hardcoded, so a
# new quota type shows up without a release. "five_hour" -> (5, 'hour', None),
# "seven_day_sonnet" -> (7, 'day', 'sonnet').
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_UNIT_SUFFIXES = {"hour": "h", "day": "d"}
_TITLE_CASE_EXCEPTIONS = {"oauth": "OAuth", "api": "API", "ai": "AI"}

# Known ISO 4217 -> display symbol for money amounts. Unknown codes fall back to
# the code itself (e.g. "AUD 1.23").
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
    """A single usage metric. ``utilization`` is a percentage 0..100.

    ``field`` is the API response key the metric was read from; it is stable
    across polls, unlike ``name``, which is derived from that key.
    """

    field: str
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
class PrepaidBalance:
    """Prepaid usage-credit balance left to pay for extra usage.

    Supplementary data: when it cannot be read the field stays ``None`` and the
    balance is simply hidden — it never turns into an error.
    """

    amount_minor: float
    currency: str | None
    decimal_places: int | None

    @property
    def text(self) -> str:
        return format_credits(self.amount_minor, self.currency, self.decimal_places)


@dataclass(frozen=True)
class UsageResult:
    """Outcome of a usage poll: either ``metrics`` populated, or ``error`` set."""

    metrics: list[Metric]
    extra: ExtraUsage | None = None
    plan: str | None = None
    prepaid: PrepaidBalance | None = None
    error: str | None = None
    auth_error: bool = False
    rate_limited: bool = False
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def read_access_token() -> str | None:
    """Read the current OAuth access token, or None if unavailable.

    Returns None when the file is missing, temporarily unreadable (e.g. locked
    while Claude Code rewrites it on token rotation), or not shaped as expected —
    the caller treats all of these as "no token right now". No .exists() precheck:
    it would race the rewrite; a missing file just surfaces as OSError here.
    """
    try:
        creds = json.loads(CLAUDE_CREDENTIALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(creds, dict):
        return None
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    return oauth.get("accessToken") or None


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


def _headers_for(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": _user_agent(),
        "anthropic-beta": _OAUTH_BETA,
    }


def api_headers() -> dict[str, str] | None:
    token = read_access_token()
    if not token:
        return None
    return _headers_for(token)


# Quota field discovery


def parse_field_name(field: str) -> tuple[int, str, str | None] | None:
    """Split an API field name into ``(number, unit, variant)``.

    e.g. ``'five_hour'`` -> ``(5, 'hour', None)``, ``'seven_day_sonnet'`` ->
    ``(7, 'day', 'sonnet')``. Returns None when the number word or the unit is
    not recognized — which is the case for the code names (``nimbus_quill``) the
    API uses for quota types it has not announced publicly yet.
    """
    parts = field.split("_", 2)
    if len(parts) < 2:
        return None

    number = _NUMBER_WORDS.get(parts[0])
    unit = parts[1]
    if number is None or unit not in _UNIT_SUFFIXES:
        return None

    return (number, unit, parts[2] if len(parts) > 2 else None)


def is_active_quota(field: str, entry: Any) -> bool:
    """Return True if a usage entry is a quota that applies to this account.

    The API lists quota types before they apply, under code names (e.g.
    ``nimbus_quill``), with a utilization of 0 and no reset window. An entry
    therefore counts only if it has a reset window, a name that parses, or the
    ``from_account_limits`` marker set by :func:`_merge_scoped_limits`.
    """
    if not isinstance(entry, dict) or entry.get("utilization") is None:
        return False

    return (
        bool(entry.get("resets_at"))
        or parse_field_name(field) is not None
        or bool(entry.get("from_account_limits"))
    )


def _title_case_variant(text: str) -> str:
    """Title-case a variant string, respecting abbreviation exceptions."""
    return " ".join(
        _TITLE_CASE_EXCEPTIONS.get(word.lower(), word.title()) for word in text.split("_")
    )


def quota_label(field: str) -> str:
    """Human label for a quota field: ``Session (5hr)``, ``Weekly (Sonnet)``.

    Falls back to title case of the whole field name for code-named quotas.
    """
    parsed = parse_field_name(field)
    if parsed is None:
        return _title_case_variant(field)

    number, unit, variant = parsed
    if variant:
        suffix = _title_case_variant(variant)
    elif unit == "hour":
        suffix = f"{number}hr"
    else:
        suffix = f"{number} {unit}"

    return f"Session ({suffix})" if unit == "hour" else f"Weekly ({suffix})"


def _field_sort_key(field: str) -> tuple[int, int, int, str]:
    """Default display order: shorter periods first, base before variants."""
    parsed = parse_field_name(field)
    if parsed is None:
        return (2, 0, 0, field)

    number, unit, variant = parsed
    unit_order = 0 if unit == "hour" else 1
    variant_order = 0 if variant is None else 1
    return (unit_order, number, variant_order, variant or "")


def active_quota_fields(payload: dict[str, Any]) -> list[str]:
    """Quota field names in the response that apply to this account, ordered.

    A field qualifies when it is an object carrying ``resets_at`` and
    :func:`is_active_quota` accepts it. This drops quota types the plan does not
    have (reported as ``null``, e.g. the session limit on plans without one), the
    code-named types the API announces before activating them, and the non-quota
    objects of the response (``extra_usage``, ``spend``).
    """
    available = [
        key
        for key, value in payload.items()
        if isinstance(value, dict) and "resets_at" in value and is_active_quota(key, value)
    ]
    return sorted(available, key=_field_sort_key)


# Response parsing


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
    """Build one metric per active quota field, in display order."""
    metrics: list[Metric] = []
    for field in active_quota_fields(payload):
        block = payload[field]
        util = block.get("utilization")
        metrics.append(
            Metric(
                field=field,
                name=quota_label(field),
                utilization=float(util) if isinstance(util, (int, float)) else None,
                resets_at=_parse_resets_at(block.get("resets_at")),
            )
        )
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
    """Surface model-scoped limits from the ``limits`` array as quota fields.

    Newer usage responses carry per-model limits only inside the ``limits``
    array (via ``scope.model``), no longer as top-level fields like
    ``seven_day_sonnet``. Each scoped limit is mapped onto a synthetic quota
    field so the existing field names keep working. The period prefix is derived
    from the response — the non-scoped limit of the same ``group`` shares its
    ``resets_at`` with an existing top-level quota field, whose name supplies the
    prefix (a weekly limit scoped to Fable becomes ``seven_day_fable``). An
    existing top-level field is never overwritten (it carries higher-precision
    data). Inactive scoped limits (no reset window) are still surfaced at 0% so
    the model's limit is visible before the model is first used; each synthetic
    field carries ``from_account_limits`` so it survives :func:`is_active_quota`
    even when its inherited prefix is a code name the field parser cannot read.
    Ported from jens-duttke/usage-monitor-for-claude.
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
            "from_account_limits": True,
        }
    return merged


# Supplementary data: plan name and prepaid credit balance

# Keyed by access token, so an account switch (Claude Code writes a new token to
# the credentials file) drops the cached values automatically. Single-threaded in
# practice: the widget keeps at most one poll in flight.
_supplementary: dict[str, Any] = {"token": None, "org_uuid": None, "plan": None, "prepaid": None, "prepaid_at": None}


def _readable_identifier(identifier: str) -> str:
    """snake_case -> capitalized words, only the first letter of each word raised.

    ``default_claude_max_20x`` -> ``Default Claude Max 20x`` (``str.title()``
    would turn the ``20x`` suffix into ``20X``).
    """
    words = [word for word in identifier.split("_") if word]
    return " ".join(word[:1].upper() + word[1:] for word in words)


def plan_name(profile: dict[str, Any] | None) -> str:
    """Plan name as claude.ai shows it, or "" when the profile names none.

    ``organization.plan_display_name`` is the name the site uses; Anthropic does
    not rename the internal ``organization_type`` when a plan is renamed (Max 20x
    became "Ultra" while the type stayed ``claude_max``), so the type is only a
    fallback for a response without a display name.
    """
    if not isinstance(profile, dict):
        return ""
    org = profile.get("organization")
    org = org if isinstance(org, dict) else {}

    display_name = org.get("plan_display_name")
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()

    organization_type = org.get("organization_type")
    if not isinstance(organization_type, str):
        return ""
    return _readable_identifier(organization_type)


def fetch_profile() -> dict[str, Any] | None:
    """Fetch the account profile, or None on any failure (best-effort data)."""
    token = read_access_token()
    if not token:
        return None
    try:
        resp = requests.get(API_URL_PROFILE, headers=_headers_for(token), timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _normalize_prepaid_credits(data: Any) -> PrepaidBalance | None:
    """Reduce a prepaid-credits response to amount, currency and decimal places.

    The OAuth response is leaner than the one behind the web app: the money
    objects nested in the tranches are null, so only the top-level
    ``balance.money`` supplies the currency and its exponent. A response whose
    ``amount`` is not a number means the account has no prepaid credits.
    """
    if not isinstance(data, dict):
        return None

    amount = data.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None

    # isinstance rather than "or {}": the latter substitutes an empty dict for a
    # null, but passes a value that changed type straight into the next get().
    balance = data.get("balance")
    money = balance.get("money") if isinstance(balance, dict) else None
    money = money if isinstance(money, dict) else {}

    currency = money.get("currency") or data.get("currency")
    exponent = money.get("exponent")

    return PrepaidBalance(
        amount_minor=float(amount),
        currency=currency if isinstance(currency, str) else None,
        decimal_places=exponent if isinstance(exponent, int) else 2,
    )


def fetch_prepaid_credits(org_uuid: Any) -> PrepaidBalance | None:
    """Read the prepaid usage-credit balance of an organization.

    Supplementary data: every failure — a malformed organization uuid, a missing
    token, an HTTP error, an unexpected payload — returns None, so a balance that
    cannot be read simply stays hidden instead of becoming an error message.
    """
    if not isinstance(org_uuid, str) or not _ORG_UUID_PATTERN.match(org_uuid):
        return None
    token = read_access_token()
    if not token:
        return None
    try:
        resp = requests.get(
            API_URL_PREPAID_CREDITS.format(org_uuid=org_uuid),
            headers=_headers_for(token),
            timeout=5,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    return _normalize_prepaid_credits(payload)


def _fetch_supplementary(payload: dict[str, Any], token: str) -> tuple[str | None, PrepaidBalance | None]:
    """Best-effort plan name and prepaid balance for the current account.

    The profile is fetched once per token (it also carries the organization uuid
    the prepaid endpoint needs); the prepaid balance is re-read at most every
    ``_PREPAID_TTL_S`` seconds, and only while the account actually has paid
    extra usage enabled — without it there is no balance to show.
    """
    if _supplementary["token"] != token:
        _supplementary.update(token=token, org_uuid=None, plan=None, prepaid=None, prepaid_at=None)
        profile = fetch_profile()
        org = (profile or {}).get("organization")
        _supplementary["plan"] = plan_name(profile) or None
        if isinstance(org, dict) and isinstance(org.get("uuid"), str):
            _supplementary["org_uuid"] = org["uuid"]

    extra = payload.get("extra_usage")
    org_uuid = _supplementary["org_uuid"]
    due = (
        isinstance(extra, dict)
        and extra.get("is_enabled")
        and org_uuid is not None
        and (
            _supplementary["prepaid_at"] is None
            or time.monotonic() - _supplementary["prepaid_at"] >= _PREPAID_TTL_S
        )
    )
    if due:
        _supplementary["prepaid"] = fetch_prepaid_credits(org_uuid)
        _supplementary["prepaid_at"] = time.monotonic()

    return _supplementary["plan"], _supplementary["prepaid"]


# Errors


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
    token = read_access_token()
    if not token:
        return UsageResult(metrics=[], error="No Claude Code credentials found")
    headers = _headers_for(token)

    try:
        resp = requests.get(API_URL_USAGE, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.SSLError:
        return UsageResult(metrics=[], error="Certificate error")
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
    if not isinstance(payload, dict):
        return UsageResult(metrics=[], error="Invalid response from server")

    payload = _merge_scoped_limits(payload)
    plan, prepaid = _fetch_supplementary(payload, token)
    return UsageResult(
        metrics=_parse_metrics(payload),
        extra=_parse_extra_usage(payload),
        plan=plan,
        prepaid=prepaid,
    )


if __name__ == "__main__":
    # Manual smoke test: python3 api.py
    result = fetch_usage()
    if not result.ok:
        print(f"ERROR: {result.error} (auth_error={result.auth_error})")
    else:
        print(f"plan: {result.plan or '?'}")
        for m in result.metrics:
            if m.available:
                resets = m.resets_at.astimezone().strftime("%Y-%m-%d %H:%M") if m.resets_at else "-"
                print(f"{m.name:20} {m.utilization:5.1f}%  resets {resets}")
            else:
                print(f"{m.name:20}   n/a")
        extra = result.extra
        if extra is not None:
            if extra.has_limit:
                print(f"{'Extra':20} {extra.utilization:5.1f}%  {extra.spent_text} / {extra.limit_text}")
            else:
                print(f"{'Extra':20}   {extra.spent_text} spent (uncapped)")
            if result.prepaid is not None:
                print(f"{'Prepaid':20}   {result.prepaid.text} available")
