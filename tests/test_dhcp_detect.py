from __future__ import annotations

import struct

from app.dhcp_detect import MAGIC_COOKIE, default_dhcp_range, parse_offer


def _offer_packet(xid: int) -> bytes:
    packet = struct.pack(
        "!BBBBIHHIIII16s192s4s",
        2,
        1,
        6,
        0,
        xid,
        0,
        0,
        0,
        int.from_bytes(bytes([192, 0, 2, 150]), "big"),
        int.from_bytes(bytes([192, 0, 2, 1]), "big"),
        0,
        b"\xaa\xbb\xcc\xdd\xee\xff".ljust(16, b"\x00"),
        b"\x00" * 192,
        MAGIC_COOKIE,
    )
    options = b"".join(
        [
            b"\x35\x01\x02",
            b"\x36\x04\xc0\x00\x02\x01",
            b"\x03\x04\xc0\x00\x02\x01",
            b"\x06\x08\xc0\x00\x02\x35\xc0\x00\x02\x36",
            b"\xff",
        ]
    )
    return packet + options


def test_parse_offer_extracts_server_and_options():
    offer = parse_offer(_offer_packet(1234), 1234)

    assert offer is not None
    assert offer["offered_ip"] == "192.0.2.150"
    assert offer["server_id"] == "192.0.2.1"
    assert offer["router"] == "192.0.2.1"
    assert offer["dns_servers"] == ["192.0.2.53", "192.0.2.54"]


def test_parse_offer_ignores_unexpected_transaction_id():
    assert parse_offer(_offer_packet(1234), 5678) is None


def test_default_dhcp_range_for_24_network():
    assert default_dhcp_range("192.0.2.0", "255.255.255.0") == ("192.0.2.100", "192.0.2.200")

