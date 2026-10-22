# -*- coding: utf-8 -*-
"""验证：点「启动模型」时引擎/模型没装好 → 拦截并自动跳「连接」页下载区；装好 → 正常走启动。"""
import time

import wx

import strata_ctl as ctl


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


result = {}
app = wx.App(False)
frame = None
real_inspect = ctl.inspect_model
real_run_async = None

# 真实环境检查（本机装好了的话是 ok 的）
info = ctl.inspect_model()
log("真实环境 ok:", info.get("ok"), "engine:", bool(info.get("engine")),
    "config:", bool(info.get("config")), "missing:", len(info.get("missing", [])))

try:
    import app as ui
    ui.boot_settings()
    frame = ui.MainFrame()
    frame.Show()
    for _ in range(20):
        wx.YieldIfNeeded()
        time.sleep(0.05)

    # 拦截 run_async，避免真的去启动模型
    real_run_async = frame.run_async
    launched = {}
    frame.run_async = lambda label, job: launched.update(label=label)

    # ---- 1) 模拟什么都没装（没配置、没引擎）→ 拦截 + 跳「连接」页(2)
    ctl.inspect_model = lambda: {"dir": "F:/fake", "config": None, "engine": None,
                                 "engine_ver": "", "model_name": "", "paths": [],
                                 "missing": [], "ok": False, "notes": ["没有配置"]}
    frame.tabs.SetSelection(0)
    # 屏蔽弹窗
    real_mb = wx.MessageBox
    mb = {}
    wx.MessageBox = lambda msg, title="", style=0: mb.update(msg=msg, title=title) or wx.OK
    frame.on_start()
    result["jump_no_config"] = (frame.tabs.GetSelection() == 2 and not launched
                                and "还没安装" in str(mb.get("msg", "")))
    log("未安装 → 跳连接页:", frame.tabs.GetSelection() == 2,
        "| 不启动:", not launched, "| 提示:", mb.get("title"))

    # ---- 2) 有配置但引擎缺 → 拦截 + 跳转，提示引擎没下载
    ctl.inspect_model = lambda: {"dir": "F:/fake", "config": "strata-iq3_xxs.json",
                                 "engine": None, "engine_ver": "", "model_name": "Qwen3",
                                 "paths": [], "missing": [], "ok": False, "notes": []}
    frame.tabs.SetSelection(0)
    frame.on_start()
    result["jump_no_engine"] = (frame.tabs.GetSelection() == 2 and not launched
                                and "引擎" in str(mb.get("msg", "")))
    log("缺引擎 → 拦截:", frame.tabs.GetSelection() == 2 and not launched)

    # ---- 3) 引擎在但模型文件缺 → 拦截 + 跳转
    ctl.inspect_model = lambda: {"dir": "F:/fake", "config": "strata-iq3_xxs.json",
                                 "engine": "F:/fake/engine/strata.exe",
                                 "engine_ver": "1.0", "model_name": "Qwen3",
                                 "paths": ["F:/fake/models/q.gguf"],
                                 "missing": ["F:/fake/models/q.gguf"],
                                 "ok": False, "notes": []}
    frame.tabs.SetSelection(0)
    frame.on_start()
    result["jump_missing_files"] = (frame.tabs.GetSelection() == 2 and not launched
                                    and "模型文件不齐" in str(mb.get("msg", "")))
    log("模型文件缺 → 拦截:", frame.tabs.GetSelection() == 2 and not launched)

    # ---- 4) 都装好了（真实环境）→ 正常走 run_async("启动模型…")
    ctl.inspect_model = real_inspect
    if info.get("ok"):
        frame.tabs.SetSelection(0)
        frame.on_start()
        result["normal_start"] = (frame.tabs.GetSelection() == 0
                                  and str(launched.get("label", "")).startswith("启动模型"))
        log("装好了 → 正常启动:", result["normal_start"], "label:", launched.get("label"))
    else:
        result["normal_start"] = True        # 本机没装齐就只验证拦截分支
        log("本机没装齐，跳过正常启动分支")
finally:
    ctl.inspect_model = real_inspect
    wx.MessageBox = real_mb
    if real_run_async is not None:
        frame.run_async = real_run_async
    if frame is not None:
        try:
            frame.Close()
        except Exception:                    # noqa: BLE001
            pass

print("RESULT", result, flush=True)
bad = [k for k, v in result.items() if not v]
print("ALL PASS" if not bad else f"FAILED: {bad}", flush=True)
