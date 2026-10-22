# -*- coding: utf-8 -*-
"""Strata 桌面控制台 - 控制层。

只做"事情"：读写配置、调服务端 HTTP 接口、管进程、清缓存。
不涉及任何界面，方便单独测试。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------- 路径 / 常量

STRATA_DIR = Path(r"F:\Qwen3.8-27b\qwen27b\Strata")
PORT = 8080
HOST = "127.0.0.1"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def current_base() -> str:
    """服务地址（端口可被"接入"功能改掉，所以每次现算）。"""
    return f"http://{HOST}:{PORT}"


def set_port(p: int) -> None:
    global PORT
    PORT = int(p)


def set_strata_dir(path) -> bool:
    """切换到另一个盘上的 Strata 安装目录。看起来不像就返回 False。"""
    global STRATA_DIR, CONFIG, SERVER_PY
    d = Path(path)
    looks_right = (d / "serve" / "server.py").is_file() or (d / "engine").is_dir()
    if not looks_right:
        return False
    STRATA_DIR = d
    CONFIG = find_config()
    SERVER_PY = d / "serve" / "server.py"
    return True


def find_config() -> Path | None:
    """strata-<model>.json：优先 iq3_xxs，否则目录里第一个。"""
    preferred = STRATA_DIR / "strata-iq3_xxs.json"
    if preferred.is_file():
        return preferred
    for p in sorted(STRATA_DIR.glob("strata-*.json")):
        if not p.name.endswith((".bak", ".before")):
            return p
    return None


def find_python() -> Path:
    p = STRATA_DIR / ".venv" / "Scripts" / "python.exe"
    if p.is_file():
        return p
    raise FileNotFoundError(f"找不到 Strata 的 Python：{p}")


CONFIG = find_config()
SERVER_PY = STRATA_DIR / "serve" / "server.py"


# ---------------------------------------------------------------- 配置读写

def load_config() -> dict:
    if CONFIG is None:
        raise FileNotFoundError(f"在 {STRATA_DIR} 里没找到 strata-*.json")
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def config_value(key: str, default=None):
    try:
        return load_config().get(key, default)
    except Exception:
        return default


def save_config(cfg: dict, backup: bool = True) -> Path | None:
    if CONFIG is None:
        raise FileNotFoundError("没有配置文件")
    bak = None
    if backup and CONFIG.is_file():
        bak = CONFIG.with_name(CONFIG.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(CONFIG, bak)
    CONFIG.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    return bak


def idle_unload_seconds() -> float:
    """配置里的自动卸载秒数（0 = 永不卸载）。"""
    v = config_value("idle_unload_s", 0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def disable_idle_unload() -> tuple[bool, str]:
    """把 idle_unload_s 设为 0。返回 (是否有改动, 说明)。"""
    cfg = load_config()
    old = cfg.get("idle_unload_s", 0)
    try:
        old_n = float(old or 0)
    except (TypeError, ValueError):
        old_n = 0.0
    if old_n == 0 and "idle_unload_s" in cfg:
        return False, "自动卸载本来就是关闭的（idle_unload_s = 0）"
    cfg["idle_unload_s"] = 0
    bak = save_config(cfg)
    return True, ("已把 idle_unload_s 从 %g 秒改为 0（永不自动卸载）" % old_n) + (
        f"；原文件备份：{bak.name}" if bak else "")


# ---------------------------------------------------------------- HTTP

class ApiError(RuntimeError):
    pass


def http_json(path: str, method: str = "GET", payload=None, timeout: float = 15.0,
              port: int | None = None) -> dict:
    url = (f"http://{HOST}:{int(port)}" if port else current_base()) + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"   # 控制类接口要求（缺了会 415）
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # 127.0.0.1 绝不过代理，否则某些环境下会 502/拒绝
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", {}).get("message") or body
        except Exception:
            msg = body or str(e)
        raise ApiError(f"HTTP {e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise ApiError(f"连不上服务（{e.reason}）") from None
    except TimeoutError:
        raise ApiError("请求超时") from None


def health(timeout: float = 3.0) -> dict | None:
    """服务在跑就返回 /health 的字典，否则 None。"""
    try:
        return http_json("/health", timeout=timeout)
    except Exception:
        return None


def server_running() -> bool:
    return health(timeout=2.0) is not None


def health_for(port: int, timeout: float = 1.5) -> dict | None:
    """查任意端口的 /health（不改当前接入）。"""
    try:
        return http_json("/health", timeout=timeout, port=port)
    except Exception:
        return None


def port_state(port: int) -> str:
    """free = 没人监听；strata = 已经是 Strata；other = 被别的程序占着。"""
    if health_for(port) is not None:
        return "strata"
    try:
        with socket.create_connection((HOST, int(port)), timeout=0.5):
            return "other"
    except OSError:
        return "free"


def find_free_port(start: int, limit: int = 40) -> int:
    for p in range(int(start), int(start) + limit):
        if p > 65535:
            break
        if port_state(p) == "free":
            return p
    return 0


def config_port(default: int = 8080) -> int:
    """配置里写的监听端口。起模型必须用它，不能被「接入」改出来的端口带偏。"""
    try:
        v = config_value("port", default)
        p = int(str(v).strip())
        return p if 0 < p < 65536 else default
    except Exception:
        return default


def ensure_config_port() -> tuple[int, int]:
    """把 PORT 校正回配置里的端口，返回 (原端口, 现端口)。"""
    old = PORT
    p = config_port()
    if p and p != old:
        set_port(p)
    return old, PORT


_model_cache = {"at": 0.0, "name": "", "port": -1}


def served_model(timeout: float = 1.2) -> str:
    """服务端实际在跑哪个模型：/props → /v1/models，5 秒缓存。"""
    now = time.time()
    if now - _model_cache["at"] < 5 and _model_cache["port"] == PORT:
        return _model_cache["name"]
    name = ""
    try:
        j = http_json("/props", timeout=timeout)
        name = str(j.get("model") or j.get("model_alias") or j.get("model_name") or "")
        if not name and j.get("model_path"):
            name = Path(str(j["model_path"])).name
    except Exception:
        pass
    if not name:
        try:
            j = http_json("/v1/models", timeout=timeout)
            data = j.get("data") or []
            if data:
                name = str(data[0].get("id") or "")
        except Exception:
            pass
    _model_cache.update(at=now, name=name, port=PORT)
    return name


def inspect_model() -> dict:
    """看一眼安装目录：配置 / 引擎 / 模型文件在不在（不启动任何东西）。"""
    out = {"dir": str(STRATA_DIR), "config": None, "engine": None, "engine_ver": "",
           "model_name": "", "paths": [], "missing": [], "ok": False, "notes": []}
    cfg = find_config()
    if cfg is None:
        out["notes"].append("目录下没有 strata-*.json 配置")
        return out
    out["config"] = str(cfg)
    try:
        j = json.loads(cfg.read_text(encoding="utf-8-sig"))
    except Exception as e:                     # noqa: BLE001
        out["notes"].append(f"配置读不出来：{e}")
        return out
    out["model_name"] = str(j.get("model_name") or "")

    eng = Path(str(j.get("exe") or ""))
    if not eng.is_file():
        alt = STRATA_DIR / "engine" / "strata.exe"
        if alt.is_file():
            out["notes"].append("配置里的 exe 路径不对，改用 engine/strata.exe")
            eng = alt
    if eng.is_file():
        out["engine"] = str(eng)
        bj = eng.parent / "BUILD.json"
        if bj.is_file():
            try:
                out["engine_ver"] = str(json.loads(bj.read_text(encoding="utf-8")).get("version", ""))
            except Exception:                  # noqa: BLE001
                pass
    else:
        out["missing"].append(str(eng))

    args = [str(a) for a in (j.get("args") or [])]
    hints = ("--native", "--ple-gguf", "--pack", "--mtp", "--expert-profile",
             "--control-vector-scaled")
    for i, a in enumerate(args):
        if a in hints and i + 1 < len(args):
            out["paths"].append(re.sub(r":[\d.]+$", "", args[i + 1]))
    if j.get("tokenizer"):
        out["paths"].append(str(j["tokenizer"]))
    vis = j.get("vision") or {}
    for key in ("exe", "mmproj", "model"):
        if vis.get(key):
            out["paths"].append(str(vis[key]))
    for pth in out["paths"]:
        if pth and not Path(pth).exists():
            out["missing"].append(pth)
    out["ok"] = bool(out["engine"]) and not out["missing"]
    if out["missing"]:
        out["notes"].append(f"{len(out['missing'])} 个路径不存在（模型可能没下完或被挪走）")
    return out



# ---------------------------------------------------------------- 进程

def _run(args: list[str], timeout: float = 20.0, cwd: str | Path | None = None) -> tuple[int, str, str]:
    """跑一个 Windows 控制台程序，返回 (返回码, stdout, stderr)；输出按 OEM 代码页解码。"""
    try:
        p = subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, cwd=str(cwd) if cwd else None)
        rc, out, err = p.returncode, p.stdout or b"", p.stderr or b""
    except subprocess.TimeoutExpired:
        return -1, "", "超时"
    except Exception as e:                     # noqa: BLE001
        return -1, "", str(e)

    def dec(raw: bytes) -> str:
        for enc in ("oem", "mbcs", "utf-8"):
            try:
                return raw.decode(enc)
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", "replace")
    return rc, dec(out), dec(err)


def _run_oem(args: list[str], timeout: float = 20.0, cwd: str | Path | None = None) -> str:
    """只要输出的那个用法（tasklist / netstat / nvidia-smi）。"""
    return _run(args, timeout=timeout, cwd=cwd)[1]


def _tasklist(name: str) -> list[int]:
    out = _run_oem(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"], timeout=15)
    pids = []
    for line in out.splitlines():
        m = re.match(r'"([^"]+)","(\d+)"', line.strip())
        if m and m.group(1).lower() == name.lower():
            pids.append(int(m.group(2)))
    return pids


def open_url(url: str) -> str:
    """用默认浏览器打开 URL。按可靠性依次尝试，返回实际用的方式；全失败则抛异常。"""
    errs = []
    try:
        os.startfile(url)                      # ShellExecute：Windows 上最直接
        return "os.startfile"
    except Exception as e:                     # noqa: BLE001
        errs.append(f"startfile: {e}")
    try:
        subprocess.Popen(["explorer.exe", url], creationflags=CREATE_NO_WINDOW)
        return "explorer.exe"
    except Exception as e:                     # noqa: BLE001
        errs.append(f"explorer: {e}")
    try:
        import webbrowser
        if webbrowser.open(url):
            return "webbrowser"
        errs.append("webbrowser 返回 False")
    except Exception as e:                     # noqa: BLE001
        errs.append(f"webbrowser: {e}")
    raise ApiError("打不开浏览器（" + "；".join(errs) + "）")


def free_vram_mib() -> int | None:
    """显卡当前空闲显存（MiB）；读不到返回 None。"""
    try:
        out = _run_oem(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                       timeout=15)
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return None


def engine_pids() -> list[int]:
    return _tasklist("strata.exe")


def vision_pids() -> list[int]:
    return _tasklist("strata-vision.exe")


def port_pids(port: int = PORT) -> list[int]:
    """监听该端口的进程 PID（netstat 解析，不依赖 psutil）。"""
    out = _run_oem(["netstat", "-ano"], timeout=25)
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper().startswith("TCP") and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}") and parts[4].isdigit():
                pids.append(int(parts[4]))
    return sorted(set(pids))


def kill_pid(pid: int, tree: bool = True) -> None:
    args = ["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else [])
    _run_oem(args, timeout=30)


# ---------------------------------------------------------------- 模型 / 服务

_server_proc: subprocess.Popen | None = None


def start_server(log_sink=None) -> subprocess.Popen:
    """启动 Strata 服务（异步，不等待加载完成）。log_sink(line) 会收到服务端输出。"""
    global _server_proc
    ensure_config_port()                   # 「接入」换过端口时先回到模型自己的端口
    if server_running():
        raise ApiError("服务已经在运行了")
    if CONFIG is None:
        raise ApiError(f"{STRATA_DIR} 下没有 strata-*.json 配置")
    if not SERVER_PY.is_file():
        raise ApiError(f"找不到 {SERVER_PY}")
    info = inspect_model()
    if not info["config"]:
        raise ApiError("没找到 strata-*.json 配置")
    if info["missing"]:
        head = "；".join(Path(x).name for x in info["missing"][:3])
        raise ApiError(f"模型文件不齐，先补齐再启动：{head}")
    state = port_state(PORT)
    if state == "other":
        free = find_free_port(PORT + 1)
        raise ApiError(f"端口 {PORT} 被别的程序占用（不是 Strata）。"
                       + (f"换到 {free} 再启动。" if free else "换个端口再启动。"))
    py = find_python()

    env = os.environ.copy()
    for k in ("PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS"):   # WorkBuddy 的 shim 会干扰（见技能笔记）
        env.pop(k, None)

    logfile = STRATA_DIR / "strata-launcher-server.log"
    lf = open(logfile, "ab", buffering=0)
    proc = subprocess.Popen(
        [str(py), str(SERVER_PY), "--engine", "strata", "--config", str(CONFIG), "--port", str(PORT)],
        cwd=str(STRATA_DIR), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )
    _server_proc = proc

    def pump():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            try:
                lf.write((line + "\n").encode("utf-8", "replace"))
            except Exception:
                pass
            if log_sink:
                try:
                    log_sink(line)
                except Exception:
                    pass

    import threading
    threading.Thread(target=pump, daemon=True).start()
    return proc


def stop_server() -> str:
    """停掉整个服务（前端 + 引擎 + 视觉编码器）。返回说明。"""
    global _server_proc
    pids = port_pids()
    if not pids and _server_proc is None:
        # 服务没在跑，但可能有残留引擎进程
        left = engine_pids() + vision_pids()
        for p in left:
            kill_pid(p)
        return "服务本来就没在运行" + (f"；清理了 {len(left)} 个残留进程" if left else "")
    for p in pids:
        kill_pid(p, tree=True)
    if _server_proc is not None:
        try:
            _server_proc.wait(timeout=10)
        except Exception:
            pass
        _server_proc = None
    time.sleep(0.6)
    for p in engine_pids() + vision_pids():     # 兜底
        kill_pid(p)
    return "服务已停止"


def unload_model() -> str:
    """从显存卸载模型（服务继续运行，下次请求再加载）。"""
    r = http_json("/unload", method="POST", payload={}, timeout=120)
    st = r.get("status")
    if st == "busy":
        return "有请求正在处理，暂时不能卸载"
    return "模型已从显存卸载（服务仍在运行）"


def load_model() -> str:
    """让服务加载模型（已加载则立即返回）。"""
    r = http_json("/load", method="POST", payload={}, timeout=900)
    return "模型已加载" if r.get("status") == "loaded" else f"返回：{r}"


# ---------------------------------------------------------------- 视觉模型

VISION_NEEDS_MIB = 1600          # 编码器在 GPU 上要 ~1.3 GiB，留点余量


def start_vision() -> str:
    """让服务把图像编码器拉起来（POST /load）。

    注意顺序：引擎已经在显存里时，空闲显存只剩两三百兆，编码器一加载就会把引擎挤崩
    （引擎日志自己会写 "that is little room ... if the engine exits right after this line"）。
    所以此时先拒绝，并让用户先卸载模型——卸载后再 /load，服务会**先**起编码器再让引擎
    按剩下的显存分配专家缓存，顺序才安全。
    """
    if not server_running():
        raise ApiError("服务没在运行，先启动模型")
    if vision_pids():
        return "视觉模型已经在运行"
    h = health()
    if h and h.get("loaded"):
        free = free_vram_mib()
        if free is not None and free < VISION_NEEDS_MIB:
            raise ApiError(
                f"显存只剩 {free} MiB，编码器需要约 {VISION_NEEDS_MIB} MiB：现在拉起它会把引擎挤崩。\n"
                f"做法：先点「卸载模型」，再点「启动视觉模型」（卸载后服务会先起编码器、再加载模型，顺序才安全）。\n"
                f"想随时开关视觉模型又不卸载：把 --vram-reserve-mib 从 700 提到 1800（网页 Settings 里改 "
                f"vram_reserve_mib）并重启服务，代价是专家缓存小一点、解码慢几个百分点。")
    http_json("/load", method="POST", payload={}, timeout=900)
    time.sleep(1.0)
    return "视觉模型已启动" if vision_pids() else "已发出启动请求，但没看到编码器进程（看日志）"


def stop_vision() -> str:
    """结束图像编码器进程，把显存还回去。

    再启动它时若引擎仍在显存里，可能因为显存不够而失败（见 start_vision）。
    """
    pids = vision_pids()
    if not pids:
        return "视觉模型本来就没在运行"
    for p in pids:
        kill_pid(p, tree=False)
    time.sleep(0.8)
    return (f"已关闭视觉模型（结束 {len(pids)} 个进程），显存已还回。"
            f"注意：引擎还在显存里时重新启动它可能挤爆引擎——先「卸载模型」再启动更稳。")


# ---------------------------------------------------------------- 缓存清理

def _dir_size(d: Path) -> int:
    total = 0
    try:
        for p in d.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def cache_report() -> dict:
    """列出可清理的东西及大小。"""
    tmp = Path(tempfile.gettempdir())
    vdirs = sorted([p for p in tmp.glob("strata-vision-*") if p.is_dir()],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    logs = [p for p in (STRATA_DIR / "strata-iq3_xxs.log",
                        STRATA_DIR / "strata-frontend.out",
                        STRATA_DIR / "strata-launcher-server.log") if p.is_file()]
    return {
        "vision_dirs": vdirs,
        "vision_bytes": sum(_dir_size(d) for d in vdirs),
        "logs": logs,
        "log_bytes": sum(p.stat().st_size for p in logs),
    }


def clear_cache(vision: bool = True, logs: bool = True) -> dict:
    """清理图片编码缓存（临时目录）与日志。返回 {释放字节, 明细}。"""
    rep = cache_report()
    freed = 0
    notes = []
    running = bool(vision_pids())
    dirs = rep["vision_dirs"]
    if vision and dirs:
        # 编码器在跑时，最新那个目录可能正在用：跳过它
        skip = dirs[0] if running and dirs else None
        for d in dirs:
            if d == skip:
                continue
            size = _dir_size(d)
            shutil.rmtree(d, ignore_errors=True)
            freed += size
        notes.append(f"图片编码缓存：清理 {len(dirs) - (1 if skip else 0)} 个临时目录"
                     + ("（正在使用的 1 个已跳过）" if skip else ""))
    if logs:
        for p in rep["logs"]:
            try:
                size = p.stat().st_size
                with open(p, "wb"):
                    pass
                freed += size
            except OSError:
                pass
        notes.append(f"日志：清空 {len(rep['logs'])} 个文件")
    return {"freed": freed, "notes": notes}


# ---------------------------------------------------------------- 服务端指标

def metrics(timeout: float = 5.0) -> dict | None:
    """GET /metrics：引擎参数、实时状态、硬件、历史曲线。服务没在跑返回 None。"""
    try:
        return http_json("/metrics", timeout=timeout)
    except Exception:
        return None


def status_v1(timeout: float = 5.0) -> dict | None:
    """GET /v1/status：运行时间、上下文上下限、视觉可用性、内存/显存。"""
    try:
        return http_json("/v1/status", timeout=timeout)
    except Exception:
        return None


# ---------------------------------------------------------------- 上下文长度

def max_context_setting() -> int | None:
    """运行配置 args 里的 --max-context（要重启服务才生效的那个）。"""
    try:
        args = [str(x) for x in load_config().get("args", [])]
    except Exception:
        return None
    for i, a in enumerate(args):
        if a == "--max-context" and i + 1 < len(args):
            try:
                return int(str(args[i + 1]).replace("_", ""))
            except ValueError:
                return None
    return None


def set_max_context(n: int) -> tuple[bool, str]:
    """把 --max-context 写进运行配置。返回 (是否有改动, 说明)。"""
    n = int(n)
    cfg = load_config()
    args = [str(x) for x in cfg.get("args", [])]
    old = None
    for i, a in enumerate(args):
        if a == "--max-context" and i + 1 < len(args):
            old = args[i + 1]
            args[i + 1] = str(n)
            break
    if old is None:
        args += ["--max-context", str(n)]
        old = "未设置"
    if str(old) == str(n):
        return False, f"配置里已经是 {n}"
    cfg["args"] = args
    bak = save_config(cfg)
    return True, (f"--max-context 已改为 {n}（原 {old}）"
                  + (f"；备份 {bak.name}" if bak else "") + "；重启服务后生效")


# ---------------------------------------------------------------- 版本 / 更新

REPO = "Niko1221/Strata"
API = f"https://api.github.com/repos/{REPO}"
# github.com 在这台机器上直连不通，只能走镜像；git 的全局代理是死的，要 -c 清掉
GIT_MIRRORS = [f"https://gh-proxy.com/https://github.com/{REPO}",
               f"https://ghproxy.net/https://github.com/{REPO}",
               f"https://github.com/{REPO}"]
DL_MIRRORS = ["https://gh-proxy.com/", "https://ghproxy.net/", ""]
ASSET = "strata-windows-x64.zip"          # 这台机器用的：Windows / NVIDIA / CUDA 13


def _openers():
    """先用直连，再用环境里的代理（谁通算谁）。"""
    return (urllib.request.build_opener(urllib.request.ProxyHandler({})),
            urllib.request.build_opener())


def _json_get(url: str, timeout: float = 25.0) -> dict:
    last = None
    for op in _openers():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "strata-control",
                                                       "Accept": "application/vnd.github+json"})
            with op.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                 # noqa: BLE001
            last = e
    raise ApiError(f"读不到 {url}（{last}）")


def _vtuple(s: str) -> tuple:
    return tuple(int(x) for x in str(s or "0").split(".")[:4] if x.isdigit())


def local_commit(short: bool = True) -> str:
    rc, out, _ = _run(["git", "rev-parse", "HEAD"], timeout=30, cwd=STRATA_DIR)
    s = out.strip().splitlines()[0] if out.strip() else ""
    return s[:7] if (short and s) else s


def engine_version() -> str:
    """engine/BUILD.json 里的引擎版本（服务没启动也能读）。"""
    try:
        return str(json.loads((STRATA_DIR / "engine" / "BUILD.json").read_text(encoding="utf-8"))
                   .get("version", "?"))
    except Exception:
        return "?"


def update_check(timeout: float = 25.0) -> dict:
    """问 GitHub：源码 main 到哪了、最新发行版是哪个。"""
    info = {"local": local_commit(), "remote": None, "behind": None, "shas": True,
            "engine": engine_version(), "latest": None, "published": None,
            "asset": ASSET, "size": None, "has_update": False, "error": None}
    try:
        c = _json_get(API + "/commits/main", timeout)
        info["remote"] = str(c.get("sha", ""))[:7]
        try:
            cm = _json_get(f"{API}/compare/{info['local']}...main", timeout)
            info["behind"] = cm.get("behind_by")
        except Exception:                     # noqa: BLE001 - 只是"落后几个提交"，查不到无所谓
            pass
        rel = _json_get(API + "/releases/latest", timeout)
        info["latest"] = str(rel.get("tag_name") or "").lstrip("v")
        info["published"] = rel.get("published_at")
        assets = {a.get("name"): a for a in rel.get("assets", [])}
        info["asset"] = ASSET if ASSET in assets else next(iter(assets), None)
        if info["asset"]:
            info["size"] = (assets[info["asset"]] or {}).get("size")
    except Exception as e:                    # noqa: BLE001
        info["error"] = str(e)
        return info
    if info["remote"] and info["remote"] != info["local"]:
        info["has_update"] = True
    if info["latest"] and info["engine"] not in ("?", "") and \
            _vtuple(info["latest"]) > _vtuple(info["engine"]):
        info["has_update"] = True
    return info


def _download(url: str, dest: Path, size: int | None, sink=None, progress=None) -> None:
    """镜像下不动就试下一个：gh-proxy → ghproxy → 直连；每种再分直连/环境代理。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for m in DL_MIRRORS:
        u = m + url
        for op in _openers():
            try:
                if dest.exists():
                    dest.unlink()
                req = urllib.request.Request(u, headers={"User-Agent": "strata-control"})
                with op.open(req, timeout=60) as r, open(dest, "wb") as f:
                    got, t0 = 0, time.time()
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if progress and size:
                            mb = got / 2 ** 20
                            speed = mb / max(0.1, time.time() - t0)
                            progress(min(0.99, got / size), f"{mb:.0f} / {size / 2 ** 20:.0f} MB（{speed:.1f} MB/s）")
                got = dest.stat().st_size if dest.exists() else 0
                if size and got != size:
                    last = RuntimeError(f"大小不对：{got} ≠ {size}")
                    continue
                if sink:
                    sink(f"下载完成：{u}")
                return
            except Exception as e:             # noqa: BLE001
                last = e
    raise ApiError(f"下载失败（{last}）")


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha_from_release(tag: str, asset: str) -> str | None:
    """发行版里带 SHA256.txt，拿它对一下下载的包。"""
    try:
        for op in _openers():
            for m in DL_MIRRORS:
                u = f"{m}https://github.com/{REPO}/releases/download/{tag}/SHA256.txt"
                try:
                    req = urllib.request.Request(u, headers={"User-Agent": "strata-control"})
                    with op.open(req, timeout=30) as r:
                        text = r.read().decode("utf-8", "replace")
                    for line in text.splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and asset in parts[-1]:
                            return parts[0].lower()
                except Exception:              # noqa: BLE001
                    continue
    except Exception:                          # noqa: BLE001
        pass
    return None


