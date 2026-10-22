# -*- coding: utf-8 -*-
"""Strata 对外 API 网关 v2。

本机 Strata 只监听 127.0.0.1，别人用不上。这一层在它前面加一个凭 Key 使用的
OpenAI 兼容入口，并且支持三种开放范围：

  local  仅本机       http://127.0.0.1:<port>/v1
  lan    局域网       http://<本机 IP>:<port>/v1（同一 WiFi 的人可用）
  public 公网         https://<随机后缀>.run.pinggy-free.link/v1
                     用 ssh 反向隧道（pinggy 免费通道），地址带随机后缀，
                     免费通道约 60 分钟到期，可一键关停/重新生成。

多 Key 与每 Key 独立限制（都在本模块内强制执行，不依赖上游）：
  token_limit    总流量上限（累计 prompt+completion token，0 = 不限）
  context_limit  单请求上下文长度（估算输入 token，0 = 不限）
  qps            每秒请求数（0 = 不限）
  max_seconds    单请求时长上限（超时中断，0 = 不限）

每个请求都会记一条历史（IP / token / 内容预览 / 耗时 / 限速状态），
连接页的监控面板就是读这里的数据。
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import strata_ctl as ctl

DEFAULT_PORT = 8230
DEFAULT_MODE = "lan"
MODES = {"local": "仅本机", "lan": "局域网", "public": "公网"}
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_FWD_REQ = {"content-type", "accept", "accept-language", "user-agent",
            "anthropic-version", "anthropic-beta", "x-stainless-lang",
            "openai-organization"}
_FWD_RES = {"content-type", "cache-control", "content-encoding", "content-length",
            "x-request-id", "openai-version"}

_srv: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
_lock = threading.RLock()

stats = {"requests": 0, "ok": 0, "denied": 0, "errors": 0, "limited": 0,
         "last": "", "last_at": 0.0, "started_at": 0.0}

# 每个 Key 的 QPS 时间窗、最近 IP
_qps_win: dict[str, deque] = {}
_last_ip: dict[str, str] = {}

HISTORY_MAX = 600
_history: deque = deque(maxlen=HISTORY_MAX)      # 最新的在前
_usage_tick: dict[str, int] = {}


# ---------------------------------------------------------------- 路径

def _state_dir() -> Path:
    d = Path(os.environ.get("APPDATA") or Path.home()) / "StrataControl"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keys_path() -> Path:
    return _state_dir() / "api_keys.json"


def _history_path() -> Path:
    return _state_dir() / "api_requests.jsonl"


# ---------------------------------------------------------------- 小工具

def gen_key() -> str:
    return "sk-strata-" + secrets.token_urlsafe(30)


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:                             # noqa: BLE001
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                return ip
    except Exception:                             # noqa: BLE001
        pass
    return "127.0.0.1"


def est_tokens(text: str) -> int:
    """粗估 token：中文按 1 字 1 token，其他按 3.6 字符 1 token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    return int(cjk + (len(text) - cjk) / 3.6) + (1 if text else 0)


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:n] + ("…" if len(s) > n else "")


# ---------------------------------------------------------------- 设置

def load() -> dict:
    s = ctl.load_settings()
    mode = str(s.get("api_mode") or DEFAULT_MODE)
    if mode not in MODES:
        mode = DEFAULT_MODE
    return {"enabled": bool(s.get("api_enabled")), "mode": mode,
            "port": int(s.get("api_port") or DEFAULT_PORT)}


def save(enabled: bool | None = None, mode: str | None = None,
         port: int | None = None) -> dict:
    patch = {}
    if enabled is not None:
        patch["api_enabled"] = bool(enabled)
    if mode is not None:
        patch["api_mode"] = str(mode)
    if port is not None:
        patch["api_port"] = int(port)
    ctl.save_settings(**patch)
    return load()


def bind_host(mode: str) -> str:
    """局域网和公网都要绑 0.0.0.0；仅本机才只守回环。"""
    return "127.0.0.1" if mode == "local" else "0.0.0.0"


def local_base(port: int | None = None) -> str:
    port = int(port or load()["port"])
    return f"http://127.0.0.1:{port}"


def public_base(port: int | None = None, mode: str | None = None) -> str:
    """给使用者看的地址。"""
    s = load()
    mode = mode or s["mode"]
    port = int(port or s["port"])
    if mode == "public":
        if _pub.get("tunnel") and _tunnel.get("url"):
            suf = suffix()
            return f"{_tunnel['url']}/{suf}/v1" if suf else _tunnel["url"]
        info = public_info()
        if info["active"] and info["ext_ip"]:
            return f"http://{info['ext_ip']}:{port}/{info['suffix']}/v1"
        return ""                                 # 公网地址没生成时留空，界面显示"未生成"
    host = lan_ip() if mode == "lan" else "127.0.0.1"
    return f"http://{host}:{port}/v1"


# ---------------------------------------------------------------- 上游模型服务

def upstream_cfg() -> dict:
    """手动指定的上游（URL + 可选 Key）。URL 留空 = 跟随控制台「接入」的端口。"""
    s = ctl.load_settings()
    return {"url": str(s.get("api_upstream_url") or "").strip().rstrip("/"),
            "key": str(s.get("api_upstream_key") or "").strip()}


def upstream_base() -> str:
    u = upstream_cfg()["url"]
    return u or ctl.current_base()


def save_upstream(url: str | None = None, key: str | None = None) -> None:
    patch = {}
    if url is not None:
        patch["api_upstream_url"] = url.strip().rstrip("/")
    if key is not None:
        patch["api_upstream_key"] = key.strip()
    if patch:
        ctl.save_settings(**patch)


# ----------------------- Cloudflare Zero Trust 用户自有凭证（命名隧道） ---------------

def cf_cfg() -> dict:
    """用户自己的 Cloudflare 凭证：Tunnel Token + 自定义域名（都可空）。"""
    s = ctl.load_settings()
    return {"token": str(s.get("cf_tunnel_token") or "").strip(),
            "domain": str(s.get("cf_domain") or "").strip().lower().rstrip("/")}


def cf_configured() -> bool:
    c = cf_cfg()
    return bool(c["token"] and c["domain"])


def save_cf(token: str | None = None, domain: str | None = None) -> dict:
    patch = {}
    if token is not None:
        patch["cf_tunnel_token"] = token.strip()
    if domain is not None:
        patch["cf_domain"] = domain.strip().lower()
    if patch:
        ctl.save_settings(**patch)
    return cf_cfg()


# ---------------------------------------------------------------- Key 存储

def _norm_key(k: dict) -> dict:
    out = {
        "id": str(k.get("id") or secrets.token_hex(4)),
        "name": str(k.get("name") or "未命名"),
        "secret": str(k.get("secret") or gen_key()),
        "enabled": bool(k.get("enabled", True)),
        "token_limit": int(k.get("token_limit") or 0),
        "context_limit": int(k.get("context_limit") or 0),
        "qps": float(k.get("qps") or 0),
        "max_seconds": int(k.get("max_seconds") or 0),
        "used_tokens": int(k.get("used_tokens") or 0),
        "requests": int(k.get("requests") or 0),
        "limited": int(k.get("limited") or 0),
        "last_ip": str(k.get("last_ip") or ""),
        "ips": [str(x) for x in (k.get("ips") or []) if str(x).strip()][:6],
        "last_at": float(k.get("last_at") or 0.0),
        "created": float(k.get("created") or time.time()),
    }
    out["secret_hint"] = out["secret"][:14] + "…" + out["secret"][-4:]
    return out


