"""Tiny HTTP service that wraps :mod:`app.dhcp_detect` for on-demand probing.

The main pxe-app container lives on the docker bridge network and therefore
cannot see DHCP traffic on the operator's PXE VLAN. This module is designed
to run in a sidecar container with ``network_mode: host`` and the
``NET_RAW`` / ``NET_BIND_SERVICE`` capabilities so the probe can actually
bind UDP/68 on the host's network.

Endpoints:
  GET /healthz   -> 200 OK
  POST /probe    -> {"offers": [...], "checked_at": "...", "error": "..."}
                    Optional JSON body: {"interface": "eth0", "timeout": 5}
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from app.dhcp_detect import probe
from app.models import utcnow


logger = logging.getLogger("dhcp-probe-server")

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 5.0


def _run_probe(interface: str | None, timeout: float) -> dict[str, Any]:
    checked_at = utcnow().astimezone(timezone.utc).isoformat()
    payload: dict[str, Any] = {"checked_at": checked_at, "interface": interface, "timeout": timeout}
    try:
        offers = probe(interface, timeout)
        payload["offers"] = offers
    except PermissionError as exc:
        payload["offers"] = []
        payload["error"] = (
            "Probe lacks CAP_NET_BIND_SERVICE/CAP_NET_RAW. "
            f"Run the sidecar with the right capabilities. ({exc})"
        )
    except OSError as exc:
        payload["offers"] = []
        payload["error"] = f"Probe failed: {exc}"
    return payload


class ProbeHandler(BaseHTTPRequestHandler):
    server_version = "pxe-dhcp-probe/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.info("%s - %s", self.address_string(), format % args)

    def _write_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if self.path == "/healthz":
            self._write_json(200, {"ok": True})
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        if self.path != "/probe":
            self._write_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body: dict[str, Any] = {}
        if raw:
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except (ValueError, json.JSONDecodeError) as exc:
                self._write_json(400, {"error": f"invalid JSON body: {exc}"})
                return
        interface = body.get("interface") or os.environ.get("PXE_PROBE_INTERFACE") or None
        timeout_value = body.get("timeout")
        try:
            timeout = float(timeout_value) if timeout_value is not None else DEFAULT_TIMEOUT
        except (TypeError, ValueError):
            self._write_json(400, {"error": "timeout must be numeric"})
            return
        payload = _run_probe(interface, timeout)
        self._write_json(200, payload)


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), ProbeHandler)
    logger.info("DHCP probe server listening on %s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="HTTP wrapper for the DHCP probe")
    parser.add_argument("--host", default=os.environ.get("PXE_PROBE_HTTP_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PXE_PROBE_HTTP_PORT", str(DEFAULT_PORT))),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