def _stream(cmd: list[str], cwd: Path, sink=None, timeout: float = 3600.0) -> int:
    """跑一条命令并把输出一行行交给 sink（GBK 安全解码）。"""
    env = os.environ.copy()
    for k in ("PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS"):
        env.pop(k, None)
    p = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=CREATE_NO_WINDOW)
    if not p.stdout:
        return p.wait(timeout=timeout)
    for raw in iter(p.stdout.readline, b""):
        line = raw.decode("oem", "replace").rstrip()
        if line and sink:
            sink(line)
    try:
        return p.wait(timeout=timeout)
    except Exception:                          # noqa: BLE001
        p.kill()
        return -1


def perform_update(sink=None, progress=None) -> str:
    """一键更新：拉源码（镜像）→ 下引擎包（镜像 + sha256 校验）→ setup.py --update 安装。

    必须先停掉服务，否则 engine/strata.exe 被占用换不掉。
    """
    def say(t: str):
        if sink:
            sink(t)

    if server_running():
        raise ApiError("模型服务还在运行——引擎文件被占用时换不了。先点「关闭模型」。")

    eng = STRATA_DIR / "engine"
    build = eng / "BUILD.json"
    if not build.is_file():                    # 缺了它 update_installed_engine 会直接 return，什么都不做且不出声
        prev = eng / "BUILD.json.prev"
        if prev.is_file():
            prev.replace(build)
            say("engine/BUILD.json 之前被移开了，已放回（缺了它更新会静默跳过）")
        else:
            raise ApiError(f"{build} 不存在，判断不了已装的引擎版本。请先在 Strata 目录跑一次 setup.py。")

    old_commit, old_engine = local_commit(), engine_version()
    say(f"更新前：源码 {old_commit}，引擎 {old_engine}")

    say("拉取源码 …")
    fetched = False
    for m in GIT_MIRRORS:
        say("  试 " + m)
        rc, out, err = _run(["git", "-c", "http.proxy=", "-c", "https.proxy=", "fetch", m, "main"],
                            timeout=900, cwd=STRATA_DIR)
        if rc == 0:
            fetched = True
            say(f"  已拉取（{m}）")
            break
        say(f"  失败：{(err or out).strip()[:200]}")
    if not fetched:
        raise ApiError("三个源都拉不到——检查网络，或稍后再试。")

    new_sha = local_commit()                   # 先记下旧的，方便回退
    _run(["git", "branch", "-f", "pre-update-backup", old_commit], timeout=60, cwd=STRATA_DIR)
    rc, out, err = _run(["git", "checkout", "-B", "main", "FETCH_HEAD"], timeout=300, cwd=STRATA_DIR)
    if rc != 0:
        raise ApiError("切换到新版本失败：" + (err or out).strip()[:300])
    say(f"源码已更新：{old_commit} → {local_commit()}（旧版在分支 pre-update-backup）")

    rel = _json_get(API + "/releases/latest")
    tag = str(rel.get("tag_name") or "").strip()
    assets = {a.get("name"): a for a in rel.get("assets", [])}
    asset = ASSET if ASSET in assets else None
    if not asset:
        raise ApiError(f"发行版 {tag} 里没有 {ASSET}。")
    size = (assets[asset] or {}).get("size")

    dest_dir = Path(tempfile.gettempdir()) / f"strata-update-{tag.lstrip('v')}"
    dest = dest_dir / asset
    if dest.is_file() and (not size or dest.stat().st_size == size):
        say(f"安装包已在本地：{dest}（{size / 2 ** 20:.0f} MB），跳过下载")
    else:
        say(f"下载 {asset}（{size / 2 ** 20:.0f} MB）…")
        _download(f"https://github.com/{REPO}/releases/download/{tag}/{asset}",
                  dest, size, sink=say, progress=progress)

    want = _sha_from_release(tag, asset)
    got = _sha256_of(dest)
    if want and want != got:
        raise ApiError(f"校验失败：下载的包 sha256 是 {got[:16]}…，发行版写的是 {want[:16]}…（已删掉重下）\n{dest}")
    say(f"sha256 校验通过（{got[:16]}…）" if want else f"sha256 {got[:16]}…（发行版没给，跳过校验）")

    say("安装引擎（setup.py --update）…")
    rc = _stream([find_python(), "setup.py", "--update", "--prebuilt", str(dest_dir) + os.sep],
                 STRATA_DIR, sink=say)
    if rc != 0:
        raise ApiError(f"setup.py 退出码 {rc}——看上面的输出。源码已更新到 {local_commit()}，"
                       f"需要的话可以 git checkout pre-update-backup 退回去。")

    new_engine = engine_version()
    try:                                       # 165 MB 的包没必要留着
        dest.unlink(missing_ok=True)
    except Exception:                          # noqa: BLE001
        pass
    return (f"更新完成：源码 {old_commit} → {local_commit()}，引擎 {old_engine} → {new_engine}。"
            f"旧引擎留在 engine/.previous，想退回就跑 setup.py --rollback-engine。"
            f"点「启动模型」就能用新版。")