def keys() -> list[dict]:
    """读 Key 表；第一次用时把 v1 的单 Key 迁移过来。"""
    with _lock:
        p = _keys_path()
        data = []
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8")).get("keys") or []
            except Exception:                     # noqa: BLE001
                data = []
        if not data:
            legacy = str(ctl.load_settings().get("api_key") or "")
            data = [{"id": "default", "name": "默认 Key", "secret": legacy or gen_key(),
                     "created": time.time()}]
            _write_keys(data)
        return [_norm_key(k) for k in data]


def _write_keys(items: list[dict]) -> None:
    try:
        _keys_path().write_text(json.dumps({"keys": items}, indent=1, ensure_ascii=False),
                                encoding="utf-8")
    except Exception:                             # noqa: BLE001
        pass
    # 兼容旧设置：第一把 Key 同步到 api_key，老界面/第三方读得到
    if items:
        try:
            ctl.save_settings(api_key=items[0].get("secret", ""))
        except Exception:                         # noqa: BLE001
            pass


def key_add(name: str = "", **cfg) -> dict:
    with _lock:
        items = keys()
        k = _norm_key({"name": name or f"Key {len(items) + 1}",
                       "secret": gen_key(), "created": time.time(), **cfg})
        items.append(k)
        _write_keys(items)
        return k


def key_update(kid: str, **cfg) -> dict | None:
    with _lock:
        items = keys()
        for k in items:
            if k["id"] == kid:
                for f in ("name", "token_limit", "context_limit", "qps",
                          "max_seconds", "enabled", "secret"):
                    if f in cfg and cfg[f] is not None:
                        k[f] = cfg[f]
                k = _norm_key(k)
                _write_keys(items)
                return k
    return None


def key_delete(kid: str) -> bool:
    with _lock:
        items = keys()
        left = [k for k in items if k["id"] != kid]
        if len(left) == len(items):
            return False
        _write_keys(left)
        _qps_win.pop(kid, None)
        return True


def key_reset(kid: str) -> bool:
    with _lock:
        items = keys()
        for k in items:
            if k["id"] == kid:
                k["used_tokens"] = 0
                k["requests"] = 0
                k["limited"] = 0
                _write_keys(items)
                return True
    return False


def find_key(secret: str) -> dict | None:
    for k in keys():
        if secrets.compare_digest(k["secret"], secret):
            return k
    return None


_GENERIC_NAME = re.compile(r"^(默认 Key|Key \d+)$")


def _functional_name(k: dict) -> str:
    """按 Key 的配置起一个功能性名字（额度/上下文/串行/QPS/时长）。"""
    parts = []
    if k["token_limit"]:
        n = k["token_limit"]
        parts.append(f"额度{n // 1000}k" if n % 1000 == 0 and n >= 1000 else f"额度{n}")
    if k["context_limit"]:
        n = k["context_limit"]
        parts.append(f"{n // 1000}k上下文" if n % 1000 == 0 and n >= 1000 else f"{n}上下文")
    if k["qps"] == 1:
        parts.append("串行")
    elif k["qps"]:
        parts.append(f"QPS{k['qps']:g}")
    if k["max_seconds"]:
        parts.append(f"限{k['max_seconds']}秒")
    return "·".join(parts) + " Key" if parts else "不限量 Key"


def rename_generic_keys() -> int:
    """把「默认 Key / Key N」这类通用名按各自配置改成功能性名字。
    只动匹配通用名的（幂等：改过就不再匹配），手动起过名的绝不碰。"""
    with _lock:
        items = keys()
        changed = 0
        seen: set[str] = set()
        for k in items:
            if not _GENERIC_NAME.match(k["name"]):
                seen.add(k["name"])
                continue
            k["name"] = _functional_name(k)
            base, n = k["name"], 2
            while k["name"] in seen:          # 同配置撞名时补个序号
                k["name"] = f"{base} {n}"
                n += 1
            seen.add(k["name"])
            changed += 1
        if changed:
            _write_keys(items)
        return changed


# ---------------------------------------------------------------- IP 拉黑

_blocked: set[str] | None = None          # 懒加载：第一次用到才读配置


def blocked_ips() -> list[str]:
    """已拉黑的 IP（存 settings.json 的 api_blocked_ips，重启还在）。"""
    global _blocked
    if _blocked is None:
        s = ctl.load_settings()
        _blocked = {str(x).strip() for x in (s.get("api_blocked_ips") or [])
                    if str(x).strip()}
    return sorted(_blocked)


def is_blocked(ip: str) -> bool:
    if _blocked is None:
        blocked_ips()
    return str(ip or "").strip() in (_blocked or set())


def block_ip(ip: str, block: bool = True) -> list[str]:
    """拉黑 / 解除拉黑一个 IP，返回最新黑名单。"""
    global _blocked
    ip = str(ip or "").strip()
    if _blocked is None:
        blocked_ips()
    changed = False
    if block and ip and ip not in _blocked:
        _blocked.add(ip)
        changed = True
    elif not block and ip in _blocked:
        _blocked.discard(ip)
        changed = True
    if changed:
        try:
            ctl.save_settings(api_blocked_ips=sorted(_blocked))
        except Exception:                         # noqa: BLE001
            pass
    return sorted(_blocked)


def _bump(kid: str, **delta) -> None:
    with _lock:
        items = keys()
        for k in items:
            if k["id"] == kid:
                for f, v in delta.items():
                    if isinstance(v, (int, float)) and f in k:
                        k[f] = k[f] + v if isinstance(k[f], (int, float)) else v
                    else:
                        k[f] = v
                _write_keys(items)
                return


# ---- 串行排队：每把 Key 一个闸门，一次只放行一个请求；
#      上一个完整返回（含流式输出结束）后，才放行下一个。
_key_gates: dict = {}
_key_waiting: dict = {}


def _key_gate(kid: str):
    with _lock:
        g = _key_gates.get(kid)
        if g is None:
            g = threading.Semaphore(1)
            _key_gates[kid] = g
        return g


def _queue_enter(kid: str) -> None:
    with _lock:
        _key_waiting[kid] = _key_waiting.get(kid, 0) + 1


def _queue_leave(kid: str) -> None:
    with _lock:
        _key_waiting[kid] = max(0, _key_waiting.get(kid, 0) - 1)


def _queue_len(kid: str) -> int:
    with _lock:
        return _key_waiting.get(kid, 0)


# ---------------------------------------------------------------- 历史

def history(limit: int = 120, kid: str = "") -> list[dict]:
    with _lock:
        items = [r for r in _history if not kid or r["key_id"] == kid]
    return items[:limit]


def _record(rec: dict) -> None:
    with _lock:
        _history.appendleft(rec)
    try:
        with _history_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                             # noqa: BLE001
        pass


def load_history_from_disk(limit: int = 120, kid: str = "") -> list[dict]:
    """内存里为空时（刚启动），从落盘记录里补一段。"""
    p = _history_path()
    if not p.is_file():
        return []
    out = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:                 # noqa: BLE001
                    continue
                if not kid or r.get("key_id") == kid:
                    out.append(r)
    except Exception:                             # noqa: BLE001
        return []
    return list(reversed(out))[:limit]


# ---------------------------------------------------------------- 公网入口（UPnP 端口映射）
#
# 不依赖任何第三方服务：app 自带的网关绑定 0.0.0.0，再用 UPnP 让路由器把端口
# 映射到本机，外面就能用 http://<公网IP>:<端口>/<随机后缀>/v1 访问。
# 随机后缀是一段不可猜的路径前缀（相当于只在暗门后面开放 API），
# 既能满足「带随机后缀的地址」，也能挡掉扫描器顺着 /v1 找到你。

