"""
Minimal BattlEye RCon client (UDP): log in, send one command, read the
reply (handling the multi-packet case), done. No keepalive loop since we
only need one round trip per call, well inside the connection timeout.

Protocol reference: BE's published RCon spec. Packet layout:
    'B' 'E' <crc32 LE of (0xFF + payload)> 0xFF <payload>
Login payload:   0x00 + password
Command payload: 0x01 + sequence_byte + command text
"""
import socket
import struct
import zlib

HEADER = b"BE"


def _packet(payload):
    crc = zlib.crc32(b"\xff" + payload) & 0xFFFFFFFF
    return HEADER + struct.pack("<I", crc) + b"\xff" + payload


def send_command(host, port, password, command, timeout=4.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        addr = (host, port)
        sock.sendto(_packet(b"\x00" + password.encode()), addr)
        data, _ = sock.recvfrom(4096)
        if len(data) < 9 or data[7] != 0x00 or data[8] != 0x01:
            raise ConnectionError("RCon login failed: wrong password or server unreachable")

        sock.sendto(_packet(b"\x01\x00" + command.encode()), addr)

        chunks = {}
        total = 1
        while len(chunks) < total:
            data, _ = sock.recvfrom(4096)
            if len(data) < 9 or data[7] != 0x01:
                continue
            if len(data) > 11 and data[9] == 0x00:
                total = data[10]
                index = data[11]
                text = data[12:]
            else:
                index = 0
                text = data[9:]
            chunks[index] = text.decode(errors="replace")

        return "".join(chunks.get(i, "") for i in range(total))
    finally:
        sock.close()
