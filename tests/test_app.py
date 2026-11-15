"""Tests for the ping-tool request handler and its helpers.

Requests are driven through a real `BaseHTTPRequestHandler` over a socket pair,
so the status line, JSON body and `Content-length` header are all exercised the
way a monitoring probe sees them. ICMP itself is mocked: the tests assert the
verdict logic, never the network.

Tests marked `xfail` describe the known defects listed in
`.clinerules/40-validation.md`. `xfail_strict` is enabled in `pyproject.toml`, so
fixing a defect without deleting its marker (and updating that table) fails the
suite on purpose.
"""
import email.message
import http.client
import json
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from icmplib import Hop, Host, NameLookupError, SocketAddressError
from requests.exceptions import RequestException

from app.__main__ import (
    HEADER_HOPS_MUST_EXCLUDE_ORG_CSV,
    HEADER_HOPS_MUST_INCLUDE_ORG_CSV,
    HEADER_HOST,
    HEADER_MIN_LATENCY_MS,
    HEADER_ROUTE_EXCLUDE_CSV,
    HEADER_ROUTE_INCLUDE_CSV,
    HEADER_SOURCE,
    OS_ENV_IPINFO_TOKEN,
    WebRequestHandler,
    get_ip_org,
)

TARGET = 'arbitrary.internet.host.com'
TARGET_IP = '203.0.113.10'
BACKUP_GATEWAY = '198.51.100.1'
FIBRE_GATEWAY = '203.0.113.1'


class _ServerStub:
    """The minimum `HTTPServer` surface that `BaseHTTPRequestHandler` touches."""
    server_name = 'localhost'
    server_port = 8080


class _QuietHandler(WebRequestHandler):
    """The production handler without the access log that would pollute a test run."""

    def log_message(self, format, *args):
        pass


def _request(path, headers=None):
    """Serve one GET through the real handler, returning ``(status, body)``."""
    lines = [f'GET {path} HTTP/1.0']
    for name, value in (headers or {}).items():
        lines.append(f'{name}: {value}')
    raw_request = ('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8')

    client, connection = socket.socketpair()
    client.settimeout(10)
    connection.settimeout(10)
    try:
        client.sendall(raw_request)
        # HTTP/1.0 with no keep-alive: the handler serves exactly one request.
        _QuietHandler(connection, ('127.0.0.1', 0), _ServerStub())
        response = http.client.HTTPResponse(client)
        response.begin()
        return response.status, json.loads(response.read())
    finally:
        client.close()
        connection.close()


def _handler_with_headers(headers=None):
    """A handler instance carrying parsed headers but no socket, for helper tests."""
    handler = WebRequestHandler.__new__(WebRequestHandler)
    handler.headers = email.message.Message()
    for name, value in (headers or {}).items():
        handler.headers[name] = value
    return handler


def _alive_host(address=TARGET_IP, rtts=(15.2, 15.9, 16.1)):
    return Host(address=address, packets_sent=3, rtts=list(rtts))


def _silent_host(address=TARGET_IP):
    return Host(address=address, packets_sent=3, rtts=[])


def _hop(address, rtt=15.0, distance=1):
    return Hop(address=address, packets_sent=1, rtts=[rtt], distance=distance)


def _org_for_ip(ip):
    """Org lookup that attributes the final hop to a forbidden carrier."""
    return 'AS54321 Forbidden Carrier' if ip == TARGET_IP else 'AS12345 Example Transit'


PING_HEADERS = {
    HEADER_HOST: TARGET,
    HEADER_SOURCE: 'eth1',
    HEADER_MIN_LATENCY_MS: '10',
}

TRACEROUTE_HEADERS = {
    HEADER_HOST: TARGET,
    HEADER_SOURCE: 'eth1',
    HEADER_MIN_LATENCY_MS: '10',
    HEADER_ROUTE_INCLUDE_CSV: BACKUP_GATEWAY,
    HEADER_ROUTE_EXCLUDE_CSV: FIBRE_GATEWAY,
    HEADER_HOPS_MUST_INCLUDE_ORG_CSV: 'AS12345',
    HEADER_HOPS_MUST_EXCLUDE_ORG_CSV: 'AS54321',
}


