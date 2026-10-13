import socket
import threading
import time
import numpy as np
import pytest
from adsb import modes, ppm, rtltcp, sdrtx
from adsb.receiver import demodulated

FRAMES = [modes.encode_position(0x840000 + i, 35 + i * .01, 139.5, 10000 + i * 100, i % 2 == 1) for i in range(40)]


@pytest.mark.parametrize('chunk', [4097, 65536, 333333])
def test_stream_demodulator_matches_whole_file(chunk):
    raw = ppm.cu8(ppm.waveform(FRAMES, gap=137), noise=0.03, rng=np.random.default_rng(3))
    assert ppm.demodulate(raw) == FRAMES
    blocks = [raw[i:i + chunk] for i in range(0, len(raw), chunk)]
    assert list(demodulated(blocks)) == FRAMES     # odd chunk sizes split I/Q pairs


def test_sdrtx_rtltcp_loopback():
    tx = sdrtx.Transmitter(noise=0.02)
    server = sdrtx.serve(tx, 0, '127.0.0.1')
    port = server.server_address[1]
    got = []
    def receive():
        for frame in demodulated(rtltcp.stream(('127.0.0.1', port), ppm.RATE)):
            got.append(frame)
            if len(got) >= len(FRAMES):
                return
    t = threading.Thread(target=receive, daemon=True)
    t.start()
    for _ in range(100):
        if tx.client:
            break
        time.sleep(.02)
    assert tx.client
    for frame in FRAMES:
        tx.ingest(frame)
    t.join(10)
    server.shutdown()
    assert sorted(got) == sorted(FRAMES)
    assert tx.counts['modulated'] == len(FRAMES)


def test_sdrtx_drops_without_client():
    tx = sdrtx.Transmitter()
    tx.ingest(FRAMES[0])
    assert tx.counts['dropped'] == 1 and tx.frames.empty()


class Stop(Exception):
    pass


def test_rtltcp_rejects_non_rtl_server(monkeypatch, capsys):
    srv = socket.create_server(('127.0.0.1', 0))
    def answer():
        c, _ = srv.accept()
        c.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
        c.close()
    threading.Thread(target=answer, daemon=True).start()
    def stop(_):
        raise Stop
    monkeypatch.setattr(rtltcp.time, 'sleep', stop)
    with pytest.raises(Stop):
        next(rtltcp.stream(srv.getsockname(), ppm.RATE))
    assert 'not an rtl_tcp server' in capsys.readouterr().err
