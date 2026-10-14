import json
import urllib.error
import urllib.request
import pytest
from adsb import simulator, simweb

CENTRE = (35.5494, 139.7798)
ROW = {'icao': '86d3a1', 'callsign': 'jal123', 'lat': 35.5, 'lon': 139.8, 'altitude_ft': 12000,
       'speed_kt': 360, 'track_deg': 90, 'vertical_rate_fpm': 0, 'category': 3}


@pytest.fixture
def web(tmp_path):
    sim = simulator.Simulation(simulator.fleet(2, CENTRE, 40, seed=1), CENTRE, 40, str(tmp_path / 'fleet.json'))
    httpd = simweb.serve(sim, 0, '127.0.0.1')
    base = f'http://127.0.0.1:{httpd.server_address[1]}'

    def call(method, path, body=None, ctype='application/json', origin=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        headers = {'Content-Type': ctype} if data is not None else {}
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read()) if 'json' in r.headers['Content-Type'] else r.read()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield sim, call
    httpd.shutdown()


def test_bulk_replace_and_dead_reckoning(web, tmp_path):
    sim, call = web
    second = dict(ROW, icao='840001', callsign='ANA1', track_deg=0, vertical_rate_fpm=1200)
    assert call('PUT', '/api/aircraft', {'aircraft': [ROW, second]}) == (200, {'count': 2})
    status, data = call('GET', '/api/aircraft')
    assert status == 200 and [a['icao'] for a in data['aircraft']] == ['86D3A1', '840001']
    assert data['aircraft'][0]['callsign'] == 'JAL123' and data['aircraft'][0]['bounded'] is False
    sim.tick(60)                         # one minute later
    east, north = sim.snapshot()['aircraft']
    assert east['lat'] == pytest.approx(35.5, abs=0.01)
    assert simulator.distance_nm((35.5, 139.8), (east['lat'], east['lon'])) == pytest.approx(6.0, abs=0.01)
    assert north['lat'] == pytest.approx(35.6, abs=0.001) and north['altitude_ft'] == 13200
    saved = json.loads((tmp_path / 'fleet.json').read_text())
    assert len(saved['aircraft']) == 2
    reloaded = simulator.Simulation([], CENTRE, 40, str(tmp_path / 'fleet.json'))
    assert reloaded.load() and [a.callsign for a in reloaded.planes] == ['JAL123', 'ANA1']


def test_unbounded_aircraft_leave_the_radius():
    sim = simulator.Simulation([simulator.aircraft_from_dict(ROW)], CENTRE, 5)
    for _ in range(120):
        sim.tick(5)                      # 10 minutes at 360 kt = 60 NM east
    ac = sim.snapshot()['aircraft'][0]
    assert ac['track_deg'] == 90 and simulator.distance_nm(CENTRE, (ac['lat'], ac['lon'])) > 50


@pytest.mark.parametrize('bad', [dict(ROW, icao='XYZ123'), dict(ROW, lat=91), dict(ROW, speed_kt='fast'),
                                 dict(ROW, callsign='TOO-LONG1'), dict(ROW, altitude_ft=60000)])
def test_rejects_invalid_rows(web, bad):
    sim, call = web
    before = sim.snapshot()['aircraft']
    status, data = call('PUT', '/api/aircraft', {'aircraft': [bad]})
    assert status == 400 and data['error'] and sim.snapshot()['aircraft'] == before


def test_rejects_duplicates_and_non_json(web):
    _, call = web
    assert call('PUT', '/api/aircraft', {'aircraft': [ROW, ROW]})[0] == 400
    assert call('PUT', '/api/aircraft', b'{"aircraft": []}', 'text/plain')[0] == 400


def test_random_fleet_and_page(web):
    sim, call = web
    assert call('POST', '/api/aircraft/random', {'count': 7, 'lat': 34.0, 'lon': 135.0, 'radius_nm': 30}) == (200, {'count': 7})
    data = sim.snapshot()
    assert len(data['aircraft']) == 7 and data['centre'] == {'lat': 34.0, 'lon': 135.0}
    assert all(a['bounded'] for a in data['aircraft'])
    assert call('POST', '/api/aircraft/random', {'count': 9999})[0] == 400
    status, page = call('GET', '/')
    assert status == 200 and 'ADS-B'.encode() in page


def test_frames_carry_the_set_values():
    from adsb import receiver
    sim = simulator.Simulation([simulator.aircraft_from_dict(dict(ROW, category=1))], CENTRE, 40)
    rx = receiver.Receiver()
    for _ in range(3):
        for f in sim.tick(0.5):
            rx.handle(f)
    state = rx.tracker.aircraft['86D3A1']
    assert state['callsign'] == 'JAL123' and state['altitude_ft'] == 12000
    assert state['ground_speed_kt'] == pytest.approx(360, abs=1)


def test_writes_reject_other_origins_and_rebinding(web):
    sim, call = web
    from adsb.simweb import write_allowed
    assert write_allowed({'Host': '127.0.0.1:8091'})
    assert write_allowed({'Host': 'localhost:8091', 'Origin': 'http://localhost:8091'})
    assert write_allowed({'Host': '192.168.1.5:8091', 'Origin': 'http://192.168.1.5:8091'})
    assert not write_allowed({'Host': 'localhost:8091', 'Origin': 'http://evil.example'})
    assert not write_allowed({'Host': 'evil.example:8091'})
    before = sim.snapshot()['aircraft']
    status, data = call('PUT', '/api/aircraft', {'aircraft': [ROW]}, origin='http://evil.example')
    assert status == 403 and sim.snapshot()['aircraft'] == before
