#!/usr/bin/env python3
"""Run on the board: real Unix-socket / root PTY smoke test (no network changes)."""
import json
import base64
import socket
import time

class Peer:
    def __init__(self, sock):
        self.sock, self.buf = sock, bytearray()
        self.sock.settimeout(0.5)

    def send(self, obj):
        self.sock.sendall(json.dumps(obj).encode() + b"\n")

    def byte(self, deadline):
        while not self.buf:
            if time.monotonic() >= deadline:
                raise TimeoutError("gateway response timed out")
            try:
                data = self.sock.recv(8192)
            except socket.timeout:
                continue
            if not data:
                raise EOFError("gateway disconnected")
            self.buf.extend(data)
        value = self.buf[0]
        del self.buf[0]
        return value

    def line(self, timeout=5):
        deadline, data = time.monotonic() + timeout, bytearray()
        while True:
            value = self.byte(deadline)
            if value == 10:
                return json.loads(data)
            data.append(value)

    def response(self, request_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            msg = self.line()
            if msg.get("t") == "res" and msg.get("id") == request_id:
                assert msg.get("ok"), msg
                return msg
        raise TimeoutError("matching response missing")

    def shell_output(self):
        deadline, data, escaped = time.monotonic() + 10, bytearray(), False
        while True:
            value = self.byte(deadline)
            if escaped:
                escaped = False
                if value == 0x45:
                    event = self.line()
                    assert event.get("name") == "term.exit", event
                    return bytes(data), event
                assert value == 1, "invalid raw escape"
                data.append(1)
            elif value == 1:
                escaped = True
            else:
                data.append(value)


def main():
    with open("/etc/bt-gateway/token") as f:
        token = f.read().strip()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect("/run/bt-gateway.sock")
        peer = Peer(sock)
        hello = peer.line()
        assert hello.get("term_protocol") == "escaped-v1"
        assert hello.get("gateway_version") == "2.0.2", hello
        assert hello.get("protocol_min") == 1 and hello.get("protocol_max") == 2, hello
        peer.send({"t": "auth", "token": token})
        auth = peer.line()
        assert auth.get("ok") is True
        assert "file" in auth.get("caps", []), auth
        assert "device" in auth.get("caps", []), auth
        assert "multiwifi" in auth.get("caps", []), auth
        assert "update" in auth.get("caps", []), auth
        peer.send({"t": "cmd", "id": 99, "name": "file.caps", "args": {}})
        file_caps = peer.response(99)["data"]
        assert file_caps["chunk_bytes"] == 1536, file_caps
        assert "window-v2" in file_caps.get("protocols", []), file_caps
        assert file_caps.get("window_chunks") == 4, file_caps
        print("PASS: file manager window-v2 capability")
        peer.send({"t": "cmd", "id": 97, "name": "update.status", "args": {}})
        update = peer.response(97)["data"]
        assert update.get("gateway_version") == "2.0.2", update
        print("PASS: versioned update capability")
        peer.send({"t": "cmd", "id": 98, "name": "device.usb.list",
                   "args": {"offset": 0, "limit": 1}})
        usb = peer.response(98)["data"]
        assert isinstance(usb.get("devices"), list), usb
        print("PASS: USB device inventory capability")
        peer.send({"t": "cmd", "id": 100, "name": "term.exec",
                   "args": {"command": "printf bt-exec-ok"}})
        executed = peer.response(100)["data"]
        assert base64.b64decode(executed["output_b64"]) == b"bt-exec-ok", executed
        print("PASS: term.exec bounded root command")
        for session in range(2):
            rid = session * 2 + 1
            peer.send({"t": "cmd", "id": rid, "name": "term.shell",
                       "args": {"cols": 80, "rows": 24, "protocol": "escaped-v1"}})
            peer.response(rid)
            assert peer.line().get("name") == "term.ready"
            # A terminal can print the old marker text without terminating the protocol.
            sock.sendall(b"id; printf 'session ended\\n'; exit\n")
            output, event = peer.shell_output()
            assert b"uid=0(root)" in output, output
            assert event["data"]["code"] == 0, event
            peer.send({"t": "cmd", "id": rid + 1, "name": "sys.info", "args": {}})
            peer.response(rid + 1)
            print("PASS: shell", session + 1, "root output, explicit exit, JSON query after exit")
    print("All PTY smoke tests passed")

if __name__ == "__main__":
    main()
