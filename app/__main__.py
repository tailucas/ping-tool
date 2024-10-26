#!/usr/bin/env python
import json
import logging
import logging.handlers
import ipaddress
import os
import signal
import socket
import sys
from threading import Thread
from typing import List

from functools import cached_property
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlparse

from icmplib import Host, Hop
from icmplib import ping, multiping, traceroute, resolve
from icmplib import ICMPLibError, NameLookupError, ICMPSocketError
from icmplib import SocketAddressError, SocketPermissionError
from icmplib import SocketUnavailableError, SocketBroadcastError, TimeoutExceeded
from icmplib import ICMPError, DestinationUnreachable, TimeExceeded


from . import APP_NAME
log = logging.getLogger(APP_NAME)
log.setLevel(logging.INFO)
log_handler = None
syslog_server = None
try:
    syslog_address = os.environ["SYSLOG_ADDRESS"]
    syslog_server = urlparse(syslog_address)
except KeyError:
    pass
if syslog_server and len(syslog_server.netloc) > 0:
    protocol = None
    if syslog_server.scheme == 'udp':
        protocol = socket.SOCK_DGRAM
    log_handler = logging.handlers.SysLogHandler(address=(syslog_server.hostname, syslog_server.port), socktype=protocol)
elif os.path.exists("/dev/log"):
    log_handler = logging.handlers.SysLogHandler(address="/dev/log")
elif sys.stdout.isatty() or "SUPERVISOR_ENABLED" in os.environ:
    log_handler = logging.StreamHandler(stream=sys.stdout)
if log_handler:
    # define the log format
    formatter = logging.Formatter("%(name)s %(threadName)s [%(levelname)s] %(message)s")
    log_handler.setFormatter(formatter)
    log.addHandler(log_handler)


HEADER_HOST = 'Host'
HEADER_SOURCE = 'Source'
HEADER_MIN_LATENCY_MS = 'MinLatencyMs'
HEADER_ROUTE_INCLUDE_CSV = 'RouteIncludeCsv'
HEADER_ROUTE_EXCLUDE_CSV = 'RouteExcludeCsv'


