#!/usr/bin/env python3
"""Set a BlueZ Management mode without btmgmt's interactive/TTY dependency."""
import argparse
import ctypes
import os
import socket
import struct
import sys

HCI_CHANNEL_CONTROL = 3
HCI_DEV_NONE = 0xFFFF
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002
MGMT_OP_READ_INFO = 0x0004
OPERATIONS = {"connectable": 0x0007, "fast-connectable": 0x0008}
SETTING_CONNECTABLE = 0x00000002
SETTING_FAST_CONNECTABLE = 0x00000004
STATUS = {
    0x01: "unknown command", 0x02: "not connected", 0x03: "failed",
    0x04: "connect failed", 0x05: "authentication failed", 0x06: "not paired",
    0x07: "no resources", 0x08: "timeout", 0x09: "already connected",
    0x0A: "busy", 0x0B: "rejected", 0x0C: "not supported",
    0x0D: "invalid parameters", 0x0E: "disconnected", 0x0F: "not powered",
    0x10: "cancelled", 0x11: "invalid index", 0x12: "rfkill blocked",
    0x13: "already paired", 0x14: "permission denied",
}


class SockaddrHCI(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("device", ctypes.c_ushort),
                ("channel", ctypes.c_ushort)]


def open_control():
    """Python exposes only the one-field HCI tuple; bind the channel via libc."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
    libc.socket.restype = ctypes.c_int
    libc.bind.argtypes = (ctypes.c_int, ctypes.POINTER(SockaddrHCI), ctypes.c_uint32)
    libc.bind.restype = ctypes.c_int
    proto = getattr(socket, "BTPROTO_HCI", 1)
    flags = (socket.SOCK_RAW | getattr(socket, "SOCK_CLOEXEC", 0) |
             getattr(socket, "SOCK_NONBLOCK", 0))
    fd = libc.socket(int(socket.AF_BLUETOOTH), int(flags), int(proto))
    if fd < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    address = SockaddrHCI(socket.AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
    if libc.bind(fd, ctypes.byref(address), ctypes.sizeof(address)) != 0:
        error = ctypes.get_errno()
        os.close(fd)
        raise OSError(error, os.strerror(error))
    try:
        return socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, proto, fileno=fd)
    except Exception:
        os.close(fd)
        raise


def command_packet(operation, index, enabled=None):
    payload = b"" if enabled is None else bytes((1 if enabled else 0,))
    return struct.pack("<HHH", operation, index, len(payload)) + payload


def parse_response(packet, operation, index):
    if len(packet) < 6:
        return None
    event, event_index, length = struct.unpack_from("<HHH", packet)
    payload = packet[6:]
    if len(payload) != length or event_index not in (index, HCI_DEV_NONE):
        return None
    if event in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS) and len(payload) >= 3:
        completed, status = struct.unpack_from("<HB", payload)
    else:
        return None
    if completed != operation:
        return None
    return status, payload[3:] if event == MGMT_EV_CMD_COMPLETE else b""


def parse_result(packet, operation, index):
    response = parse_response(packet, operation, index)
    return None if response is None else response[0]


def request(operation, index=0, enabled=None, timeout=5.0):
    with open_control() as control:
        control.settimeout(timeout)
        control.sendall(command_packet(operation, index, enabled))
        while True:
            response = parse_response(control.recv(4096), operation, index)
            if response is None:
                continue
            status, data = response
            if status:
                raise RuntimeError("management status 0x%02x (%s)" %
                                   (status, STATUS.get(status, "unknown error")))
            return data


def set_mode(name, enabled, index=0, timeout=5.0):
    request(OPERATIONS[name], index, enabled, timeout)


def read_settings(index=0):
    data = request(MGMT_OP_READ_INFO, index)
    if len(data) < 17:
        raise RuntimeError("short read-info response")
    return struct.unpack_from("<II", data, 9)


def ensure_connectable(index=0):
    supported, current = read_settings(index)
    changed = False
    if not current & SETTING_CONNECTABLE:
        set_mode("connectable", True, index)
        changed = True
    if supported & SETTING_FAST_CONNECTABLE and not current & SETTING_FAST_CONNECTABLE:
        set_mode("fast-connectable", True, index)
        changed = True
    supported, current = read_settings(index)
    if not current & SETTING_CONNECTABLE:
        raise RuntimeError("connectable setting was not enabled")
    if supported & SETTING_FAST_CONNECTABLE and not current & SETTING_FAST_CONNECTABLE:
        raise RuntimeError("fast-connectable setting was not enabled")
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=sorted(OPERATIONS) + ["ensure-connectable"])
    parser.add_argument("value", nargs="?", choices=("on", "off"))
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args(argv)
    if not (0 <= args.index <= 0xffff):
        parser.error("--index must be 0..65535")
    if args.mode == "ensure-connectable":
        if args.value is not None:
            parser.error("ensure-connectable does not accept on/off")
        print("changed" if ensure_connectable(args.index) else "unchanged")
    else:
        if args.value is None:
            parser.error("mode requires on/off")
        set_mode(args.mode, args.value == "on", args.index)
        print(args.mode, args.value)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as error:
        print("bt-mgmt-mode:", error, file=sys.stderr)
        sys.exit(1)
