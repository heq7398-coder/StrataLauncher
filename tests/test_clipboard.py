# -*- coding: utf-8 -*-
"""验证：Key 选中→复制（按钮 + Key 码框）、接口地址点击复制。"""
import ctypes
import json
import time
from pathlib import Path

import wx

import api_gate


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def clip_text() -> str:
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u32.GetClipboardData.restype = ctypes.c_void_p
    u32.GetClipboardData.argtypes = [ctypes.c_uint]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    CF_UNICODETEXT = 13
    if not u32.OpenClipboard(0):
        return "<clipboard-busy>"
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = k32.GlobalLock(h)
        s = ctypes.c_wchar_p(p).value or ""
        k32.GlobalUnlock(h)
        return s
    finally:
        u32.CloseClipboard()


result = {}
app = wx.App(False)
try:
    import app as ui
    ui.boot_settings()
    frame = ui.MainFrame()
    frame.Show()
    for _ in range(30):
        wx.YieldIfNeeded()
        time.sleep(0.1)
    ks = api_gate.keys()
    log(f"keys={len(ks)} lst_rows={frame.lst_keys.GetItemCount()}")

    # 1) 真实点击路径：构造 MouseEvent 走 on_down 选中第 0 行
    lst = frame.lst_keys
    y = 26 + lst.row_h // 2                     # 表头 26px
    e = wx.MouseEvent()
    e.SetPosition((60, y))
    lst.on_down(e)
    wx.YieldIfNeeded()
    sel0 = lst.GetFirstSelected()
    k0 = frame._sel_key()
    result["click_sel"] = (sel0, k0["id"] if k0 else None, ks[0]["id"])
    log("点击选中:", result["click_sel"])

    # 2) 选中旧 Key（倒数第 2 把，验证「之前添加的」也能复制）
    old = [k for k in ks if k["name"] == "不限量 Key"][0]
    idx = ks.index(old)
    lst.Select(idx)
    frame.on_key_copy()
    wx.YieldIfNeeded()
    got = clip_text()
    result["btn_copy_old"] = {"ok": got == old["secret"], "len": len(got)}
    log("按钮复制旧Key:", result["btn_copy_old"])

    # 3) Key 码框：_show_key 同步显示完整 secret，点击复制
    frame._show_key(old)
    wx.YieldIfNeeded()
    shown = frame.txt_key_secret.GetValue()
    frame._secret_copy()
    wx.YieldIfNeeded()
    got2 = clip_text()
    result["secret_box"] = {"shown_full": shown == old["secret"],
                            "copied": got2 == old["secret"]}
    log("Key码框:", result["secret_box"])

    # 4) Key 码框在占位状态点击 → 不复制（剪贴板保持原值）
    frame.txt_key_secret.SetValue("（在上面列表点一行选中 Key，点这里就复制完整 Key 码）")
    frame._secret_copy()
    wx.YieldIfNeeded()
    got3 = clip_text()
    result["placeholder_no_copy"] = got3 == old["secret"]
    log("占位不覆盖:", result["placeholder_no_copy"])

    # 5) 接口地址：设置后点击 = 复制；「生成中…」不复制
    frame._set_addr("http://demo.test:8230/abc/v1", "fg")
    frame._addr_select()
    wx.YieldIfNeeded()
    got4 = clip_text()
    frame._set_addr("生成中…", "muted")
    frame._addr_select()
    wx.YieldIfNeeded()
    got5 = clip_text()
    result["addr_copy"] = {"normal": got4 == "http://demo.test:8230/abc/v1",
                           "generating_skip": got5 == "http://demo.test:8230/abc/v1"}
    log("地址复制:", result["addr_copy"])

    ok = (result["click_sel"][0] == 0 and k0 and result["click_sel"][2] == k0["id"]
          and result["btn_copy_old"]["ok"] and result["secret_box"]["shown_full"]
          and result["secret_box"]["copied"] and result["placeholder_no_copy"]
          and all(result["addr_copy"].values()))
    result["_all_ok"] = bool(ok)
finally:
    try:
        frame.shutdown()
        frame.Destroy()
    except Exception:                            # noqa: BLE001
        pass
    app.ExitMainLoop()

out = Path(api_gate.ctl.settings_path()).parent / "_copy_result.json"
out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
print("RESULT:", json.dumps(result, ensure_ascii=False), flush=True)
