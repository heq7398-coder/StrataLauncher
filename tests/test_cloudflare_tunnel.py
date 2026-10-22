# -*- coding: utf-8 -*-
"""验证：Cloudflare 命名隧道（Tunnel Token + 自定义域名）全链路逻辑。"""
import subprocess
import time
from pathlib import Path

import wx

import api_gate


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


result = {}
app = wx.App(False)
frame = None
real_run = api_gate.subprocess.run
try:
    import app as ui
    ui.boot_settings()
    frame = ui.MainFrame()
    frame.Show()
    for _ in range(20):
        wx.YieldIfNeeded()
        time.sleep(0.05)
    result["layout_ok"] = bool(getattr(frame, "btn_gate_cf", None))
    log("布局含「Cloudflare 隧道配置」按钮:", result["layout_ok"])
    log("on_cf_config 存在:", callable(getattr(frame, "on_cf_config", None)))

    # ---- 1) 配置存取
    api_gate.save_cf(token="  ey-test-token  ", domain="  API.Example.com/ ")
    cfg = api_gate.cf_cfg()
    result["cfg_roundtrip"] = (cfg["token"] == "ey-test-token" and cfg["domain"] == "api.example.com")
    result["cf_configured"] = api_gate.cf_configured()
    log("配置存取:", cfg, "configured:", result["cf_configured"])
    api_gate.save_cf(token="ey-test-token", domain="")
    result["need_both"] = not api_gate.cf_configured()
    log("只填 Token 不算配置好:", result["need_both"])

    # ---- 2) token 模式命令行 + 地址
    api_gate.save_cf(token="ey-test-token", domain="api.example.com")

    # 用真实 Popen 起一个长眠进程当「cloudflared」，避免逐个补 fake 属性
    real_popen = api_gate.subprocess.Popen
    real_exe = api_gate._cloudflared_exe
    fp_holder = {}

    def fake_popen(cmd, **kw):
        if "tunnel" in cmd:
            fp_holder["cmd"] = cmd
            fp_holder["proc"] = real_popen(["cmd", "/c", "ping", "-n", "30", "127.0.0.1"],
                                           stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            return fp_holder["proc"]
        return real_popen(cmd, **kw)

    api_gate._cloudflared_exe = lambda: "C:/fake/cloudflared.exe"
    api_gate.subprocess.Popen = fake_popen
    api_gate.tunnel_stop()                           # 清掉收编/复用状态，强制走 spawn
    try:
        (Path(api_gate._keys_path()).parent / "tunnel_8241.log").unlink(missing_ok=True)
        url = api_gate.tunnel_start(8241, timeout=5.0)
        result["token_cmd"] = (fp_holder["cmd"][:2] == ["C:/fake/cloudflared.exe", "tunnel"]
                               and "run" in fp_holder["cmd"] and "--token" in fp_holder["cmd"]
                               and fp_holder["cmd"][fp_holder["cmd"].index("--token") + 1] == "ey-test-token"
                               and "--protocol" in fp_holder["cmd"])
        result["token_url"] = (url == "https://api.example.com")
        result["mode_flag"] = (api_gate._tunnel.get("mode") == "token")
        log("token 命令行:", fp_holder.get("cmd"))
        log("token 模式地址:", url)
        logp = Path(api_gate._keys_path()).parent / "tunnel_8241.log"
        result["log_marker"] = logp.exists() and "mode=token" in logp.read_text(encoding="utf-8", errors="replace")
        log("日志含 mode=token 标记:", result["log_marker"])
    finally:
        api_gate.subprocess.Popen = real_popen
        api_gate._cloudflared_exe = real_exe
        api_gate.tunnel_stop()

    # ---- 3) token 隧道收编（残留进程 + 日志标记）
    logp = Path(api_gate._keys_path()).parent / "tunnel_8241.log"
    logp.write_text("mode=token\n2026-10-22 INF Registered tunnel connection\n", encoding="utf-8")

    def fake_run(cmd, **kw):
        if cmd[:2] == ["tasklist", "/fi"]:
            class R:
                stdout = b'"cloudflared.exe","1234"\n'
            return R()
        return real_run(cmd, **kw)
    api_gate.subprocess.run = fake_run
    try:
        url = api_gate._tunnel_adopt_locked(8241)
        result["adopt_token"] = (url == "https://api.example.com"
                                 and api_gate._tunnel.get("mode") == "token"
                                 and api_gate._tunnel.get("pids") == [1234])
        log("token 隧道收编:", url, api_gate._tunnel.get("mode"), api_gate._tunnel.get("pids"))
    finally:
        api_gate.subprocess.run = real_run
        api_gate.tunnel_stop()

    # ---- 4) public_start 优先走用户命名隧道（跳过 UPnP）
    api_gate.start(mode="local", port=8241)
    time.sleep(0.4)
    real_ts = api_gate.tunnel_start
    api_gate.tunnel_start = lambda port, timeout=40.0: "https://api.example.com"
    try:
        msg = api_gate.public_start(8241)
        result["public_prefers_token"] = ("你的账号" in msg and "api.example.com" in msg
                                          and api_gate.public_info()["tunnel"])
        log("public_start 输出:", msg)
    finally:
        api_gate.tunnel_start = real_ts
        api_gate.public_stop(silent=True)
    # public_base 应给出域名地址
    api_gate._tunnel.update(url="https://api.example.com", mode="token", pids=[1234])
    api_gate._pub.update(active=True, tunnel=True, reachable=True)
    result["base_domain"] = (api_gate.public_base(8241, "public")
                             == f"https://api.example.com/{api_gate.suffix()}/v1")
    log("public_base 域名地址:", api_gate.public_base(8241, "public"))
    api_gate.tunnel_stop()
    api_gate._pub.update(active=False, tunnel=False, reachable=False)

    # ---- 5) 状态行文案
    src = (Path(__file__).parent / "app.py").read_text(encoding="utf-8")
    result["hint_text"] = ("当前默认公网链路未连通，已切换至 Cloudflare 隧道模式" in src
                           and "配置你自己的 Cloudflare Zero Trust 账号凭证" in src)
    result["hint_gate"] = (info_hint := ("cf_configured" in src and "tunnel_mode" in src))
    log("浅橙提示文案存在:", result["hint_text"], "状态行分支存在:", result["hint_gate"])
finally:
    api_gate.save_cf(token="", domain="")            # 还原用户配置
    try:
        api_gate.stop()
    except Exception:                                # noqa: BLE001
        pass
    if frame is not None:
        try:
            frame.Close()
        except Exception:                            # noqa: BLE001
            pass

print("RESULT", result, flush=True)
bad = [k for k, v in result.items() if not v]
print("ALL PASS" if not bad else f"FAILED: {bad}", flush=True)
