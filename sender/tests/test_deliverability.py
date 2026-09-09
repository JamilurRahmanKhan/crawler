import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from deliverability import (
    reverse_ip,
    check_blacklists,
    check_spf,
    check_dmarc,
    check_dkim_selectors,
    DEFAULT_DNSBL_ZONES,
)


# --- reverse_ip ----------------------------------------------------------------

def test_reverse_ip():
    assert reverse_ip("1.2.3.4") == "4.3.2.1"


# --- check_blacklists (mocked DNS) ----------------------------------------------

@patch("deliverability.dns.resolver.resolve")
def test_check_blacklists_flags_listed_zone(mock_resolve):
    mock_resolve.return_value = MagicMock()  # resolving = listed
    listed = check_blacklists("1.2.3.4", zones=["test.zone.org"])
    assert "test.zone.org" in listed


@patch("deliverability.dns.resolver.resolve")
def test_check_blacklists_clean_when_nxdomain(mock_resolve):
    import dns.resolver as real_resolver
    mock_resolve.side_effect = real_resolver.NXDOMAIN()
    listed = check_blacklists("1.2.3.4", zones=["test.zone.org"])
    assert listed == []


@patch("deliverability.dns.resolver.resolve")
def test_check_blacklists_lookup_error_does_not_count_as_listed(mock_resolve):
    # never false-positive a health check on a transient DNS error
    mock_resolve.side_effect = Exception("timeout")
    listed = check_blacklists("1.2.3.4", zones=["test.zone.org"])
    assert listed == []


@patch("deliverability.dns.resolver.resolve")
def test_check_blacklists_checks_multiple_zones(mock_resolve):
    import dns.resolver as real_resolver

    def side_effect(query, *args, **kwargs):
        if "zone-a" in query:
            return MagicMock()
        raise real_resolver.NXDOMAIN()

    mock_resolve.side_effect = side_effect
    listed = check_blacklists("1.2.3.4", zones=["zone-a.org", "zone-b.org"])
    assert listed == ["zone-a.org"]


def test_default_dnsbl_zones_nonempty():
    assert len(DEFAULT_DNSBL_ZONES) > 0


# --- check_spf (mocked DNS) ------------------------------------------------------

def _txt_record(text: str):
    rec = MagicMock()
    rec.strings = [text.encode()]
    return rec


@patch("deliverability.dns.resolver.resolve")
def test_check_spf_found(mock_resolve):
    mock_resolve.return_value = [_txt_record("v=spf1 include:_spf.google.com ~all")]
    result = check_spf("acme.com")
    assert result["found"] is True
    assert "v=spf1" in result["record"]


@patch("deliverability.dns.resolver.resolve")
def test_check_spf_not_found_among_other_txt_records(mock_resolve):
    mock_resolve.return_value = [_txt_record("google-site-verification=abc123")]
    result = check_spf("acme.com")
    assert result["found"] is False


@patch("deliverability.dns.resolver.resolve")
def test_check_spf_handles_dns_failure_gracefully(mock_resolve):
    mock_resolve.side_effect = Exception("no such domain")
    result = check_spf("acme.com")
    assert result["found"] is False
    assert result["record"] is None


# --- check_dmarc (mocked DNS) -----------------------------------------------------

@patch("deliverability.dns.resolver.resolve")
def test_check_dmarc_found_with_policy(mock_resolve):
    mock_resolve.return_value = [_txt_record("v=DMARC1; p=quarantine; rua=mailto:d@acme.com")]
    result = check_dmarc("acme.com")
    assert result["found"] is True
    assert result["policy"] == "quarantine"


@patch("deliverability.dns.resolver.resolve")
def test_check_dmarc_not_found(mock_resolve):
    import dns.resolver as real_resolver
    mock_resolve.side_effect = real_resolver.NXDOMAIN()
    result = check_dmarc("acme.com")
    assert result["found"] is False
    assert result["policy"] is None


# --- check_dkim_selectors (mocked DNS, best-effort) -------------------------------

@patch("deliverability.dns.resolver.resolve")
def test_check_dkim_selectors_finds_a_match(mock_resolve):
    import dns.resolver as real_resolver

    def side_effect(query, *args, **kwargs):
        if query.startswith("google._domainkey"):
            return [MagicMock()]
        raise real_resolver.NXDOMAIN()

    mock_resolve.side_effect = side_effect
    result = check_dkim_selectors("acme.com")
    assert "google" in result


@patch("deliverability.dns.resolver.resolve")
def test_check_dkim_selectors_empty_when_none_match(mock_resolve):
    import dns.resolver as real_resolver
    mock_resolve.side_effect = real_resolver.NXDOMAIN()
    result = check_dkim_selectors("acme.com")
    assert result == []
