"""Minimal SOCKS5 proxy (no auth, CONNECT only) for tests. Each container running it has its own source address."""
import socket, struct, threading

def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except OSError: pass

def handle(c):
    try:
        c.recv(262); c.sendall(b"\x05\x00")
        _, _, _, atyp = c.recv(4)
        if atyp == 1: addr = socket.inet_ntoa(c.recv(4))
        elif atyp == 3: addr = c.recv(c.recv(1)[0]).decode()
        else: c.close(); return
        port = struct.unpack("!H", c.recv(2))[0]
        r = socket.create_connection((addr, port), timeout=5)
        c.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        threading.Thread(target=pipe, args=(r, c), daemon=True).start()
        pipe(c, r)
    except Exception:
        c.close()

srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 1080)); srv.listen(64)
while True:
    conn, _ = srv.accept(); threading.Thread(target=handle, args=(conn,), daemon=True).start()
