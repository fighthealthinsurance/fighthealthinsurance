"""client_network.py: the client's network as Cloudflare saw it, bucketed,
keyed and classed, and the agreement cap (assistant_ip_limit.py) reading it
exactly as it did before the helpers moved there."""

import datetime
import hashlib
import hmac
import ipaddress

import pytest
from django.test import RequestFactory, override_settings

from fighthealthinsurance import assistant_ip_limit, client_network

MONDAY = datetime.date(2026, 10, 5)


def _scope(*headers):
    return {"headers": [(k, v) for k, v in headers]}


class TestReadingTheAddress:
    def test_only_cloudflares_header_is_read_from_meta(self):
        meta = {"HTTP_X_FORWARDED_FOR": "198.51.100.1", "REMOTE_ADDR": "10.0.0.1"}
        assert client_network.cf_ip_from_meta(meta) == ""
        meta["HTTP_CF_CONNECTING_IP"] = " 203.0.113.9 "
        assert client_network.cf_ip_from_meta(meta) == "203.0.113.9"

    def test_only_cloudflares_header_is_read_from_a_scope(self):
        scope = _scope((b"x-forwarded-for", b"198.51.100.1"))
        scope["client"] = ("10.0.0.1", 5000)
        assert client_network.cf_ip_from_scope(scope) == ""
        scope["headers"].append((b"cf-connecting-ip", b"203.0.113.9"))
        assert client_network.cf_ip_from_scope(scope) == "203.0.113.9"

    def test_no_scope_has_no_address(self):
        assert client_network.cf_ip_from_scope(None) == ""

    @pytest.mark.parametrize(
        "raw,expected",
        [("us", "US"), ("T1", "T1"), ("XX", "XX"), ("USA", ""), ("", ""), ("1A", "")],
    )
    def test_the_country_header_must_be_a_code(self, raw, expected):
        assert client_network.cf_country_from_meta({"HTTP_CF_IPCOUNTRY": raw}) == expected

    def test_the_country_is_read_from_a_scope(self):
        scope = _scope((b"cf-ipcountry", b"CA"))
        assert client_network.cf_country_from_scope(scope) == "CA"

    @pytest.mark.parametrize("code,stored", [("US", "US"), ("T1", ""), ("XX", "")])
    def test_tor_and_unknown_are_not_stored_as_places(self, code, stored):
        assert client_network.country_label(code) == stored


class TestPrefixes:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("203.0.113.77", "203.0.113.0/24"),
            ("2001:db8:1:2:3:4:5:6", "2001:db8:1::/48"),
            ("::ffff:203.0.113.77", "203.0.113.0/24"),
            ("junk", "none"),
            ("", "none"),
            (None, "none"),
            ("1" * 100, "none"),
        ],
    )
    def test_the_metrics_buckets(self, raw, expected):
        assert client_network.prefix_of(raw, v4_bits=24, v6_bits=48) == expected

    def test_a_whole_address_length_keeps_the_bare_address(self):
        assert (
            client_network.prefix_of("203.0.113.77", v4_bits=32, v6_bits=64)
            == "203.0.113.77"
        )


class TestKeys:
    @override_settings(SECRET_KEY="test-secret-one")
    def test_a_key_is_stable_through_the_week(self):
        keys = {
            client_network.period_key(
                b"label",
                client_network.week_start(MONDAY + datetime.timedelta(days=n)).isoformat(),
                "203.0.113.0/24",
            )
            for n in range(7)
        }
        assert len(keys) == 1

    @override_settings(SECRET_KEY="test-secret-one")
    def test_a_key_changes_with_the_week(self):
        this_week = client_network.period_key(b"l", MONDAY.isoformat(), "a")
        next_week = client_network.period_key(
            b"l", (MONDAY + datetime.timedelta(days=7)).isoformat(), "a"
        )
        assert this_week != next_week

    def test_a_key_changes_with_the_secret(self):
        with override_settings(SECRET_KEY="test-secret-one"):
            one = client_network.period_key(b"l", "p", "a")
        with override_settings(SECRET_KEY="test-secret-two"):
            two = client_network.period_key(b"l", "p", "a")
        assert one != two

    @override_settings(SECRET_KEY="test-secret-one")
    def test_a_key_holds_no_address(self):
        key = client_network.period_key(b"l", "p", "203.0.113.0/24")
        assert "203.0.113" not in key
        assert len(key) == 64

    def test_week_start_is_the_iso_monday(self):
        sunday = datetime.date(2026, 10, 11)
        assert client_network.week_start(sunday) == MONDAY
        assert client_network.week_start(MONDAY) == MONDAY


class TestClassification:
    @pytest.mark.parametrize(
        "ip,asn,country,expected",
        [
            ("203.0.113.1", "COMCAST-7922", "T1", "tor"),
            ("160.79.105.3", "", "US", "ai_platform"),
            ("203.0.113.1", "ANTHROPIC", "", "ai_platform"),
            ("203.0.113.1", "AMAZON-02", "", "hosting"),
            ("203.0.113.1", "GOOGLE-CLOUD-PLATFORM", "", "hosting"),
            ("203.0.113.1", "GOOGLE", "", "hosting"),
            ("203.0.113.1", "GOOGLE-FIBER", "", "isp"),
            ("203.0.113.1", "AS-CHOOPA", "", "hosting"),
            ("203.0.113.1", "COMCAST-7922", "", "isp"),
            ("203.0.113.1", "", "", "unknown"),
            ("2001:db8::1", "", "", "unknown"),
        ],
    )
    def test_classes(self, ip, asn, country, expected):
        assert client_network.classify(ip, asn, country) == expected

    def test_extra_platform_ranges_come_from_settings(self):
        with override_settings(FHI_AI_PLATFORM_CIDRS="198.51.100.0/24, junk"):
            assert client_network.classify("198.51.100.7", "", "") == "ai_platform"
        with override_settings(FHI_AI_PLATFORM_CIDRS=""):
            assert client_network.classify("198.51.100.7", "", "") == "unknown"

    def test_every_class_is_in_the_fixed_set(self):
        assert set(client_network.NETWORK_CLASSES) == {
            "isp",
            "hosting",
            "ai_platform",
            "tor",
            "unknown",
            "none",
        }


def _old_address_of(raw):
    """assistant_ip_limit.address_of as it was before the move, inline."""
    raw = str(raw or "").strip()
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return "none"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _old_key_for(secret, address, day):
    secret = hmac.new(
        secret.encode("utf-8"), b"fhi-assistant-agreements-per-ip-v1", hashlib.sha256
    ).digest()
    message = f"{day.isoformat()}|{address}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


class TestAgreementCapUnchanged:
    @pytest.mark.parametrize(
        "raw",
        [
            "203.0.113.77",
            "2001:db8:1:2:3:4:5:6",
            "::ffff:203.0.113.77",
            "junk",
            "",
            " 203.0.113.77 ",
        ],
    )
    def test_the_address_is_what_it_was(self, raw):
        request = RequestFactory().get("/", HTTP_CF_CONNECTING_IP=raw)
        assert assistant_ip_limit.address_of(request) == _old_address_of(raw)

    def test_the_key_is_what_it_was(self):
        with override_settings(SECRET_KEY="test-secret-golden"):
            for address in ("203.0.113.77", "2001:db8:1:2::/64", "none"):
                assert assistant_ip_limit.key_for(address, MONDAY) == _old_key_for(
                    "test-secret-golden", address, MONDAY
                )
