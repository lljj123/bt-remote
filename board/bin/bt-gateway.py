#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bt-gateway — 香橙派 3 LTS「蓝牙管控网关」

手机通过蓝牙经典串口 SPP(RFCOMM) 连上板子，用 NDJSON 协议查询/管理本机。
设计约束（见交接规格）：
  * 管理命令走白名单/校验；鉴权后的 term.shell 显式授予交互 root 终端
  * 协议字段名严格照规格，不擅自改名
  * 系统文件改动前先备份，WiFi 切换带独立看门狗自动回滚

通道：
  * 蓝牙 SPP  —— BlueZ 5 ProfileManager1 注册，手机连这个
  * 本地 Unix socket（/run/bt-gateway.sock）—— 板端自测，跑的是同一套 session 代码
两者共用 NDJSON 帧 + 鉴权 + 命令分发。
"""
import dbus
import base64
import binascii
import dbus.service
import fcntl
import glob
import hmac
import json
import logging
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from functools import wraps
from datetime import datetime, timedelta

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# ------------------------------------------------------------------ 常量
TOKEN_FILE = "/etc/bt-gateway/token"
STATE_FILE = "/etc/bt-gateway/state.json"
BACKUP_ROOT = "/etc/bt-gateway"
GUARD_SCRIPT = "/etc/bt-gateway/wifi-guard.sh"
GUARD_FLAG = "/run/bt-gateway/wifi-switch-flag"
GUARD_UNIT = "btg-wifi-guard"
NETWORK_LOCK = threading.Lock()
TERM_PROTOCOL = "escaped-v1"
TERM_MUX_PROTOCOL = "json-mux-v1"
TERM_CHUNK = 2048
TERM_INPUT_LIMIT = 65536
SOCK_PATH = "/run/bt-gateway.sock"
NETPLAN_WIFI = os.environ.get("BTG_NETPLAN_WIFI", "/etc/netplan/30-wifis-dhcp.yaml")
NETPLAN_GLOB = "/etc/netplan/*.yaml"
WLAN = os.environ.get("BTG_WLAN", "wlan0")
BT_ADDR_PATH = "/sys/class/bluetooth/hci0/address"
BT_STORE = "/var/lib/bluetooth"
MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
_ADAPTER_ADDR = [None]

PROFILE_PATH = "/org/btgateway/profile"
AGENT_PATH = "/org/btgateway/agent"
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
SRV_NAME = "OrangePi 3LTS Gateway"

VER = 1
DEV_ID = "orangepi-3lts"
DEV_NAME = os.environ.get("BTG_DEV_NAME", "Orange Pi Bluetooth Gateway")
CAPS = ["sys", "svc", "net", "pwr", "log", "term"]

MAX_LINE = 4096
AUTH_TIMEOUT = 30.0
AUTH_MAX_FAIL = 3
AUTH_BLOCK_SECS = 60
BEAT_TIMEOUT = 60.0

SHELL_WHITELIST = ("/bin/bash", "/bin/sh", "/bin/dash")
TERM_MIN_COLS, TERM_MAX_COLS = 20, 500
TERM_MIN_ROWS, TERM_MAX_ROWS = 5, 200

SVC_WHITELIST = [
    "bt-gateway.service",
    "bluetooth.service",
    "ssh.service",
    "ModemManager.service",
]
SVC_ACTIONS = ("start", "stop", "restart", "enable", "disable")
DANGEROUS_SVC = {("ssh.service", "stop"), ("ssh.service", "disable")}

UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:\\-]{0,127}$")
SSID_MAX = 32
ALERT_COOLDOWN = 300

# 注意：**不要**给 RegisterProfile 传 ServiceRecord。
# BlueZ 只在「未提供 record」时才用 get_generic_record() 生成记录，它会把动态分配到的
# RFCOMM channel 和 L2CAP PSM 写进 ProtocolDescriptorList；一旦传了自定义 ServiceRecord，
# BlueZ 就原样 sdp_xml_parse_record() 注册，不会补 channel —— 手机 SDP 查到 Serial Port
# 却拿不到通道号，RFCOMM 连接建不起来。名字交给 Name 选项即可。

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("bt-gateway")

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def log(msg, level=logging.INFO):
    LOG.log(level, msg)


def systemd_notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return True  # foreground/debug execution
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.settimeout(1)
            notifier.sendto(message.encode("utf-8"), address)
        return True
    except OSError as error:
        log("systemd notify failed: %s" % error, logging.ERROR)
        return False


# ------------------------------------------------------------------ 工具
def run(cmd, timeout=10, shell=False):
    """返回 (rc, stdout, stderr)。"""
    try:
        p = subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 127, "", str(e)


def rd(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read().strip().strip("\x00")
    except Exception:
        return default


def rdi(path, default=None):
    v = rd(path)
    try:
        return int(v)
    except Exception:
        return default


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def jdump(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def _shrink(obj):
    """把一帧压到 MAX_LINE 以内，返回 (obj, line_bytes)。

    规则（照规格）：超长时置 data.truncated=true。
    唯一的例外是 data 本身是数组（目前只有 net.wifi.scan）：
    JSON 数组里没地方挂 truncated 标志，若把数组换成对象会直接破坏手机端契约，
    所以这种情形改为从尾部丢弃元素（已按信号强度降序，丢的是最弱的 AP），
    并在板上日志里记录丢弃数量。
    """
    line = jdump(obj).encode("utf-8")
    if len(line) <= MAX_LINE:
        return obj, line
    data = obj.get("data") if isinstance(obj, dict) else None

    if isinstance(data, list) and data:
        cur, dropped = list(data), 0
        while cur and len(jdump(dict(obj, data=cur)).encode("utf-8")) > MAX_LINE:
            cut = max(1, len(cur) // 10)
            cur, dropped = cur[:-cut], dropped + cut
        if cur:
            log("payload %d bytes > %d limit: dropped %d tail item(s)"
                % (len(line), MAX_LINE, dropped), logging.WARNING)
            o = dict(obj, data=cur)
            return o, jdump(o).encode("utf-8")

    if isinstance(data, dict):
        o = dict(obj, data=dict(data, truncated=True))
        while len(jdump(o).encode("utf-8")) > MAX_LINE:
            lists = [k for k, v in o["data"].items() if isinstance(v, list) and v]
            if not lists:
                break
            for k in lists:
                o["data"][k] = o["data"][k][:len(o["data"][k]) // 2]
        if len(jdump(o).encode("utf-8")) <= MAX_LINE:
            return o, jdump(o).encode("utf-8")

    o = dict(obj)
    o["data"] = {"truncated": True, "note": "payload exceeded %d bytes" % MAX_LINE}
    return o, jdump(o).encode("utf-8")


class CmdError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def bt_adapter_addr():
    """本机蓝牙适配器地址（带缓存）。

    注意：sunxi/uwe5622(sprdbt_tty) 驱动**没有** /sys/class/bluetooth/hci0/address
    这个节点，只读 sysfs 会永远拿到空串，进而使 is_bonded() 恒为 False、把**所有**
    连接都当成未配对设备拒掉。依次退到 /var/lib/bluetooth/<MAC>/ 目录名和
    hciconfig 输出。
    """
    if _ADAPTER_ADDR[0]:
        return _ADAPTER_ADDR[0]
    addr = rd(BT_ADDR_PATH).upper()
    if not MAC_RE.match(addr):
        addr = ""
    if not addr:
        try:
            for name in sorted(os.listdir(BT_STORE)):
                if MAC_RE.match(name.upper()):
                    addr = name.upper()
                    break
        except OSError:
            pass
    if not addr:
        rc, out, _ = run(["hciconfig", "hci0"])
        m = re.search(r"BD Address:\s*([0-9A-Fa-f:]{17})", out) if rc == 0 else None
        if m:
            addr = m.group(1).upper()
    if addr:
        _ADAPTER_ADDR[0] = addr
    return addr


def is_bonded(mac):
    """已配对(bonded)设备 = /var/lib/bluetooth/<adapter>/<MAC>/ 存在"""
    adapter = bt_adapter_addr()
    if not adapter:
        log("无法确定蓝牙适配器地址，拒绝连接 %s" % mac, logging.ERROR)
        return False
    return os.path.isdir(os.path.join(BT_STORE, adapter, mac.upper()))


# ------------------------------------------------------------------ 事件总线
EV_LOCK = threading.Lock()
EV_SEQ = [0]
EVENTS = {"alert": [], "svc": []}


def push_event(kind, data):
    with EV_LOCK:
        EV_SEQ[0] += 1
        EVENTS.setdefault(kind, []).append((EV_SEQ[0], data))
        if len(EVENTS[kind]) > 50:
            EVENTS[kind] = EVENTS[kind][-50:]


# ------------------------------------------------------------------ 采样器
class Sampler(threading.Thread):
    """/proc 采样线程：CPU 占用、网卡速率、进程占用都靠两次采样求差。"""

    def __init__(self, interval=1.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.lock = threading.Lock()
        self.cur = None
        self.prev = None
        self.stop = threading.Event()

    # --- 原始读取
    @staticmethod
    def cpu_stat():
        with open("/proc/stat") as f:
            parts = f.readline().split()
        v = [int(x) for x in parts[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return sum(v), idle

    @staticmethod
    def net_dev():
        out = {}
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                c = rest.split()
                out[name.strip()] = (int(c[0]), int(c[8]))  # rx_bytes, tx_bytes
        return out

    @staticmethod
    def procs():
        out = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % pid, "rb") as f:
                    raw = f.read()
            except Exception:
                continue
            rp = raw.rfind(b")")
            lp = raw.find(b"(")
            if rp < 0 or lp < 0:
                continue
            comm = raw[lp + 1:rp].decode("utf-8", "replace")
            rest = raw[rp + 2:].split()
            try:
                utime, stime, rss = int(rest[11]), int(rest[12]), int(rest[21])
            except Exception:
                continue
            out[int(pid)] = (utime + stime, rss, comm)
        return out

    def snapshot(self):
        total, idle = self.cpu_stat()
        return {
            "ts": time.time(),
            "total": total,
            "idle": idle,
            "net": self.net_dev(),
            "procs": self.procs(),
        }

    def run(self):
        self.prev = self.snapshot()
        while not self.stop.is_set():
            time.sleep(self.interval)
            s = self.snapshot()
            with self.lock:
                self.prev, self.cur = self.cur or self.prev, s

    def pair(self):
        """返回 (prev, cur)，不足两帧时阻塞补一帧。"""
        with self.lock:
            if self.cur is not None:
                return self.prev, self.cur
        a = self.snapshot()
        time.sleep(0.5)
        b = self.snapshot()
        with self.lock:
            self.prev, self.cur = a, b
            return a, b


SAMPLER = Sampler()


# ------------------------------------------------------------------ 慢查询缓存
_MEMO = {}
_MEMO_LOCK = threading.Lock()


def memo(key, ttl, fn):
    now = time.time()
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _MEMO_LOCK:
        _MEMO[key] = (now, val)
    return val


# ------------------------------------------------------------------ 取值
def meminfo():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            d[k] = int(v.split()[0])
    return d


def cpu_temp_c():
    """type 为 cpu-thermal / gpu-thermal 的 thermal zone（毫摄氏度）。"""
    best = None
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        t = rd(os.path.join(z, "type"))
        v = rdi(os.path.join(z, "temp"))
        if v is None:
            continue
        if t == "cpu-thermal":
            return v / 1000.0
        if t == "gpu-thermal" and best is None:
            best = v / 1000.0
    return best


def wifi_link():
    """iw dev wlan0 link 解析 -> dict（未连接时 ssid 为 None）。"""
    def _q():
        rc, out, _ = run(["iw", "dev", WLAN, "link"], timeout=5)
        d = {"ssid": None, "bssid": None, "signal_dbm": None, "freq": None}
        if rc != 0:
            return d
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Connected to "):
                d["bssid"] = line.split()[2]
            elif line.startswith("SSID:"):
                d["ssid"] = _unescape_iw(line.split(":", 1)[1].strip()) or None
            elif line.startswith("freq:"):
                try:
                    d["freq"] = int(float(line.split(":", 1)[1].strip()))
                except Exception:
                    pass
            elif line.startswith("signal:"):
                m = re.search(r"(-?\d+)", line)
                if m:
                    d["signal_dbm"] = int(m.group(1))
        return d
    return memo("iwlink", 3.0, _q)


def default_gateway():
    rc, out, _ = run(["ip", "-4", "route", "show", "default"], timeout=5)
    for line in out.splitlines():
        p = line.split()
        if "via" in p:
            return p[p.index("via") + 1]
    return None


def iface_ipv4(name):
    rc, out, _ = run(["ip", "-j", "-4", "addr", "show", "dev", name], timeout=5)
    try:
        arr = json.loads(out)
    except Exception:
        return None
    for a in arr:
        for ai in a.get("addr_info", []):
            if ai.get("family") == "inet" and ai.get("scope") == "global":
                return ai.get("local")
    return None


# ------------------------------------------------------------------ sys.*
def cmd_sys_info(_s, _a):
    up = 0.0
    try:
        up = float(rd("/proc/uptime", "0").split()[0])
    except Exception:
        pass
    now = time.time()
    return {
        "hostname": socket.gethostname(),
        "model": rd("/proc/device-tree/model") or "unknown",
        "os": next((l.split("=", 1)[1].strip().strip('"')
                    for l in open("/etc/os-release") if l.startswith("PRETTY_NAME=")), "unknown"),
        "kernel": os.uname().release,
        "uptime_s": int(up),
        "boot_ts": int(now - up),
    }


def build_metrics():
    prev, cur = SAMPLER.pair()
    dt = max(cur["ts"] - prev["ts"], 1e-6)
    dt_total = max(cur["total"] - prev["total"], 1)

    cpu_pct = round(100.0 * (1.0 - (cur["idle"] - prev["idle"]) / dt_total), 1)
    load = rd("/proc/loadavg", "0 0 0").split()
    mhz = rdi("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", 0)
    mhz_max = rdi("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq", 0)

    mi = meminfo()
    total_kb = mi.get("MemTotal", 0)
    avail_kb = mi.get("MemAvailable", 0)
    zram_raw = 0
    try:
        zram_raw = int(rd("/sys/block/zram0/mm_stat", "0").split()[0])
    except Exception:
        pass

    st = os.statvfs("/")
    root_total = st.f_blocks * st.f_frsize // 1024
    root_free = st.f_bavail * st.f_frsize // 1024
    root_used = (st.f_blocks - st.f_bfree) * st.f_frsize // 1024

    net = {}
    for name in (WLAN, "end0"):
        c = cur["net"].get(name)
        p = prev["net"].get(name, c)
        d = {"rx_bps": int(max((c[0] - p[0]) / dt, 0)) if c and p else 0,
             "tx_bps": int(max((c[1] - p[1]) / dt, 0)) if c and p else 0}
        carrier = rd("/sys/class/net/%s/carrier" % name, "")
        d["state"] = "no-carrier" if carrier == "0" else rd(
            "/sys/class/net/%s/operstate" % name, "unknown")
        if name == WLAN:
            lk = wifi_link()
            d["ssid"] = lk["ssid"]
            d["signal_dbm"] = lk["signal_dbm"]
        net[name] = d

    top = []
    for pid, (jif, rss, comm) in cur["procs"].items():
        pj = prev["procs"].get(pid)
        if not pj:
            continue
        cpu = 100.0 * (jif - pj[0]) / dt_total
        if cpu <= 0:
            continue
        top.append({"pid": pid, "name": comm,
                    "cpu_pct": round(cpu, 1),
                    "mem_pct": round(100.0 * rss * PAGE_SIZE / (total_kb * 1024), 1)})
    top.sort(key=lambda x: -x["cpu_pct"])
    top = top[:5]

    return {
        "ts": int(cur["ts"]),
        "cpu": {"pct": cpu_pct,
                "load1": float(load[0]), "load5": float(load[1]),
                "load15": float(load[2]),
                "mhz": round(mhz / 1000.0), "mhz_max": round(mhz_max / 1000.0),
                "temp_c": cpu_temp_c()},
        "mem": {"total_kb": total_kb, "used_kb": total_kb - avail_kb,
                "avail_kb": avail_kb, "cached_kb": mi.get("Cached", 0),
                "zram_used_kb": zram_raw // 1024},
        "disk": {"root_total_kb": root_total, "root_used_kb": root_used,
                 "root_free_kb": root_free},
        "net": net,
        "top": top,
    }


def cmd_sys_metrics(_s, _a):
    return build_metrics()


def cmd_sys_subscribe(sess, a):
    if not isinstance(a, dict):
        raise CmdError("E_ARGS", "args must be object")
    what = a.get("what")
    if not isinstance(what, list) or not what:
        raise CmdError("E_ARGS", "what must be a non-empty list")
    for w in what:
        if w not in ("metrics", "alerts"):
            raise CmdError("E_ARGS", "unknown subscription: %s" % w)
    iv = a.get("interval_s", 2)
    if not isinstance(iv, (int, float)) or isinstance(iv, bool) or not (1 <= iv <= 3600):
        raise CmdError("E_ARGS", "interval_s must be 1..3600")
    for w in what:
        sess.subs[w] = float(iv)
    return {"ok": True, "what": what, "interval_s": float(iv)}


def cmd_sys_unsubscribe(sess, a):
    if not isinstance(a, dict):
        raise CmdError("E_ARGS", "args must be object")
    what = a.get("what") or list(sess.subs.keys())
    if not isinstance(what, list):
        raise CmdError("E_ARGS", "what must be a list")
    for w in what:
        sess.subs.pop(w, None)
    return {"ok": True}


def cmd_dev_ping(_s, _a):
    return {"t": "pong", "ts": int(time.time())}


# ------------------------------------------------------------------ svc.*
def unit_props(units):
    """systemctl show 批量取属性（空行分块）。"""
    out = {}
    rc, txt, _ = run(["systemctl", "show", "-p", "Id", "-p", "Description",
                      "-p", "ActiveState", "-p", "UnitFileState",
                      "-p", "MemoryCurrent"] + list(units), timeout=15)
    if rc != 0:
        return out
    for block in txt.split("\n\n"):
        cur = {}
        for line in block.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                cur[k] = v
        uid = cur.get("Id")
        if uid:
            out[uid] = cur
    return out


def _svc_row(unit, props):
    p = props.get(unit, {})
    rss = p.get("MemoryCurrent", "")
    try:
        rss_kb = int(rss) // 1024
    except Exception:
        rss_kb = None
    return {"unit": unit,
            "desc": p.get("Description", ""),
            "active": p.get("ActiveState", "unknown"),
            "enabled": p.get("UnitFileState", "unknown"),
            "rss_kb": rss_kb}


def cmd_svc_list(_s, _a):
    props = unit_props(SVC_WHITELIST)
    return [_svc_row(u, props) for u in SVC_WHITELIST]


def cmd_svc_act(_s, a):
    if not isinstance(a, dict):
        raise CmdError("E_ARGS", "args must be object")
    unit, action = a.get("unit"), a.get("action")
    if unit not in SVC_WHITELIST:
        raise CmdError("E_PERM", "unit not in whitelist")
    if action not in SVC_ACTIONS:
        raise CmdError("E_ARGS", "action must be one of %s" % "|".join(SVC_ACTIONS))
    if (unit, action) in DANGEROUS_SVC:
        if a.get("confirm") != action.upper():
            raise CmdError("E_CONFIRM",
                           "dangerous action on %s requires confirm=%s"
                           % (unit, action.upper()))
    rc, _o, err = run(["systemctl", action, unit], timeout=60)
    if rc != 0:
        raise CmdError("E_SVC", (err or "systemctl %s failed rc=%s" % (action, rc)).strip()[:200])
    props = unit_props([unit]).get(unit, {})
    return {"unit": unit,
            "active": props.get("ActiveState", "unknown"),
            "enabled": props.get("UnitFileState", "unknown")}


# ------------------------------------------------------------------ net.*
def cmd_net_ifaces(_s, _a):
    rc, out, _ = run(["ip", "-j", "addr", "show"], timeout=8)
    try:
        arr = json.loads(out)
    except Exception:
        raise CmdError("E_NET", "ip -j addr failed")
    res = []
    for dev in arr:
        name = dev.get("ifname")
        if name == "lo":
            continue
        rcm, om, _ = run(["ip", "-j", "-4", "route", "show", "dev", name], timeout=5)
        metric = None
        try:
            for r in json.loads(om):
                if "metric" in r:
                    metric = min(metric, r["metric"]) if metric is not None else r["metric"]
        except Exception:
            pass
        res.append({"name": name,
                    "state": dev.get("operstate", "unknown"),
                    "ipv4": iface_ipv4(name),
                    "mac": dev.get("address"),
                    "metric": metric})
    return res


def cmd_net_wifi_status(_s, _a):
    lk = wifi_link()
    return {"ssid": lk["ssid"], "bssid": lk["bssid"],
            "signal_dbm": lk["signal_dbm"], "freq": lk["freq"],
            "ip": iface_ipv4(WLAN), "gw": default_gateway(),
            "dns": netplan_dns(), "backend": "netplan"}


def netplan_dns():
    rc, out, _ = run(["resolvectl", "dns", WLAN], timeout=5)
    if rc == 0 and ":" in out:
        tail = out.split(":", 1)[1].split()
        addrs = [x for x in tail if re.match(r"^[0-9a-fA-F:.]+$", x)]
        if addrs:
            return addrs
    try:
        with open("/run/systemd/resolve/resolv.conf") as f:
            return [l.split()[1] for l in f if l.startswith("nameserver")]
    except Exception:
        return []


def parse_iw_scan(text):
    aps, cur = [], None
    for line in text.splitlines():
        if line.startswith("BSS "):
            if cur:
                aps.append(cur)
            bssid = line.split()[1].split("(")[0]
            cur = {"bssid": bssid, "ssid": None, "signal_dbm": None, "freq": None,
                   "security": "open", "_rsn": False, "_wpa": False, "_priv": False,
                   "_sae": False, "_psk": False, "_assoc": "associated" in line}
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("freq:"):
            try:
                cur["freq"] = int(float(s.split(":", 1)[1].strip()))
            except Exception:
                pass
        elif s.startswith("signal:"):
            m = re.search(r"(-?\d+)", s)
            if m:
                cur["signal_dbm"] = int(m.group(1))
        elif s.startswith("SSID:"):
            raw = s.split(":", 1)[1].strip()
            cur["ssid"] = _unescape_iw(raw) or None
        elif s.startswith("capability:"):
            if "Privacy" in s:
                cur["_priv"] = True
        elif "Authentication suites:" in s:
            if "SAE" in s:
                cur["_sae"] = True
            if "PSK" in s:
                cur["_psk"] = True
        elif s.startswith("RSN:"):
            cur["_rsn"] = True
        elif s.startswith("WPA:"):
            cur["_wpa"] = True
    if cur:
        aps.append(cur)
    for a in aps:
        sae = a.pop("_sae", False)
        psk = a.pop("_psk", False)
        priv = a.pop("_priv", False)
        rsn = a.pop("_rsn", False)
        wpa = a.pop("_wpa", False)
        if rsn and sae and psk:
            a["security"] = "WPA2/WPA3"
        elif rsn and sae:
            a["security"] = "WPA3"
        elif rsn:
            a["security"] = "WPA2"
        elif wpa:
            a["security"] = "WPA"
        elif priv:
            a["security"] = "WEP"
    return [a for a in aps]


def known_ssids():
    """netplan 里配过的 SSID -> has_psk。"""
    out = {}
    for path in sorted(glob.glob(NETPLAN_GLOB)):
        try:
            import yaml
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        wifis = ((data.get("network") or {}).get("wifis") or {})
        for _dev, cfg in wifis.items():
            aps = (cfg or {}).get("access-points") or {}
            for ssid, v in aps.items():
                out[str(ssid)] = bool(isinstance(v, dict) and v.get("password"))
    return out


def _unescape_iw(s):
    """iw 把非可打印/非 ASCII 字节写成 \\xNN，这里还原成原始字符串。"""
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "\\" and s[i + 1:i + 2] == "x" and i + 4 <= len(s):
            try:
                out.append(int(s[i + 2:i + 4], 16))
                i += 4
                continue
            except ValueError:
                pass
        if s[i] == "\\" and i + 1 < len(s):
            out.append(ord(s[i + 1]))
            i += 2
            continue
        out += s[i].encode("utf-8", "replace")
        i += 1
    return out.decode("utf-8", "replace")


def _scan_raw():
    """UWE5622 上 `iw scan`（阻塞等完成）会挂死，改用 trigger + dump。"""
    rc, _o, err = run(["iw", "dev", WLAN, "scan", "trigger"], timeout=10)
    if rc != 0:
        raise CmdError("E_SCAN", (err or "iw scan trigger failed").strip()[:200])
    best = ""
    for _ in range(8):
        time.sleep(2)
        rc, out, err = run(["iw", "dev", WLAN, "scan", "dump"], timeout=15)
        if rc == 0 and out.count("BSS ") >= 1:
            return out
        best = out or best
    if best.strip():
        return best
    raise CmdError("E_SCAN", (err or "scan returned nothing").strip()[:200])


def cmd_net_wifi_scan(_s, a):
    if isinstance(a, dict) and a.get("rescan") is False:
        out = memo("iwscan", 20.0, _scan_raw)
    else:
        out = _scan_raw()
        with _MEMO_LOCK:
            _MEMO["iwscan"] = (time.time(), out)
    aps = parse_iw_scan(out)
    cur = wifi_link()
    known = known_ssids()
    for ap in aps:
        assoc = ap.pop("_assoc", False)
        ap["in_use"] = bool(
            (cur["bssid"] and ap["bssid"].lower() == cur["bssid"].lower()) or assoc)
        ap["known"] = ap["ssid"] in known if ap["ssid"] else False
    aps.sort(key=lambda x: (x["signal_dbm"] is None, -(x["signal_dbm"] or -999)))
    return aps


def cmd_net_wifi_saved(_s, _a):
    return [{"ssid": k, "has_psk": v} for k, v in sorted(known_ssids().items())]


def _netplan_set_ap(ssid, psk):
    """改 30-wifis-dhcp.yaml 的 access-points；其它字段原样保留。"""
    import yaml
    with open(NETPLAN_WIFI) as f:
        data = yaml.safe_load(f) or {}
    net = data.setdefault("network", {})
    wifis = net.setdefault("wifis", {})
    cfg = wifis.setdefault(WLAN, {})
    entry = {}
    if psk:
        entry["password"] = psk
    cfg["access-points"] = {ssid: entry}
    # version: 2 若原本没有就不要硬加（netplan 也能吃，但保持原样）
    tmp = NETPLAN_WIFI + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True,
                       sort_keys=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, NETPLAN_WIFI)


def _wifi_ok(timeout_s, want_iface=WLAN, want_ssid=None):
    """轮询：拿到 IPv4 且能 ping 通默认网关。"""
    deadline = time.monotonic() + timeout_s
    ip = None
    while time.monotonic() < deadline:
        ip = iface_ipv4(want_iface)
        if ip and (want_ssid is None or wifi_link()["ssid"] == want_ssid):
            rc, routes, _ = run(["ip", "-4", "route", "show", "default", "dev", want_iface])
            match = re.search(r"\bvia\s+(\S+)", routes) if rc == 0 else None
            gw = match.group(1) if match else None
            if gw:
                rc, _o, _e = run(["ping", "-c", "1", "-W", "2", "-I", want_iface, gw],
                                 timeout=6)
                if rc == 0:
                    return True, ip
        time.sleep(1.5)
    return False, ip


def _guard_arm(backup_path, timeout_s):
    """切换前的独立看门狗：即使本进程崩溃，到点也会把 netplan 还原。"""
    os.makedirs(os.path.dirname(GUARD_FLAG), exist_ok=True)
    with open(GUARD_FLAG, "w") as f:
        f.write(backup_path + "\n" + NETPLAN_WIFI + "\n")
    run(["systemctl", "reset-failed", GUARD_UNIT + ".timer"], timeout=10)
    rc, _, err = run(["systemd-run", "--unit=" + GUARD_UNIT, "--collect",
         "--on-active=%d" % int(timeout_s), "/bin/sh", GUARD_SCRIPT], timeout=15)
    if rc != 0:
        _guard_disarm()
        raise CmdError("E_NET", "cannot arm WiFi rollback: " + err[:160])


def _guard_disarm():
    run(["systemctl", "stop", GUARD_UNIT + ".timer"], timeout=15)
    run(["systemctl", "stop", GUARD_UNIT + ".service"], timeout=15)
    try:
        os.unlink(GUARD_FLAG)
    except Exception:
        pass


def network_transaction(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not NETWORK_LOCK.acquire(blocking=False):
            raise CmdError("E_BUSY", "network configuration is already changing")
        try:
            if os.path.exists(GUARD_FLAG):
                raise CmdError("E_BUSY", "WiFi rollback is still pending")
            return fn(*args, **kwargs)
        finally:
            NETWORK_LOCK.release()
    return wrapped


@network_transaction
def cmd_net_wifi_connect(_s, a):
    if not isinstance(a, dict):
        raise CmdError("E_ARGS", "args must be object")
    ssid = a.get("ssid")
    psk = a.get("psk")
    if not isinstance(ssid, str) or not ssid.strip():
        raise CmdError("E_ARGS", "ssid required")
    if len(ssid.encode("utf-8")) > SSID_MAX:
        raise CmdError("E_ARGS", "ssid too long (>32 bytes)")
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in ssid):
        raise CmdError("E_ARGS", "ssid contains control characters")
    if psk is not None:
        if not isinstance(psk, str):
            raise CmdError("E_ARGS", "psk must be a string")
        ok_len = (8 <= len(psk) <= 63) or (len(psk) == 64 and
                                           re.fullmatch(r"[0-9a-fA-F]{64}", psk))
        if not ok_len:
            raise CmdError("E_ARGS", "psk must be 8..63 chars or 64 hex")
    try:
        timeout_s = int(a.get("timeout_s", 30))
    except Exception:
        raise CmdError("E_ARGS", "timeout_s must be int")
    timeout_s = max(10, min(timeout_s, 180))

    ts = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    backup = os.path.join(BACKUP_ROOT, "netplan.bak.%s" % ts)
    shutil.copy2(NETPLAN_WIFI, backup)
    os.chmod(backup, 0o600)
    log("wifi.connect: ssid=%r backup=%s" % (ssid, backup))

    # Budget includes apply (60s), validation, rollback apply (60s),
    # rollback validation (30s), and subprocess/cleanup overhead.
    _guard_arm(backup, timeout_s + 240)
    try:
        _netplan_set_ap(ssid, psk)
        rc, _o, err = run(["netplan", "apply"], timeout=60)
        if rc != 0:
            raise CmdError("E_NETPLAN", (err or "netplan apply failed").strip()[:200])
        ok, ip = _wifi_ok(timeout_s, want_ssid=ssid)
        if ok:
            _guard_disarm()
            return {"ok": True, "tried": ssid, "rolled_back": False, "ip": ip}
        raise CmdError("E_WIFI_FAIL", "new network is unreachable")
    except Exception as error:
        shutil.copy2(backup, NETPLAN_WIFI)
        os.chmod(NETPLAN_WIFI, 0o600)
        rc, _, _ = run(["netplan", "apply"], timeout=60)
        restored, _ = _wifi_ok(30)
        if rc != 0 or not restored:
            raise CmdError("E_NET", "rollback not verified; watchdog remains armed") from error
        _guard_disarm()
        if isinstance(error, CmdError) and error.code == "E_WIFI_FAIL":
            return {"ok": False, "tried": ssid, "rolled_back": True,
                    "ip": iface_ipv4(WLAN), "err": "E_WIFI_FAIL"}
        raise



@network_transaction
def cmd_net_wifi_forget(_s, a):
    if not isinstance(a, dict) or not a.get("ssid"):
        raise CmdError("E_ARGS", "ssid required")
    ssid = a["ssid"]
    if not isinstance(ssid, str):
        raise CmdError("E_ARGS", "ssid must be string")
    cur = wifi_link()
    if cur["ssid"] == ssid:
        raise CmdError("E_IN_USE",
                       "refusing to forget the SSID currently in use "
                       "(would drop the link); switch away first")
    import yaml
    changed = []
    for path in sorted(glob.glob(NETPLAN_GLOB)):
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        wifis = ((data.get("network") or {}).get("wifis") or {})
        for _dev, cfg in wifis.items():
            aps = (cfg or {}).get("access-points") or {}
            if ssid in aps:
                aps.pop(ssid)
                changed.append(path)
        if changed and changed[-1] == path:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False,
                               allow_unicode=True, sort_keys=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
    if changed:
        rc, _, err = run(["netplan", "apply"], timeout=60)
        if rc != 0:
            raise CmdError("E_NETPLAN", err[:200])
    return {"ok": True, "removed_from": changed}


# ------------------------------------------------------------------ pwr.*
def cmd_pwr_reboot(_s, a):
    if not isinstance(a, dict) or a.get("confirm") != "REBOOT":
        raise CmdError("E_CONFIRM", 'confirm must be "REBOOT"')
    threading.Timer(1.5, lambda: _do_power("reboot")).start()
    return {"ok": True}


def cmd_pwr_poweroff(_s, a):
    if not isinstance(a, dict) or a.get("confirm") != "POWEROFF":
        raise CmdError("E_CONFIRM", 'confirm must be "POWEROFF"')
    threading.Timer(1.5, lambda: _do_power("poweroff")).start()
    return {"ok": True}


def _do_power(what):
    log("executing %s" % what, logging.WARNING)
    rc, _o, _e = run(["systemctl", "--no-block", what], timeout=15)
    if rc != 0:
        run(["/sbin/shutdown", "-r" if what == "reboot" else "-h", "now"], timeout=15)


def cmd_pwr_rtc_get(_s, _a):
    raw = rd("/sys/class/rtc/rtc0/wakealarm", "")
    st = load_state().get("rtc", {})
    try:
        v = int(raw)
    except Exception:
        v = 0
    if v <= 0:
        return {"enabled": False, "at": None, "repeat": st.get("repeat", "none")}
    return {"enabled": True,
            "at": datetime.fromtimestamp(v).strftime("%Y-%m-%dT%H:%M"),
            "repeat": st.get("repeat", "none")}


def cmd_pwr_rtc_set(_s, a):
    if not isinstance(a, dict) or not a.get("at"):
        raise CmdError("E_ARGS", "at required (YYYY-MM-DDTHH:MM)")
    at = a["at"]
    repeat = a.get("repeat", "none")
    enabled = bool(a.get("enabled", True))
    if repeat not in ("none", "daily"):
        raise CmdError("E_ARGS", "repeat must be none|daily")
    try:
        dt = datetime.strptime(at, "%Y-%m-%dT%H:%M")
    except Exception:
        raise CmdError("E_ARGS", "at must be YYYY-MM-DDTHH:MM")
    now = datetime.now()
    if dt <= now:
        if repeat == "daily":
            while dt <= now:                    # 今天的点已过 -> 顺延到下一次
                dt += timedelta(days=1)
        else:
            raise CmdError("E_ARGS", "at is in the past")
    st = load_state()
    st["rtc"] = {"at": dt.strftime("%Y-%m-%dT%H:%M"), "repeat": repeat,
                 "enabled": enabled}
    save_state(st)
    if not enabled:
        with open("/sys/class/rtc/rtc0/wakealarm", "w") as f:
            f.write("0\n")
        return {"ok": True, "enabled": False, "at": None, "repeat": repeat}
    _rtc_arm(dt)
    return {"ok": True, "enabled": True,
            "at": dt.strftime("%Y-%m-%dT%H:%M"), "repeat": repeat}


def _rtc_arm(dt):
    """写 /sys/class/rtc/rtc0/wakealarm。sun6i 的 RTC 会静默忽略过去的时刻，
    所以写完必须回读确认，避免"看起来设上了其实没设"。"""
    epoch = int(time.mktime(dt.timetuple()))
    with open("/sys/class/rtc/rtc0/wakealarm", "w") as f:
        f.write("0\n")
    with open("/sys/class/rtc/rtc0/wakealarm", "w") as f:
        f.write("%d\n" % epoch)
    back = rd("/sys/class/rtc/rtc0/wakealarm", "")
    try:
        ok = int(back) == epoch
    except Exception:
        ok = False
    if not ok:
        raise CmdError("E_RTC", "RTC rejected the alarm (%s)" % (dt.strftime("%Y-%m-%dT%H:%M")))
    log("rtc wakealarm armed at %s (%d)" % (dt, epoch))


def rtc_rearm_on_start():
    """daily 模式的闹钟在每次开机/服务启动后重新武装。"""
    st = load_state().get("rtc", {})
    if not st.get("enabled") or st.get("repeat") != "daily" or not st.get("at"):
        return
    try:
        dt = datetime.strptime(st["at"], "%Y-%m-%dT%H:%M")
    except Exception:
        return
    now = datetime.now()
    nxt = now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    try:
        _rtc_arm(nxt)
    except Exception as e:
        log("rtc rearm failed: %s" % e, logging.WARNING)


# ------------------------------------------------------------------ log.*
def _check_unit(unit):
    if not isinstance(unit, str) or not UNIT_RE.match(unit):
        raise CmdError("E_ARGS", "bad unit name")
    if unit not in SVC_WHITELIST and unit != "bt-gateway.service":
        raise CmdError("E_PERM", "unit not allowed for logs")


def cmd_log_tail(_s, a):
    unit = (a or {}).get("unit", "bt-gateway.service")
    _check_unit(unit)
    try:
        n = int((a or {}).get("lines", 50))
    except Exception:
        raise CmdError("E_ARGS", "lines must be int")
    n = max(1, min(n, 500))
    rc, out, err = run(["journalctl", "-u", unit, "-n", str(n), "-o", "cat",
                        "--no-pager"], timeout=15)
    if rc != 0 and not out:
        raise CmdError("E_LOG", (err or "journalctl failed").strip()[:200])
    return {"unit": unit, "lines": out.splitlines()}


def cmd_log_follow(sess, a):
    a = a or {}
    unit = a.get("unit", "bt-gateway.service")
    _check_unit(unit)
    on = bool(a.get("on", True))
    if on:
        sess.log_follow_start(unit)
    else:
        sess.log_follow_stop()
    return {"ok": True, "unit": unit, "on": on}


# ------------------------------------------------------------------ term.*
def _set_winsize(fd, cols, rows):
    cols = max(TERM_MIN_COLS, min(TERM_MAX_COLS, int(cols)))
    rows = max(TERM_MIN_ROWS, min(TERM_MAX_ROWS, int(rows)))
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass
    return cols, rows


def cmd_term_shell(sess, a):
    a = a or {}
    if a.get("protocol") not in (TERM_PROTOCOL, TERM_MUX_PROTOCOL):
        raise CmdError("E_ARGS", "unsupported terminal protocol")
    if sess.mux_mode and a.get("protocol") != TERM_MUX_PROTOCOL:
        raise CmdError("E_ARGS", "reconnect to use legacy terminal protocol")
    shell = a.get("shell", "/bin/bash")
    if shell not in SHELL_WHITELIST:
        raise CmdError("E_ARGS", "shell must be one of %s"
                       % "|".join(SHELL_WHITELIST))
    try:
        cols = int(a.get("cols", 80))
        rows = int(a.get("rows", 24))
    except (TypeError, ValueError):
        raise CmdError("E_ARGS", "cols/rows must be int")
    term = str(a.get("term", "xterm-256color"))[:64]
    if sess.shell_pid is not None:
        raise CmdError("E_BUSY", "a shell is already running on this session")
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.update({
            "TERM": term, "HOME": "/root", "USER": "root", "LOGNAME": "root",
            "SHELL": shell, "LANG": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        })
        try:
            os.chdir("/root")
        except OSError:
            pass
        try:
            os.execvpe(shell, [shell, "-i"], env)
        except Exception:
            pass
        os._exit(127)
    cols, rows = _set_winsize(fd, cols, rows)
    os.set_blocking(fd, False)
    sess.shell_pid = pid
    sess.shell_fd = fd
    sess.shell_cols = cols
    sess.shell_rows = rows
    log("shell open peer=%s pid=%d user=root term=%s size=%dx%d"
        % (sess.peer, pid, term, cols, rows))
    return {"pid": pid, "user": "root", "shell": shell,
            "cols": cols, "rows": rows, "cwd": "/root"}


# ------------------------------------------------------------------ 命令表
COMMANDS = {
    "sys.info": cmd_sys_info,
    "sys.metrics": cmd_sys_metrics,
    "sys.subscribe": cmd_sys_subscribe,
    "sys.unsubscribe": cmd_sys_unsubscribe,
    "dev.ping": cmd_dev_ping,
    "svc.list": cmd_svc_list,
    "svc.act": cmd_svc_act,
    "net.ifaces": cmd_net_ifaces,
    "net.wifi.status": cmd_net_wifi_status,
    "net.wifi.scan": cmd_net_wifi_scan,
    "net.wifi.saved": cmd_net_wifi_saved,
    "net.wifi.connect": cmd_net_wifi_connect,
    "net.wifi.forget": cmd_net_wifi_forget,
    "pwr.reboot": cmd_pwr_reboot,
    "pwr.poweroff": cmd_pwr_poweroff,
    "pwr.rtc.get": cmd_pwr_rtc_get,
    "pwr.rtc.set": cmd_pwr_rtc_set,
    "log.tail": cmd_log_tail,
    "log.follow": cmd_log_follow,
    "term.shell": cmd_term_shell,
}


# ------------------------------------------------------------------ 会话
class Session(threading.Thread):
    """一条连接 = 一个 Session 线程，跑 NDJSON 协议。"""

    _lock = threading.Lock()
    _fail = {}          # mac -> [连续失败次数, 解封时间]
    _current = None     # 同时只允许 1 条连接

    def __init__(self, sock, peer, local=False):
        super().__init__(daemon=True)
        self.sock = sock
        self.peer = peer
        self.local = local
        self.wlock = threading.RLock()
        self.buf = b""
        self.authed = False
        self.subs = {}
        self.last_ev = {"alert": 0, "svc": 0}
        self.logf = None
        self.logf_unit = None
        self.closed = False
        self.last_beat = time.time()
        self.raw_mode = False
        self.shell_pid = None
        self.shell_fd = None
        self.shell_cols = 80
        self.shell_rows = 24
        self.mux_mode = False
        self.term_id = None
        self.term_thread = None
        self.term_stop = threading.Event()
        self.term_lock = threading.Lock()
        self.term_input = bytearray()
        self.term_size = None
        self.job_lock = threading.Lock()

    # ---------------- 发送
    def send(self, obj):
        if self.closed:
            return
        obj, line = _shrink(obj)
        try:
            with self.wlock:
                self.sock.sendall(line + b"\n")
        except Exception as e:
            log("send error %s: %s" % (self.peer, e))
            self.close()

    def send_ev(self, name, data):
        with self.wlock:
            if self.raw_mode or not self.authed:
                return
            self.send({"t": "ev", "name": name, "data": data})

    def res_ok(self, rid, data):
        self.send({"t": "res", "id": rid, "ok": True, "data": data})

    def res_err(self, rid, code, msg):
        self.send({"t": "res", "id": rid, "ok": False, "err": code, "msg": msg})

    # ---------------- 收
    def readline(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while b"\n" not in self.buf:
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise socket.timeout()
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
            if len(self.buf) > MAX_LINE * 8:      # 没有换行的垃圾流
                self.buf = b""
                raise CmdError("E_TOO_LONG", "line too long")
        line, self.buf = self.buf.split(b"\n", 1)
        if len(line) > MAX_LINE:
            raise CmdError("E_TOO_LONG", "line too long")
        return line.strip()

    # ---------------- 鉴权
    def authenticate(self):
        token = rd(TOKEN_FILE)
        if not token:
            self.send_ev("alert", {"level": "crit", "item": "auth",
                                   "msg": "token file missing"})
            return False
        deadline = time.time() + AUTH_TIMEOUT
        while time.time() < deadline:
            try:
                raw = self.readline(timeout=max(0.5, deadline - time.time()))
            except CmdError as e:
                self.res_err(None, e.code, e.msg)
                continue
            except socket.timeout:
                continue
            except Exception:
                return False
            if raw is None:
                return False
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                self.res_err(None, "E_JSON", "invalid json")
                continue
            if not isinstance(msg, dict) or msg.get("t") != "auth":
                continue                          # 未鉴权前其余消息忽略
            got = msg.get("token") or ""
            if isinstance(got, str) and hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8")):
                self.authed = True
                with Session._lock:
                    Session._fail.pop(self.peer, None)
                self.send({"t": "auth", "ok": True, "caps": CAPS})
                log("auth ok: %s" % self.peer)
                return True
            fails = self.bad_token()
            if fails >= AUTH_MAX_FAIL:
                self.send({"t": "auth", "ok": False, "err": "E_AUTH",
                           "msg": "bad token; too many attempts, disconnecting",
                           "fails": fails})
                log("too many bad tokens from %s -> drop" % self.peer,
                    logging.WARNING)
                return False
            self.send({"t": "auth", "ok": False, "err": "E_AUTH",
                       "msg": "bad token", "fails": fails})
        log("auth timeout: %s" % self.peer)
        return False

    def bad_token(self, record=True):
        with Session._lock:
            slot = Session._fail.setdefault(self.peer, [0, 0])
            if record:
                slot[0] += 1
                if slot[0] >= AUTH_MAX_FAIL:
                    slot[1] = time.time() + AUTH_BLOCK_SECS
                    log("MAC blocked %ds: %s" % (AUTH_BLOCK_SECS, self.peer),
                        logging.WARNING)
            return slot[0]

    @classmethod
    def is_blocked(cls, mac):
        with cls._lock:
            slot = cls._fail.get(mac)
            if not slot:
                return 0
            if slot[1] and time.time() < slot[1]:
                return int(slot[1] - time.time())
            if slot[1] and time.time() >= slot[1]:
                cls._fail.pop(mac, None)
            return 0

    # ---------------- log.follow
    def log_follow_start(self, unit):
        self.log_follow_stop()
        p = subprocess.Popen(["journalctl", "-u", unit, "-f", "-n", "0",
                              "-o", "cat", "--no-pager"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True)
        self.logf, self.logf_unit = p, unit

        def pump():
            try:
                for line in p.stdout:
                    if self.closed:
                        break
                    self.send_ev("log", {"unit": unit, "line": line.rstrip("\n")})
            except Exception:
                pass
        threading.Thread(target=pump, daemon=True).start()

    def log_follow_stop(self):
        if self.logf:
            try:
                self.logf.terminate()
                self.logf.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.logf.kill()
                self.logf.wait(timeout=2)
            except Exception:
                pass
            self.logf = None
        self.logf_unit = None

    # ---------------- 事件推送
    def pusher(self):
        last_metrics = 0.0
        while not self.closed:
            time.sleep(0.5)
            if self.raw_mode:
                continue          # 裸相位：终端独占链路，暂停一切 JSON 推送
            try:
                iv = self.subs.get("metrics")
                if iv and time.time() - last_metrics >= iv:
                    last_metrics = time.time()
                    self.send_ev("metrics", build_metrics())
                with EV_LOCK:
                    alerts = list(EVENTS["alert"]) if "alerts" in self.subs else []
                    svcs = list(EVENTS["svc"])
                for seq, data in alerts:
                    if seq > self.last_ev["alert"]:
                        self.last_ev["alert"] = seq
                        self.send_ev("alert", data)
                for seq, data in svcs:
                    if seq > self.last_ev["svc"]:
                        self.last_ev["svc"] = seq
                        self.send_ev("svc", data)
            except Exception as e:
                log("pusher error %s: %s" % (self.peer, e), logging.WARNING)
                break

    # ---------------- 主循环
    def run(self):
        log("session start %s (local=%s)" % (self.peer, self.local))
        threading.Thread(target=self.pusher, daemon=True).start()
        try:
            self.send({"t": "hello", "ver": VER, "dev": DEV_ID,
                       "name": DEV_NAME, "auth": "required", "term_protocol": TERM_PROTOCOL,
                       "term_protocols": [TERM_PROTOCOL, TERM_MUX_PROTOCOL]})
            if not self.authenticate():
                self.close()
                return
            self.last_beat = time.time()
            while not self.closed:
                try:
                    raw = self.readline(timeout=1.0)
                except socket.timeout:
                    if time.time() - self.last_beat > BEAT_TIMEOUT:
                        log("heartbeat timeout %s" % self.peer)
                        break
                    continue
                except CmdError as e:
                    self.res_err(None, e.code, e.msg)
                    continue
                except Exception:
                    break
                if raw is None:
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except Exception:
                    self.res_err(None, "E_JSON", "invalid json")
                    continue
                if not isinstance(msg, dict) or msg.get("t") != "cmd":
                    continue
                rid = msg.get("id")
                name = msg.get("name")
                args = msg.get("args") or {}
                if not isinstance(name, str) or not isinstance(args, dict):
                    self.res_err(rid, "E_ARGS", "name must be string and args must be object")
                    continue
                self.last_beat = time.time()
                if name in ("term.input", "term.resize", "term.close"):
                    try:
                        self.res_ok(rid, self._term_control(name, args))
                    except CmdError as e:
                        self.res_err(rid, e.code, e.msg)
                    continue
                fn = COMMANDS.get(name)
                if not fn:
                    self.res_err(rid, "E_NOCMD", "unknown command: %s" % name)
                    continue
                if self.mux_mode and name not in ("term.shell", "dev.ping"):
                    if not self.job_lock.acquire(blocking=False):
                        self.res_err(rid, "E_BUSY", "another command is running; retry after its response")
                    else:
                        threading.Thread(target=self._command_job,
                                         args=(rid, name, fn, args), daemon=True).start()
                    continue
                t0 = time.time()
                try:
                    data = fn(self, args)
                    if name == "term.shell":
                        if args.get("protocol") == TERM_MUX_PROTOCOL:
                            self.mux_mode = True
                            self.term_id = uuid.uuid4().hex
                            self.term_stop.clear()
                            with self.term_lock:
                                self.term_input.clear()
                                self.term_size = None
                            data.update({"sid": self.term_id, "protocol": TERM_MUX_PROTOCOL})
                            self.res_ok(rid, data)
                            self.send_ev("term.ready", {"sid": self.term_id,
                                         "protocol": TERM_MUX_PROTOCOL,
                                         "cols": self.shell_cols, "rows": self.shell_rows})
                            self.term_thread = threading.Thread(target=self._term_mux, daemon=True)
                            self.term_thread.start()
                            continue
                        with self.wlock:
                            self.raw_mode = True
                            self.res_ok(rid, data)
                            self.send({"t": "ev", "name": "term.ready",
                                       "data": {"cols": self.shell_cols,
                                                "rows": self.shell_rows,
                                                "protocol": TERM_PROTOCOL}})
                        # Buffered NDJSON belongs to the old phase, never to a root PTY.
                        self.buf = b""
                        self._term_bridge()
                        self.last_beat = time.time()
                        continue
                    self.res_ok(rid, data)
                except CmdError as e:
                    self.res_err(rid, e.code, e.msg)
                except Exception as e:
                    log("cmd %s error: %r" % (name, e), logging.ERROR)
                    self.res_err(rid, "E_INTERNAL", str(e)[:200])
                self.last_beat = time.time()
                log("cmd %s -> %.2fs" % (name, time.time() - t0))
        finally:
            self.close()
            if self.term_thread is not None:
                self.term_thread.join(timeout=5)
            else:
                self._shell_cleanup()

    def _command_job(self, rid, name, fn, args):
        # One ordinary command at a time; controls and heartbeat stay responsive.
        # Already-started system mutations must finish their own rollback logic.
        try:
            if not self.closed:
                self.res_ok(rid, fn(self, args))
        except CmdError as e:
            self.res_err(rid, e.code, e.msg)
        except Exception as e:
            log("cmd %s error: %r" % (name, e), logging.ERROR)
            self.res_err(rid, "E_INTERNAL", str(e)[:200])
        finally:
            if self.closed:
                self.log_follow_stop()
            self.job_lock.release()

    def _term_control(self, name, args):
        with self.term_lock:
            if (not self.mux_mode or self.term_id is None or
                    args.get("sid") != self.term_id or self.term_stop.is_set()):
                raise CmdError("E_STATE", "terminal session is not active")
            if name == "term.input":
                encoded = args.get("b64")
                if not isinstance(encoded, str) or len(encoded) > 4 * ((TERM_CHUNK + 2) // 3):
                    raise CmdError("E_ARGS", "invalid terminal input size")
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    raise CmdError("E_ARGS", "invalid base64")
                if len(data) > TERM_CHUNK:
                    raise CmdError("E_ARGS", "terminal input exceeds chunk limit")
                if len(self.term_input) + len(data) > TERM_INPUT_LIMIT:
                    raise CmdError("E_BUSY", "terminal input buffer full")
                self.term_input.extend(data)
                return {"accepted": len(data)}
            if name == "term.resize":
                cols, rows = args.get("cols"), args.get("rows")
                if type(cols) is not int or type(rows) is not int:
                    raise CmdError("E_ARGS", "cols/rows must be integers")
                self.term_size = (cols, rows)
            else:
                self.term_stop.set()
            return {"accepted": True}

    def _term_mux(self):
        fd, pid, sid = self.shell_fd, self.shell_pid, self.term_id
        reason = "exit"
        try:
            while not self.closed and not self.term_stop.is_set():
                with self.term_lock:
                    size, self.term_size = self.term_size, None
                    pending = bool(self.term_input)
                if size is not None:
                    self.shell_cols, self.shell_rows = _set_winsize(fd, *size)
                readable, writable, _ = select.select([fd], [fd] if pending else [], [], 0.05)
                if writable:
                    with self.term_lock:
                        try:
                            n = os.write(fd, self.term_input[:TERM_CHUNK])
                            del self.term_input[:n]
                        except BlockingIOError:
                            pass
                if readable:
                    try:
                        data = os.read(fd, TERM_CHUNK)
                    except BlockingIOError:
                        continue
                    if not data:
                        break
                    self.send_ev("term.output", {"sid": sid,
                                 "b64": base64.b64encode(data).decode("ascii")})
                    # Yield between bounded frames so command responses can interleave.
                    time.sleep(0.001)
            if self.closed or self.term_stop.is_set():
                reason = "close"
        except (OSError, ValueError):
            reason = "pty_closed"
        finally:
            with self.term_lock:
                self.term_stop.set()
                self.term_input.clear()
            try:
                os.killpg(os.tcgetpgrp(fd), signal.SIGHUP)
            except OSError:
                pass
            code = self._reap_shell(pid)
            try:
                os.close(fd)
            except OSError:
                pass
            # Keep the shell busy until its final event is on the wire.
            self.send_ev("term.exit", {"sid": sid, "code": code, "reason": reason})
            with self.term_lock:
                self.shell_fd = None
                self.term_id = None
                self.shell_pid = None

    def _reap_shell(self, pid):
        for _ in range(10):
            try:
                w, st = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return -1
            if w:
                return os.waitstatus_to_exitcode(st)
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGHUP)
        except Exception:
            pass
        for _ in range(10):
            try:
                w, st = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return -1
            if w:
                return os.waitstatus_to_exitcode(st)
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
            w, st = os.waitpid(pid, 0)
            return os.waitstatus_to_exitcode(st)
        except Exception:
            return -1

    def _shell_cleanup(self):
        if self.shell_fd is not None:
            try:
                os.close(self.shell_fd)
            except Exception:
                pass
            self.shell_fd = None
        if self.shell_pid is not None:
            self._reap_shell(self.shell_pid)
            self.shell_pid = None

    def _term_bridge(self):
        fd, pid, conn = self.shell_fd, self.shell_pid, self.sock
        esc, pend = 0, bytearray()
        queued = bytearray()
        reason = "exit"
        conn.settimeout(5.0)

        def output(data):
            conn.sendall(data.replace(b"\x01", b"\x01\x01"))

        try:
            while not self.closed:
                reads = [fd] + ([conn] if len(queued) < 262144 else [])
                ready, writable, _ = select.select(reads, [fd] if queued else [], [], 1.0)
                if fd in ready:
                    try:
                        data = os.read(fd, 8192)
                    except BlockingIOError:
                        data = None
                    except OSError:
                        break
                    if data == b"":
                        break
                    if data:
                        output(data)
                if fd in writable:
                    try:
                        n = os.write(fd, queued)
                        del queued[:n]
                    except BlockingIOError:
                        pass
                    except OSError:
                        break
                if conn in ready:
                    chunk = conn.recv(4096)
                    if not chunk:
                        reason = "eof"
                        break
                    for byte in chunk:
                        if esc == 0:
                            if byte == 1:
                                esc = 1
                            else:
                                queued.append(byte)
                        elif esc == 1:
                            esc = 0
                            if byte == 1:
                                queued.append(1)
                            elif byte == 0x43:  # C: close even while a foreground program runs
                                reason = "close"
                                break
                            elif byte == 0x52:
                                pend.clear()
                                esc = 2
                        else:
                            pend.append(byte)
                            if len(pend) == 4:
                                cols, rows = struct.unpack("!HH", pend)
                                self.shell_cols, self.shell_rows = _set_winsize(fd, cols, rows)
                                esc = 0
                    if reason == "close":
                        break
        except (OSError, ValueError):
            reason = "error"
        finally:
            if reason in ("close", "eof", "error") or self.closed:
                # PTY foreground jobs may have a different process group from bash.
                try:
                    os.killpg(os.tcgetpgrp(fd), signal.SIGHUP)
                except OSError:
                    pass
            code = self._reap_shell(pid)
            self.shell_pid = None
            try:
                os.close(fd)
            except OSError:
                pass
            self.shell_fd = None
            if reason in ("eof", "error") or self.closed:
                self.close()
            else:
                try:
                    with self.wlock:
                        output(b"\r\n[session ended, exit code %d]\r\n" % code)
                        # Only the gateway can produce this unescaped control marker.
                        conn.sendall(b"\x01E" + jdump({"t": "ev", "name": "term.exit",
                                     "data": {"code": code}}).encode("utf-8") + b"\n")
                        self.raw_mode = False
                except OSError:
                    self.close()
            log("shell close peer=%s pid=%d code=%d reason=%s"
                % (self.peer, pid, code, reason))

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.term_stop.set()
        self.log_follow_stop()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        with Session._lock:
            if Session._current is self:
                Session._current = None
        log("session closed %s" % self.peer)


# ------------------------------------------------------------------ 监控线程
def svc_monitor():
    """白名单 unit 状态变化 -> ev/svc"""
    last = {}
    while True:
        try:
            props = unit_props(SVC_WHITELIST)
            for u in SVC_WHITELIST:
                st = props.get(u, {}).get("ActiveState", "unknown")
                if u in last and last[u] != st:
                    push_event("svc", {"unit": u, "active": st})
                    log("svc event: %s %s -> %s" % (u, last[u], st))
                last[u] = st
        except Exception as e:
            log("svc_monitor: %s" % e, logging.WARNING)
        time.sleep(5)


_last_alert = {}


def alert_monitor():
    """阈值告警 -> ev/alert（带冷却，避免刷屏）"""
    while True:
        time.sleep(10)
        try:
            m = build_metrics()
            now = time.time()
            checks = []
            t = m["cpu"]["temp_c"]
            if t is not None:
                checks.append(("temp", t, 80, 85, "CPU 温度 %.1f°C" % t))
            mem_pct = 100.0 * m["mem"]["used_kb"] / max(m["mem"]["total_kb"], 1)
            checks.append(("mem", mem_pct, 90, 95, "内存占用 %.0f%%" % mem_pct))
            d = m["disk"]
            disk_pct = 100.0 * d["root_used_kb"] / max(d["root_total_kb"], 1)
            checks.append(("disk", disk_pct, 90, 95, "根分区占用 %.0f%%" % disk_pct))
            ncpu = os.cpu_count() or 1
            checks.append(("load", m["cpu"]["load1"], ncpu * 2.0, ncpu * 4.0,
                           "负载 load1=%.2f（%d 核）" % (m["cpu"]["load1"], ncpu)))
            for item, val, warn, crit, msg in checks:
                level = "crit" if val >= crit else ("warn" if val >= warn else None)
                if not level:
                    _last_alert.pop(item, None)
                    continue
                if now - _last_alert.get(item, 0) < ALERT_COOLDOWN:
                    continue
                _last_alert[item] = now
                push_event("alert", {"level": level, "item": item, "msg": msg})
        except Exception as e:
            log("alert_monitor: %s" % e, logging.WARNING)


def rtc_monitor():
    """daily 闹钟：RTC 到点/开机后重新武装（服务重启时也走这里）。"""
    rtc_rearm_on_start()
    while True:
        time.sleep(300)
        st = load_state().get("rtc", {})
        if st.get("enabled") and st.get("repeat") == "daily":
            raw = rd("/sys/class/rtc/rtc0/wakealarm", "0")
            try:
                armed = int(raw) > 0
            except Exception:
                armed = False
            if not armed:
                rtc_rearm_on_start()


# ------------------------------------------------------------------ 本地 socket
def local_socket_server():
    try:
        if os.path.exists(SOCK_PATH):
            os.unlink(SOCK_PATH)
    except Exception:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    srv.listen(4)
    log("local socket ready at %s" % SOCK_PATH)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except Exception:
                time.sleep(1)
                continue
            Session(conn, "local", local=True).start()
    threading.Thread(target=loop, daemon=True).start()


# ------------------------------------------------------------------ BlueZ
def start_bluez():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    class Profile(dbus.service.Object):
        @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
        def Release(self):
            log("profile released")

        @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}",
                             out_signature="")
        def NewConnection(self, device, fd, props):
            mac = str(device).split("dev_")[-1].replace("_", ":").upper()
            if not is_bonded(mac):
                log("reject non-bonded connection from %s" % mac, logging.WARNING)
                try:
                    os.close(fd.take())
                except Exception:
                    pass
                return
            blocked = Session.is_blocked(mac)
            if blocked:
                log("reject blocked MAC %s (%ds left)" % (mac, blocked),
                    logging.WARNING)
                try:
                    os.close(fd.take())
                except Exception:
                    pass
                return
            try:
                sock = socket.socket(fileno=fd.take())
            except Exception as e:
                log("bad fd from %s: %s" % (mac, e))
                return
            with Session._lock:
                old = Session._current
            if old is not None and not old.closed:
                log("kicking existing session to make room for %s" % mac)
                try:
                    dev = bus.get_object(
                        "org.bluez",
                        "/org/bluez/hci0/dev_" + old.peer.replace(":", "_"))
                    dev.Disconnect(dbus_interface="org.bluez.Device1")
                except Exception:
                    pass
                old.close()
            sess = Session(sock, mac)
            with Session._lock:
                Session._current = sess
            sess.start()

        @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
        def RequestDisconnection(self, device):
            mac = str(device).split("dev_")[-1].replace("_", ":").upper()
            log("disconnect request %s" % mac)
            with Session._lock:
                cur = Session._current
            if cur is not None and cur.peer == mac:
                cur.close()

    class Agent(dbus.service.Object):
        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Release(self):
            log("agent released")

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            log("RequestPinCode %s -> 0000" % device)
            return "0000"

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def DisplayPinCode(self, device, pincode):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            return dbus.UInt32(0)

        @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
        def DisplayPasskey(self, device, passkey, entered):
            pass

        @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
        def RequestConfirmation(self, device, passkey):
            log("RequestConfirmation %s -> accept" % device)

        @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
        def RequestAuthorization(self, device):
            log("RequestAuthorization %s -> accept" % device)

        @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            log("AuthorizeService %s %s -> accept" % (device, uuid))

        @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
        def Cancel(self):
            log("agent Cancel")

    Agent(bus, AGENT_PATH)
    am = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                        "org.bluez.AgentManager1")
    try:
        am.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        am.RequestDefaultAgent(AGENT_PATH)
        log("Agent1 registered (NoInputNoOutput, Just Works)")
    except Exception as e:
        log("agent register failed: %s" % e, logging.ERROR)
        raise

    Profile(bus, PROFILE_PATH)
    mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                         "org.bluez.ProfileManager1")
    opts = {"Name": SRV_NAME, "Role": "server", "Channel": dbus.UInt16(0),
            "RequireAuthentication": False, "RequireAuthorization": False,
            "AutoConnect": True}
    try:
        mgr.RegisterProfile(PROFILE_PATH, SPP_UUID, opts)
        log("SPP profile registered (%s)" % SPP_UUID)
    except Exception as e:
        log("RegisterProfile failed: %s" % e, logging.ERROR)
        raise

    def _adapter_props(bus):
        obj = bus.get_object("org.bluez", "/org/bluez/hci0")
        return dbus.Interface(obj, "org.freedesktop.DBus.Properties")

    # bluetoothd 重启 / 消失时 Profile1 + Agent1 注册会随之失效（BlueZ 只会回调
    # Release，不会自动重注册）。检测 org.bluez 的唯一名变化后直接退出，交给
    # systemd（Restart=always）重新拉起并注册 —— unit 文件保持规格原样。
    sysbus = dbus.Interface(bus.get_object("org.freedesktop.DBus",
                                           "/org/freedesktop/DBus"),
                            "org.freedesktop.DBus")
    try:
        owner0 = str(sysbus.GetNameOwner("org.bluez"))
    except Exception:
        owner0 = None
    log("org.bluez owner: %s" % (owner0 or "<none>"))

    def _exit_now(why):
        LOG.warning("exiting for systemd restart (%s)" % why)
        logging.shutdown()
        os._exit(1)

    def poll_owner():
        try:
            cur = str(sysbus.GetNameOwner("org.bluez"))
        except Exception:
            cur = None
        if cur != owner0:
            _exit_now("org.bluez owner %s -> %s" % (owner0, cur))
            return False
        return True

    GLib.timeout_add(5000, poll_owner)

    # 默认关闭新配对窗口；已有配对仍允许连接。
    def set_adapter():
        try:
            props = _adapter_props(bus)
            props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
            props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(False))
            props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(False))
            log("adapter: Pairable=false Discoverable=false")
        except Exception as e:
            log("adapter setup failed: %s" % e, logging.WARNING)
            raise

    set_adapter()

    def on_props(interface, changed, _invalidated, path=None):
        if interface == "org.bluez.Device1" and str(changed.get("Paired")) == "1":
            log("device bonded: %s -> 关闭可发现" % path)
            try:
                _adapter_props(bus).Set("org.bluez.Adapter1", "Discoverable",
                                        dbus.Boolean(False))
            except Exception:
                pass
    try:
        bus.add_signal_receiver(on_props,
                                dbus_interface="org.freedesktop.DBus.Properties",
                                signal_name="PropertiesChanged",
                                path_keyword="path")
    except Exception as e:
        log("signal receiver failed: %s" % e, logging.WARNING)

    log("waiting for phone connection ...")
    def watchdog_tick():
        if not SAMPLER.is_alive():
            _exit_now("sampler thread stopped")
        return systemd_notify("WATCHDOG=1")

    GLib.timeout_add(5000, watchdog_tick)
    if not systemd_notify("READY=1\nWATCHDOG=1\nSTATUS=Bluetooth profile registered"):
        raise RuntimeError("cannot notify systemd readiness")
    GLib.MainLoop().run()


# ------------------------------------------------------------------ main
def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0

    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", WLAN):
        log("invalid BTG_WLAN", logging.ERROR)
        return 1
    if not os.path.isabs(NETPLAN_WIFI) or "\n" in NETPLAN_WIFI:
        log("BTG_NETPLAN_WIFI must be an absolute path without newlines", logging.ERROR)
        return 1
    if not rd(TOKEN_FILE):
        log("token file %s missing — 先跑安装步骤" % TOKEN_FILE, logging.ERROR)
        return 1

    log("bt-gateway starting (ver=%d, caps=%s)" % (VER, ",".join(CAPS)))
    SAMPLER.start()
    time.sleep(1.2)

    local_socket_server()
    threading.Thread(target=svc_monitor, daemon=True).start()
    threading.Thread(target=alert_monitor, daemon=True).start()
    threading.Thread(target=rtc_monitor, daemon=True).start()

    # 用 GLib 的信号源处理退出：进程主体跑在 GLib 主循环里，普通 signal handler
    # 里 raise SystemExit 会被 PyGObject 吞掉，systemd stop 会一直等到 TimeoutStopSec。
    def _on_term(signum):
        LOG.info("signal %s -> exiting (BlueZ 随连接关闭自动释放 profile)" % signum)
        logging.shutdown()
        os._exit(0)

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _on_term, signal.SIGTERM)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _on_term, signal.SIGINT)

    try:
        start_bluez()
    except Exception as e:
        log("bluez setup failed: %s — restarting via systemd" % e, logging.ERROR)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
