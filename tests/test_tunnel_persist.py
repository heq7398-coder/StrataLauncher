# -*- coding: utf-8 -*-
"""验证：布局、隧道地址稳定（复用/收编）、Key 实时保存。"""
import json
import subprocess
import time
from pathlib import Path

import wx

import api_gate


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cloudflared_pids() -> set:
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq cloudflared.exe",
                              "/fo", "csv", "/nh"], capture_output=True,
                             timeout=8).stdout.decode("gbk", "replace")
        pids = set()
        for ln in out.splitlines():
            parts = [x.strip().strip('"') for x in ln.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "cloudflared.exe":
                try:
                    pids.add(int(parts[1]))
                except ValueError:
                    pass
        return pids
    except Exception:                                # noqa: BLE001
        return set()


result = {}
app = wx.App(False)
frame = None
before_pids = cloudflared_pids()
try:
    import app as ui
    ui.boot_settings()
    frame = ui.MainFrame()
    frame.Show()
    for _ in range(20):
        wx.YieldIfNeeded()
        time.sleep(0.05)
    result["layout_ok"] = True

    # ---- 0) 起一个本地网关（收编探测需要真的能打到网关）
    api_gate.start(mode="local", port=8241)
    time.sleep(0.5)

    # ---- 1) 隧道地址稳定
    url1 = api_gate.tunnel_start(8241)
    log("tunnel url1 =", url1)
    url2 = api_gate.tunnel_start(8241)
    result["reuse_same_session"] = (url2 == url1)
    log("同会话复用:", url2 == url1)

    api_gate.public_stop(silent=True)                # 模拟「对外服务」关
    url3 = api_gate.tunnel_start(8241)
    result["reuse_after_toggle_off"] = (url3 == url1)
    log("关再开复用:", url3 == url1)

    # 模拟控制台重启：清空模块态（cloudflared 进程还活着）
    api_gate._tunnel.update(proc=None, url="", log=None, since=0.0,
                            watch=None, pids=[])
    time.sleep(1)
    url4 = api_gate.tunnel_start(8241)
    result["adopt_after_restart"] = (url4 == url1)
    log("重启后收编同址:", url4 == url1)

    # ---- 2) Key 实时保存：选中一把 → 改 token 上限 → 落盘 → 还原
    ks = api_gate.keys()
    target = ks[0]
    old_q, old_c = int(target["token_limit"]), int(target["context_limit"])
    frame._show_key(target)
    wx.YieldIfNeeded()
    frame.spin_key_quota.SetValue(old_q + 777)
    frame.on_key_live_edit()                         # 起防抖计时器
    frame._live_key_save()                           # 无头环境定时器不派发，直接调保存
    wx.YieldIfNeeded()
    cur = [k for k in api_gate.keys() if k["id"] == target["id"]][0]
    result["live_save"] = int(cur["token_limit"]) == old_q + 777
    result["timer_armed"] = bool(frame._live_edit_timer.IsRunning())
    log("实时保存生效:", result["live_save"], "防抖计时器:", result["timer_armed"])
    api_gate.key_update(target["id"], token_limit=old_q, context_limit=old_c)
    result["restored"] = True

    result["_all_ok"] = all(v for k, v in result.items() if k != "tunnel_url")
    result["tunnel_url"] = url1
finally:
    try:
        api_gate.tunnel_stop()
    except Exception:                                # noqa: BLE001
        pass
    api_gate.stop()
    for pid in cloudflared_pids() - before_pids:     # 只清理本次测试拉起的
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=8)
    try:
        frame.shutdown()
        frame.Destroy()
    except Exception:                                # noqa: BLE001
        pass
    app.ExitMainLoop()

out = Path(api_gate.ctl.settings_path()).parent / "_persist_result.json"
out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
print("RESULT:", json.dumps(result, ensure_ascii=False), flush=True)