# ---------------------------------------------------------------- 本机接口发现
# 8080 被别的模型占了是常事：与其猜，不如把本机所有在监听的端口都问一遍，
# 看谁的 /health 长得像 Strata（或 llama.cpp / ollama）。netstat 给出的端口不多，
# 并发 GET 一遍只要一两秒。

def listening_ports() -> list[int]:
    """本机所有 TCP LISTENING 端口（本机地址上的），用 netstat，比全端口扫快得多。"""
    out = _run_oem(["netstat", "-ano", "-p", "TCP"], timeout=30)
    ports = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if not parts[3].upper().startswith("LISTEN"):
            continue
        local = parts[1]
        if ":" not in local:
            continue
        addr, _, p = local.rpartition(":")
        if not p.isdigit():
            continue
        if addr in ("127.0.0.1", "0.0.0.0", "::1", "[::1]", "[::]", "::"):
            ports.add(int(p))
    return sorted(ports)


def _http_text(url: str, timeout: float = 0.8):
    """(状态码, 前 4 KB 文本)；连不上返回 (None, None)。127.0.0.1 不走代理。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": "strata-control"})
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, r.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(4096).decode("utf-8", "replace")
        except Exception:                      # noqa: BLE001
            return e.code, ""
    except Exception:                          # noqa: BLE001
        return None, None


def _clean(s, n: int = 60) -> str:
    """只留 ASCII + 中日韩常用字符，别把二进制响应塞进界面。"""
    def ok(ch: str) -> bool:
        o = ord(ch)
        return (0x20 <= o < 0x7F) or (0x3000 <= o <= 0x9FFF) or (0xFF00 <= o <= 0xFFEF)
    s = "".join(ch if (ch.isprintable() and ok(ch)) else " " for ch in str(s or ""))
    return " ".join(s.split())[:n]


def _probe_one(port: int, timeout: float = 0.8) -> dict | None:
    code, body = _http_text(f"http://{HOST}:{port}/health", timeout)
    if code is None:
        return None
    j = None
    if body:
        try:
            j = json.loads(body)
        except Exception:                      # noqa: BLE001
            j = None
    if isinstance(j, dict):
        if j.get("service") == "strata" or ("status" in j and "loaded" in j and "max_context" in j):
            kind = "strata"
        elif "model_path" in j or "default_generation_settings" in j:
            kind = "llamacpp"
        else:
            kind = "http"
        model = j.get("model") or j.get("model_path") or j.get("model_name") or j.get("model_alias") or ""
        ctx = j.get("max_context") or (j.get("default_generation_settings") or {}).get("n_ctx")
        return {"port": port, "kind": kind, "model": _clean(model), "ctx": ctx,
                "loaded": j.get("loaded"), "version": j.get("engine") or j.get("version")}
    if body and "Ollama" in body:
        return {"port": port, "kind": "ollama", "model": "ollama", "ctx": None,
                "loaded": None, "version": None}
    if code == 200:
        # /health 说不上它是谁：再问 /v1/models，认出 OpenAI 兼容的大模型服务
        c2, b2 = _http_text(f"http://{HOST}:{port}/v1/models", timeout)
        j2 = None
        if b2:
            try:
                j2 = json.loads(b2)
            except Exception:                  # noqa: BLE001
                j2 = None
        if isinstance(j2, dict) and isinstance(j2.get("data"), list) and j2.get("data"):
            names = ", ".join(str(d.get("id") or "?") for d in j2["data"][:3])
            return {"port": port, "kind": "openai", "model": _clean(names),
                    "ctx": None, "loaded": None, "version": j2.get("version")}
        return {"port": port, "kind": "http", "model": _clean(body or "（不是 JSON 的 HTTP 服务）"),
                "ctx": None, "loaded": None, "version": None}
    # /health 不是 200：再看一眼根路径，认 ollama 之类的
    c2, b2 = _http_text(f"http://{HOST}:{port}/", timeout)
    if b2 and "Ollama" in b2:
        return {"port": port, "kind": "ollama", "model": "ollama", "ctx": None,
                "loaded": None, "version": None}
    return None


def discover_servers(timeout: float = 0.8, workers: int = 48, progress=None) -> list[dict]:
    """把本机在监听的端口全问一遍，返回能认出来的服务（strata 排前面）。"""
    ports = listening_ports()
    found: list[dict] = []
    if not ports:
        return found
    done = 0
    with ThreadPoolExecutor(max_workers=min(workers, len(ports))) as ex:
        futs = [ex.submit(_probe_one, p, timeout) for p in ports]
        for f in as_completed(futs):
            done += 1
            if progress:
                progress(done, len(ports))
            try:
                r = f.result()
            except Exception:                  # noqa: BLE001
                r = None
            if r:
                found.append(r)
    order = {"strata": 0, "llamacpp": 1, "ollama": 2, "http": 3}
    found.sort(key=lambda x: (order.get(x["kind"], 9), x["port"]))
    return found


# ---------------------------------------------------------------- 跨盘找 Strata
# 模型下到哪个盘都行：只要找到那一份 Strata（有 serve/server.py 或 engine/），
# 读它的运行配置就能连上。带时间预算，扫不完就先给已经找到的。

SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata",
             "$recycle.bin", "system volume information", "node_modules", ".git",
             "appdata", "perflogs", "recovery", "windows.old", "winsxs", "drivers",
             "temp", "tmp", "cache", "caches", "venv", ".venv", "site-packages"}


def drive_roots() -> list[Path]:
    roots = []
    for c in string.ascii_uppercase:
        p = Path(f"{c}:\\")
        try:
            if p.exists():
                roots.append(p)
        except Exception:                      # noqa: BLE001
            continue
    return roots


def describe_install(d: Path) -> dict:
    ver = "?"
    bj = d / "engine" / "BUILD.json"
    if bj.is_file():
        try:
            ver = str(json.loads(bj.read_text(encoding="utf-8")).get("version", "?"))
        except Exception:                      # noqa: BLE001
            pass
    cfgs = sorted(p.name for p in d.glob("strata-*.json")
                  if not p.name.endswith((".bak", ".before")))
    return {"dir": str(d), "engine": ver,
            "server": (d / "serve" / "server.py").is_file(),
            "python": (d / ".venv" / "Scripts" / "python.exe").is_file(),
            "configs": cfgs[:8], "nconfigs": len(cfgs)}


def find_strata_installs(max_depth: int = 3, budget_s: float = 25.0, progress=None) -> list[dict]:
    """在所有盘里找 Strata 目录（目录名叫 strata，或里面有 serve/server.py）。"""
    t0 = time.time()
    hits: list[dict] = []
    seen: set[str] = set()
    scanned = [0]

    def scan(d: Path, depth: int):
        if time.time() - t0 > budget_s:
            return
        try:
            entries = list(os.scandir(d))
        except Exception:                      # noqa: BLE001
            return
        for e in entries:
            if time.time() - t0 > budget_s:
                return
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except Exception:                  # noqa: BLE001
                continue
            name = e.name.lower()
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            p = Path(e.path)
            scanned[0] += 1
            if progress and scanned[0] % 200 == 0:
                progress(scanned[0], str(p)[:60])
            if name == "strata" or (p / "serve" / "server.py").is_file():
                key = str(p).lower()
                if key not in seen:
                    seen.add(key)
                    hits.append(describe_install(p))
                continue                       # 找到了就不再往里钻
            if depth < max_depth:
                scan(p, depth + 1)

    for root in drive_roots():
        scan(root, 1)
        if time.time() - t0 > budget_s:
            break
    hits.sort(key=lambda h: (not h["server"], h["dir"]))
    return hits


def config_model_paths() -> dict:
    """当前运行配置里模型文件都在哪儿（哪个盘一目了然）。"""
    try:
        args = [str(x) for x in load_config().get("args", [])]
    except Exception:                          # noqa: BLE001
        return {}
    out = {}
    for key in ("--pack", "--native", "--ple-gguf", "--mtp", "--expert-profile"):
        for i, a in enumerate(args):
            if a == key and i + 1 < len(args):
                out[key] = args[i + 1]
                break
    return out


# ---------------------------------------------------------------- 设置持久化

def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "StrataControl"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:                          # noqa: BLE001
        d = Path(tempfile.gettempdir()) / "StrataControl"
        d.mkdir(parents=True, exist_ok=True)
    return d / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except Exception:                          # noqa: BLE001
        return {}


def save_settings(**patch) -> dict:
    s = load_settings()
    s.update({k: v for k, v in patch.items() if v is not None})
    try:
        settings_path().write_text(json.dumps(s, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception:                          # noqa: BLE001
        pass
    return s


# ---------------------------------------------------------------- 开机自启
# 用标准库 winreg 写 HKCU 的 Run 项：不需要管理员权限，也不用去调 reg.exe。

RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "StrataControl"


def exe_path() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return str(Path(__file__).resolve().parent / "app.py")


def autostart_value() -> str | None:
    """注册表里那条命令；没有返回 None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_SUBKEY) as k:
            v, _ = winreg.QueryValueEx(k, RUN_NAME)
            return str(v)
    except Exception:                          # noqa: BLE001 - 没有这一项就是没开
        return None