class WebRequestHandler(BaseHTTPRequestHandler):
    @cached_property
    def url(self):
        return urlparse(self.path)

    @cached_property
    def query_data(self):
        return dict(parse_qsl(self.url.query))

    @cached_property
    def post_data(self):
        content_length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(content_length)

    @cached_property
    def form_data(self):
        return dict(parse_qsl(self.post_data.decode("utf-8")))

    @cached_property
    def cookies(self):
        return SimpleCookie(self.headers.get("Cookie"))

    def _split_to_set(self, header_name):
        my_set = set()
        header_csv = self.headers.get(header_name)
        if header_csv:
            for h in header_csv.split(','):
                my_set.add(h)
        return my_set

    def _check_latency(self, test_latency):
        min_latency_arg = self.headers.get(HEADER_MIN_LATENCY_MS)
        if min_latency_arg:
            min_latency = float(min_latency_arg)
            if test_latency < min_latency:
                error_reason = f'Minimum RTT {test_latency}ms is less than minimum allowed {min_latency:.3f}ms.'
                log.info(error_reason)
                return {'result': 'error', 'reason': error_reason}
            else:
                log.info(f'Minimum RTT {test_latency}ms exceeds allowed latency {min_latency:.3f}ms.')
        else:
            log.warning(f'Not enforcing latency expectations due to missing header {HEADER_MIN_LATENCY_MS}.')
        return None

    def _included(self, address, networks: set):
        if len(networks) == 0:
            log.warning(f'Skipping inclusion test for {address}.')
            return None
        for n in networks:
            if ipaddress.ip_address(address) in ipaddress.ip_network(n):
                log.info(f'{address} is in {networks!s}.')
                return True
        log.info(f'{address} is missing from {networks!s}.')
        return False

    def do_GET(self):
        self.protocol_version = 'HTTP/1.0'
        log.info(f'GET {self.path} - headers {self.headers.keys()}')
        response = {'result': 'OK'}
        try:
            host_name = self.headers.get(HEADER_HOST)
            source = self.headers.get(HEADER_SOURCE)
            route_must_include = self._split_to_set(HEADER_ROUTE_INCLUDE_CSV)
            route_must_exclude = self._split_to_set(HEADER_ROUTE_EXCLUDE_CSV)
            test_ok = True
            match self.path:
                case '/ping':
                    host: Host = ping(address=host_name, count=3, interval=1, source=source, privileged=False)
                    if source:
                        log.info(f'Ping {host_name} from {source}...')
                    else:
                        log.warning(f'Ping {host_name} (no source address header {HEADER_SOURCE} specified)...')
                    log.info(f'{host_name} ({host.address}) is alive? {host.is_alive} ' \
                        f'with round-trips of {host.packets_sent} packets ' \
                        f'(min: {host.min_rtt}, avg: {host.avg_rtt}, max: {host.max_rtt}, jitter: {host.jitter}) ' \
                        f'and loss {host.packet_loss*100}%.')
                    response[self.path[1:]] = {'host': host.address, 'rtts': host.rtts}
                    failure = self._check_latency(test_latency=host.min_rtt)
                    if failure:
                        response = failure
                        test_ok = False
                case '/traceroute':
                    log.info(f'Traceroute to {host_name}...')
                    hops: List[Hop] = traceroute(address=host_name, count=1, source=source)
                    hop_count = 0
                    missing_hosts: set = route_must_include.copy()
                    forbidden_hosts = []
                    hop: Hop = None
                    for hop in hops:
                        hop_count += 1
                        log.info(f'Hop {hop_count} to {hop.address} is alive? {hop.is_alive} ' \
                            f'with round-trips of {hop.packets_sent} packets ' \
                            f'(min: {hop.min_rtt}, avg: {hop.avg_rtt}, max: {hop.max_rtt}, jitter: {hop.jitter}) ' \
                            f'and loss {hop.packet_loss*100}%.')
                        if self._included(address=hop.address, networks=route_must_include):
                            missing_hosts.remove(hop.address)
                        if self._included(address=hop.address, networks=route_must_exclude):
                            forbidden_hosts.append(hop.address)
                    error_reason = ''
                    if len(missing_hosts) > 0:
                        error_reason += f'Hosts missing from route: {missing_hosts!s}. '
                    if len(forbidden_hosts) > 0:
                        error_reason += f'Hosts forbidden from route: {forbidden_hosts!s}. '
                    if hop:
                        response[self.path[1:]] = {'host': hop.address, 'rtts': hop.rtts}
                        failure = self._check_latency(test_latency=hop.min_rtt)
                        if failure:
                            error_reason += failure['reason']
                    if len(error_reason) > 0:
                        test_ok = False
                        log.info(error_reason)
                        response = {'result': 'error', 'reason': error_reason.rstrip()}
                case _:
                    log.warning(f'No test selected from path {self.path}')
        except Exception as e:
            log.exception('Issue processing command.')
            response = {'result': 'error', 'reason': f'{e!s}'}
            test_ok = False
        if test_ok:
            self.send_response(200)
        else:
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        json_response = json.dumps(response)
        wire_response = bytes(json_response, 'utf-8')
        self.send_header("Content-length", len(wire_response))
        self.end_headers()
        self.wfile.write(wire_response)


def run_server(address, port):
    global server
    server = HTTPServer((address, port), WebRequestHandler)
    server.serve_forever()


server: HTTPServer = None
def handler(signum, frame):
    global server
    log.info(f'Signal {signum} received.')
    if server:
        server.shutdown()


def main():
    signal.signal(signal.SIGTERM, handler)
    server_address = os.environ['SERVER_ADDRESS']
    server_port = int(os.environ['SERVER_PORT'])
    log.info(f'Listening on {server_address}:{server_port}.')
    thread = Thread(target=run_server, name='Server', args=(server_address, server_port))
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    log.info('Shutting down...')


if __name__ == "__main__":
    main()