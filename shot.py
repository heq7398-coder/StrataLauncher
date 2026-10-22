# -*- coding: utf-8 -*-
"""截图（模拟数据版）：深/浅两套主题下的仪表盘、设置页、控制台、悬浮窗。"""
import os
import sys
import time
from pathlib import Path

os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 截图要按物理像素抓
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import wx
import strata_ctl as ctl
import app as A

OUT = Path(__file__).parent / "shots"
OUT.mkdir(exist_ok=True)

HIST = lambda base, amp: [round(base + amp * ((i * 37) % 17) / 17.0, 2) for i in range(60)]

M = {
    "engine": {"model": "qwen3.8-flash-next-iq3_xxs", "max_context": 65536, "images": True,
               "version": "0.1.45", "context": 65536, "kv": "int8", "kv_resident": 32768,
               "expert_slots": 4775, "expert_cache_mib": 7937, "vram_free_mib": 312},
    "live": {"state": "idle", "queued": 0, "tok_s": None, "generated": None,
             "prompt_tokens": None, "prompt_total": None, "max_tokens": None},
    "requests": [{"time": time.time() - 300, "finish": "stop", "prompt_tokens": 1240,
                  "reused": 512, "output_tokens": 386, "prompt_ms": 900,
                  "decode_tok_s": 41.7, "duration_s": 9.2}],
    "totals": {"since": time.time() - 7200, "requests": 12, "prompt_tokens": 48000,
               "reused": 9600, "output_tokens": 5400, "decode_ms": 129000},
    "hardware": {"gpu_util": 63, "gpu_mem_used": 14.6 * 2 ** 30, "gpu_mem_total": 16.0 * 2 ** 30,
                 "gpu_temp": 71, "gpu_power": 118, "gpu_power_limit": 160,
                 "gpu_pcie_gen": 4, "gpu_pcie_gen_max": 5, "gpu_pcie_width": 8,
                 "gpu_pcie_rx_mb": 2100, "cpu": 34, "ram_used": 39.5 * 2 ** 30,
                 "ram_total": 63.8 * 2 ** 30, "disk_read_mb": 860, "disk_write_mb": 42},
    "hardware_static": {"gpu_name": "NVIDIA GeForce RTX 4060 Ti", "cores": 14, "threads": 20},
    "history": {"tok_s": HIST(38, 8), "gpu_util": HIST(60, 35), "gpu_mem_used": HIST(15.0, 0.4),
                "gpu_temp": HIST(66, 9), "gpu_power": HIST(110, 40),
                "gpu_pcie_rx_mb": HIST(1800, 900), "cpu": HIST(30, 25),
                "disk_read_mb": HIST(700, 600)},
}
HEALTH = {"status": "ok", "max_context": 65536, "model": "qwen3.8-flash-next-iq3_xxs",
          "loaded": True, "service": "strata"}
SYS = {"cpu": 27.5,
       "ram": {"used": 22.8, "total": 63.8, "pct": 36},
       "gpu": {"name": "NVIDIA GeForce RTX 4060 Ti", "util": 60,
               "mem_used": 6.1, "mem_total": 16.0, "temp": 57}}

A.boot_settings()
application = wx.App(False)
frame = A.MainFrame()
frame.SetPosition((30, 20))
frame.SetSize((920, 860))
frame.Show()
frame.on_snapshot({"health": HEALTH, "metrics": M, "vision": [1234], "idle": 0,
                   "ctx_cfg": 65536})


def grab(win, name):
    wx.YieldIfNeeded()
    time.sleep(0.3)
    wx.YieldIfNeeded()
    p = win.GetScreenPosition()
    s = win.GetSize()
    if win is frame:
        (OUT / "_sizes.txt").write_text(
            f"frame={s} page1={frame.tabs.pages[1].GetSize()} "
            f"area={frame.tabs.area.GetClientSize()} "
            f"card={frame.cards['speed'].GetSize()} "
            f"cardparent={frame.cards['speed'].GetParent().GetSize()}", encoding="utf-8")
    bmp = wx.Bitmap(s[0], s[1])
    mdc = wx.MemoryDC(bmp)
    sdc = wx.ScreenDC()
    mdc.Blit(0, 0, s[0], s[1], sdc, p[0], p[1])
    mdc.SelectObject(wx.NullBitmap)
    bmp.SaveFile(str(OUT / name), wx.BITMAP_TYPE_PNG)


def run():
    A.THEME.set("dark")
    frame.after_theme()
    frame.tabs.SetSelection(1)
    frame.render_dashboard(M)
    wx.YieldIfNeeded()
    grab(frame, "dark_dash.png")
    frame.tabs.SetSelection(3)
    grab(frame, "dark_settings.png")
    frame.tabs.SetSelection(0)
    grab(frame, "dark_ctl.png")

    f = A.FloatFrame(None)
    f.main_ref = frame
    f.Show()
    f.update({"health": HEALTH, "metrics": M, "sys": SYS})
    grab(f, "dark_float.png")
    f.SetSize((430, 420))
    wx.YieldIfNeeded()
    time.sleep(0.2)
    f.update({"health": HEALTH, "metrics": M, "sys": SYS})
    grab(f, "dark_float_big.png")
    f.SetSize((250, 215))
    wx.YieldIfNeeded()
    time.sleep(0.2)
    f.update({"health": None, "metrics": None, "sys": SYS})   # 模型没跑也要有数据
    grab(f, "dark_float_small.png")
    # 生成中的流动状态：亮波扫过模块格子
    MG = dict(M)
    MG["live"] = {"state": "generating", "queued": 0, "tok_s": 41.7,
                  "generated": 386, "prompt_tokens": 1240, "prompt_total": None,
                  "max_tokens": None}
    f.SetSize((430, 420))
    wx.YieldIfNeeded()
    time.sleep(0.2)
    f.update({"health": HEALTH, "metrics": MG, "sys": SYS})
    f.modcard.phase = 6
    grab(f, "dark_float_flow.png")
    # 折叠态
    f.modcard.on_click()
    wx.YieldIfNeeded()
    grab(f, "dark_float_fold.png")
    f.modcard.on_click()
    f.Destroy()

    A.THEME.set("light")
    frame.after_theme()
    frame.tabs.SetSelection(1)
    frame.render_dashboard(M)
    grab(frame, "light_dash.png")
    A.THEME.set("dark")
    frame.after_theme()
    frame.tabs.SetSelection(2)
    grab(frame, "dark_conn.png")
    Path(OUT / "_ok.txt").write_text("done", encoding="utf-8")
    frame.shutdown()
    frame.Destroy()
    application.ExitMainLoop()


wx.CallLater(1200, run)
application.MainLoop()
os._exit(0)
