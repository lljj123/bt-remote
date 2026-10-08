#!/usr/bin/env python3
"""Run on the board: real Unix-socket / root PTY smoke test (no network changes)."""
import json
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
        assert peer.line().get("term_protocol") == "escaped-v1"
        peer.send({"t": "auth", "token": token})
        assert peer.line().get("ok") is True
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