# --- /ping verdicts ---------------------------------------------------------

def test_ping_success_reports_rtts():
    with patch('app.__main__.ping', return_value=_alive_host()) as icmp_ping, \
            patch('app.__main__.get_ip_org', return_value=None):
        status, body = _request('/ping', PING_HEADERS)

    icmp_ping.assert_called_once_with(
        address=TARGET, count=3, interval=1, source='eth1', privileged=False)
    assert status == 200
    assert body['result'] == 'OK'
    assert body['ping'] == {'host': TARGET_IP, 'rtts': [15.2, 15.9, 16.1]}


def test_ping_without_latency_header_still_passes_when_replies_arrive():
    headers = {HEADER_HOST: TARGET, HEADER_SOURCE: 'eth1'}
    with patch('app.__main__.ping', return_value=_alive_host()), \
            patch('app.__main__.get_ip_org', return_value=None):
        status, body = _request('/ping', headers)

    assert status == 200
    assert body['result'] == 'OK'
    assert len(body['ping']['rtts']) == 3


def test_ping_no_replies_is_a_circuit_verdict_not_a_crash():
    # The data-cap case: interface valid, zero ICMP replies, no latency header.
    headers = {HEADER_HOST: TARGET, HEADER_SOURCE: 'eth1'}
    with patch('app.__main__.ping', return_value=_silent_host()), \
            patch('app.__main__.get_ip_org') as ip_org:
        status, body = _request('/ping', headers)

    assert status == 503
    assert body['result'] == 'error'
    assert 'No ICMP replies' in body['reason']
    assert 'loss 100.0%' in body['reason']
    assert body['ping'] == {'host': TARGET_IP, 'rtts': []}
    # A dead circuit must not pay for an IPinfo lookup it cannot use.
    ip_org.assert_not_called()


def test_ping_latency_below_minimum_fails_and_keeps_measurements():
    slow_host = _alive_host(rtts=(1.0, 1.2, 1.4))
    with patch('app.__main__.ping', return_value=slow_host), \
            patch('app.__main__.get_ip_org', return_value=None):
        status, body = _request('/ping', PING_HEADERS)

    assert status == 503
    assert body['result'] == 'error'
    assert 'Minimum RTT' in body['reason']
    assert body['ping'] == {'host': TARGET_IP, 'rtts': [1.0, 1.2, 1.4]}


# --- /traceroute verdicts ---------------------------------------------------

def test_traceroute_success_satisfies_route_and_org_constraints():
    hops = [_hop(BACKUP_GATEWAY, distance=1), _hop(TARGET_IP, rtt=16.0, distance=2)]
    with patch('app.__main__.traceroute', return_value=hops) as icmp_traceroute, \
            patch('app.__main__.get_ip_org', return_value='AS12345 Example Transit'):
        status, body = _request('/traceroute', TRACEROUTE_HEADERS)

    icmp_traceroute.assert_called_once_with(address=TARGET, count=1, source='eth1')
    assert status == 200
    assert body['result'] == 'OK'
    assert body['traceroute'] == {'host': TARGET_IP, 'rtts': [16.0]}


def test_traceroute_forbidden_gateway_fails_the_test():
    hops = [_hop(FIBRE_GATEWAY, distance=1), _hop(TARGET_IP, rtt=16.0, distance=2)]
    with patch('app.__main__.traceroute', return_value=hops), \
            patch('app.__main__.get_ip_org', return_value=None):
        status, body = _request('/traceroute', TRACEROUTE_HEADERS)

    assert status == 503
    assert 'Hosts forbidden from route' in body['reason']


def test_traceroute_missing_required_gateway_fails_the_test():
    hops = [_hop(TARGET_IP, rtt=16.0, distance=1)]
    with patch('app.__main__.traceroute', return_value=hops), \
            patch('app.__main__.get_ip_org', return_value=None):
        status, body = _request('/traceroute', TRACEROUTE_HEADERS)

    assert status == 503
    assert 'Hosts missing from route' in body['reason']