_pub: dict = {"active": False, "ext_ip": "", "local_ip": "", "mapped": False,
              "igd": None, "error": "", "since": 0.0}

_SSDP_ADDR = ("239.255.255.250", 1900)
_SSDP_TARGETS = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
                 "urn:schemas-upnp-org:service:WANIPConnection:1",
                 "urn:schemas-upnp-org:service:WANPPPConnection:1")


def _rand_suffix() -> str:
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:14].lower()


def ensure_suffix() -> str:
    """公网地址里的随机后缀（重新生成一次，旧地址立刻失效）。"""
    suf = str(ctl.load_settings().get("api_suffix") or "")
    if len(suf) < 8:
        suf = _rand_suffix()
        ctl.save_settings(api_suffix=suf)
    return suf


def new_suffix() -> str:
    suf = _rand_suffix()
    ctl.save_settings(api_suffix=suf)
    return suf


def suffix() -> str:
    return str(ctl.load_settings().get("api_suffix") or "")


def _ssdp_locations(timeout: float = 2.5) -> list[str]:
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\nST: %s\r\n\r\n')
    for bind_ip in (None, lan_ip()):          # 默认接口搜不到时，绑本机 IP 再搜一轮
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if bind_ip:
            try:
                sock.bind((bind_ip, 0))
            except OSError:                   # noqa: PERF203
                pass
        sock.settimeout(0.6)
        found, until = [], time.time() + max(1.2, timeout / 2)
        try:
            for st in _SSDP_TARGETS:
                try:
                    sock.sendto((msg % st).encode(), _SSDP_ADDR)
                except OSError:
                    pass
            while time.time() < until:
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    break
                m = re.search(r"(?im)^LOCATION:\s*(\S+)", data.decode("utf-8", "replace"))
                if m and m.group(1) not in found:
                    found.append(m.group(1))
        finally:
            sock.close()
        if found:
            return found
    return []


def _igd_service(location: str):
    """从 IGD 描述文件里找 WANIPConnection / WANPPPConnection 的控制地址。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    xml = op.open(location, timeout=5).read().decode("utf-8", "replace")
    for m in re.finditer(r"<service>(.*?)</service>", xml, re.S | re.I):
        block = m.group(1)
        st = re.search(r"<serviceType>(.*?)</serviceType>", block, re.S | re.I)
        cu = re.search(r"<controlURL>(.*?)</controlURL>", block, re.S | re.I)
        if not (st and cu):
            continue
        if "WANIPConnection" in st.group(1) or "WANPPPConnection" in st.group(1):
            return st.group(1).strip(), urllib.parse.urljoin(location, cu.group(1).strip())
    return None, None


def _soap(control: str, stype: str, action: str, body: str, timeout: float = 6) -> str:
    env = ('<?xml version="1.0"?>\n'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
           f'<s:Body><u:{action} xmlns:u="{stype}">{body}</u:{action}></s:Body></s:Envelope>')
    req = urllib.request.Request(control, data=env.encode("utf-8"), method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{stype}#{action}"'})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body_xml = e.read().decode("utf-8", "replace")
            m = re.search(r"<errorDescription>(.*?)</errorDescription>", body_xml, re.S | re.I)
            detail = m.group(1).strip() if m else ""
        except Exception:                     # noqa: BLE001
            pass
        raise ctl.ApiError(f"{action} 被路由器拒绝：{detail or e}") from None


def gateway_ip() -> str:
    """默认网关 IP（给「去路由器管理页开 UPnP / 手动转发」的指引用）。"""
    try:
        out = subprocess.run(["route", "print", "0.0.0.0"], capture_output=True,
                             creationflags=CREATE_NO_WINDOW).stdout.decode("gbk", "replace")
        for line in out.splitlines():
            m = re.match(r"\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                return m.group(1)
    except Exception:                         # noqa: BLE001
        pass
    return ""


def probe_public_port(ext_ip: str, port: int, timeout: float = 3.0) -> bool:
    """从本机连「公网IP:端口」试一把：通了说明端口已转发，外网能访问。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((str(ext_ip), int(port)))
        return True
    except Exception:                         # noqa: BLE001
        return False
    finally:
        s.close()


_fw_rule_done: set[int] = set()


def firewall_rule_name(port: int) -> str:
    return f"Strata API 网关 {port}"


def _rule_exists(name: str) -> bool:
    try:
        q = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        out = q.stdout.decode("gbk", "replace")
        return "规则名称" in out or "Rule Name" in out
    except Exception:                         # noqa: BLE001
        return False


def ensure_firewall_rule(port: int) -> str:
    """防火墙放行入站 TCP 端口：别的设备/外网才能连进来（Windows 默认全拦）。
    没有规则就自动加：已提权就静默成功，否则弹一次系统授权框（UAC）；
    无论成败每次运行只试一次，失败也不影响网关本身。"""
    port = int(port)
    if port in _fw_rule_done:
        return ""
    _fw_rule_done.add(port)
    name = firewall_rule_name(port)
    if _rule_exists(name):
        return "防火墙已放行"
    add_args = ["advfirewall", "firewall", "add", "rule", f"name={name}",
                "dir=in", "action=allow", "protocol=TCP", f"localport={port}"]
    try:
        q = subprocess.run(["netsh", *add_args], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=30)
        if q.returncode == 0:
            return "防火墙已放行"
    except Exception:                         # noqa: BLE001
        pass
    try:                                      # 没提权：弹一次 UAC，用户点「是」即可
        cmd = (f"Start-Process netsh -ArgumentList 'advfirewall firewall add rule "
               f"name=\\\"{name}\\\" dir=in action=allow protocol=TCP localport={port}' "
               f"-Verb RunAs -WindowStyle Hidden -Wait")
        subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=180)
        if _rule_exists(name):
            return "防火墙已放行（经系统授权）"
    except Exception:                         # noqa: BLE001
        pass
    return f"防火墙放行没成功：请在 Windows 防火墙手动放行入站 TCP {port}"


def _ext_ip(control: str, stype: str) -> str:
    xml = _soap(control, stype, "GetExternalIPAddress", "")
    m = re.search(r"<NewExternalIPAddress>(.*?)</NewExternalIPAddress>", xml, re.S | re.I)
    return (m.group(1).strip() if m else "")


def _map_port(control: str, stype: str, ext_port: int, local_ip: str) -> None:
    _soap(control, stype, "AddPortMapping",
          "<NewRemoteHost></NewRemoteHost>"
          f"<NewExternalPort>{ext_port}</NewExternalPort>"
          "<NewProtocol>TCP</NewProtocol>"
          f"<NewInternalPort>{ext_port}</NewInternalPort>"
          f"<NewInternalClient>{local_ip}</NewInternalClient>"
          "<NewEnabled>1</NewEnabled>"
          "<NewPortMappingDescription>Strata API</NewPortMappingDescription>"
          "<NewLeaseDuration>0</NewLeaseDuration>")


def _unmap_port(control: str, stype: str, ext_port: int) -> None:
    _soap(control, stype, "DeletePortMapping",
          "<NewRemoteHost></NewRemoteHost>"
          f"<NewExternalPort>{ext_port}</NewExternalPort>"
          "<NewProtocol>TCP</NewProtocol>")


def _is_private_ip(ip: str) -> bool:
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except Exception:                             # noqa: BLE001
        return True
    return (a in (0, 10, 127) or (a == 192 and b == 168)
            or (a == 172 and 16 <= b <= 31) or (a == 100 and 64 <= b <= 127) or a >= 224)