def autostart_enabled() -> bool:
    return autostart_value() is not None


def set_autostart(on: bool, args: str = "") -> str:
    import winreg
    if on:
        cmd = f'"{exe_path()}"'
        if args:
            cmd += " " + args
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_SUBKEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
        return "已开启开机自启：" + cmd
    if not autostart_enabled():
        return "开机自启本来就是关的"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_SUBKEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        try:
            winreg.DeleteValue(k, RUN_NAME)
        except FileNotFoundError:
            pass
    return "已关闭开机自启"


# ---------------------------------------------------------------- 系统硬件监控
# 不依赖模型服务：CPU/内存走 kernel32，GPU 走 NVML（驱动自带 nvml.dll）。

import ctypes                                              # noqa: E402


class _MemStatusEx(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


_cpu_prev: tuple[int, int, int] | None = None


def cpu_percent() -> float | None:
    """整机 CPU 占用 %（两次采样差分，Poller 每 2 秒调一次正好）。"""
    global _cpu_prev
    try:
        k32 = ctypes.windll.kernel32
        idle = ctypes.c_ulonglong()
        kern = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        if not k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return None
        cur = (idle.value, kern.value, user.value)
    except Exception:                                      # noqa: BLE001
        return None
    prev = _cpu_prev
    _cpu_prev = cur
    if not prev:
        return None
    d_idle = cur[0] - prev[0]
    d_total = (cur[0] + cur[1] + cur[2]) - (prev[0] + prev[1] + prev[2])
    if d_total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total)))


