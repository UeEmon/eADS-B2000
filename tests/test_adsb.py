import itertools
import socket
import threading
import pytest
from pyais import decode as decode_ais
from adsb import mapping, modes, ppm, receiver, simulator
from adsb.tracker import Tracker

# Reference frames from "The 1090MHz Riddle" (Junzi Sun)
IDENT = '8D4840D6202CC371C32CE0576098'
POS_EVEN = '8D40621D58C382D690C8AC2863A7'
POS_ODD = '8D40621D58C386435CC412692AD6'
VEL = '8D485020994409940838175B284F'


def test_known_frames():
    assert modes.decode(IDENT)['callsign'] == 'KLM1023'
    even, odd = modes.decode(POS_EVEN), modes.decode(POS_ODD)
    assert even['altitude_ft'] == 38000 and not even['odd'] and odd['odd']
    lat, lon = modes.cpr_global(even['cpr'], odd['cpr'], odd_newest=False)
    assert lat == pytest.approx(52.2572, abs=1e-4) and lon == pytest.approx(3.91937, abs=1e-4)
    v = modes.decode(VEL)
    assert v['ground_speed_kt'] == pytest.approx(159.2, abs=0.1)
    assert v['track_deg'] == pytest.approx(182.88, abs=0.01)
    assert v['vertical_rate_fpm'] == -832


def test_crc_rejects_bit_error():
    bad = bytearray.fromhex(IDENT)
    bad[6] ^= 0x10
    with pytest.raises(ValueError, match='CRC'):
        modes.decode(bytes(bad))


def test_nl_equator():
    assert modes.nl(0) == 59 and modes.nl(87) == 1 and modes.nl(52.2572) == 36


@pytest.mark.parametrize('lat,lon', [(35.5494, 139.7798), (-33.9, 151.2), (0.1, -0.1), (64.1, -21.9)])
def test_encode_round_trip(lat, lon):
    icao = 0x86D3A1
    e = modes.decode(modes.encode_position(icao, lat, lon, 12500, False))
    o = modes.decode(modes.encode_position(icao, lat, lon, 12500, True))
    assert e['altitude_ft'] == 12500 and e['icao'] == '86D3A1'
    got = modes.cpr_global(e['cpr'], o['cpr'], odd_newest=True)
    assert got == pytest.approx((lat, lon), abs=1e-4)
    assert modes.cpr_local(*e['cpr'], False, lat + 0.5, lon - 0.5) == pytest.approx((lat, lon), abs=1e-4)
    assert modes.decode(modes.encode_identification(icao, 'JAL123'))['callsign'] == 'JAL123'
    v = modes.decode(modes.encode_velocity(icao, 420, 287, -1500))
    assert v['ground_speed_kt'] == pytest.approx(420, abs=1) and v['track_deg'] == pytest.approx(287, abs=0.2)
    assert v['vertical_rate_fpm'] == pytest.approx(-1500, abs=64)


def test_ppm_round_trip_with_noise():
    frames = [IDENT, POS_EVEN, POS_ODD, VEL]
    raw = ppm.cu8(ppm.waveform(frames, offset_hz=20000), noise=0.03, rng=__import__('numpy').random.default_rng(1))
    assert ppm.demodulate(raw) == frames


def test_simulator_to_tracker_and_ais_bridge():
    planes = simulator.fleet(3, (35.5494, 139.7798), 40, seed=7)
    rx = receiver.Receiver()
    for _ in range(4):
        for ac in planes:
            ac.step(0.5, (35.5494, 139.7798), 40)
            for f in ac.frames():
                row = rx.handle(f)
                assert row['sentence_type'] == 'ADSB' and row['status'] == 'ok'
    assert rx.stats['errors'] == 0
    for ac in planes:
        state = rx.tracker.aircraft[f'{ac.icao:06X}']
        assert state['callsign'] == ac.callsign
        assert (state['lat'], state['lon']) == pytest.approx((ac.lat, ac.lon), abs=0.01)
        sentence = mapping.ais_type9(state, second=12)
        msg = decode_ais(sentence).asdict()
        assert msg['msg_type'] == 9 and msg['mmsi'] == mapping.pseudo_mmsi(state['icao'])
        assert msg['lat'] == pytest.approx(state['lat'], abs=1e-4)
        assert msg['speed'] == round(state['ground_speed_kt'])


