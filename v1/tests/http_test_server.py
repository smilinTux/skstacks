"""Loopback HTTP servers for tests that can never hang the run.

The stdlib pattern ``HTTPServer(...)`` + ``serve_forever`` in a thread +
``shutdown()`` has two ways to block forever:

* The listening socket is blocking. When ``select()`` reports a pending
  connection and the client resets it before ``accept()`` runs, the kernel
  drops it from the queue and ``accept()`` waits for the next client. The
  serve loop never gets back to its shutdown check, so ``shutdown()`` waits
  forever too.
* Accepted sockets are blocking. A client that connects and sends nothing
  (or half a request) parks the handler in ``readline()``; with the
  single-threaded server that also stalls the loop.

These subclasses put a timeout on both sockets, and ``stop()`` bounds the
shutdown wait, so a misbehaving client costs at most a few seconds and a
failed stop raises instead of hanging.
"""

from __future__ import annotations

import http.server
import threading

ACCEPT_TIMEOUT = 0.5  # seconds accept() may wait before the loop rechecks shutdown
IO_TIMEOUT = 5.0  # seconds a request socket may sit idle
STOP_TIMEOUT = 10.0  # seconds stop() waits for the serve loop to exit


class _Bounded:
    def server_bind(self):
        super().server_bind()
        self.socket.settimeout(ACCEPT_TIMEOUT)

    def get_request(self):
        conn, addr = super().get_request()
        conn.settimeout(IO_TIMEOUT)
        return conn, addr


class HTTPServer(_Bounded, http.server.HTTPServer):
    pass


class ThreadingHTTPServer(_Bounded, http.server.ThreadingHTTPServer):
    pass


def start(server):
    """Serve ``server`` in a daemon thread; returns the server."""
    server._test_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server._test_thread.start()
    return server


def stop(server, timeout: float = STOP_TIMEOUT) -> None:
    """Shut ``server`` down and close it, raising if that takes over ``timeout``."""
    stopper = threading.Thread(target=server.shutdown, daemon=True)
    stopper.start()
    stopper.join(timeout)
    server.server_close()
    worker = getattr(server, "_test_thread", None)
    if worker is not None:
        worker.join(timeout)
    if stopper.is_alive() or (worker is not None and worker.is_alive()):
        raise AssertionError(f"test HTTP server did not stop within {timeout}s")
