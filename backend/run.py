"""
Dual-stack (IPv4 + IPv6) launcher for the order-song backend.

Usage (from repo root):
    python backend/run.py
    python backend/run.py --port=8000

Replaces the old command:
    python -m uvicorn app.main:app --reload --host :: --port 5732 --app-dir backend

Creates an IPv6 dual-stack socket (IPV6_V6ONLY=0) to accept both IPv4
and IPv6 connections on the same port. If dual-stack is unavailable on
the OS, falls back to 0.0.0.0 (IPv4 only).
"""
import asyncio
import os
import signal
import socket
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

import uvicorn

from app.core.config import settings


def make_socket(port: int) -> socket.socket:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
        sock.listen(511)
        sock.setblocking(False)
        print(f"  Dual-stack socket on [::]:{port}  (IPv4 + IPv6)")
        return sock
    except (AttributeError, OSError) as exc:
        try:
            sock.close()
        except Exception:
            pass
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.listen(511)
        sock.setblocking(False)
        print(f"  Dual-stack unavailable ({exc}), IPv4-only on 0.0.0.0:{port}")
        return sock


def main():
    port = settings.server.port
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])

    sock = make_socket(port)

    config = uvicorn.Config("app.main:app", log_level="info")
    server = uvicorn.Server(config)

    asyncio.run(server.serve(sockets=[sock]))


if __name__ == "__main__":
    main()
