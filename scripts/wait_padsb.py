"""Stand-in for the eNMEA2000 TCP input: wait for $PADSB lines (CI / local check)."""
import socket
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 10111
need = int(sys.argv[2]) if len(sys.argv) > 2 else 5
with socket.create_server(('0.0.0.0', port)) as server:
    server.settimeout(120)
    conn, peer = server.accept()
    conn.settimeout(60)
    lines = conn.makefile('r')
    got = 0
    while got < need:
        line = lines.readline()
        if not line:
            sys.exit('connection closed')
        if line.startswith('$PADSB,'):
            got += 1
            print(line.strip())
print(f'OK: {got} $PADSB sentences from {peer[0]}')
