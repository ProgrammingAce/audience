#!/usr/bin/env python3
#
# Copyright (C) 2026
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""audience state server — a tiny read-only HTTP face for remote mirrors.

The curses app keeps everything it shows on screen in ``Audience.log``. This
module exposes a snapshot of that log over HTTP so a remote agent (e.g. a
Raspberry Pi with an e-ink display) can mirror what's on the host screen.

It is purely additive: ``GET /state`` and ``GET /health`` are read-only, and
``POST /command`` accepts JSON ``{"text": ...}`` — a command the remote captured
by voice and transcribed on-device — and enqueues it as a question turn via
``app.submit_voice``. The model answers it as if the operator had typed it;
there is no audio or speech-to-text on the host.

Everything uses the standard library only, matching the rest of the app.
"""

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Module-level mutable state for token auth (set by start_server).
_serve_token = None
_warned_unauthenticated = False


def set_serve_token(token):
    """Configure the auth token; log a one-time warning if token is None (unauthenticated)."""
    global _serve_token, _warned_unauthenticated
    _serve_token = token
    if token is None and not _warned_unauthenticated:
        print(
            "[warning] state server is running without authentication; "
            "set AUDIENCE_TOKEN or --serve-token to enable auth",
            flush=True,
        )
        _warned_unauthenticated = True


def _check_auth(handler):
    """Return True if the request is authenticated (token configured and header present)."""
    if _serve_token is None:
        return True
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == _serve_token
    return auth == _serve_token


def _snapshot_log(app):
    """Copy the host log under its lock into a JSON-friendly list.

    Mirrors the snapshot taken at the top of ``Audience.render``: grab the lock
    briefly, copy out, then work from the copy. ``transient`` status hints are
    included — the remote shows whatever the operator currently sees.
    """
    with app.log_lock:
        entries = list(app.log)
    return [{"style": e[0], "text": e[1]} for e in entries]


def make_handler(app):
    """Build a request handler bound to a running ``Audience`` instance."""

    class StateHandler(BaseHTTPRequestHandler):
        # Quiet by default; the curses UI owns the terminal and stray logging
        # would corrupt the display.
        def log_message(self, fmt, *args):
            pass

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


        def _require_auth(self):
            """Return False and send 401 if auth is required and missing."""
            if not _check_auth(self):
                self._send_json({"error": "unauthorized"}, status=401)
                return False
            return True

        def do_GET(self):
            if not self._require_auth():
                return
            if self.path == "/health":
                self._send_json({"ok": True})
            elif self.path == "/state":
                self._send_json({
                    "log": _snapshot_log(app),
                    "ts": time.time(),
                    "host": socket.gethostname(),
                    # True while the worker is mid-reply, so a remote can wait
                    # for a command's answer to fully complete before redrawing.
                    "busy": bool(getattr(app, "generating", False))
                            or not app.jobs.empty(),
                })
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self):
            if not self._require_auth():
                return
            if self.path != "/command":
                self._send_json({"error": "not found"}, status=404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length else b""
            try:
                text = json.loads(raw.decode("utf-8")).get("text", "")
            except (ValueError, UnicodeDecodeError, AttributeError):
                self._send_json({"error": "bad request"}, status=400)
                return
            text = (text or "").strip()
            if not text:
                self._send_json({"error": "empty command"}, status=400)
                return
            # Feed the transcribed command to the worker as a question turn and
            # surface it on the host screen so the operator sees the remote talk.
            app.emit(f"[remote voice] {text}", style="hint")
            app.submit_voice(text)
            self._send_json({"received": text})

    return StateHandler


def start_server(app, host="0.0.0.0", port=8770, token=None):
    """Start the state server on a daemon thread and return the server.

    Parameters
    ----------
    app : Audience
        The running audience application instance.
    host, port : str, int
        Bind address and port (default 0.0.0.0:8770).
    token : str | None
        Optional bearer token for authentication. When set, all requests must
        include ``Authorization: Bearer <token>``; when unset the server works
        without auth but logs a one-time warning.

    Returns the ``ThreadingHTTPServer`` so the caller can ``shutdown()`` it,
    or ``None`` if the socket could not be bound (e.g. port in use) — a remote
    mirror is a convenience, so a bind failure should never take down the app.
    """
    set_serve_token(token)
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(app))
    except OSError as e:
        app.emit(f"[remote] state server could not bind {host}:{port} ({e})",
                 style="error")
        return None
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app.emit(f"[remote] state server listening on {host}:{port}", style="hint")
    return httpd
