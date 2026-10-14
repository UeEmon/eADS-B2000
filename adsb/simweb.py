"""Web page and JSON API for the ADS-B simulator (python -m adsb.simulator --web 8091).

Same trust model as eNMEA2000's NMEA emulator: no login, bound to localhost by
the compose file. Writes require a JSON body, so a page on another origin
cannot change the fleet without a CORS preflight, which this server never allows.
Writes also require an Origin (when sent) matching the Host, and a Host that is
localhost or an IP address, which defeats DNS rebinding.

    GET  /                    settings page
    GET  /api/aircraft        current (dead-reckoned) state of every aircraft
    PUT  /api/aircraft        replace the whole fleet: {"aircraft": [{icao, callsign, lat, lon,
                              altitude_ft, speed_kt, track_deg, vertical_rate_fpm, category}, ...]}
    POST /api/aircraft/random {"count": 20, "lat": 35.55, "lon": 139.78, "radius_nm": 60}
"""
from __future__ import annotations

import ipaddress
import json
import threading
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).with_name('static')
MAX_BODY = 1 << 20


def _host_ok(host: str) -> bool:
    name = urlsplit('//' + host).hostname or ''
    if name == 'localhost':
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def write_allowed(headers) -> bool:
    host = headers.get('Host', '')
    if not _host_ok(host):
        return False
    origin = headers.get('Origin')
    return origin is None or urlsplit(origin).netloc == host


def make_handler(sim):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'eADS-B2000-sim'

        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data):
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

        def _body(self):
            if not write_allowed(self.headers):
                raise PermissionError('origin or host not allowed')
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                raise ValueError('Content-Type must be application/json')
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 < length <= MAX_BODY:
                raise ValueError('body missing or too large')
            return json.loads(self.rfile.read(length))

        def do_GET(self):
            if self.path in ('/', '/index.html'):
                self._send(200, (STATIC / 'sim.html').read_bytes(), 'text/html; charset=utf-8')
            elif self.path == '/api/aircraft':
                self._json(200, sim.snapshot())
            elif self.path == '/health':
                self._json(200, {'ok': True})
            else:
                self._json(404, {'error': 'not found'})

        def do_PUT(self):
            if self.path != '/api/aircraft':
                return self._json(404, {'error': 'not found'})
            try:
                body = self._body()
                count = sim.replace(body.get('aircraft') if isinstance(body, dict) else body)
            except PermissionError as e:
                return self._json(403, {'error': str(e)})
            except ValueError as e:      # json.JSONDecodeError is a ValueError
                return self._json(400, {'error': str(e)})
            self._json(200, {'count': count})

        def do_POST(self):
            if self.path != '/api/aircraft/random':
                return self._json(404, {'error': 'not found'})
            try:
                body = self._body()
                if not isinstance(body, dict):
                    raise ValueError('object required')
                count = sim.randomize(int(body.get('count', 20)),
                                      (float(body.get('lat', sim.centre[0])), float(body.get('lon', sim.centre[1]))),
                                      float(body.get('radius_nm', sim.radius)), body.get('seed'))
            except PermissionError as e:
                return self._json(403, {'error': str(e)})
            except (TypeError, ValueError) as e:
                return self._json(400, {'error': str(e)})
            self._json(200, {'count': count})

    return Handler


def serve(sim, port: int, host: str = '0.0.0.0') -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(sim))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