def test_traceroute_last_hop_latency_below_minimum_fails():
    hops = [_hop(BACKUP_GATEWAY, distance=1), _hop(TARGET_IP, rtt=2.0, distance=2)]
    with patch('app.__main__.traceroute', return_value=hops), \
            patch('app.__main__.get_ip_org', return_value='AS12345 Example Transit'):
        status, body = _request('/traceroute', TRACEROUTE_HEADERS)

    assert status == 503
    assert 'Minimum RTT' in body['reason']


def test_traceroute_forbidden_org_fails_the_test():
    hops = [_hop(BACKUP_GATEWAY, distance=1), _hop(TARGET_IP, rtt=16.0, distance=2)]
    with patch('app.__main__.traceroute', return_value=hops), \
            patch('app.__main__.get_ip_org', side_effect=_org_for_ip):
        status, body = _request('/traceroute', TRACEROUTE_HEADERS)

    assert status == 503
    assert 'AS54321 Forbidden Carrier' in body['reason']


# --- helper functions -------------------------------------------------------

def test_split_to_set_parses_simple_csv():
    handler = _handler_with_headers({HEADER_ROUTE_INCLUDE_CSV: 'alpha,beta'})
    assert handler._split_to_set(HEADER_ROUTE_INCLUDE_CSV) == {'alpha', 'beta'}


def test_split_to_set_returns_empty_set_when_header_absent():
    handler = _handler_with_headers()
    assert handler._split_to_set(HEADER_ROUTE_INCLUDE_CSV) == set()


def test_check_latency_skips_enforcement_without_header():
    handler = _handler_with_headers()
    assert handler._check_latency(test_latency=1.0) is None


def test_check_latency_rejects_rtt_below_minimum():
    handler = _handler_with_headers({HEADER_MIN_LATENCY_MS: '10'})
    failure = handler._check_latency(test_latency=3.0)
    assert failure['result'] == 'error'
    assert failure['reason'] == 'Minimum RTT 3.0ms is less than minimum allowed 10.000ms.'


def test_check_latency_accepts_rtt_above_minimum():
    handler = _handler_with_headers({HEADER_MIN_LATENCY_MS: '10'})
    assert handler._check_latency(test_latency=15.0) is None


def test_included_net_skips_check_without_networks():
    handler = _handler_with_headers()
    assert handler._included_net(address=BACKUP_GATEWAY, networks=set()) is None


def test_included_net_matches_address_in_network():
    handler = _handler_with_headers()
    assert handler._included_net(
        address=BACKUP_GATEWAY, networks={'198.51.100.0/24'}) is True


def test_included_net_rejects_address_outside_network():
    handler = _handler_with_headers()
    assert handler._included_net(
        address=FIBRE_GATEWAY, networks={'198.51.100.0/24'}) is False


def test_included_org_matches_case_insensitively():
    handler = _handler_with_headers()
    assert handler._included_org(
        org='as12345 Example Transit', orgs={'AS12345'}) == 'AS12345'


def test_included_org_skips_check_without_orgs():
    handler = _handler_with_headers()
    assert handler._included_org(org='AS12345 Example Transit', orgs=set()) is None


def test_included_org_returns_none_when_no_org_matches():
    handler = _handler_with_headers()
    assert handler._included_org(
        org='AS12345 Example Transit', orgs={'AS54321'}) is None


# --- IPinfo enrichment ------------------------------------------------------

def test_get_ip_org_without_token_is_disabled(monkeypatch):
    monkeypatch.delenv(OS_ENV_IPINFO_TOKEN, raising=False)
    assert get_ip_org(ip=TARGET_IP) is None


def test_get_ip_org_returns_trimmed_org_name(monkeypatch):
    monkeypatch.setenv(OS_ENV_IPINFO_TOKEN, 'test-token')
    answer = SimpleNamespace(text='AS12345 Example Transit\n')
    with patch('app.__main__.requests.get', return_value=answer) as http_get:
        assert get_ip_org(ip=TARGET_IP) == 'AS12345 Example Transit'

    assert http_get.call_args.kwargs['timeout'] == 3
    assert http_get.call_args.kwargs['headers']['Authorization'] == 'Bearer test-token'


def test_get_ip_org_treats_empty_answer_as_unknown(monkeypatch):
    monkeypatch.setenv(OS_ENV_IPINFO_TOKEN, 'test-token')
    with patch('app.__main__.requests.get', return_value=SimpleNamespace(text='')):
        assert get_ip_org(ip=TARGET_IP) is None


