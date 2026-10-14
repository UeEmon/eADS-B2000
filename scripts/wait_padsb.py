"""Stand-in for an eNMEA2000/iPad input: wait for ADS-B sentences (CI / local check).

    python3 scripts/wait_padsb.py 10111 5                 # $PADSB over TCP (eNMEA2000)
    python3 scripts/wait_padsb.py 10110 5 udp             # $PADSB over UDP (iPad)
"""
import socket
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 10111
need = int(sys.argv[2]) if len(sys.argv) > 2 else 5
protocol = sys.argv[3] if len(sys.argv) > 3 else 'tcp'
prefix = sys.argv[4] if len(sys.argv) > 4 else '$PADSB,'


def tcp_lines():
    with socket.create_server(('0.0.0.0', port)) as server:
        server.settimeout(120)
        conn, peer = server.accept()
        conn.settimeout(60)
        for line in conn.makefile('r'):
            yield line, peer[0]
    sys.exit('connection closed')


def udp_lines():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('0.0.0.0', port))
        sock.settimeout(120)
        while True:
            data, peer = sock.recvfrom(4096)
            for line in data.decode(errors='replace').splitlines():
                yield line, peer[0]


got = 0
for line, peer in (udp_lines() if protocol == 'udp' else tcp_lines()):
    if line.startswith(prefix):
        got += 1
        print(line.strip())
        if got >= need:
            break
print(f'OK: {got} {prefix.rstrip(",")} sentences over {protocol.upper()} from {peer}')