def _public_ip_via_http() -> str:
    """UPnP 拿不到时，问一个回显服务（HTTP 源在国内更快更稳，拿不到就算了）。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    urls = ("http://members.3322.org/dyndns/getip", "http://ip.3322.net",
            "http://4.ipw.cn", "https://myip.ipip.net", "https://api.ipify.org",
            "https://ipv4.icanhazip.com", "https://api.ip.sb/ip")
    for url in urls:
        try:
            txt = op.open(url, timeout=4).read().decode("utf-8", "replace")
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", txt)
            if m:
                return m.group(1)
        except Exception:                         # noqa: BLE001
            continue
    return ""


def public_info() -> dict:
    port = load()["port"]
    suf = suffix()
    ext = _pub.get("ext_ip") or ""
    addr = f"http://{ext}:{port}/{suf}/v1" if (ext and suf) else ""
    return {"active": bool(_pub["active"]), "addr": addr, "ext_ip": ext,
            "local_ip": _pub.get("local_ip") or lan_ip(), "mapped": bool(_pub["mapped"]),
            "suffix": suf, "error": _pub.get("error", ""), "port": port,
            "reachable": bool(_pub.get("reachable")),
            "tunnel": bool(_pub.get("tunnel")),
            "tunnel_mode": str(_tunnel.get("mode") or "quick"),
            "cf_configured": cf_configured(),
            "gateway": _pub.get("gateway") or gateway_ip(),
            "hint": _pub.get("hint", ""),
            "behind_nat": bool(ext and _is_private_ip(ext))}


def _forward_hint(port: int, local_ip: str) -> str:
    gw = gateway_ip()
    where = f"浏览器打开 http://{gw} 进路由器管理页" if gw else "进路由器管理页"
    return (f"{where}：① 找到 UPnP 设置并打开，然后关-开一次「对外服务」；"
            f"② 或在「端口转发」里手动添加 外部 TCP {port} → {local_ip}:{port}")


_probe_busy = False
_last_probe = 0.0


def reachability_async() -> None:
    """后台探测「公网IP:端口」是否已通（手动转发了就能探到），60 秒冷却。
    通了就清掉错误并把状态标成可达，界面上的警告随之消失。"""
    global _probe_busy, _last_probe
    info = public_info()
    if not info["active"] or not info["ext_ip"] or info["mapped"] or info["reachable"]:
        return
    if _probe_busy or time.time() - _last_probe < 60:
        return
    _probe_busy = True
    _last_probe = time.time()

    def job():
        global _probe_busy
        try:
            if probe_public_port(info["ext_ip"], info["port"], timeout=3.5):
                _pub["reachable"] = True
                _pub["error"] = ""
        finally:
            _probe_busy = False
    threading.Thread(target=job, daemon=True, name="gate-reach").start()


def public_start(port: int | None = None) -> str:
    """开公网入口：网关绑 0.0.0.0 → UPnP 映射端口 → 返回带随机后缀的地址。"""
    port = int(port or load()["port"])
    if not running():
        raise ctl.ApiError("先把 API 网关开起来（勾选「开启对外 API 接口」并保存）")
    suf = ensure_suffix()
    local = lan_ip()
    _pub.update(active=True, ext_ip="", local_ip=local, mapped=False, igd=None,
                reachable=False, tunnel=False, gateway=gateway_ip(), error="",
                since=time.time())

    # 配置了用户自己的 Cloudflare 凭证（Tunnel Token + 自定义域名）→ 直接走命名隧道，
    # 地址固定为 https://<域名>/<后缀>/v1，不需要路由器 UPnP / 端口转发
    if cf_configured():
        try:
            turl = tunnel_start(port)               # token 模式：返回 https://<域名>
            _pub.update(tunnel=True, reachable=True, error="", active=True)
            return (f"Cloudflare 隧道已连接（你的账号）：{turl}/{suf}/v1"
                    "（自定义域名，地址固定不变；隧道断线会自动重连）")
        except ctl.ApiError as e:
            _pub["error"] = f"你的 Cloudflare 隧道没建起来：{e}；改走自动隧道兜底"
    ext, mapped, igd = "", False, None
    try:
        locations = _ssdp_locations()
    except Exception as e:                        # noqa: BLE001
        locations = []
        _pub["error"] = f"UPnP 搜索失败：{e}"
    if not locations:
        _pub["error"] = "路由器 UPnP 没开（搜索不到路由器的 UPnP 设备）"
    for loc in locations:
        try:
            stype, control = _igd_service(loc)
            if not control:
                continue
            ext = _ext_ip(control, stype)
            _map_port(control, stype, port, local)
            mapped, igd = True, (control, stype)
            break
        except ctl.ApiError as e:
            _pub["error"] = str(e)[:160]
        except Exception as e:                    # noqa: BLE001
            _pub["error"] = f"路由器端口映射失败：{str(e)[:120]}"
    if not ext:
        ext = _public_ip_via_http()
    if not mapped and _pub.get("error", "").startswith("路由器端口映射失败"):
        _pub["error"] = _pub["error"] or "路由器端口映射失败"
    _pub.update(ext_ip=ext, mapped=mapped, igd=igd, active=True)
    if not ext:
        raise ctl.ApiError("没拿到公网 IP：宽带可能是运营商大内网（NAT），需要在路由器上手动做端口转发。")
    addr = f"http://{ext}:{port}/{suf}/v1"
    if _is_private_ip(ext):
        return f"公网入口已生成：{addr}（注意：这看着是内网 IP，外网可能访问不到）"
    if not mapped:
        # UPnP 没成也要立刻探一次：如果路由器上已手动转发（或运营商直通），外网照样能访问
        if probe_public_port(ext, port, timeout=3.5):
            _pub["reachable"] = True
            _pub["error"] = ""
            return f"公网入口已生成：{addr}（外网端口已通，可以直接用）"
        # 自动兜底：Cloudflare 快速隧道（免注册、无固定到期，任何网络都能打通）
        try:
            turl = tunnel_start(port)
            _pub.update(tunnel=True, reachable=True, error="")
            taddr = f"{turl}/{suf}/v1"
            return (f"已自动打通外网（隧道模式）：{taddr}"
                    "（流量经第三方中继 Cloudflare；隧道断线会自动重连，届时地址会自动换新）")
        except ctl.ApiError as e:
            _pub["error"] = f"UPnP 没通，自动隧道也没建成：{e}"
            _pub["hint"] = _forward_hint(port, local)
        _pub["error"] = _pub["error"] or "路由器没开 UPnP，端口没映射上"
        _pub["hint"] = _forward_hint(port, local)
        return (f"公网地址没生成，也没能自动打通：{addr} 只是内网可直连。"
                + _pub["hint"])
    return f"公网入口已生成：{addr}"


_probe_busy = False
_last_probe = 0.0

# ---------------------------------------------------------------- 自动隧道兜底
# UPnP / IPv6 都依赖路由器设置；要让「任何电脑、任何网络」全自动打通外网，
# 最后一张牌是加密反向隧道（本机 ssh 反向连接公益中继，中继把外网流量带进来）。
# 免费通道约 60 分钟到期，程序会自动续期换地址（无需人工干预）。

_tunnel: dict = {"proc": None, "url": "", "log": None, "since": 0.0, "watch": None,
                 "pids": [], "mode": "quick"}
_tunnel_lock = threading.Lock()
_tunnel_stop_evt = threading.Event()

_CF_URLS = (
    "https://gh-proxy.com/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    "https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
)


def _cloudflared_exe() -> str:
    """cloudflared 的本机路径；没有就自动下载（走国内镜像优先，一次 55MB）。"""
    p = Path(_keys_path()).parent / "bin" / "cloudflared.exe"
    if p.is_file() and p.stat().st_size > 10_000_000:
        return str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in _CF_URLS:
        try:
            f = Path(str(p) + ".part")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with op.open(req, timeout=300) as r, open(f, "wb") as out:
                total = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
            if total < 10_000_000:                      # 假下载/截断，换下一个源
                continue
            f.replace(p)
            return str(p)
        except Exception:                               # noqa: BLE001
            continue
    try:
        f.unlink(missing_ok=True)
    except Exception:                                   # noqa: BLE001
        pass
    raise ctl.ApiError("cloudflared 自动下载失败（网络不通或镜像全挂），稍后重试；"
                       "也可在路由器上开 UPnP / 端口转发，网关会自动优先用直连")


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                             capture_output=True, timeout=8
                             ).stdout.decode("gbk", "replace")
        return f'"{pid}"' in out
    except Exception:                              # noqa: BLE001
        return True                                # 查不到就当活着，别误判隧道挂了


def _tunnel_adopt_locked(port: int) -> str:
    """上一轮（或上次会话）留下的 cloudflared 还活着就直接收编——接口地址保持不变。
    判定：日志里有标记/URL + 本机有 cloudflared 进程在跑。cloudflared 注册不上边缘会
    自己退出，所以「进程活着」基本等于「隧道健康」；不用 HTTP 探测——本机 DNS
    经常解析不了 trycloudflare 随机子域名，探测会误判。"""
    log_path = Path(_keys_path()).parent / f"tunnel_{port}.log"
    try:
        if time.time() - log_path.stat().st_mtime > 30 * 86400:
            return ""                              # 日志太老，不可信
        txt = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:                              # noqa: BLE001
        return ""
    pids = []
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq cloudflared.exe",
                              "/fo", "csv", "/nh"], capture_output=True,
                             timeout=8).stdout.decode("gbk", "replace")
        for ln in out.splitlines():
            parts = [x.strip().strip('"') for x in ln.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "cloudflared.exe":
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
        if not pids:
            return ""                              # 本机根本没有 cloudflared 在跑
    except Exception:                              # noqa: BLE001
        pass                                       # 进程查不了就按日志收编
    if "mode=token" in txt:                        # 用户自有凭证的命名隧道：地址固定
        cfg = cf_cfg()
        if not (cfg["token"] and cfg["domain"]):
            return ""                              # 凭证被清了就不收编，下次重开
        _tunnel.update(proc=None, url=f"https://{cfg['domain']}", log=None,
                       since=time.time(), watch=None, pids=pids, mode="token")
        return _tunnel["url"]
    urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", txt, re.I)
    if not urls:
        return ""
    url = urls[-1].rstrip("/").lower()
    _tunnel.update(proc=None, url=url, log=None, since=time.time(),
                   watch=None, pids=pids, mode="quick")
    return url


def _tunnel_watchdog(port: int) -> None:
    """隧道进程意外退出时自动重开，保持外网可达。"""
    while not _tunnel_stop_evt.wait(30):
        if _tunnel.get("watch") is not threading.current_thread():
            return                                  # 已有更新的看门狗接管
        p = _tunnel.get("proc")
        if p is not None:
            if p.poll() is None:
                continue                            # 还活着
        else:
            pids = [x for x in (_tunnel.get("pids") or []) if _pid_alive(x)]
            if pids:
                _tunnel["pids"] = pids              # 收编的隧道还活着
                continue
        try:                                        # 进程意外退出 → 重开（优先收编残留）
            tunnel_start(port, timeout=35.0)
        except Exception:                           # noqa: BLE001
            pass


def _tunnel_stop_locked() -> None:
    p = _tunnel.get("proc")
    if p is not None:
        try:
            p.terminate()
        except Exception:                         # noqa: BLE001
            pass
    f = _tunnel.get("log")
    if f is not None:
        try:
            f.close()
        except Exception:                         # noqa: BLE001
            pass
    _tunnel.update(proc=None, url="", log=None, since=0.0, watch=None, pids=[])


def tunnel_stop() -> None:
    _tunnel_stop_evt.set()
    with _tunnel_lock:
        _tunnel_stop_locked()


def tunnel_start(port: int, timeout: float = 40.0) -> str:
    """建立 Cloudflare 隧道，返回中继基础 URL。
    配置了用户自己的凭证（Tunnel Token + 自定义域名）时走命名隧道
    （cloudflared tunnel run --token），地址固定为 https://<域名>；
    没配置就走免注册的 trycloudflare 快速隧道（随机地址）。
    两种模式都只要进程活着就一直沿用同一个地址：本会话复用、跨开关复用、
    跨控制台重启收编残留进程——只有进程真死了才会重开。"""
    cfg = cf_cfg()
    token_mode = bool(cfg["token"] and cfg["domain"])
    with _tunnel_lock:
        p = _tunnel.get("proc")
        if p is not None and p.poll() is None and _tunnel.get("url"):
            return _tunnel["url"]                   # 本会话已在跑：地址不变
        if (_tunnel.get("pids") and _tunnel.get("url")
                and _tunnel.get("mode") == ("token" if token_mode else "quick")
                and all(_pid_alive(x) for x in _tunnel["pids"])):
            return _tunnel["url"]                   # 收编的隧道还活着：地址不变
        _tunnel_stop_locked()
        _tunnel_stop_evt.clear()
        url = _tunnel_adopt_locked(port)            # 先试着收编上一轮残留的
        if url and _tunnel.get("mode") == ("token" if token_mode else "quick"):
            watch = threading.Thread(target=_tunnel_watchdog, args=(port,),
                                     daemon=True, name="tunnel-watch")
            watch.start()
            _tunnel["watch"] = watch
            return url
        exe = _cloudflared_exe()
        log_path = Path(_keys_path()).parent / f"tunnel_{port}.log"
        try:
            f = open(log_path, "w", encoding="utf-8")
            f.write(f"mode={'token' if token_mode else 'quick'}\n")
            f.flush()
        except Exception as e:                    # noqa: BLE001
            raise ctl.ApiError(f"隧道日志写不了：{e}") from None
        # --protocol http2：QUIC(UDP) 在不少国内网络被限速/拦截，
        # 只能注册上 1 条连接导致边缘回源 530；HTTP2 走 TCP 443 稳定得多
        if token_mode:
            cmd = [exe, "tunnel", "--no-autoupdate", "--protocol", "http2",
                   "run", "--token", cfg["token"]]
        else:
            cmd = [exe, "tunnel", "--url", f"http://127.0.0.1:{port}",
                   "--protocol", "http2", "--no-autoupdate"]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=f,
                                    creationflags=CREATE_NO_WINDOW)
        except Exception as e:                    # noqa: BLE001
            f.close()
            raise ctl.ApiError(f"cloudflared 启动失败：{e}") from None
        _tunnel.update(proc=proc, url="", log=f, since=time.time(), watch=None,
                       pids=[], mode="token" if token_mode else "quick")
        _tunnel_stop_evt.clear()
        if token_mode:
            # 命名隧道不往日志里打 URL：注册上边缘（进程稳定存活 ~6 秒）就算成功；
            # token 错误 / 网络不通时 cloudflared 会很快自己退出
            until = time.time() + min(timeout, 15.0)
            while time.time() < until:
                if proc.poll() is not None:
                    break
                time.sleep(0.5)
            if proc.poll() is None:
                url = f"https://{cfg['domain']}"
                _tunnel["url"] = url
                watch = threading.Thread(target=_tunnel_watchdog, args=(port,),
                                         daemon=True, name="tunnel-watch")
                watch.start()
                _tunnel["watch"] = watch
                return url
            _tunnel_stop_locked()
            raise ctl.ApiError("你的 Cloudflare 命名隧道没建起来（Token 无效、隧道已删除或网络被拦截），"
                               "请检查 Cloudflare Zero Trust 后台的隧道凭证；清空 Token 可回落到自动隧道")
        pat = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)
        until = time.time() + timeout
        while time.time() < until:
            if proc.poll() is not None:
                break
            try:
                txt = Path(log_path).read_text(encoding="utf-8", errors="replace")
            except Exception:                     # noqa: BLE001
                txt = ""
            m = pat.search(txt)
            if m:
                url = m.group(0).rstrip("/").lower()
                _tunnel["url"] = url
                watch = threading.Thread(target=_tunnel_watchdog, args=(port,),
                                         daemon=True, name="tunnel-watch")
                watch.start()
                _tunnel["watch"] = watch
                return url
            time.sleep(0.5)
        _tunnel_stop_locked()
        raise ctl.ApiError("自动隧道没建成（网络不通或被拦截），可稍后重试；"
                           "或在路由器上开 UPnP / 做端口转发后自动切换为直连")


def public_stop(silent: bool = False) -> str:
    # 注意：不杀隧道进程——「对外服务」关了只是网关不接客，接口地址保持不变，
    # 下次开启（甚至下次打开控制台）继续用同一个地址；只有点「重新生成」才换。
    port = load()["port"]
    igd = _pub.get("igd")
    if igd:
        try:
            _unmap_port(igd[0], igd[1], port)
        except Exception:                         # noqa: BLE001
            pass
    was = _pub["active"]
    _pub.update(active=False, igd=None, mapped=False, error="", tunnel=False)
    if silent:
        return ""
    return "公网入口已关停（路由器的端口映射也已撤销）" if was else "公网入口本来就没开"
# ---------------------------------------------------------------- 鉴权 / 限额

def _key_header(handler) -> str:
    a = (handler.headers.get("Authorization") or "").strip()
    if a.lower().startswith("bearer "):
        return a[7:].strip()
    return (handler.headers.get("x-api-key") or handler.headers.get("api-key") or "").strip()


def _prompt_text(body: dict) -> str:
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        msgs = body.get("input") if isinstance(body.get("input"), list) else []
    parts = []
    for m in msgs[-6:]:
        if not isinstance(m, dict):
            if isinstance(m, str):
                parts.append(m)
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for x in c:
                if isinstance(x, dict):
                    if isinstance(x.get("text"), str):
                        parts.append(x["text"])
                    elif x.get("type") == "input_text" and isinstance(x.get("content"), str):
                        parts.append(x["content"])
    if body.get("system") and isinstance(body.get("system"), str):
        parts.insert(0, body["system"])
    text = "\n".join(parts)
    if not text and isinstance(body.get("prompt"), str):
        text = body["prompt"]
    return text


def _reply_text(raw: bytes, ctype: str) -> tuple[str, dict]:
    """从上游响应里抠出正文预览和 usage。"""
    usage = {}
    text = ""
    if not raw:
        return "", usage
    if "text/event-stream" in (ctype or "") or raw[:6] == b"data: ":
        chunks = []
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload in (b"[DONE]", b""):
                continue
            try:
                d = json.loads(payload)
            except Exception:                     # noqa: BLE001
                continue
            if isinstance(d.get("usage"), dict):
                usage = d["usage"]
            for keypath in ("delta", "content_block_delta", "message"):
                seg = d.get(keypath)
                if isinstance(seg, dict):
                    for f in ("content", "text"):
                        v = seg.get(f)
                        if isinstance(v, str):
                            chunks.append(v)
            if isinstance(d.get("choices"), list) and d["choices"]:
                msg = (d["choices"][0] or {}).get("message") or {}
                if isinstance(msg.get("content"), str):
                    chunks.append(msg["content"])
            if d.get("type") == "content_block_delta":
                dd = d.get("delta") or {}
                if isinstance(dd.get("text"), str):
                    chunks.append(dd["text"])
        text = "".join(chunks)
        return text, usage
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except Exception:                             # noqa: BLE001
        return raw.decode("utf-8", "replace")[:400], usage
    if isinstance(d.get("usage"), dict):
        usage = d["usage"]
    ch = d.get("choices")
    if isinstance(ch, list) and ch:
        msg = (ch[0] or {}).get("message") or {}
        if isinstance(msg.get("content"), str):
            text = msg["content"]
        elif isinstance(msg.get("content"), list):
            text = "".join(x.get("text", "") for x in msg["content"] if isinstance(x, dict))
    if not text:
        cont = d.get("content")
        if isinstance(cont, list):
            text = "".join(x.get("text", "") for x in cont if isinstance(x, dict))
        elif isinstance(cont, str):
            text = cont
    if not text and isinstance(d.get("output_text"), str):
        text = d["output_text"]
    return text, usage


def _usage_tokens(usage: dict, prompt_txt: str, reply_txt: str) -> tuple[int, int]:
    pin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    pout = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    if not pin and prompt_txt:
        pin = est_tokens(prompt_txt)
    if not pout and reply_txt:
        pout = est_tokens(reply_txt)
    return pin, pout


def _qps_hit(kid: str, limit: float) -> bool:
    if limit <= 0:
        return False
    now = time.time()
    with _lock:
        win = _qps_win.setdefault(kid, deque())
        while win and now - win[0] > 1.0:
            win.popleft()
        if len(win) >= max(1, int(round(limit))):
            return True
        win.append(now)
        return False


# ---------------------------------------------------------------- 处理器

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StrataGate/2.0"
    sys_version = ""

    def log_message(self, fmt, *args):            # noqa: A003
        pass

    # -------------------------------------------------- 小工具
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, x-api-key, anthropic-version, anthropic-beta")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:                         # noqa: BLE001
            pass

    def _body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding", "").lower() == "chunked"):
            chunks = []
            while True:
                line = self.rfile.readline(65536).strip()
                try:
                    n = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if n == 0:
                    self.rfile.readline(65536)
                    break
                chunks.append(self.rfile.read(n))
                self.rfile.read(2)
            return b"".join(chunks)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _client_ip(self) -> str:
        xff = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return xff or (self.client_address[0] if self.client_address else "")

    # -------------------------------------------------- 路由
    def do_OPTIONS(self):                         # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):                             # noqa: N802
        self._proxy("GET")

    def do_POST(self):                            # noqa: N802
        self._proxy("POST")

    def do_DELETE(self):                          # noqa: N802
        self._proxy("DELETE")

    def do_PUT(self):                             # noqa: N802
        self._proxy("PUT")

    # -------------------------------------------------- 主逻辑
    def _proxy(self, method: str):
        path = self.path.split("?")[0].rstrip("/") or "/"
        query = ("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""
        upstream = getattr(self.server, "upstream", ctl.current_base)()

        # ---- IP 黑名单：被拉黑的地址什么都拿不到，直接 403
        ip = self._client_ip()
        if is_blocked(ip):
            stats["denied"] += 1
            stats["last"] = "403 IP 已拉黑"
            stats["last_at"] = time.time()
            _record({"key_id": "", "key_name": "-", "ip": ip, "path": path, "model": "",
                     "prompt": "", "tokens_in": 0, "tokens_out": 0, "ms": 0, "reply": "",
                     "status": "IP 已拉黑", "time": time.time()})
            self._json(403, {"error": {"message": f"你的 IP（{ip}）已被拉黑，禁止访问本接口",
                                       "type": "permission_error", "code": 403}})
            return

        # 公网模式：地址里带一段随机后缀（/<后缀>/v1/...）。
        # 带了后缀就认；公网模式下不带后缀直接 404，扫描器顺着 /v1 摸不进来。
        mode = load()["mode"]
        suf = suffix()
        pref = f"/{suf}" if suf else ""
        if pref and (path == pref or path.startswith(pref + "/")):
            path = path[len(pref):] or "/"
        elif mode == "public":
            self._json(404, {"error": {"message": "地址不对：公网入口需要带随机后缀（见控制台里复制的地址）",
                                       "type": "not_found", "code": 404}})
            return

        if path in ("/", ""):
            self._json(200, {"service": "strata-gate", "mode": mode,
                             "endpoints": ["/v1/models", "/v1/chat/completions",
                                           "/v1/messages", "/v1/responses"]})
            return
        if path == "/gate/health":
            self._json(200, {"service": "strata-gate", "status": "ok",
                             "upstream": upstream, "mode": mode,
                             "keys": len([k for k in keys() if k["enabled"]])})
            return
        if not path.startswith("/v1/"):
            self._json(404, {"error": {"message": f"这里只开放 /v1/* 接口（{path} 不对外）",
                                       "type": "not_found", "code": 404}})
            return

        stats["requests"] += 1
        started = time.time()

        # ---- 鉴权
        secret = _key_header(self)
        rec = find_key(secret) if secret else None
        if rec is None or not rec["enabled"]:
            stats["denied"] += 1
            stats["last"] = "401 Key 无效"
            stats["last_at"] = time.time()
            self._reject(401, "API Key 无效或已停用：请求头带 Authorization: Bearer <Key>",
                         "authentication_error", {"key_id": "", "key_name": "-", "ip": ip,
                                                  "path": path, "status": "401 Key 无效",
                                                  "tokens_in": 0, "tokens_out": 0,
                                                  "ms": 0, "prompt": "", "reply": "", "model": ""})
            return

        body = self._body() if method in ("POST", "PUT", "DELETE") else None
        bj = {}
        if body:
            try:
                bj = json.loads(body.decode("utf-8", "replace"))
            except Exception:                     # noqa: BLE001
                bj = {}
        prompt_txt = _prompt_text(bj) if bj else ""
        model = str(bj.get("model") or "") if bj else ""
        in_est = est_tokens(prompt_txt) if prompt_txt else 0
        # 记录这把 Key 的来源 IP（最新在前，最多留 6 个），界面上直接可见、可一键拉黑
        try:
            ips = [x for x in (rec.get("ips") or []) if x and x != ip]
            ips.insert(0, ip)
            _bump(rec["id"], last_ip=ip, ips=ips[:6])
        except Exception:                         # noqa: BLE001
            pass
        base_rec = {"key_id": rec["id"], "key_name": rec["name"], "ip": ip, "path": path,
                    "model": model, "prompt": _clip(prompt_txt, 120),
                    "tokens_in": in_est, "tokens_out": 0, "ms": 0, "reply": ""}

        def limited(reason: str, code: int):
            stats["limited"] += 1
            stats["last"] = f"{code} {reason}"
            stats["last_at"] = time.time()
            _bump(rec["id"], limited=1)
            r = dict(base_rec, status=reason, ms=int((time.time() - started) * 1000))
            self._reject(code, f"[{rec['name']}] {reason}", "rate_limit_error", r)

        # ---- 额度 / 上下文（QPS 在拿到串行闸门后再查，排队不占额度）
        if rec["token_limit"] and rec["used_tokens"] >= rec["token_limit"]:
            limited(f"token 额度已用完（{rec['used_tokens']}/{rec['token_limit']}）", 403)
            return
        if rec["context_limit"] and in_est > rec["context_limit"]:
            limited(f"上下文超长（约 {in_est} > 上限 {rec['context_limit']} token）", 400)
            return

        # ---- 串行排队：同一把 Key 一次只放行一个请求，前面的完整返回后才开始下一个
        gate = _key_gate(rec["id"])
        if _queue_len(rec["id"]) >= 8:
            limited(f"排队已满（已有 8 个在等），请稍后再试", 429)
            return
        _queue_enter(rec["id"])
        try:
            gate.acquire()
        finally:
            _queue_leave(rec["id"])
        try:
            # 轮到自己了再查一次：停用 / 删除的 Key，排队中的请求立刻取消，不再执行
            now_rec = find_key(rec["secret"])
            if now_rec is None or not now_rec["enabled"]:
                stats["denied"] += 1
                stats["last"] = "403 Key 已停用"
                stats["last_at"] = time.time()
                _record(dict(base_rec, status="Key 已停用（排队取消）",
                             ms=int((time.time() - started) * 1000), time=time.time()))
                self._json(403, {"error": {
                    "message": f"[{rec['name']}] Key 已停用或已删除，排队中的请求已取消",
                    "type": "permission_error", "code": 403}})
                return
            self._forward(method, path, query, body, rec, base_rec,
                          prompt_txt, model, in_est, upstream, started, ip)
        finally:
            gate.release()

    def _forward(self, method, path, query, body, rec, base_rec,
                 prompt_txt, model, in_est, upstream, started, ip):
        """真正转发 + 计量（调用方已拿到该 Key 的串行闸门）。"""
        # ---- QPS：排队轮到自己了再查，QPS=1 时同一秒内只接一个
        if _qps_hit(rec["id"], rec["qps"]):
            stats["limited"] += 1
            stats["last"] = "429 QPS 超限"
            stats["last_at"] = time.time()
            _bump(rec["id"], limited=1)
            r = dict(base_rec, status=f"QPS 超限（限 {rec['qps']:g}/秒）",
                     ms=int((time.time() - started) * 1000))
            self._reject(429, f"[{rec['name']}] QPS 超限（限 {rec['qps']:g}/秒）",
                         "rate_limit_error", r)
            return

        # ---- 转发
        headers = {}
        for h in self.headers.keys():
            if h.lower() in _FWD_REQ:
                headers[h] = self.headers[h]
        headers["Accept-Encoding"] = "identity"
        up_key = upstream_cfg()["key"]
        if up_key:
            headers["Authorization"] = f"Bearer {up_key}"
            headers["x-api-key"] = up_key

        req = urllib.request.Request(upstream + path + query, data=body,
                                     headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            r = opener.open(req, timeout=1800)
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:                     # noqa: BLE001
                pass
            stats["errors"] += 1
            stats["last"] = f"上游 {e.code}"
            stats["last_at"] = time.time()
            _record(dict(base_rec, status=f"上游 {e.code}",
                         ms=int((time.time() - started) * 1000), time=time.time()))
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(raw)))
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(raw)
            except Exception:                     # noqa: BLE001
                pass
            return
        except Exception as e:                    # noqa: BLE001
            stats["errors"] += 1
            _record(dict(base_rec, status="上游连不上",
                         ms=int((time.time() - started) * 1000), time=time.time()))
            self._json(502, {"error": {
                "message": f"连不上本机 Strata 服务（{upstream}）：{e}。先在控制台点「启动模型」。",
                "type": "gateway_error", "code": 502}})
            return

        # ---- 回写 + 计量
        sink: list[bytes] = []
        status_txt = "ok"
        try:
            status_txt = self._relay(r, sink, rec["max_seconds"])
        finally:
            try:
                r.close()
            except Exception:                     # noqa: BLE001
                pass

        raw = b"".join(sink)
        reply_txt, usage = _reply_text(raw, r.headers.get("Content-Type", ""))
        pin, pout = _usage_tokens(usage, prompt_txt, reply_txt)
        total = pin + pout
        ms = int((time.time() - started) * 1000)
        if status_txt == "ok":
            stats["ok"] += 1
        elif status_txt.startswith("超时"):
            stats["limited"] += 1
            _bump(rec["id"], limited=1)
        stats["last"] = f"{method} {path} ({rec['name']})"
        stats["last_at"] = time.time()
        _bump(rec["id"], used_tokens=total, requests=1, last_ip=ip, last_at=time.time())
        _record(dict(base_rec, status=status_txt, tokens_in=pin, tokens_out=pout,
                     ms=ms, reply=_clip(reply_txt, 160), time=time.time()))

    def _reject(self, code: int, msg: str, kind: str, rec: dict) -> None:
        rec = dict(rec)
        rec["status"] = rec.get("status") or msg
        rec.setdefault("time", time.time())
        _record(rec)
        self._json(code, {"error": {"message": msg, "type": kind, "code": code}})

    def _relay(self, r, sink: list, max_seconds: int = 0) -> str:
        length = r.headers.get("Content-Length")
        ctype = r.headers.get("Content-Type", "")
        chunked = length is None
        self.send_response(r.status)
        for h in r.headers.keys():
            if h.lower() in _FWD_RES and h.lower() != "content-length":
                self.send_header(h, r.headers[h])
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", length)
        self._cors()
        self.end_headers()

        reader = getattr(r, "read1", r.read)
        began = time.time()
        note = "ok"
        try:
            while True:
                if max_seconds and time.time() - began > max_seconds:
                    note = f"超时中断（限 {max_seconds} 秒）"
                    break
                buf = reader(2048)
                if not buf:
                    break
                if len(sink) < 4096:              # 只留 8MB 左右用于解析
                    sink.append(buf)
                if chunked:
                    self.wfile.write(b"%x\r\n" % len(buf) + buf + b"\r\n")
                else:
                    self.wfile.write(buf)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            note = "客户端断开"
        if chunked:
            try:
                if note != "ok":                  # 中断时给流式客户端一个明确的错误帧
                    piece = ("data: " + json.dumps(
                        {"error": {"message": note, "type": "rate_limit_error"}},
                        ensure_ascii=False) + "\n\n").encode("utf-8")
                    self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:                     # noqa: BLE001
                pass
        return note


# ---------------------------------------------------------------- 启停

def running() -> bool:
    return _srv is not None


class _Gate6Server(ThreadingHTTPServer):
    """IPv4 + IPv6 双栈监听（:: + IPV6_V6ONLY=0）。
    路由器开了 IPv6 后，外网可直接用本机的公网 IPv6 地址访问，无需端口转发。"""

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:                           # noqa: BLE001
            pass                                  # 平台不支持双栈也能跑（只有 v6）
        super().server_bind()


def global_ipv6() -> str:
    """本机的公网 IPv6 地址（2xxx 开头的全球单播）；没有返回空串。"""
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            ip = info[4][0].split("%")[0]
            if ip.startswith(("fe80", "fc", "fd", "::1")):
                continue                          # 链路本地 / ULA / 回环不算
            if ip.count(":") >= 2:
                return ip
    except Exception:                             # noqa: BLE001
        pass
    return ""


def v6_base(port: int | None = None, mode: str | None = None) -> str:
    """本机 IPv6 直连地址（路由器开 IPv6 后外网可用）；没有 IPv6 返回空串。"""
    ip = global_ipv6()
    if not ip:
        return ""
    s = load()
    mode = mode or s["mode"]
    port = int(port or s["port"])
    mid = f"/{suffix()}" if mode == "public" and suffix() else ""
    return f"http://[{ip}]:{port}{mid}/v1"


def start(mode: str | None = None, port: int | None = None, upstream=None) -> str:
    global _srv, _thread
    s = load()
    mode = mode or s["mode"]
    port = int(port or s["port"])
    host = bind_host(mode)
    with _lock:
        if _srv is not None:
            _stop_locked()
        try:
            if mode == "local":
                srv = ThreadingHTTPServer((host, port), _Handler)
            else:
                try:                              # 双栈：IPv4 + IPv6 一起听
                    srv = _Gate6Server(("::", port), _Handler)
                except OSError as e:
                    if e.errno in (10048, 98):    # 端口被占
                        raise
                    srv = ThreadingHTTPServer((host, port), _Handler)   # 无 v6 时回落纯 v4
        except OSError as e:
            raise ctl.ApiError(f"端口 {port} 起不来（{e}）。换个端口，或先关掉占用它的程序。") from None
        srv.daemon_threads = True
        srv.upstream = upstream or upstream_base         # type: ignore[attr-defined]
        _srv = srv
        _thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.3},
                                   daemon=True, name="strata-gate")
        _thread.start()
        stats["started_at"] = time.time()
    if mode != "local":
        # 别的设备/外网要连进来，必须放行防火墙（失败不影响网关，日志有提示的职责在界面层）
        threading.Thread(target=ensure_firewall_rule, args=(port,), daemon=True,
                         name="gate-fw").start()
    where = {"local": "仅本机", "lan": "局域网", "public": "公网（隧道）"}[mode]
    return f"API 网关已开启（{where}），端口 {port}"


def _stop_locked() -> None:
    global _srv, _thread
    if _srv is not None:
        try:
            _srv.shutdown()
        except Exception:                         # noqa: BLE001
            pass
        try:
            _srv.server_close()
        except Exception:                         # noqa: BLE001
            pass
    _srv, _thread = None, None


def stop() -> str:
    with _lock:
        was = _srv is not None
        _stop_locked()
    public_stop(silent=True)
    return "API 网关已关闭" if was else "API 网关本来就没开"


def apply(enabled: bool, mode: str, port: int) -> str:
    """保存并按开关/模式启停；公网模式额外做端口映射。"""
    save(enabled=enabled, mode=mode, port=port)
    if not enabled:
        return stop() if running() else "已保存（对外接口未开启）"
    if mode != "public":
        public_stop(silent=True)                  # 切走就把端口映射撤掉，不留后门
    msg = start(mode=mode, port=port)
    if mode == "public":
        try:
            msg += "；" + public_start(port)
        except ctl.ApiError as e:
            msg += f"；公网入口没开成：{e}"
    return msg


def autostart() -> str:
    s = load()
    if not s["enabled"]:
        return ""
    keys()                                        # 确保 Key 表存在
    msg = start(mode=s["mode"], port=s["port"])
    if s["mode"] == "public":
        try:
            msg += "；" + public_start(s["port"])
        except ctl.ApiError as e:
            msg += f"；公网入口没开成：{e}"
    return msg


def snapshot(kid: str = "", limit: int = 80) -> dict:
    """给界面用的一份快照。"""
    ks = keys()
    for k in ks:
        k["secret_hint"] = k["secret"][:14] + "…" + k["secret"][-4:]
        k["limited"] = k.get("limited", 0)
    hist = history(limit, kid)
    if not hist:
        hist = load_history_from_disk(limit, kid)
    return {"running": running(), "mode": load()["mode"], "port": load()["port"],
            "addr": public_base(), "public": public_info(), "keys": ks,
            "history": hist, "stats": dict(stats)}