def test_get_ip_org_suppresses_request_errors(monkeypatch):
    monkeypatch.setenv(OS_ENV_IPINFO_TOKEN, 'test-token')
    with patch('app.__main__.requests.get', side_effect=RequestException('boom')):
        assert get_ip_org(ip=TARGET_IP) is None


# --- documented defects (xfail) ---------------------------------------------
# `xfail_strict = true` in pyproject.toml: when one of these starts passing,
# delete the marker and update the regression table in `.clinerules/40-validation.md`.

@pytest.mark.xfail(reason='Known defect: unknown paths answer 200 (40-validation.md).')
def test_unknown_path_is_not_a_success():
    status, _ = _request('/nope', PING_HEADERS)
    assert 400 <= status < 500


@pytest.mark.xfail(reason='Known defect: missing Host is a 500 (40-validation.md).')
def test_missing_host_is_a_caller_error():
    with patch('app.__main__.ping', side_effect=NameLookupError('None')):
        status, body = _request('/ping', {HEADER_SOURCE: 'eth1'})

    assert status == 400
    assert body.get('error_code') == 'missing_host'


@pytest.mark.xfail(reason='Known defect: bad Source is a 500 (40-validation.md).')
def test_bad_source_interface_is_a_caller_error():
    headers = {HEADER_HOST: TARGET, HEADER_SOURCE: 'bogus0'}
    with patch('app.__main__.ping', side_effect=SocketAddressError('bogus0')):
        status, body = _request('/ping', headers)

    assert status == 400
    assert body.get('error_code') == 'bad_source'


@pytest.mark.xfail(reason='Known defect: DNS failure is a 500 (40-validation.md).')
def test_dns_failure_is_a_network_verdict():
    headers = {HEADER_HOST: 'nonexistent.invalid', HEADER_SOURCE: 'eth1'}
    with patch('app.__main__.ping', side_effect=NameLookupError('nonexistent.invalid')):
        status, body = _request('/ping', headers)

    assert status == 503
    assert body.get('error_code') == 'dns_failure'


@pytest.mark.xfail(reason='Known defect: CSV values are not stripped (40-validation.md).')
def test_split_to_set_strips_whitespace():
    handler = _handler_with_headers({HEADER_ROUTE_INCLUDE_CSV: 'alpha, beta'})
    assert handler._split_to_set(HEADER_ROUTE_INCLUDE_CSV) == {'alpha', 'beta'}


@pytest.mark.xfail(reason='Known defect: forbidden orgs are reported as hosts '
                          '(40-validation.md).')
def test_traceroute_forbidden_org_is_reported_as_an_org():
    hops = [_hop(BACKUP_GATEWAY, distance=1), _hop(TARGET_IP, rtt=16.0, distance=2)]
    with patch('app.__main__.traceroute', return_value=hops), \
            patch('app.__main__.get_ip_org', side_effect=_org_for_ip):
        status, body = _request('/traceroute', TRACEROUTE_HEADERS)

    assert status == 503
    assert 'Orgs forbidden from route' in body['reason']


@pytest.mark.xfail(reason='Known defect: an empty trace passes with no measurements '
                          '(40-validation.md).')
def test_traceroute_without_hops_does_not_pass():
    headers = {HEADER_HOST: TARGET, HEADER_SOURCE: 'eth1'}
    with patch('app.__main__.traceroute', return_value=[]):
        status, _ = _request('/traceroute', headers)

    assert status == 503


@pytest.mark.xfail(reason='Known defect: a failed trace drops the measurement key '
                          '(20-http-api-contract.md, 40-validation.md).')
def test_traceroute_failure_keeps_measurements():
    hops = [_hop(BACKUP_GATEWAY, distance=1), _hop(TARGET_IP, rtt=2.0, distance=2)]
    with patch('app.__main__.traceroute', return_value=hops), \
            patch('app.__main__.get_ip_org', return_value='AS12345 Example Transit'):
        status, body = _request('/traceroute', TRACEROUTE_HEADERS)

    assert status == 503
    assert body['traceroute'] == {'host': TARGET_IP, 'rtts': [2.0]}
