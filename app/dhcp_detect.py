from __future__ import annotations

import argparse
import json
import os
import random
import socket
import struct
import sys
import time
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any


DHCP_DISCOVER = 1
DHCP_OFFER = 2
MAGIC_COOKIE = b"\x63\x82\x53\x63"


def mac_bytes(interface: str | None) -> bytes:
    if interface:
        path = Path("/sys/class/net") / interface / "address"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            return bytes.fromhex(value.replace(":", ""))
    # Locally administered random MAC for probing only.
    return bytes([0x02, *[random.randrange(0, 256) for _ in range(5)]])


def build_discover(xid: int, mac: bytes) -> bytes:
    packet = struct.pack(
        "!BBBBIHHIIII16s192s4s",
        1,
        1,
        6,
        0,
        xid,
        0,
        0x8000,
        0,
        0,
        0,
        0,
        mac.ljust(16, b"\x00"),
        b"\x00" * 192,
        MAGIC_COOKIE,
    )
    options = b"".join(
        [
            b"\x35\x01\x01",  # DHCP message type: discover
            b"\x37\x04\x01\x03\x06\x0f",  # parameter request list
            b"\xff",
        ]
    )
    return packet + options


def parse_options(data: bytes) -> dict[int, bytes]:
    options: dict[int, bytes] = {}
    if len(data) < 240 or data[236:240] != MAGIC_COOKIE:
        return options
    index = 240
    while index < len(data):
        code = data[index]
        index += 1
        if code == 255:
            break
        if code == 0:
            continue
        if index >= len(data):
            break
        length = data[index]
        index += 1
        options[code] = data[index : index + length]
        index += length
    return options


def parse_offer(data: bytes, expected_xid: int) -> dict[str, Any] | None:
    if len(data) < 240:
        return None
    op, _, _, _, xid = struct.unpack("!BBBBI", data[:8])
    if op != 2 or xid != expected_xid:
        return None
    options = parse_options(data)
    if options.get(53) != bytes([DHCP_OFFER]):
        return None
    yiaddr = str(IPv4Address(data[16:20]))
    server_id = str(IPv4Address(options[54])) if 54 in options and len(options[54]) == 4 else None
    router = str(IPv4Address(options[3][:4])) if 3 in options and len(options[3]) >= 4 else None
    dns_servers = []
    if 6 in options:
        dns_servers = [
            str(IPv4Address(options[6][index : index + 4]))
            for index in range(0, len(options[6]) - 3, 4)
        ]
    return {
        "offered_ip": yiaddr,
        "server_id": server_id,
        "router": router,
        "dns_servers": dns_servers,
    }


def probe(interface: str | None, timeout: float) -> list[dict[str, Any]]:
    xid = random.randrange(1, 0xFFFFFFFF)
    discover = build_discover(xid, mac_bytes(interface))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if interface and hasattr(socket, "SO_BINDTODEVICE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode())
    sock.bind(("", 68))
    sock.settimeout(0.25)
    sock.sendto(discover, ("255.255.255.255", 67))

    deadline = time.monotonic() + timeout
    offers: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str]] = set()
    while time.monotonic() < deadline:
        try:
            data, address = sock.recvfrom(4096)
        except socket.timeout:
            continue
        offer = parse_offer(data, xid)
        if not offer:
            continue
        offer["source_ip"] = address[0]
        key = (offer["server_id"], offer["source_ip"])
        if key not in seen:
            seen.add(key)
            offers.append(offer)
    return offers


def default_dhcp_range(network: str, netmask: str) -> tuple[str, str]:
    net = IPv4Network(f"{network}/{netmask}", strict=False)
    usable = max(net.num_addresses - 2, 1)
    first_host = int(net.network_address) + 1
    if usable >= 200:
        start_offset = 100
        end_offset = 200
    elif usable >= 30:
        start_offset = max(2, usable // 3)
        end_offset = max(start_offset, (usable * 2) // 3)
    else:
        start_offset = 1
        end_offset = usable
    return str(IPv4Address(first_host + start_offset - 1)), str(IPv4Address(first_host + end_offset - 1))


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe for DHCP offers on the local network")
    parser.add_argument("--interface", help="Network interface to bind for the probe")
    parser.add_argument("--timeout", type=float, default=5.0, help="Probe timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    try:
        offers = probe(args.interface, args.timeout)
    except PermissionError as exc:
        print(f"error: DHCP probe requires root/CAP_NET_BIND_SERVICE: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"error: DHCP probe failed: {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps({"offers": offers}, indent=2))
    elif offers:
        for offer in offers:
            print(
                "DHCP offer from "
                f"{offer.get('server_id') or offer.get('source_ip')} "
                f"offering {offer.get('offered_ip')}"
            )
    else:
        print("No DHCP offers detected")
    return 0 if offers else 1


if __name__ == "__main__":
    raise SystemExit(main())

