"""Unit tests for the API client's response handling.

Run with `make test` (or `python3 -m unittest tests -v`). No network access: the
payloads below are the anonymized shapes documented by the upstream project
(https://github.com/jens-duttke/usage-monitor-for-claude/blob/main/docs/api-reference.md).
"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

import api

_SEVEN_DAY_RESET = "2026-09-11T20:59:59.966479+00:00"
_FIVE_HOUR_RESET = "2026-09-05T12:59:59.966454+00:00"


def sample_payload() -> dict:
    """A usage response like the documented example: mixed, with code names."""
    return {
        "five_hour": {"utilization": 48.0, "resets_at": _FIVE_HOUR_RESET},
        "seven_day": {"utilization": 64.0, "resets_at": _SEVEN_DAY_RESET},
        "seven_day_oauth_apps": None,
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 2.0, "resets_at": _SEVEN_DAY_RESET},
        "tangelo": None,
        # Announced but not activated yet: no reset window, name does not parse.
        "nimbus_quill": {"utilization": 0.0, "resets_at": None},
        "extra_usage": {"is_enabled": True, "used_credits": 0.0, "monthly_limit": None, "currency": "EUR", "decimal_places": 2},
        "limits": [],
        "spend": {"percent": 0, "balance": None},
    }


class TestParseFieldName(unittest.TestCase):
    def test_parses_period_fields(self):
        self.assertEqual(api.parse_field_name("five_hour"), (5, "hour", None))
        self.assertEqual(api.parse_field_name("seven_day"), (7, "day", None))
        self.assertEqual(api.parse_field_name("seven_day_sonnet"), (7, "day", "sonnet"))

    def test_rejects_code_names(self):
        for field in ("nimbus_quill", "tangelo", "iguana_necktie", "extra_usage", "hour"):
            self.assertIsNone(api.parse_field_name(field), field)


class TestQuotaLabel(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(api.quota_label("five_hour"), "Session (5hr)")
        self.assertEqual(api.quota_label("seven_day"), "Weekly (7 day)")
        self.assertEqual(api.quota_label("seven_day_sonnet"), "Weekly (Sonnet)")
        self.assertEqual(api.quota_label("seven_day_oauth_apps"), "Weekly (OAuth Apps)")

    def test_code_name_falls_back_to_title_case(self):
        self.assertEqual(api.quota_label("nimbus_quill"), "Nimbus Quill")


class TestIsActiveQuota(unittest.TestCase):
    def test_null_entry_is_not_a_quota(self):
        self.assertFalse(api.is_active_quota("seven_day_opus", None))

    def test_announced_but_not_activated(self):
        self.assertFalse(api.is_active_quota("nimbus_quill", {"utilization": 0.0, "resets_at": None}))

    def test_code_name_with_reset_window_counts(self):
        self.assertTrue(api.is_active_quota("nimbus_quill", {"utilization": 10.0, "resets_at": _SEVEN_DAY_RESET}))

    def test_parsed_name_without_reset_counts(self):
        self.assertTrue(api.is_active_quota("seven_day_fable", {"utilization": 0.0, "resets_at": None}))

    def test_marker_counts_even_under_unparsable_prefix(self):
        entry = {"utilization": 0.0, "resets_at": None, "from_account_limits": True}
        self.assertTrue(api.is_active_quota("nimbus_quill_fable", entry))


class TestActiveQuotaFields(unittest.TestCase):
    def test_orders_shorter_periods_first_and_base_before_variants(self):
        payload = sample_payload()
        payload["seven_day_opus"] = {"utilization": 5.0, "resets_at": _SEVEN_DAY_RESET}
        payload["fourteen_day"] = {"utilization": 1.0, "resets_at": _SEVEN_DAY_RESET}
        self.assertEqual(
            api.active_quota_fields(payload),
            ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "fourteen_day"],
        )

    def test_excludes_null_inactive_and_non_quota_objects(self):
        fields = api.active_quota_fields(sample_payload())
        self.assertEqual(fields, ["five_hour", "seven_day", "seven_day_sonnet"])
        for unwanted in ("extra_usage", "spend", "nimbus_quill", "seven_day_opus", "tangelo"):
            self.assertNotIn(unwanted, fields)


class TestMergeScopedLimits(unittest.TestCase):
    @staticmethod
    def _payload_with_limits(limit: dict) -> dict:
        payload = sample_payload()
        payload["limits"] = [
            {"kind": "weekly_all", "group": "weekly", "percent": 64, "resets_at": _SEVEN_DAY_RESET, "scope": None},
            limit,
        ]
        return payload

    def test_active_scoped_limit_becomes_synthetic_field(self):
        payload = self._payload_with_limits({
            "kind": "weekly_model", "group": "weekly", "percent": 30, "resets_at": _SEVEN_DAY_RESET,
            "scope": {"model": {"display_name": "Fable"}},
        })
        merged = api._merge_scoped_limits(payload)
        self.assertEqual(
            merged["seven_day_fable"],
            {"utilization": 30.0, "resets_at": _SEVEN_DAY_RESET, "from_account_limits": True},
        )

    def test_inactive_scoped_limit_still_exposed_at_zero(self):
        payload = self._payload_with_limits({
            "kind": "weekly_model", "group": "weekly", "percent": 0, "resets_at": None,
            "scope": {"model": {"display_name": "Fable"}},
        })
        merged = api._merge_scoped_limits(payload)
        self.assertEqual(
            merged["seven_day_fable"],
            {"utilization": 0.0, "resets_at": None, "from_account_limits": True},
        )
        self.assertTrue(api.is_active_quota("seven_day_fable", merged["seven_day_fable"]))

    def test_existing_top_level_field_not_overwritten(self):
        payload = self._payload_with_limits({
            "kind": "weekly_model", "group": "weekly", "percent": 99, "resets_at": _SEVEN_DAY_RESET,
            "scope": {"model": {"display_name": "Sonnet"}},
        })
        merged = api._merge_scoped_limits(payload)
        self.assertEqual(merged["seven_day_sonnet"]["utilization"], 2.0)
        self.assertNotIn("from_account_limits", merged["seven_day_sonnet"])

    def test_top_level_fields_keep_their_shape(self):
        merged = api._merge_scoped_limits(self._payload_with_limits({
            "kind": "weekly_model", "group": "weekly", "percent": 30, "resets_at": _SEVEN_DAY_RESET,
            "scope": {"model": {"display_name": "Fable"}},
        }))
        self.assertNotIn("from_account_limits", merged["seven_day"])

    def test_payload_without_scoped_limits_is_unchanged(self):
        payload = sample_payload()
        payload["limits"] = None
        self.assertIs(api._merge_scoped_limits(payload), payload)
        self.assertEqual(api._merge_scoped_limits(sample_payload()), sample_payload())


class TestParseMetrics(unittest.TestCase):
    def test_names_and_order_come_from_the_response(self):
        payload = sample_payload()
        payload["limits"] = [
            {"kind": "weekly_all", "group": "weekly", "percent": 64, "resets_at": _SEVEN_DAY_RESET, "scope": None},
            {"kind": "weekly_model", "group": "weekly", "percent": 30, "resets_at": _SEVEN_DAY_RESET,
             "scope": {"model": {"display_name": "Fable"}}},
        ]
        metrics = api._parse_metrics(api._merge_scoped_limits(payload))
        self.assertEqual([m.name for m in metrics], ["Session (5hr)", "Weekly (7 day)", "Weekly (Fable)", "Weekly (Sonnet)"])
        self.assertEqual(metrics[0].field, "five_hour")
        self.assertEqual(metrics[0].utilization, 48.0)
        self.assertEqual(metrics[0].resets_at, datetime.fromisoformat(_FIVE_HOUR_RESET))

    def test_null_metrics_are_not_listed(self):
        self.assertNotIn("seven_day_opus", [m.field for m in api._parse_metrics(sample_payload())])


class TestParseExtraUsage(unittest.TestCase):
    def test_capped(self):
        payload = {"extra_usage": {"is_enabled": True, "used_credits": 658, "monthly_limit": 20000, "currency": "USD", "decimal_places": 2}}
        extra = api._parse_extra_usage(payload)
        self.assertEqual(extra.spent_text, "$6.58")
        self.assertEqual(extra.limit_text, "$200.00")
        self.assertAlmostEqual(extra.utilization, 3.29)

    def test_uncapped_has_no_percentage(self):
        extra = api._parse_extra_usage(sample_payload())
        self.assertFalse(extra.has_limit)
        self.assertIsNone(extra.utilization)
        self.assertIsNone(extra.limit_text)

    def test_disabled_or_malformed_is_hidden(self):
        self.assertIsNone(api._parse_extra_usage({"extra_usage": {"is_enabled": False}}))
        self.assertIsNone(api._parse_extra_usage({"extra_usage": {"is_enabled": True}}))
        self.assertIsNone(api._parse_extra_usage({"extra_usage": None}))


class TestPrepaidCredits(unittest.TestCase):
    def test_normalizes_top_level_balance(self):
        data = {
            "amount": 5597,
            "currency": "EUR",
            "balance": {"money": {"amount_minor": 5597, "currency": "EUR", "exponent": 2}, "credits": None},
            "promo_tranches": [{"remaining_amount_minor_units": 5596}],
        }
        prepaid = api._normalize_prepaid_credits(data)
        self.assertEqual(prepaid.text, "€55.97")

    def test_no_credits_means_no_balance(self):
        for data in (None, {}, {"amount": None}, {"amount": True}, {"amount": "5597"}):
            self.assertIsNone(api._normalize_prepaid_credits(data), repr(data))

    def test_org_uuid_is_validated_before_the_request(self):
        for bad in (None, "", "not-a-uuid", "12345678-1234-1234-1234-123456789XYZ"):
            self.assertIsNone(api.fetch_prepaid_credits(bad))


class TestPlanName(unittest.TestCase):
    def test_display_name_wins_over_internal_type(self):
        profile = {"organization": {"organization_type": "claude_max", "plan_display_name": "Ultra"}}
        self.assertEqual(api.plan_name(profile), "Ultra")

    def test_internal_type_is_the_fallback(self):
        profile = {"organization": {"organization_type": "claude_max"}}
        self.assertEqual(api.plan_name(profile), "Claude Max")

    def test_blank_or_missing_values_do_not_raise(self):
        for profile in (None, {}, {"organization": None}, {"organization": {"plan_display_name": "  "}},
                        {"organization": {"plan_display_name": None, "organization_type": None}}):
            self.assertEqual(api.plan_name(profile), "")


class TestFormatCredits(unittest.TestCase):
    def test_symbols_and_iso_codes(self):
        self.assertEqual(api.format_credits(658, "USD", 2), "$6.58")
        self.assertEqual(api.format_credits(658, "EUR", 2), "€6.58")
        self.assertEqual(api.format_credits(658, "AUD", 2), "6.58 AUD")
        self.assertEqual(api.format_credits(658, "JPY", 0), "¥658")
        self.assertEqual(api.format_credits(123456, None, 2), "1,234.56")


class TestFetchUsageErrors(unittest.TestCase):
    def test_missing_credentials(self):
        with patch.object(api, "read_access_token", return_value=None):
            result = api.fetch_usage()
        self.assertFalse(result.ok)
        self.assertTrue(result.error)

    @staticmethod
    def _http_error(status: int, retry_after: str | None = None):
        import requests

        response = api.requests.Response()
        response.status_code = status
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        return requests.HTTPError(response=response)

    def test_rate_limit_reports_retry_after(self):
        with patch.object(api, "read_access_token", return_value="t"), \
                patch.object(api.requests, "get", side_effect=self._http_error(429, "7384")):
            result = api.fetch_usage()
        self.assertTrue(result.rate_limited)
        self.assertEqual(result.retry_after, 7384)

    def test_auth_error_flag(self):
        with patch.object(api, "read_access_token", return_value="t"), \
                patch.object(api.requests, "get", side_effect=self._http_error(401)):
            result = api.fetch_usage()
        self.assertTrue(result.auth_error)

    def test_success_shape(self):
        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return sample_payload()

        with patch.object(api, "read_access_token", return_value="t"), \
                patch.object(api.requests, "get", return_value=_Response()), \
                patch.object(api, "_fetch_supplementary", return_value=(None, None)):
            result = api.fetch_usage()
        self.assertTrue(result.ok)
        self.assertEqual([m.field for m in result.metrics], ["five_hour", "seven_day", "seven_day_sonnet"])
        self.assertIsNotNone(result.extra)


if __name__ == "__main__":
    unittest.main()