@pytest.mark.parametrize('fmt,prefix', [('padsb', '$PADSB,840123,,35.0000'),
                                        ('ais9', '!AIVDM,1,1,,A,')])
def test_receiver_forwards_to_enmea_tcp(fmt, prefix):
    server = socket.create_server(('127.0.0.1', 0))
    got = []
    def accept():
        conn, _ = server.accept()
        got.append(conn.makefile().readline())
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    rx = receiver.Receiver(forward=server.getsockname(), fmt=fmt)
    icao = 0x840123
    for f in (modes.encode_position(icao, 35.0, 139.0, 5000, False),
              modes.encode_position(icao, 35.0, 139.0, 5000, True)):
        rx.handle(f)
    t.join(5)
    assert got and got[0].startswith(prefix)


def test_padsb_sentence():
    ac = {'icao': '86D3A1', 'callsign': 'JAL123', 'lat': 35.5494, 'lon': 139.7798, 'altitude_ft': 12500,
          'ground_speed_kt': 420.04, 'track_deg': 287.0, 'vertical_rate_fpm': -1472, 'category': 3}
    sentence = mapping.padsb(ac)
    assert sentence == '$PADSB,86D3A1,JAL123,35.549400,139.779800,12500,420.0,287.0,-1472,3*' + sentence[-2:]
    body = sentence[1:sentence.index('*')]
    cs = 0
    for c in body:
        cs ^= ord(c)
    assert sentence.endswith(f'*{cs:02X}')
    assert mapping.padsb({'icao': '86D3A1'}).startswith('$PADSB,86D3A1,,,,,,,,*')


def test_avr_parsing():
    lines = [f'*{IDENT};', '@0123456789AB' + POS_EVEN + ';', 'garbage']
    assert list(receiver.frames_from_avr(lines)) == [IDENT, POS_EVEN]


def test_ollama_assist_request(tmp_path):
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from adsb import ollama_assist
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen['path'] = self.path
            seen['body'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            data = json.dumps({'message': {'role': 'assistant', 'content': 'ok'}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.handle_request, daemon=True).start()
    src = tmp_path / 'x.py'
    src.write_text('print(1)')
    host = f'http://127.0.0.1:{httpd.server_port}'
    assert ollama_assist.chat('review', 'gpt-oss:20b', [str(src)], host) == 'ok'
    assert seen['path'] == '/api/chat' and seen['body']['model'] == 'gpt-oss:20b'
    assert 'print(1)' in seen['body']['messages'][1]['content'] and seen['body']['stream'] is False


def test_parse_outputs():
    outs = receiver.parse_outputs('tcp://enmea:10111, udp://192.168.1.30:10110/ais9,ipad.local:10111', 'padsb')
    assert [repr(o) for o in outs] == ['tcp://enmea:10111/padsb', 'udp://192.168.1.30:10110/ais9',
                                       'tcp://ipad.local:10111/padsb']
    for bad in ('ftp://h:1', 'tcp://h:1/nmea', 'tcp://h', 'udp://:10110', 'tcp://h:0'):
        with pytest.raises(ValueError):
            receiver.parse_outputs(bad)


def test_receiver_sends_to_several_outputs():
    """eNMEA2000 gets $PADSB over TCP, an iPad gets AIS Type 9 over UDP, a dead TCP target blocks neither."""
    server = socket.create_server(('127.0.0.1', 0))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(('127.0.0.1', 0))
    udp.settimeout(5)
    dead = socket.create_server(('127.0.0.1', 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    got = []
    def accept():
        conn, _ = server.accept()
        got.append(conn.makefile().readline())
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    rx = receiver.Receiver(outputs=receiver.parse_outputs(
        f'tcp://127.0.0.1:{dead_port},tcp://127.0.0.1:{server.getsockname()[1]},'
        f'udp://127.0.0.1:{udp.getsockname()[1]}/ais9'))
    icao = 0x840123
    for f in (modes.encode_position(icao, 35.0, 139.0, 5000, False),
              modes.encode_position(icao, 35.0, 139.0, 5000, True)):
        rx.handle(f)
    t.join(5)
    assert got and got[0].startswith('$PADSB,840123,')
    assert udp.recv(1024).startswith(b'!AIVDM,1,1,,A,')
    assert rx.outputs[0].stats['failures'] == 1 and rx.outputs[1].stats['sent'] == 1