def ram_info() -> dict:
    """物理内存 {used, total, pct}，单位 GB。"""
    st = _MemStatusEx()
    st.dwLength = ctypes.sizeof(_MemStatusEx)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return {}
    except Exception:                                      # noqa: BLE001
        return {}
    total = st.ullTotalPhys / 2 ** 30
    used = (st.ullTotalPhys - st.ullAvailPhys) / 2 ** 30
    return {"used": used, "total": total, "pct": st.dwMemoryLoad}


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


_nvml = None
_nvml_dev = None
_nvml_state = 0                            # 0=没试过 1=好了 2=不行
_nvml_name = ""


def _nvml_init() -> bool:
    global _nvml, _nvml_dev, _nvml_state, _nvml_name
    if _nvml_state == 1:
        return True
    if _nvml_state == 2:
        return False
    for path in ("nvml.dll", r"C:\Windows\System32\nvml.dll",
                 r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"):
        try:
            lib = ctypes.CDLL(path)
            if lib.nvmlInit() != 0:
                continue
            dev = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) != 0:
                continue
            buf = ctypes.create_string_buffer(96)
            try:
                lib.nvmlDeviceGetName(dev, buf, 96)
                raw = buf.value or b""
                try:
                    _nvml_name = raw.decode("utf-8")
                except UnicodeDecodeError:
                    _nvml_name = raw.decode("gbk", "replace")
            except Exception:                              # noqa: BLE001
                pass
            _nvml, _nvml_dev, _nvml_state = lib, dev, 1
            return True
        except OSError:
            continue
    _nvml_state = 2
    return False


def gpu_info() -> dict | None:
    """0 号 GPU {name, util, mem_used, mem_total, temp}，显存单位 GB。"""
    if not _nvml_init():
        return None
    try:
        lib, dev = _nvml, _nvml_dev
        u = _NvmlUtil()
        if lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u)) != 0:
            return None
        m = _NvmlMem()
        lib.nvmlDeviceGetMemoryInfo(dev, ctypes.byref(m))
        t = ctypes.c_uint()
        lib.nvmlDeviceGetTemperature(dev, 0, ctypes.byref(t))
        return {"name": _nvml_name, "util": u.gpu,
                "mem_used": m.used / 2 ** 30, "mem_total": m.total / 2 ** 30,
                "temp": t.value}
    except Exception:                                      # noqa: BLE001
        return None


def sys_sample() -> dict:
    """一次系统采样：{cpu, ram, gpu}，任何一项失败都是 None/缺省。"""
    return {"cpu": cpu_percent(), "ram": ram_info(), "gpu": gpu_info()}
