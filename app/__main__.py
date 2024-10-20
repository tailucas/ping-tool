#!/usr/bin/env python
import builtins
import logging.handlers
import simplejson as json
from typing import List

# setup builtins used by pylib init
from . import APP_NAME

builtins.SENTRY_EXTRAS = []


class CredsConfig:
    sentry_dsn: f'opitem:"Sentry" opfield:{APP_NAME}.dsn' = None  # type: ignore


# instantiate class
builtins.creds_config = CredsConfig()
from tailucas_pylib import app_config, log

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

    def do_GET(self):
        log.info(f'GET {self.path} - headers {self.headers.keys()}')
        response = {'result': 'OK'}
        try:
            host_name = self.headers.get('Host')
            match self.path:
                case '/ping':
                    source_ip = self.headers.get('SourceIP')
                    if source_ip:
                        log.info(f'Ping {host_name} from {source_ip}...')
                    else:
                        log.warning(f'Ping {host_name} without source IP...')
                    host: Host = ping(address=host_name, count=3, interval=1, source=source_ip, privileged=False)
                    log.info(f'{host_name} ({host.address}) is alive? {host.is_alive} ' \
                        f'with round-trips of {host.packets_sent} packets ' \
                        f'(min: {host.min_rtt}, avg: {host.avg_rtt}, max: {host.max_rtt}, jitter: {host.jitter}) ' \
                        f'and loss {host.packet_loss*100}%.')
                    response[self.path[1:]] = f'{host!s}'
                    min_latency_arg = self.headers.get('MinLatencyMs')
                    if min_latency_arg:
                        min_latency = float(min_latency_arg)
                        if host.min_rtt < min_latency:
                            error_reason = f'Minimum RTT {host.min_rtt}ms is less than minimum allowed {min_latency:.3f}ms.'
                            log.info(error_reason)
                            response = {'result': 'error', 'reason': error_reason}
                            self.send_response(500)
                        else:
                            log.info(f'Minimum RTT {host.min_rtt}ms exceeds allowed latency {min_latency:.3f}ms.')
                            self.send_response(200)
                    else:
                        log.warning(f'Not enforcing latency expectations.')
                        self.send_response(200)
                case '/traceroute':
                    log.info(f'Traceroute to {host_name}.')
                    hops: List[Hop] = traceroute(address=host_name, count=1)
                    hop: Hop
                    for hop in hops:
                        log.info(f'{hop!s}')
                case _:
                    self.send_response(200)
        except Exception as e:
            log.exception('Issue processing command.')
            self.send_response(500)
            response = {'result': 'error', 'reason': f'{e!s}'}
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        json_response = json.dumps(response)
        self.wfile.write(bytes(json_response, 'utf-8'))


def main():
    log.setLevel(logging.INFO)
    server_address = app_config.get('http', 'server_address')
    server_port = app_config.getint('http', 'server_port')
    log.info(f'Listening on {server_address}:{server_port}.')
    server = HTTPServer((server_address, server_port), WebRequestHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()