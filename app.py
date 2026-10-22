# -*- coding: utf-8 -*-
"""Strata 控制台 - 桌面版（wxPython）。

一个窗口管完：启动/关闭模型、卸载模型、启动/关闭视觉模型、清理缓存、取消自动卸载、
服务指标仪表盘、本机接口自动发现与接入、跨盘查找 Strata、上下文长度、Strata 更新、
开机自启、悬浮窗、主题与透明。

两条铁律：
  1) 界面线程不做任何联网 / 命令行调用——全部在后台线程，结果用 wx.CallAfter 送回来。
  2) 所有控件自己画。Windows 的原生按钮/页签/复选框/进度条不认 SetBackgroundColour，
     深色主题下会露出一块块白底，所以卡片、按钮、开关、滑块、页签、列表全部自绘，
     颜色直接从 THEME 里取，切主题时整窗一起重画。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import wx

import strata_ctl as ctl
import api_gate

APP_TITLE = "Strata 控制台"
LOCK_PORT = 53991          # 单实例：已被占用就说明已经开了一个
FACE = "Microsoft YaHei UI"

# ------------------------------------------------------------------ 主题

PRESETS = {
    # 浅色：干净的白卡片 + 浅灰底
    "light": {
        "bg": (240, 243, 247), "card": (255, 255, 255), "panel": (255, 255, 255),
        "widget": (247, 249, 252), "border": (214, 221, 231), "track": (226, 232, 240),
        "fg": (24, 27, 33), "muted": (104, 113, 126),
        "ok": (21, 145, 88), "warn": (183, 116, 10), "bad": (200, 52, 47),
        "accent": (37, 99, 235), "widget_fg": (24, 27, 33),
    },
    # 深色：整窗近黑（#0B0D11），卡片只用一点点提亮，文字纯白
    "dark": {
        "bg": (11, 13, 17), "card": (21, 25, 32), "panel": (21, 25, 32),
        "widget": (30, 35, 44), "border": (46, 54, 67), "track": (34, 40, 50),
        "fg": (236, 241, 248), "muted": (138, 148, 163),
        "ok": (61, 203, 133), "warn": (232, 175, 70), "bad": (242, 96, 89),
        "accent": (88, 166, 255), "widget_fg": (236, 241, 248),
    },
}

# 走势线配色：深色底一套、浅色底一套，保证两种主题下都看得清
ACCENT_L = {"speed": (37, 99, 235), "gpu": (21, 145, 88), "vram": (124, 58, 237),
            "temp": (217, 119, 6), "power": (202, 138, 4), "pcie": (8, 145, 178),
            "cpu": (219, 39, 119), "disk": (88, 99, 112)}
ACCENT_D = {"speed": (88, 166, 255), "gpu": (61, 203, 133), "vram": (167, 129, 255),
            "temp": (240, 136, 62), "power": (227, 179, 65), "pcie": (57, 197, 207),
            "cpu": (240, 120, 190), "disk": (140, 150, 165)}


class Theme:
    def __init__(self):
        self.name = "light"
        self.c = dict(PRESETS["light"])
        self.alpha = 255

    def set(self, name: str, custom: dict | None = None):
        if name in PRESETS:
            self.name = name
            self.c = dict(PRESETS[name])
        elif custom:
            self.name = "custom"
            self.c = dict(PRESETS["light"])
            self.c.update({k: tuple(v) for k, v in custom.items() if k in PRESETS["light"]})
        else:
            self.name = "light"
            self.c = dict(PRESETS["light"])

    def get(self, key: str) -> wx.Colour:
        return wx.Colour(*self.c.get(key, (0, 0, 0)))

    def save(self):
        ctl.save_settings(theme=self.name, alpha=self.alpha,
                          custom={k: list(v) for k, v in self.c.items()}
                          if self.name == "custom" else None)

    def load(self) -> dict:
        s = ctl.load_settings()
        if s.get("theme") == "custom" and s.get("custom"):
            self.set("custom", s["custom"])
        else:
            self.set(s.get("theme", "light"))
        self.alpha = int(s.get("alpha", 255) or 255)
        return s


THEME = Theme()


def C(key: str) -> wx.Colour:
    return wx.Colour(*THEME.c.get(key, (0, 0, 0)))


def K(key: str) -> tuple:
    return THEME.c.get(key, (0, 0, 0))


def lum(c) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def is_dark() -> bool:
    return lum(K("bg")) < 128


def accent(key: str) -> tuple:
    return (ACCENT_D if is_dark() else ACCENT_L).get(key, K("accent"))


def mixt(c1, c2, t: float) -> tuple:
    return (int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t))


def mix(c1, c2, t: float) -> wx.Colour:
    return wx.Colour(*mixt(c1, c2, t))


def FONT(size: int = 10, bold: bool = False, mono: bool = False) -> wx.Font:
    f = wx.Font(size, wx.FONTFAMILY_TELETYPE if mono else wx.FONTFAMILY_DEFAULT,
                wx.FONTSTYLE_NORMAL,
                wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL)
    try:
        f.SetFaceName("Consolas" if mono else FACE)
    except Exception:
        pass
    return f


# ------------------------------------------------------------------ 建控件的小助手
# 每个控件都记下自己该用什么底色（_bk）/ 字色（_ck），换主题时 apply_theme 统一刷。

def _panel(parent, bgk: str = "bg") -> wx.Panel:
    p = wx.Panel(parent)
    p._bk = bgk
    p.SetBackgroundColour(C(bgk))
    return p


def _text(parent, label: str = "", ck: str = "fg", bk: str = "card",
          size: int | None = None, bold: bool = False) -> wx.StaticText:
    t = wx.StaticText(parent, label=label)
    t._ck, t._bk = ck, bk
    t.SetForegroundColour(C(ck))
    t.SetBackgroundColour(C(bk))
    if size or bold:
        t.SetFont(FONT(size or 10, bold))
    return t


def _entry(parent, value: str = "", width: int = 90) -> wx.TextCtrl:
    e = wx.TextCtrl(parent, value=value, size=(width, 30))
    e._bk, e._ck = "widget", "fg"
    e.SetBackgroundColour(C("widget"))
    e.SetForegroundColour(C("fg"))
    e.SetFont(FONT(10))
    return e


def apply_theme(win: wx.Window) -> None:
    """换主题：把记录下来的底色/字色重新刷一遍，再全部重画。"""
    def walk(w):
        try:
            bk = getattr(w, "_bk", None)
            if bk:
                w.SetBackgroundColour(C(bk))
            ck = getattr(w, "_ck", None)
            if ck:
                w.SetForegroundColour(C(ck))
        except Exception:
            pass
        try:
            for ch in w.GetChildren():
                walk(ch)
        except Exception:
            pass
        try:
            w.Refresh()
        except Exception:
            pass

    try:
        win.SetBackgroundColour(C("bg"))
    except Exception:
        pass
    walk(win)
    try:
        win.Refresh()
        win.Update()
    except Exception:
        pass


def set_alpha(win: wx.Window, alpha: int) -> None:
    try:
        win.SetTransparent(max(60, min(255, int(alpha))))
    except Exception:
        pass


# ------------------------------------------------------------------ 小工具

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def gb(n) -> float:
    try:
        return float(n) / 2 ** 30
    except (TypeError, ValueError):
        return 0.0


def fmt(v, d: int = 0) -> str:
    if v is None:
        return "–"
    try:
        return f"{float(v):,.{d}f}"
    except (TypeError, ValueError):
        return str(v)


def kfmt(v) -> str:
    if v is None:
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}"


def set_label(ctrl: wx.StaticText, text: str, colour: str | None = None) -> None:
    """只在真的变了才写，省掉每 2 秒一次的重绘；文字变长时重排所在的 sizer。"""
    try:
        if ctrl.GetLabel() != text:
            ctrl.SetLabel(text)
            try:
                size = ctrl.GetSize()
                best = ctrl.GetBestSize()
                if size[0] < best[0] or size[1] < best[1]:
                    par = ctrl.GetParent()
                    if par:
                        par.Layout()          # 只重排这一层，够快
            except Exception:
                pass
        if colour:
            ctrl._ck = colour
            want = C(colour)
            if ctrl.GetForegroundColour() != want:
                ctrl.SetForegroundColour(want)
    except Exception:
        pass


# ------------------------------------------------------------------ 模型清单 / 下载向导

def total_ram_gb() -> float:
    try:
        import ctypes

        class MEM(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        m = MEM()
        m.dwLength = ctypes.sizeof(MEM)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 2 ** 30
    except Exception:
        return 0.0


def parse_models() -> list:
    """从 setup.py 里读 MODELS 清单：[{key, about, download_gb, ram_gb, ok_family}]。"""
    try:
        import re
        txt = (ctl.STRATA_DIR / "setup.py").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^MODELS = (\{.*?\n\})", txt, re.S | re.M)
        if not m:
            return []
        body = re.sub(r"\bNVFP4_[A-Z_0-9]+\b", "''", m.group(1))
        data = eval(body, {"__builtins__": {}}, {})          # noqa: S307 - 本地文件、字面量
        out = []
        for key, d in data.items():
            fams = d.get("families") or ("qwen", "swift")
            out.append({"key": key, "about": d.get("about", ""),
                        "download_gb": d.get("download_gb", 0),
                        "ram_gb": d.get("ram_gb", 0),
                        "ok_family": "qwen" in fams})
        return out
    except Exception:
        return []


def installed_model_key() -> str:
    """从配置文件名猜当前装的型号：strata-iq3_xxs.json -> IQ3_XXS。"""
    try:
        return ctl.CONFIG.stem.replace("strata-", "", 1).upper()
    except Exception:
        return ""


def launch_model_wizard(model_key: str = "") -> str:
    """弹一个命令行窗口跑官方 setup.py 向导（在里面选盘/上下文/下载）。"""
    py = ctl.find_python()
    if not py or not ctl.STRATA_DIR:
        raise RuntimeError("没找到 Strata 目录或 Python")
    args = [str(py), "setup.py", "--setup"]
    if model_key:
        args += ["--model", model_key]
    flags = 0x00000010 | 0x00000200            # CREATE_NEW_CONSOLE | NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, cwd=str(ctl.STRATA_DIR), creationflags=flags,
                            stdin=subprocess.DEVNULL)
    return f"已打开下载向导（PID {proc.pid}）" + (f"，型号 {model_key}" if model_key else "")


# ------------------------------------------------------------------ 自绘控件

def draw_spark(dc, x: int, y: int, w: int, h: int, vals, vmax, rgb, bg) -> None:
    """一段 60 点的迷你曲线：下边填色 + 上面一条线。"""
    dc.SetPen(wx.TRANSPARENT_PEN)
    dc.SetBrush(wx.Brush(wx.Colour(*bg)))
    dc.DrawRectangle(x, y, w, h)
    v = [float(t) for t in (vals or []) if t is not None][-60:]
    if w < 10 or h < 6 or len(v) < 2:
        return
    top = max([float(vmax or 0)] + v + [1e-9])
    step = w / (len(v) - 1)
    pts = [wx.Point(int(x + round(i * step)),
                    int(y + round(h - 2 - (t / top) * (h - 5)))) for i, t in enumerate(v)]
    dc.SetBrush(wx.Brush(wx.Colour(*mixt(bg, rgb, 0.22))))
    dc.DrawPolygon(pts + [wx.Point(x + w, y + h), wx.Point(x, y + h)])
    dc.SetPen(wx.Pen(wx.Colour(*rgb), 2))
    dc.DrawLines(pts)
    dc.SetPen(wx.TRANSPARENT_PEN)


class Btn(wx.Panel):
    """自绘按钮：normal / primary / danger / ghost 四种样子。点击发 EVT_BUTTON。"""

    def __init__(self, parent, label: str = "", size=(-1, -1), kind: str = "normal",
                 bgk: str = "card"):
        super().__init__(parent, size=size)
        self.label = label
        self.kind = kind
        self.bgk = bgk
        self.enabled = True
        self.hover = False
        self.press = False
        self.SetBackgroundColour(C(bgk))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        w, h = size
        try:
            dc = wx.ClientDC(self)
            dc.SetFont(FONT(10))
            tw = dc.GetTextExtent(label)[0] + 28
        except Exception:
            tw = 60
        self._minw = max(60 if w < 0 else w, tw)
        self._minh = 34 if h < 0 else h
        super().SetMinSize((self._minw, self._minh))
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_down)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_ENTER_WINDOW, self.on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self.on_leave)

    def SetMinSize(self, size):
        """-1 表示保持原来的那一维（直接透传 -1 会把宽度抹没，按钮会'消失'）。"""
        try:
            w, h = size
            if w is None or w < 0:
                w = self._minw
            if h is None or h < 0:
                h = self._minh
            self._minw, self._minh = int(w), int(h)
        except Exception:
            pass
        super().SetMinSize((self._minw, self._minh))

    # -- 接口
    def SetLabel(self, t: str):
        self.label = t
        self.Refresh()

    def GetLabel(self) -> str:
        return self.label

    def Enable(self, on: bool = True):
        self.enabled = bool(on)
        self.Refresh()

    def Disable(self):
        self.Enable(False)

    def IsEnabled(self) -> bool:
        return self.enabled

    def SetKind(self, k: str):
        self.kind = k
        self.Refresh()

    # -- 交互
    def on_enter(self, _e):
        self.hover = True
        self.Refresh()

    def on_leave(self, _e):
        self.hover = False
        self.press = False
        self.Refresh()

    def on_down(self, _e):
        self.press = True
        self.Refresh()

    def on_up(self, e):
        down = self.press
        self.press = False
        self.Refresh()
        if not down or not self.enabled:
            return
        w, h = self.GetClientSize()
        p = e.GetPosition()
        if 0 <= p.x < w and 0 <= p.y < h:
            ev = wx.PyCommandEvent(wx.EVT_BUTTON.typeId, self.GetId())
            ev.SetEventObject(self)
            wx.PostEvent(self, ev)

    # -- 画
    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C(self.bgk)))
        dc.Clear()
        if not self.enabled:
            face = mixt(K(self.bgk), K("fg"), 0.06)
            edge = mixt(K(self.bgk), K("fg"), 0.14)
            txt = K("muted")
        elif self.kind == "primary":
            base = K("accent")
            face = mixt(base, (255, 255, 255), 0.12) if self.hover else base
            edge = face
            txt = mixt(K("accent"), (255, 255, 255), 0.92) if lum(base) < 150 else (255, 255, 255)
            txt = (255, 255, 255) if lum(base) < 150 else (20, 22, 26)
        elif self.kind == "danger":
            base = K("bad")
            face = mixt(base, (255, 255, 255), 0.12) if self.hover else base
            edge = face
            txt = (255, 255, 255)
        elif self.kind == "ghost":
            face = mixt(K(self.bgk), K("fg"), 0.10 if self.hover else 0.0)
            edge = face
            txt = K("fg")
        else:
            base = K("widget")
            face = mixt(base, K("fg"), 0.10) if self.hover else base
            edge = mixt(K("widget"), K("fg"), 0.24)
            txt = K("fg")
        if self.press and self.enabled:
            face = mixt(face, (0, 0, 0), 0.14)
        dc.SetPen(wx.Pen(wx.Colour(*edge)))
        dc.SetBrush(wx.Brush(wx.Colour(*face)))
        dc.DrawRoundedRectangle(0, 0, w, h, 6)
        dc.SetTextForeground(wx.Colour(*txt))
        dc.SetFont(FONT(10))
        tw, th = dc.GetTextExtent(self.label)
        dc.DrawText(self.label, (w - tw) // 2, (h - th) // 2)


class Check(wx.Panel):
    """自绘复选框：GetValue/SetValue/IsChecked，切换发 EVT_CHECKBOX。"""

    def __init__(self, parent, label: str = "", value: bool = False, bgk: str = "card"):
        super().__init__(parent)
        self.label = label
        self.value = bool(value)
        self.bgk = bgk
        self.hover = False
        self.SetBackgroundColour(C(bgk))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        dc = wx.ClientDC(self)
        dc.SetFont(FONT(10))
        tw = dc.GetTextExtent(label)[0] if label else 0
        self.SetMinSize((max(60, tw + 40), 24))   # 不给宽度会被 sizer 压成 0，整排"消失"
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: (setattr(self, "hover", True), self.Refresh()))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: (setattr(self, "hover", False), self.Refresh()))

    def GetValue(self) -> bool:
        return self.value

    def IsChecked(self) -> bool:
        return self.value

    def SetValue(self, on: bool):
        self.value = bool(on)
        self.Refresh()

    def on_up(self, _e):
        self.value = not self.value
        self.Refresh()
        ev = wx.PyCommandEvent(wx.EVT_CHECKBOX.typeId, self.GetId())
        ev.SetEventObject(self)
        wx.PostEvent(self, ev)

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C(self.bgk)))
        dc.Clear()
        by = (h - 16) // 2
        if self.value:
            dc.SetPen(wx.Pen(wx.Colour(*K("accent"))))
            dc.SetBrush(wx.Brush(wx.Colour(*K("accent"))))
        else:
            edge = mixt(K("widget"), K("fg"), 0.45 if self.hover else 0.28)
            dc.SetPen(wx.Pen(wx.Colour(*edge)))
            dc.SetBrush(wx.Brush(wx.Colour(*K("widget"))))
        dc.DrawRoundedRectangle(2, by, 16, 16, 3)
        if self.value:
            ink = (255, 255, 255) if lum(K("accent")) < 150 else (18, 20, 24)
            dc.SetPen(wx.Pen(wx.Colour(*ink), 2))
            dc.DrawLines([wx.Point(6, by + 8), wx.Point(9, by + 11), wx.Point(14, by + 5)])
        dc.SetTextForeground(C("fg"))
        dc.SetFont(FONT(10))
        dc.DrawText(self.label, 25, (h - dc.GetTextExtent(self.label)[1]) // 2)


class Toggle(wx.Panel):
    """自绘拨动开关：左=关、右=开，切换发 EVT_CHECKBOX（和 Check 一样的用法）。"""

    W, H = 54, 26

    def __init__(self, parent, value: bool = False, bgk: str = "card"):
        super().__init__(parent, size=(self.W, self.H + 2))
        self.value = bool(value)
        self.bgk = bgk
        self.enabled = True
        self.hover = False
        self.SetBackgroundColour(C(bgk))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((self.W, self.H + 2))
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: (setattr(self, "hover", True), self.Refresh()))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: (setattr(self, "hover", False), self.Refresh()))

    def GetValue(self) -> bool:
        return self.value

    def IsChecked(self) -> bool:
        return self.value

    def SetValue(self, on: bool):
        self.value = bool(on)
        self.Refresh()

    def Enable(self, on: bool = True):
        self.enabled = bool(on)
        self.Refresh()

    def Disable(self):
        self.Enable(False)

    def IsEnabled(self) -> bool:
        return self.enabled

    def on_up(self, _e):
        if not self.enabled:
            return
        self.value = not self.value
        self.Refresh()
        ev = wx.PyCommandEvent(wx.EVT_CHECKBOX.typeId, self.GetId())
        ev.SetEventObject(self)
        wx.PostEvent(self, ev)

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C(self.bgk)))
        dc.Clear()
        tw, th = self.W, self.H
        x0, y0 = 0, (h - th) // 2
        dc.SetPen(wx.Pen(C(self.bgk)))
        track = (K("ok") if self.value else mixt(K("track"), K("fg"), 0.10))
        if not self.enabled:
            track = mixt(K(self.bgk), K("fg"), 0.10)
        elif self.hover:
            track = mixt(track, (255, 255, 255), 0.08)
        dc.SetBrush(wx.Brush(wx.Colour(*track)))
        dc.SetPen(wx.Pen(wx.Colour(*track)))
        dc.DrawRoundedRectangle(x0, y0, tw, th, th // 2)          # 轨道
        r = th - 6
        cx = x0 + tw - r - 3 if self.value else x0 + 3
        knob = (250, 251, 253) if (self.enabled and self.value) else mixt(K("card"), K("fg"), 0.06)
        if not self.enabled:
            knob = mixt(K(self.bgk), K("fg"), 0.22)
        dc.SetBrush(wx.Brush(wx.Colour(*knob)))
        dc.SetPen(wx.Pen(wx.Colour(*mixt(knob, (0, 0, 0), 0.18))))
        dc.DrawEllipse(cx, y0 + 3, r, r)


class Seg(wx.Panel):
    """自绘分段选择（替代 RadioBox）：GetSelection/SetSelection，切换发 EVT_RADIOBOX。"""

    def __init__(self, parent, choices, index: int = 0, bgk: str = "card"):
        super().__init__(parent)
        self.choices = list(choices)
        self.index = index
        self.bgk = bgk
        self.hover = -1
        self.rects: list = []
        self.SetBackgroundColour(C(bgk))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_MOTION, self.on_move)
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: (setattr(self, "hover", -1), self.Refresh()))
        dc = wx.ClientDC(self)
        dc.SetFont(FONT(10))
        tw = sum(dc.GetTextExtent(c)[0] + 26 for c in self.choices)
        self.SetMinSize((tw + 10, 32))

    def GetSelection(self) -> int:
        return self.index

    def SetSelection(self, i: int):
        self.index = max(0, min(len(self.choices) - 1, int(i)))
        self.Refresh()

    def on_move(self, e):
        p = e.GetPosition()
        self.hover = next((i for i, r in enumerate(self.rects) if r.Contains(p)), -1)
        self.Refresh()

    def on_up(self, e):
        p = e.GetPosition()
        i = next((k for k, r in enumerate(self.rects) if r.Contains(p)), -1)
        if i < 0 or i == self.index:
            return
        self.index = i
        self.Refresh()
        ev = wx.PyCommandEvent(wx.EVT_RADIOBOX.typeId, self.GetId())
        ev.SetEventObject(self)
        wx.PostEvent(self, ev)

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C(self.bgk)))
        dc.Clear()
        dc.SetFont(FONT(10))
        self.rects = []
        x = 0
        pad = 3
        for i, c in enumerate(self.choices):
            tw, _ = dc.GetTextExtent(c)
            iw = tw + 26
            r = wx.Rect(x, pad, iw, h - 2 * pad)
            self.rects.append(r)
            if i == self.index:
                face = K("accent")
                txt = (255, 255, 255) if lum(face) < 150 else (18, 20, 24)
                dc.SetPen(wx.Pen(wx.Colour(*face)))
            elif i == self.hover:
                face = mixt(K("widget"), K("fg"), 0.10)
                txt = K("fg")
                dc.SetPen(wx.Pen(wx.Colour(*mixt(K("widget"), K("fg"), 0.22))))
            else:
                face = K("widget")
                txt = K("fg")
                dc.SetPen(wx.Pen(wx.Colour(*mixt(K("widget"), K("fg"), 0.20))))
            dc.SetBrush(wx.Brush(wx.Colour(*face)))
            dc.DrawRoundedRectangle(r.x, r.y, r.width, r.height, 5)
            dc.SetTextForeground(wx.Colour(*txt))
            dc.DrawText(c, r.x + (r.width - tw) // 2, (h - dc.GetTextExtent(c)[1]) // 2)
            x += iw + 4


class HSlider(wx.Panel):
    """自绘滑块：GetValue/SetValue/SetRange/SetMin/SetMax，拖动发 EVT_SLIDER。"""

    def __init__(self, parent, value: int = 0, minValue: int = 0, maxValue: int = 100,
                 style: int = 0, **_kw):
        super().__init__(parent)
        self.v = int(value)
        self.lo = int(minValue)
        self.hi = int(maxValue)
        self.drag = False
        self.line = 1
        self.page = 10
        self.bgk = "card"
        self.SetBackgroundColour(C("card"))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((140, 26))
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_down)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_MOTION, self.on_move)
        self.Bind(wx.EVT_KEY_DOWN, self.on_key)

    def GetValue(self) -> int:
        return self.v

    def SetValue(self, n: int):
        n = int(max(self.lo, min(self.hi, n)))
        if n != self.v:
            self.v = n
            self.Refresh()

    def GetMin(self) -> int:
        return self.lo

    def GetMax(self) -> int:
        return self.hi

    def SetRange(self, lo: int, hi: int):
        self.lo, self.hi = int(lo), int(hi)
        self.v = max(self.lo, min(self.hi, self.v))
        self.Refresh()

    def SetMin(self, lo: int):
        self.SetRange(lo, self.hi)

    def SetMax(self, hi: int):
        self.SetRange(self.lo, hi)

    def SetLineSize(self, n: int):
        self.line = int(n)

    def SetPageSize(self, n: int):
        self.page = int(n)

    def _x2v(self, x: int) -> int:
        w = self.GetClientSize()[0]
        pad = 10
        t = (x - pad) / max(1, w - 2 * pad)
        return int(round(self.lo + max(0.0, min(1.0, t)) * (self.hi - self.lo)))

    def _v2x(self, v: int) -> int:
        w = self.GetClientSize()[0]
        pad = 10
        f = 0.0 if self.hi == self.lo else (v - self.lo) / (self.hi - self.lo)
        return pad + int(round(max(0.0, min(1.0, f)) * (w - 2 * pad)))

    def _fire(self):
        ev = wx.PyCommandEvent(wx.EVT_SLIDER.typeId, self.GetId())
        ev.SetEventObject(self)
        wx.PostEvent(self, ev)

    def on_down(self, e):
        self.drag = True
        if not self.HasCapture():
            self.CaptureMouse()
        self.SetFocus()
        self.SetValue(self._x2v(e.GetPosition().x))
        self._fire()

    def on_move(self, e):
        if self.drag and e.LeftIsDown():
            self.SetValue(self._x2v(e.GetPosition().x))
            self._fire()

    def on_up(self, e):
        if self.drag:
            self.drag = False
            if self.HasCapture():
                self.ReleaseMouse()
            self.SetValue(self._x2v(e.GetPosition().x))
            self._fire()

    def on_key(self, e):
        step = self.page if e.GetKeyCode() in (wx.WXK_PAGEDOWN, wx.WXK_PAGEUP) else self.line
        k = e.GetKeyCode()
        if k in (wx.WXK_LEFT, wx.WXK_DOWN):
            self.SetValue(self.v - step)
        elif k in (wx.WXK_RIGHT, wx.WXK_UP):
            self.SetValue(self.v + step)
        else:
            e.Skip()
            return
        self._fire()

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C(self.bgk)))
        dc.Clear()
        pad = 10
        ty = h // 2 - 3
        tw = max(2, w - 2 * pad)
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(C("track")))
        dc.DrawRoundedRectangle(pad, ty, tw, 6, 3)
        x = self._v2x(self.v)
        dc.SetBrush(wx.Brush(C("accent")))
        dc.DrawRoundedRectangle(pad, ty, max(3, x - pad), 6, 3)
        dc.SetPen(wx.Pen(wx.Colour(*mixt(K("accent"), (0, 0, 0), 0.25))))
        dc.SetBrush(wx.Brush(C("accent")))
        dc.DrawCircle(x, h // 2, 7)


class Bar(wx.Panel):
    """自绘进度条；还能 Pulse() 走不确定进度。"""

    def __init__(self, parent, value: int = 0, range: int = 100, height: int = 10,
                 colour: str = "accent", bgk: str = "card"):
        super().__init__(parent)
        self.v = int(value)
        self.rng = int(range) or 100
        self.colour = colour
        self.bgk = bgk
        self.SetBackgroundColour(C(bgk))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((-1, height))
        self._pv = 0
        self._timer = None
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def SetValue(self, n: int):
        self.Stop()
        self.v = int(max(0, min(self.rng, n)))
        self.Refresh()

    def GetValue(self) -> int:
        return self.v

    def SetRange(self, n: int):
        self.rng = int(n) or 100

    def SetColour(self, key: str):
        self.colour = key
        self.Refresh()

    def Pulse(self):
        if self._timer:
            return
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self._timer)
        self._timer.Start(90)

    def Stop(self):
        if self._timer:
            try:
                self._timer.Stop()
            except Exception:
                pass
            self._timer = None

    def _tick(self, _e):
        self._pv = (self._pv + 3) % 100
        self.Refresh()

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C(self.bgk)))
        dc.Clear()
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(C("track")))
        dc.DrawRoundedRectangle(0, h // 2 - 3, w, 6, 3)
        dc.SetBrush(wx.Brush(C(self.colour)))
        if self._timer:
            seg = max(26, int(w * 0.3))
            x = int(-seg + (self._pv / 100.0) * (w + seg))   # 从左滑入、右滑出
            x1, x2 = max(0, x), min(w, x + seg)
            if x2 - x1 > 2:
                dc.DrawRoundedRectangle(x1, h // 2 - 3, x2 - x1, 6, 3)
        else:
            fw = int(w * (self.v / self.rng)) if self.rng else 0
            if fw > 2:
                dc.DrawRoundedRectangle(0, h // 2 - 3, fw, 6, 3)


class Card(wx.Panel):
    """自绘分组框：外框 + 标题。内容放进 card.body，用 card.sizer 排版。"""

    def __init__(self, parent, title: str = "", orient: int = wx.VERTICAL, pad: int = 10):
        super().__init__(parent)
        self.title = title
        self.pad = pad
        self.SetBackgroundColour(C("bg"))
        self.body = wx.Panel(self)
        self.body._bk = "card"
        self.body.SetBackgroundColour(C("card"))
        self.sizer = wx.BoxSizer(orient)
        self.body.SetSizer(self.sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.AddSpacer((16 if title else 0) + pad)
        outer.Add(self.body, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)
        self.SetSizer(outer)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C("bg")))
        dc.Clear()
        dc.SetPen(wx.Pen(C("border")))
        dc.SetBrush(wx.Brush(C("card")))
        dc.DrawRoundedRectangle(1, 1, w - 2, h - 2, 8)
        if self.title:
            dc.SetTextForeground(C("muted"))
            dc.SetFont(FONT(9, True))
            dc.DrawText(self.title, 13, 5)


class TabBar(wx.Panel):
    def __init__(self, parent, names, bgk: str = "bg", on_pick=None):
        super().__init__(parent)
        self.names = list(names)
        self.index = 0
        self.hover = -1
        self.bgk = bgk
        self.on_pick = on_pick
        self.rects: list = []
        self.SetBackgroundColour(C(bgk))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((-1, 42))
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_MOTION, self.on_move)
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: (setattr(self, "hover", -1), self.Refresh()))

    def on_move(self, e):
        p = e.GetPosition()
        self.hover = next((i for i, r in enumerate(self.rects) if r.Contains(p)), -1)
        self.Refresh()

    def on_up(self, e):
        p = e.GetPosition()
        i = next((k for k, r in enumerate(self.rects) if r.Contains(p)), -1)
        if i >= 0 and self.on_pick:
            self.on_pick(i)

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C(self.bgk)))
        dc.Clear()
        dc.SetPen(wx.Pen(C("border")))
        dc.DrawLine(0, h - 1, w, h - 1)
        dc.SetFont(FONT(11, True))
        self.rects = []
        x = 6
        for i, n in enumerate(self.names):
            tw, th = dc.GetTextExtent(n)
            iw = tw + 30
            r = wx.Rect(x, 4, iw, h - 10)
            self.rects.append(r)
            if i == self.index:
                dc.SetPen(wx.Pen(wx.Colour(*mixt(K("bg"), K("accent"), 0.16))))
                dc.SetBrush(wx.Brush(wx.Colour(*mixt(K("bg"), K("accent"), 0.16))))
                dc.DrawRoundedRectangle(r.x, r.y, r.width, r.height, 6)
                dc.SetTextForeground(C("accent"))
            elif i == self.hover:
                dc.SetPen(wx.Pen(wx.Colour(*mixt(K("bg"), K("fg"), 0.08))))
                dc.SetBrush(wx.Brush(wx.Colour(*mixt(K("bg"), K("fg"), 0.08))))
                dc.DrawRoundedRectangle(r.x, r.y, r.width, r.height, 6)
                dc.SetTextForeground(C("fg"))
            else:
                dc.SetTextForeground(C("muted"))
            dc.DrawText(n, r.x + (r.width - tw) // 2, r.y + (r.height - th) // 2)
            if i == self.index:
                dc.SetPen(wx.Pen(C("accent"), 3))
                dc.DrawLine(r.x + 8, h - 2, r.x + r.width - 8, h - 2)
            x += iw + 4


class Tabbed(wx.Panel):
    """自绘页签 + 页面容器（替代 wx.Notebook，免得露出白色原生标签）。"""

    def __init__(self, parent, names):
        super().__init__(parent)
        self._bk = "bg"
        self.SetBackgroundColour(C("bg"))
        self.bar = TabBar(self, names, on_pick=self.SetSelection)
        self.area = wx.Panel(self)
        self.area._bk = "bg"
        self.area.SetBackgroundColour(C("bg"))
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(self.bar, 0, wx.EXPAND)
        s.Add(self.area, 1, wx.EXPAND)
        self.SetSizer(s)
        self.pages: list = []
        self.index = -1
        self.on_change = None
        self.area.Bind(wx.EVT_SIZE, lambda e: self._layout())

    def AddPage(self, page, _name: str = ""):
        page.Hide()
        self.pages.append(page)
        if len(self.pages) == 1:
            self.SetSelection(0)

    def GetSelection(self) -> int:
        return self.index

    def SetSelection(self, i: int):
        if not (0 <= i < len(self.pages)) or i == self.index:
            return
        for k, p in enumerate(self.pages):
            p.Show(k == i)
        self.index = i
        self.bar.index = i
        self.bar.Refresh()
        self._layout()
        if self.on_change:
            self.on_change()

    def _layout(self):
        if not (0 <= self.index < len(self.pages)):
            return
        p = self.pages[self.index]
        w, h = self.area.GetClientSize()
        p.SetSize(0, 0, max(1, w), max(1, h))
        p.Layout()


class SList(wx.Panel):
    """自绘列表：够用就好（表头 + 行 + 选中 + 滚轮），双击发 EVT_LIST_ITEM_ACTIVATED。
    支持把某一列画成拨动开关（SetSwitchCol）：点那一格 = 切换该行开关（回调 on_switch）。"""

    def __init__(self, parent, row_h: int = 24, bgk: str = "card"):
        super().__init__(parent)
        self.cols: list = []
        self.rows: list = []
        self.rowcol: list = []
        self.sel = -1
        self.offset = 0
        self.row_h = row_h
        self.bgk = bgk
        self.switch_col = -1              # 这一列不画文字，画启用开关
        self.on_switch = None             # def cb(row_index)
        self.SetBackgroundColour(C(bgk))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_down)     # 选中必须在 DOWN 上做：
        self.Bind(wx.EVT_LEFT_UP, self.on_up)         # （UP 链会被外部 handler 阻断）
        self.Bind(wx.EVT_LEFT_DCLICK, self.on_dclick)
        self.Bind(wx.EVT_MOUSEWHEEL, self.on_wheel)

    # -- 与 wx.ListCtrl 同名的一小撮接口
    def InsertColumn(self, _i: int, header: str, width: int = 100):
        self.cols.append((header, width))

    def DeleteAllItems(self):
        self.rows, self.rowcol, self.sel, self.offset = [], [], -1, 0
        self.Refresh()

    def InsertItem(self, _idx: int, text: str) -> int:
        self.rows.append([str(text)])
        self.rowcol.append(None)
        return len(self.rows) - 1

    def GetItemCount(self) -> int:
        return len(self.rows)

    def SetItem(self, row: int, col: int, text: str):
        while len(self.rows[row]) <= col:
            self.rows[row].append("")
        self.rows[row][col] = str(text)
        self.Refresh()

    def SetItemTextColour(self, row: int, colour):
        self.rowcol[row] = colour
        self.Refresh()

    def GetItemText(self, row: int, col: int = 0) -> str:
        try:
            return self.rows[row][col]
        except Exception:
            return ""

    def Select(self, i: int):
        self.sel = i
        self.Refresh()

    def GetFirstSelected(self) -> int:
        return self.sel

    def EnsureVisible(self, i: int):
        """把第 i 行滚进可视区（新增一行后要让它自己蹦出来）。"""
        h = self.GetClientSize()[1]
        vis = max(1, (h - 26) // self.row_h)
        if i < self.offset:
            self.offset = max(0, i)
        elif i >= self.offset + vis:
            self.offset = max(0, i - vis + 1)
        self.Refresh()

    def Freeze(self):
        pass

    def Thaw(self):
        self.Refresh()

    def _hit(self, p):
        top = 26
        i = self.offset + (p.y - top) // self.row_h
        return i if 0 <= i < len(self.rows) else -1

    def _col_x(self) -> list:
        """每列的 (左, 右) 客户区 x 范围。"""
        out, x = [], 8
        for _name, cw in self.cols:
            out.append((x, x + cw))
            x += cw
        return out

    def _switch_hit(self, p):
        """点在开关列的行号；不在开关列返回 -1。"""
        if self.switch_col < 0 or self.switch_col >= len(self._col_x()):
            return -1
        i = self._hit(p)
        if i < 0:
            return -1
        x0, x1 = self._col_x()[self.switch_col]
        return i if x0 <= p.x <= x1 else -1

    def on_down(self, e):
        sw = self._switch_hit(e.GetPosition())
        if sw >= 0:
            if self.on_switch:
                self.on_switch(sw)            # 点开关：只切状态，不动选中
        else:
            self.sel = self._hit(e.GetPosition())
        self.Refresh()

    def on_up(self, e):
        if self._switch_hit(e.GetPosition()) < 0:
            self.sel = self._hit(e.GetPosition())
        e.Skip()                              # 别阻断同窗口上其它 UP 处理器

    def on_dclick(self, e):
        i = self._hit(e.GetPosition())
        if i >= 0:
            self.sel = i
            self.Refresh()
            ev = wx.PyCommandEvent(wx.EVT_LIST_ITEM_ACTIVATED.typeId, self.GetId())
            ev.SetEventObject(self)
            wx.PostEvent(self, ev)

    def on_wheel(self, e):
        h = self.GetClientSize()[1]
        vis = max(1, (h - 26) // self.row_h)
        d = 1 if e.GetWheelRotation() > 0 else -1
        self.offset = max(0, min(max(0, len(self.rows) - vis), self.offset - d))
        self.Refresh()

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C(self.bgk)))
        dc.Clear()
        dc.SetFont(FONT(9, True))
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(C("widget")))
        dc.DrawRectangle(0, 0, w, 24)
        dc.SetTextForeground(C("muted"))
        x = 8
        for name, cw in self.cols:
            dc.DrawText(name, x, 5)
            x += cw
        dc.SetPen(wx.Pen(C("border")))
        dc.DrawLine(0, 24, w, 24)
        dc.SetFont(FONT(10))
        vis = max(1, (h - 26) // self.row_h)
        if self.offset > len(self.rows) - vis:
            self.offset = max(0, len(self.rows) - vis)
        y = 26
        for k in range(self.offset, min(len(self.rows), self.offset + vis)):
            row = self.rows[k]
            if k == self.sel:
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.SetBrush(wx.Brush(wx.Colour(*mixt(K("card"), K("accent"), 0.20))))
                dc.DrawRectangle(0, y, w, self.row_h)
                dc.SetBrush(wx.Brush(C("accent")))
                dc.DrawRectangle(0, y, 3, self.row_h)
            elif (k - self.offset) % 2:
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.SetBrush(wx.Brush(wx.Colour(*mixt(K("card"), K("bg"), 0.45))))
                dc.DrawRectangle(0, y, w, self.row_h)
            dc.SetTextForeground(self.rowcol[k] if self.rowcol[k] else C("fg"))
            x = 8
            th = dc.GetTextExtent("Ag")[1]
            for i, (_n, cw) in enumerate(self.cols):
                t = row[i] if i < len(row) else ""
                if i == self.switch_col:
                    self._draw_toggle(dc, x, cw, y, t == "启用")
                elif cw > 10:
                    while dc.GetTextExtent(t)[0] > cw - 10 and len(t) > 2:
                        t = t[:-2] + "…"
                    dc.DrawText(t, x, y + (self.row_h - th) // 2)
                else:
                    dc.DrawText(t, x, y + (self.row_h - th) // 2)
                x += cw
            y += self.row_h

    def _draw_toggle(self, dc, x: int, cw: int, y: int, on: bool):
        """行内拨动开关（状态列）：on=绿色圆角条+右侧圆钮，off=灰色+左侧圆钮。"""
        tw, thh = 30, 15
        tx = x + max(2, (cw - tw) // 2)
        ty = y + (self.row_h - thh) // 2
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(C("ok") if on else wx.Colour(*mixt(K("muted"), K("card"), 0.35))))
        dc.DrawRoundedRectangle(tx, ty, tw, thh, thh // 2)
        r = thh - 3
        kx = tx + tw - r - 2 if on else tx + 2
        dc.SetBrush(wx.Brush(C("widget") if not on else wx.Colour(255, 255, 255)))
        dc.DrawEllipse(kx, ty + 1, r, r)


class MetricCard(wx.Panel):
    """仪表盘里的指标卡：标题 + 大数字 + 副标题 + 迷你曲线，全部自绘。"""

    def __init__(self, parent, title: str, akey: str, height: int = 122):
        super().__init__(parent)
        self.title = title
        self.akey = akey
        self.value = "–"
        self.sub = ""
        self.vals: list = []
        self.vmax = None
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((150, height))
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def set(self, value: str, sub: str = "", vals=None, vmax=None) -> None:
        new = [float(v) for v in (vals or []) if v is not None][-60:]
        if value == self.value and sub == self.sub and new == self.vals and vmax == self.vmax:
            return
        self.value, self.sub, self.vals, self.vmax = value, sub, new, vmax
        if self.IsShownOnScreen():
            self.Refresh()

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C("bg")))
        dc.Clear()
        dc.SetPen(wx.Pen(C("border")))
        dc.SetBrush(wx.Brush(C("card")))
        dc.DrawRoundedRectangle(0, 0, w - 1, h - 1, 8)
        dc.SetTextForeground(C("muted"))
        dc.SetFont(FONT(9, True))
        dc.DrawText(self.title, 12, 9)
        dc.SetTextForeground(C("fg"))
        dc.SetFont(FONT(15, True))
        dc.DrawText(self.value, 12, 26)
        dc.SetTextForeground(C("muted"))
        dc.SetFont(FONT(8))
        sub = self.sub
        while dc.GetTextExtent(sub)[0] > w - 24 and len(sub) > 2:
            sub = sub[:-2] + "…"
        dc.DrawText(sub, 12, 50)
        draw_spark(dc, 10, 64, w - 20, h - 74, self.vals, self.vmax,
                   accent(self.akey), K("card"))


class MiniCard(wx.Panel):
    """悬浮窗用的小指标卡：标题 + 数字 + 一条小曲线。sc 控制整体缩放。"""

    def __init__(self, parent, title: str, akey: str, height: int = 74):
        super().__init__(parent)
        self.title = title
        self.akey = akey
        self.value = "–"
        self.sub = ""
        self.vals: list = []
        self.vmax = None
        self.sc = 1.0
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((96, height))
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def set(self, value: str, sub: str = "", vals=None, vmax=None) -> None:
        new = [float(v) for v in (vals or []) if v is not None][-60:]
        if value == self.value and sub == self.sub and new == self.vals:
            return
        self.value, self.sub, self.vals, self.vmax = value, sub, new, vmax
        if self.IsShownOnScreen():
            self.Refresh()

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C("bg")))
        dc.Clear()
        dc.SetPen(wx.Pen(C("border")))
        dc.SetBrush(wx.Brush(C("card")))
        dc.DrawRoundedRectangle(0, 0, w - 1, h - 1, max(4, int(7 * self.sc)))
        sc = self.sc
        pad = max(6, int(round(9 * sc)))
        y = max(4, int(round(6 * sc)))

        def line(text, pt, bold, colour):
            nonlocal y
            f = FONT(max(6, int(round(pt * sc))), bold)
            dc.SetFont(f)
            dc.SetTextForeground(C(colour))
            th = dc.GetTextExtent(text)[1] if text else int(pt * 1.6)
            dc.DrawText(text, pad, y)
            y += th + max(1, int(round(2 * sc)))

        line(self.title, 8, True, "muted")
        line(self.value, 13, True, "fg")
        line(self.sub, 8, False, "muted")
        top = min(y, h - max(6, int(round(8 * sc))))
        draw_spark(dc, pad, top, max(10, w - 2 * pad), max(6, h - top - max(4, int(4 * sc))),
                   self.vals, self.vmax, accent(self.akey), K("card"))


class ModCard(wx.Panel):
    """动态模型模块（参考硬件监控的内存模块）：显存按格填充、模型跑动时
    有一道亮波扫过（数据在流动）、点一下可折叠。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.name = "–"
        self.detail = ""
        self.footer = "–"
        self.frac = 0.0
        self.flow = False
        self.phase = 0
        self.sc = 1.0
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        try:
            self.open = bool(ctl.load_settings().get("float_mod_open", True))
        except Exception:                      # noqa: BLE001
            self.open = True
        self._sync_min()
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_UP, self.on_click)

    def _sync_min(self):
        self.SetMinSize((-1, int((82 if self.open else 32) * max(0.78, self.sc))))

    def apply_scale(self, sc: float):
        self.sc = sc
        self._sync_min()
        self.Refresh()

    def on_click(self, _e=None):
        self.open = not self.open
        try:
            ctl.save_settings(float_mod_open=self.open)
        except Exception:                      # noqa: BLE001
            pass
        self._sync_min()
        par = self.GetParent()
        if par:
            par.Layout()
        self.Refresh()

    def set(self, name: str, frac: float, detail: str, state_txt: str,
            speed, flow: bool):
        pct = int(round(max(0.0, min(1.0, frac)) * 100))
        self.name = name or "–"
        self.detail = detail
        self.frac = max(0.0, min(1.0, frac))
        self.flow = bool(flow)
        self.footer = f"已加载 {pct}%　·　{state_txt}"
        if speed:
            self.footer += f"　·　{speed:.1f} t/s"
        if self.IsShownOnScreen():
            self.Refresh()

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C("card")))
        dc.Clear()
        dc.SetPen(wx.Pen(C("border")))
        dc.SetBrush(wx.Brush(C("card")))
        dc.DrawRoundedRectangle(0, 0, w - 1, h - 1, max(4, int(7 * self.sc)))
        sc = self.sc
        pad = max(6, int(round(9 * sc)))
        y = max(4, int(round(6 * sc)))

        # 头部：模型名（左）+ 显存占用（右）+ 折叠箭头
        dc.SetFont(FONT(max(6, int(round(8 * sc))), True))
        dc.SetTextForeground(C("muted"))
        dc.DrawText(("▾ " if self.open else "▸ ") + self.name, pad, y)
        if self.detail:
            dc.SetFont(FONT(max(6, int(round(8 * sc))), True))
            dc.SetTextForeground(C("fg") if self.frac > 0 else C("muted"))
            tw = dc.GetTextExtent(self.detail)[0]
            dc.DrawText(self.detail, w - pad - tw, y)
        y += dc.GetTextExtent("Xg")[1] + max(3, int(round(5 * sc)))

        if not self.open:
            # 折叠：只留一条细的填充条
            bh = max(4, int(round(6 * sc)))
            dc.SetPen(wx.Pen(C("track")))
            dc.SetBrush(wx.Brush(C("track")))
            dc.DrawRoundedRectangle(pad, y, w - 2 * pad, bh, bh // 2)
            fw = int((w - 2 * pad) * self.frac)
            if fw > bh:
                dc.SetPen(wx.Pen(C("accent")))
                dc.SetBrush(wx.Brush(C("accent")))
                dc.DrawRoundedRectangle(pad, y, fw, bh, bh // 2)
            return

        # 格子：显存占用一格一格填满，跑模型时亮波扫过。
        # 行数按剩余高度自适应：窗口多高画多少行，永不裁切。
        cell_w = max(6, int(round(11 * sc)))
        gap = max(2, int(round(3 * sc)))
        cols = int((w - 2 * pad + gap) // (cell_w + gap))
        cols = max(6, cols)
        ch = max(5, int(round(9 * sc)))
        foot_h = dc.GetTextExtent("Xg")[1] + max(3, int(round(4 * sc)))
        rows = int((h - y - foot_h - pad + gap) // (ch + gap))
        rows = max(1, min(3, rows))
        total = cols * rows
        cw = (w - 2 * pad - (cols - 1) * gap) // cols
        filled = int(round(self.frac * total))
        dc.SetPen(wx.TRANSPARENT_PEN)
        for idx in range(total):
            r, c = divmod(idx, cols)
            x = pad + c * (cw + gap)
            yy = y + r * (ch + gap)
            if idx < filled:
                colr = C("accent")
                if self.flow:
                    wave = (idx - self.phase) % total
                    if wave < max(2, total // 7):
                        colr = C("ok")     # 数据流动的前沿
                elif self.frac >= 0.999:
                    colr = C("warn")       # 满了：变色提醒
            else:
                colr = C("track")
            dc.SetBrush(wx.Brush(colr))
            dc.DrawRoundedRectangle(x, yy, cw, ch, max(1, ch // 3))
        y += rows * (ch + gap) + max(2, int(round(3 * sc)))

        # 页脚
        dc.SetFont(FONT(max(6, int(round(8 * sc))), False))
        dc.SetTextForeground(C("muted"))
        dc.DrawText(self.footer, pad, y)


class StatusLine(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.text = ""
        self.SetMinSize((-1, 26))
        self.SetBackgroundColour(C("widget"))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def set(self, t: str):
        if t != self.text:
            self.text = t
            self.Refresh()

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C("widget")))
        dc.Clear()
        dc.SetPen(wx.Pen(C("border")))
        dc.DrawLine(0, 0, w, 0)
        dc.SetTextForeground(C("muted"))
        dc.SetFont(FONT(9))
        dc.DrawText(self.text, 10, (h - dc.GetTextExtent(self.text)[1]) // 2)


class Swatch(wx.Panel):
    """自定义颜色里的小色块按钮：点一下弹系统取色器。"""

    def __init__(self, parent, label: str, key: str):
        super().__init__(parent)
        self.label = label
        self.key = key
        self.hover = False
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((112, 40))
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: (setattr(self, "hover", True), self.Refresh()))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: (setattr(self, "hover", False), self.Refresh()))

    def on_up(self, _e):
        ev = wx.PyCommandEvent(wx.EVT_BUTTON.typeId, self.GetId())
        ev.SetEventObject(self)
        wx.PostEvent(self, ev)

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C("card")))
        dc.Clear()
        dc.SetPen(wx.Pen(C("accent") if self.hover else C("border")))
        dc.SetBrush(wx.Brush(wx.Colour(*K("card"))))
        dc.DrawRoundedRectangle(0, 0, w - 1, h - 1, 6)
        dc.SetPen(wx.Pen(wx.Colour(*mixt(K("card"), K("fg"), 0.35))))
        dc.SetBrush(wx.Brush(wx.Colour(*K(self.key))))
        dc.DrawRoundedRectangle(9, 9, 34, h - 18, 4)
        dc.SetTextForeground(C("fg"))
        dc.SetFont(FONT(9))
        dc.DrawText(self.label, 50, (h - dc.GetTextExtent(self.label)[1]) // 2)


class Popup(wx.Frame):
    """自绘右键菜单（原生菜单在深色主题下会是一块白）。"""

    def __init__(self, parent, items):
        rowh = 30
        super().__init__(parent, style=wx.FRAME_NO_TASKBAR | wx.STAY_ON_TOP |
                         wx.BORDER_NONE | wx.WANTS_CHARS)
        self.items = items
        self.rowh = rowh
        self.hover = -1
        self.SetSize((196, rowh * len(items) + 8))
        self.SetBackgroundColour(C("card"))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_MOTION, self.on_move)
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self.close())
        self.Bind(wx.EVT_KEY_DOWN, self.on_key)
        self.Bind(wx.EVT_ACTIVATE, self.on_activate)

    def show_at(self, x: int, y: int):
        self.SetPosition((x, y))
        self.Show()
        self.SetFocus()
        if not self.HasCapture():
            self.CaptureMouse()

    def on_activate(self, e):
        if not e.GetActive():
            self.close()

    def on_key(self, e):
        if e.GetKeyCode() == wx.WXK_ESCAPE:
            self.close()

    def on_move(self, e):
        self.hover = int(e.GetPosition().y - 4) // self.rowh
        self.Refresh()

    def on_up(self, e):
        i = int(e.GetPosition().y - 4) // self.rowh
        if 0 <= i < len(self.items):
            cb = self.items[i][1]
            self.close()
            if cb:
                cb()
        else:
            self.close()

    def close(self):
        try:
            if self.HasCapture():
                self.ReleaseMouse()
        except Exception:
            pass
        try:
            self.Destroy()
        except Exception:
            pass

    def on_paint(self, _e):
        dc = wx.BufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(C("card")))
        dc.Clear()
        dc.SetPen(wx.Pen(C("border")))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRoundedRectangle(0, 0, w - 1, h - 1, 8)
        dc.SetFont(FONT(10))
        y = 4
        for i, (label, _cb) in enumerate(self.items):
            if i == self.hover:
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.SetBrush(wx.Brush(wx.Colour(*mixt(K("card"), K("accent"), 0.22))))
                dc.DrawRoundedRectangle(4, y, w - 8, self.rowh - 2, 5)
            dc.SetTextForeground(C("fg"))
            dc.DrawText(label, 16, y + (self.rowh - dc.GetTextExtent(label)[1]) // 2 - 1)
            y += self.rowh


# ------------------------------------------------------------------ 后台轮询
def app_icon() -> "wx.Icon | None":
    """程序图标（打包后在 _MEIPASS 里，源码运行在脚本目录）。"""
    for base in (getattr(sys, "_MEIPASS", ""), str(Path(__file__).parent)):
        if not base:
            continue
        p = Path(base) / "strata.ico"
        if p.is_file():
            try:
                ic = wx.Icon(str(p), wx.BITMAP_TYPE_ICO)
                if ic.IsOk():
                    return ic
            except Exception:                  # noqa: BLE001
                pass
    return None


class Poller(threading.Thread):
    """所有联网 / 命令行查询都在这里做，绝不在界面线程跑。"""

    def __init__(self, sink):
        super().__init__(daemon=True)
        self.sink = sink
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.interval = 0.5                 # 半秒一轮：状态、按钮、日志跟服务保持同步
        self.tick = 0

    def run(self):
        while not self.stop.is_set():
            t0 = time.time()
            snap: dict = {"tick": self.tick}
            try:
                snap["health"] = ctl.health(timeout=1.5)
            except Exception:
                snap["health"] = None
            try:
                snap["metrics"] = ctl.metrics(timeout=2.5)
            except Exception:
                snap["metrics"] = None
            try:
                snap["sys"] = ctl.sys_sample()   # CPU/内存/GPU：模型没跑也要实时
            except Exception:
                snap["sys"] = None
            if snap["health"] is not None:
                try:
                    snap["model"] = ctl.served_model(timeout=1.0)   # 内部 5 秒缓存
                except Exception:
                    snap["model"] = ""
            if self.tick % 4 == 0:              # tasklist 是外部进程，慢，少跑几次
                try:
                    snap["vision"] = ctl.vision_pids()
                except Exception:
                    snap["vision"] = None
            if self.tick % 20 == 0:             # 读配置文件
                try:
                    snap["idle"] = ctl.idle_unload_seconds()
                    snap["ctx_cfg"] = ctl.max_context_setting()
                except Exception:
                    pass
            self.tick += 1
            try:
                wx.CallAfter(self.sink, snap)
            except Exception:
                return
            left = max(0.2, self.interval - (time.time() - t0))
            if self.wake.wait(left):           # 需要立刻刷新时不必等满一轮
                self.wake.clear()


# ------------------------------------------------------------------ 悬浮窗
class FloatFrame(wx.Frame):
    """迷你仪表盘浮窗：默认停在屏幕右上角，可拖动、可拖边改大小，内容按比例缩放。"""

    BASE_W, BASE_H = 368, 404
    MIN_W, MIN_H = 320, 300

    def __init__(self, parent=None):
        super().__init__(parent, title="Strata", size=(self.BASE_W, self.BASE_H),
                         style=wx.FRAME_NO_TASKBAR | wx.STAY_ON_TOP | wx.RESIZE_BORDER)
        self.poller = None
        self._drag = None
        self.sc = 1.0
        self._save_timer = None
        self.main_ref = None                  # 所属主窗口（不再用父子关系，彻底解耦）
        self._hist = {}                       # 本机 CPU/内存/GPU 走势（自己采样自己记）
        ic = app_icon()
        if ic:
            try:
                self.SetIcon(ic)
            except Exception:
                pass
        self.SetMinSize((self.MIN_W, self.MIN_H))
        self.SetBackgroundColour(C("bg"))
        self._labels = []                     # (控件, 基础字号, 是否粗体)

        root = _panel(self, "bg")
        vs = wx.BoxSizer(wx.VERTICAL)

        # ---------- 顶栏：状态点 + 标题 + 状态 + 关闭
        self.head = wx.Panel(root)
        self.head._bk = "widget"
        self.head.SetBackgroundColour(C("widget"))
        self.head.SetMinSize((-1, 32))
        hs = wx.BoxSizer(wx.HORIZONTAL)
        self.dot = wx.Panel(self.head, size=(10, 10))
        self.dot.SetBackgroundColour(C("bad"))
        hs.Add(self.dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        self.lbl_title = self._lab(self.head, "Strata", "fg", "widget", 10, True)
        hs.Add(self.lbl_title, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 7)
        self.lbl_state = self._lab(self.head, "连接中…", "muted", "widget", 9, False)
        hs.Add(self.lbl_state, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self.btn_close = Btn(self.head, "×", size=(28, 24), kind="ghost", bgk="widget")
        self.btn_close.SetToolTip("关闭悬浮窗")
        self.btn_close.Bind(wx.EVT_BUTTON, lambda e: self.close())
        hs.Add(self.btn_close, 0, wx.RIGHT | wx.TOP | wx.BOTTOM, 4)
        self.head.SetSizer(hs)
        vs.Add(self.head, 0, wx.EXPAND)

        # ---------- 主体
        body = _panel(root, "bg")
        bs = wx.BoxSizer(wx.VERTICAL)

        self.grid = wx.FlexGridSizer(cols=3, vgap=6, hgap=6)
        for i in range(3):
            self.grid.AddGrowableCol(i, 1)
        for i in range(2):
            self.grid.AddGrowableRow(i, 1)
        self.minis = {}
        # 速度放第一位（用户点名不能少），六张卡两行三列
        for key, title in (("speed", "速度 t/s"), ("gpu", "GPU %"),
                           ("vram", "显存 GB"), ("ram", "内存 GB"),
                           ("cpu", "CPU %"), ("temp", "温度 °C")):
            c = MiniCard(body, title, key, 74)
            self.minis[key] = c
            self.grid.Add(c, 1, wx.EXPAND)
        bs.Add(self.grid, 4, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # ---------- 四个关键数字（2×2，窄窗也不会挤）
        self.rows = {}
        rows = wx.FlexGridSizer(cols=2, vgap=2, hgap=8)
        rows.AddGrowableCol(0, 1)
        rows.AddGrowableCol(1, 1)
        for key, title in (("svc", "服务"), ("model", "模型"),
                           ("spd", "速度"), ("temp", "温度")):
            cell = wx.BoxSizer(wx.HORIZONTAL)
            cell.Add(self._lab(body, title, "muted", "bg", 9, False), 0, wx.ALIGN_CENTER_VERTICAL)
            v = self._lab(body, "–", "fg", "bg", 9, True)
            v.SetMinSize((96, 18))               # 预留宽度，文字变了也不用重排
            self.rows[key] = v
            cell.Add(v, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
            self.rows[key] = v
            rows.Add(cell, 1, wx.EXPAND)
        bs.Add(rows, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # ---------- 上下文
        self.ctx_row = wx.FlexGridSizer(cols=2, vgap=0, hgap=6)
        self.ctx_row.AddGrowableCol(0, 1)
        self.ctx_row.Add(self._lab(body, "上下文", "muted", "bg", 9, False), 0,
                         wx.ALIGN_CENTER_VERTICAL)
        self.lbl_ctx = self._lab(body, "–", "muted", "bg", 9, False)
        self.lbl_ctx.SetMinSize((170, 18))
        self.ctx_row.Add(self.lbl_ctx, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_RIGHT)
        bs.Add(self.ctx_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 9)
        self.g_ctx = Bar(body, 0, 100, 12, "accent", "bg")
        bs.Add(self.g_ctx, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.TOP, 8)

        # ---------- 动态模型模块（显存按格填充 + 数据流动画 + 可折叠）
        self.modcard = ModCard(body)
        bs.Add(self.modcard, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._anim = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_anim, self._anim)

        body.SetSizer(bs)
        vs.Add(body, 1, wx.EXPAND)
        root.SetSizer(vs)

        for w in (self, self.head, body, root):
            self._bind_drag(w)
            w.Bind(wx.EVT_RIGHT_UP, self.on_menu)
        for c in self.minis.values():
            self._bind_drag(c)
            c.Bind(wx.EVT_RIGHT_UP, self.on_menu)

        s = ctl.load_settings()
        size = s.get("float_size")
        if isinstance(size, list) and len(size) == 2:
            try:
                self.SetClientSize((max(self.MIN_W, int(size[0])), max(self.MIN_H, int(size[1]))))
            except Exception:
                self.SetClientSize((self.BASE_W, self.BASE_H))
        else:
            self.SetClientSize((self.BASE_W, self.BASE_H))
        pos = s.get("float_pos")
        if isinstance(pos, list) and len(pos) == 2:
            try:
                self.SetPosition((int(pos[0]), int(pos[1])))
            except Exception:
                self.dock_top_right()
        else:
            self.dock_top_right()            # 没记住位置就自动停在右上角
        set_alpha(self, THEME.alpha)
        self.Bind(wx.EVT_SIZE, self.on_resize)
        self.apply_scale(self._fit_scale(), force=True)
        # 最小尺寸按"客户区"算：窗口边框 + 标题栏的高度要补回去
        wx.CallAfter(self._fix_min)

    def _fix_min(self):
        try:
            fw = self.GetSize()[0] - self.GetClientSize()[0]
            fh = self.GetSize()[1] - self.GetClientSize()[1]
            self.SetMinSize((self.MIN_W + fw, self.MIN_H + fh))
        except Exception:
            pass

    # ---------- 小工具
    def _lab(self, parent, text, ck, bk, size, bold):
        t = _text(parent, text, ck=ck, bk=bk, size=size, bold=bold)
        self._labels.append((t, size, bold))
        return t

    def dock_top_right(self):
        """停到主屏工作区右上角。"""
        try:
            idx = wx.Display.GetFromWindow(self)
            if idx < 0:
                idx = 0
            area = wx.Display(idx).GetClientArea()
            w, h = self.GetSize()
            self.SetPosition((area.x + area.width - w - 16, area.y + 16))
        except Exception:
            self.SetPosition((40, 40))

    def _fit_scale(self) -> float:
        w, h = self.GetClientSize()
        return min(w / float(self.BASE_W), h / float(self.BASE_H))

    # ---------- 缩放
    def apply_scale(self, sc: float, force: bool = False):
        sc = max(0.78, min(2.2, float(sc)))
        if not force and abs(sc - self.sc) < 0.05:
            return
        self.sc = sc
        for ctrl, pt, bold in self._labels:
            try:
                ctrl.SetFont(FONT(max(7, int(round(pt * sc))), bold))
                ctrl.InvalidateBestSize()
            except Exception:
                pass
        for m in self.minis.values():
            m.sc = sc
            m.SetMinSize((int(104 * sc), int(70 * sc)))
        for v in self.rows.values():
            v.SetMinSize((int(96 * sc), -1))
        self.lbl_ctx.SetMinSize((int(170 * sc), -1))
        self.lbl_state.SetMinSize((int(80 * sc), -1))
        self.head.SetMinSize((-1, int(30 * sc)))
        self.dot.SetSize((max(8, int(round(10 * sc))), max(8, int(round(10 * sc)))))
        self.btn_close.SetMinSize((int(26 * sc), int(22 * sc)))
        self.g_ctx.SetMinSize((-1, max(8, int(round(11 * sc)))))
        self.modcard.apply_scale(sc)
        self.Layout()
        for m in self.minis.values():
            m.Refresh()

    def _on_anim(self, _e):
        """模型跑动时：亮波扫过模块条的格子（数据在流动）。"""
        if self.modcard.flow and self.IsShown():
            self.modcard.phase = (self.modcard.phase + 1) % 100000
            self.modcard.Refresh()

    def on_resize(self, e):
        e.Skip()
        self.apply_scale(self._fit_scale())
        if self._save_timer:
            self._save_timer.Stop()
        self._save_timer = wx.CallLater(600, self._save_size)
        self.Refresh()

    def _save_size(self):
        try:
            w, h = self.GetClientSize()
            ctl.save_settings(float_size=[int(w), int(h)])
        except Exception:
            pass

    def retheme(self):
        apply_theme(self)
        set_alpha(self, THEME.alpha)
        for m in self.minis.values():
            m.Refresh()

    # ---------- 拖动（拖标题栏 / 空白处移动，拖边框改大小）
    def _bind_drag(self, w):
        w.Bind(wx.EVT_LEFT_DOWN, self.on_down)
        w.Bind(wx.EVT_MOTION, self.on_move)
        w.Bind(wx.EVT_LEFT_UP, self.on_up)

    def on_down(self, e):
        obj = e.GetEventObject()
        sp = obj.ClientToScreen(e.GetPosition())
        pos = self.GetPosition()
        self._drag = (sp[0] - pos[0], sp[1] - pos[1])
        if not self.HasCapture():
            self.CaptureMouse()
        e.Skip()

    def on_move(self, e):
        if self._drag and e.Dragging() and e.LeftIsDown():
            obj = e.GetEventObject()
            sp = obj.ClientToScreen(e.GetPosition())
            self.SetPosition((sp[0] - self._drag[0], sp[1] - self._drag[1]))

    def on_up(self, _e):
        if self._drag is None:
            return
        self._drag = None
        if self.HasCapture():
            self.ReleaseMouse()
        try:
            p = self.GetPosition()
            ctl.save_settings(float_pos=[int(p[0]), int(p[1])])
        except Exception:
            pass

    # ---------- 右键菜单
    def on_menu(self, e):
        items = [("停到右上角", self.dock_top_right),
                 ("打开完整控制台", self.open_main),
                 ("不透明 100%", lambda: self.set_alpha_v(255)),
                 ("90%", lambda: self.set_alpha_v(230)),
                 ("75%", lambda: self.set_alpha_v(191)),
                 ("60%", lambda: self.set_alpha_v(153)),
                 ("退出程序", self.exit_app),
                 ("关闭悬浮窗", self.close)]
        p = Popup(self, items)
        try:
            sp = self.ClientToScreen(e.GetPosition())
        except Exception:
            sp = (60, 60)
        p.show_at(sp[0], sp[1])

    def set_alpha_v(self, a: int):
        THEME.alpha = a
        THEME.save()
        set_alpha(self, a)
        main = self.main_ref
        if isinstance(main, MainFrame):
            set_alpha(main, a)
            main.sl_alpha.SetValue(a)

    def open_main(self):
        """悬浮窗模式下把完整控制台开出来（同一个进程内，不再开第二个实例）。"""
        # 主窗口只是被收起来了（点过 ×）：直接还原它
        main = self.main_ref
        if isinstance(main, MainFrame):
            main.Show()
            try:
                main.Restore()
            except Exception:
                pass
            main.Raise()
            return
        try:
            if self.poller:
                self.poller.stop.set()
                self.poller = None
        except Exception:
            pass
        frame = MainFrame(float_win=self)
        try:
            frame.SetPosition((120, 60))
        except Exception:
            pass
        frame.Show()

    def exit_app(self):
        """右键菜单「退出程序」：主窗口被收起时也能正常退出。"""
        main = self.main_ref
        if isinstance(main, MainFrame):
            main.Show()                      # 弹确认框前先把它亮出来
            main.on_close(force=True)
        else:
            self.close()

    def close(self):
        try:
            p, s = self.GetPosition(), self.GetClientSize()
            ctl.save_settings(float_pos=[int(p[0]), int(p[1])],
                              float_size=[int(s[0]), int(s[1])], float_on=False)
        except Exception:
            pass
        parent = self.main_ref
        if isinstance(parent, MainFrame):
            parent.on_float_closed()
            if not parent.IsShown():
                # 主窗口已经收起来了，悬浮窗也关了 → 没有可见窗口，整个退出
                # （模型服务不受影响，继续在后台跑）
                try:
                    parent.shutdown()
                except Exception:
                    pass
                try:
                    wx.GetApp().ExitMainLoop()
                except Exception:
                    pass
        else:
            try:
                wx.GetApp().ExitMainLoop()
            except Exception:
                pass
        self.Destroy()

    # ---------- 数据
    def update(self, snap: dict):
        h = snap.get("health")
        m = snap.get("metrics")
        sysi = snap.get("sys") or {}
        running = h is not None
        loaded = bool(h and h.get("loaded"))
        try:
            self.dot.SetBackgroundColour(C("ok" if running else "bad"))
            self.dot.Refresh()
        except Exception:
            pass
        set_label(self.lbl_state, "运行中" if running else "服务未启动",
                  "ok" if running else "bad")
        set_label(self.rows["svc"], "运行中" if running else "已停止",
                  "ok" if running else "bad")
        set_label(self.rows["model"], "已加载" if loaded else ("未加载" if running else "–"),
                  "ok" if loaded else "muted")

        # ---------- 硬件：优先用模型服务的数据，没有就用本机 NVML/系统采样
        hw = (m or {}).get("hardware") or {}
        g = sysi.get("gpu") or {}
        ram = sysi.get("ram") or {}

        def pick(a, b):
            return a if a is not None else b

        gpu_util = pick(hw.get("gpu_util"), g.get("util"))
        vram_u = pick(hw.get("gpu_mem_used"),
                      g.get("mem_used") * 2 ** 30 if g.get("mem_used") is not None else None)
        vram_t = pick(hw.get("gpu_mem_total"),
                      g.get("mem_total") * 2 ** 30 if g.get("mem_total") else None)
        gpu_temp = pick(hw.get("gpu_temp"), g.get("temp"))
        cpu_pct = pick(hw.get("cpu"), sysi.get("cpu"))
        ram_u = pick(hw.get("ram_used"),
                     ram.get("used") * 2 ** 30 if ram.get("used") is not None else None)
        ram_t = pick(hw.get("ram_total"),
                     ram.get("total") * 2 ** 30 if ram.get("total") else None)

        # 本地走势（服务历史没有的项，就自己记）——见 hvals()

        def hvals(key, val):
            srv = ((m or {}).get("history") or {}).get(key)
            if srv:
                return srv
            lst = self._hist.setdefault(key, [])
            lst.append(val)
            del lst[:-60]
            return lst

        hot = (gpu_temp or 0) >= 85

        # ---------- 模型状态（先算，速度卡/模块条都要用）
        live = (m or {}).get("live") or {}
        eng = (m or {}).get("engine") or {}
        reqs = (m or {}).get("requests") or []
        last = reqs[0] if reqs else None
        state = live.get("state") or "idle"
        speed = live.get("tok_s") if state == "generating" else \
            (last or {}).get("decode_tok_s")

        # 六张指标卡：速度永远在第一位
        self.minis["speed"].set(f"{speed:.1f}" if speed else "0.0", "t/s",
                                hvals("tok_s", speed), None)
        self.minis["gpu"].set(fmt(gpu_util), "%",
                              hvals("gpu", gpu_util), 100)
        self.minis["vram"].set(f"{gb(vram_u):.1f}" if vram_u else "–",
                               f"/ {gb(vram_t):.1f} G" if vram_t else "",
                               hvals("vram", gb(vram_u) if vram_u else None), vram_t)
        self.minis["ram"].set(f"{gb(ram_u):.1f}" if ram_u else "–",
                              f"/ {gb(ram_t):.1f} G" if ram_t else "",
                              hvals("ram", gb(ram_u) if ram_u else None), ram_t)
        self.minis["cpu"].set(fmt(cpu_pct), "%",
                              hvals("cpu", cpu_pct), 100)
        self.minis["temp"].set(fmt(gpu_temp), "过热!" if hot else "°C",
                               hvals("gpu_temp", gpu_temp), 100)

        # ---------- 动态模型模块条
        frac = (vram_u / vram_t) if (vram_u is not None and vram_t) else 0.0
        if loaded:
            name = (h or {}).get("model") or "Strata 模型"
            state_cn = {"generating": "生成中", "reading": "读入提示词",
                        "queued": "排队中"}.get(state, "待机")
        elif running:
            name = (h or {}).get("model") or "Strata 模型"
            state_cn = "未加载"
        else:
            name = "未连接模型服务"
            state_cn = "服务未启动"
        self.modcard.set(name, frac if loaded else 0.0,
                         (f"{gb(vram_u):.1f} / {gb(vram_t):.1f} G"
                          if (vram_t and loaded) else ""),
                         state_cn, speed if loaded else None,
                         flow=(loaded and state == "generating"))
        if self.modcard.flow and not self._anim.IsRunning():
            self._anim.Start(120)
        elif not self.modcard.flow and self._anim.IsRunning():
            self._anim.Stop()

        set_label(self.rows["spd"],
                  f"{speed:.1f} t/s" if speed else ("生成中…" if state != "idle" else "待机"),
                  "ok" if speed else "muted")
        set_label(self.rows["temp"], f"{fmt(gpu_temp)} °C" if gpu_temp else "–",
                  "bad" if hot else "muted")
        self.g_ctx.SetColour("bad" if hot else "accent")
        ctx = eng.get("max_context") or 0
        used = 0
        if state != "idle":
            used = (live.get("prompt_tokens") or 0) + (live.get("generated") or 0)
        elif last:
            used = (last.get("prompt_tokens") or 0) + (last.get("output_tokens") or 0)
        pct = (100.0 * used / ctx) if ctx else 0
        self.g_ctx.SetValue(int(max(0, min(100, pct))))
        set_label(self.lbl_ctx, f"{kfmt(used)} / {kfmt(ctx)}（{pct:.0f}%）" if ctx else "–",
                  "muted")


# ------------------------------------------------------------------ 主窗口
class MainFrame(wx.Frame):
    def __init__(self, float_win: FloatFrame | None = None):
        super().__init__(None, title=APP_TITLE, size=(920, 860),
                         style=wx.DEFAULT_FRAME_STYLE)
        self.SetMinSize((820, 700))
        self.SetBackgroundColour(C("bg"))
        ic = app_icon()
        if ic:
            try:
                self.SetIcon(ic)              # 任务栏/标题栏图标
            except Exception:
                pass
        self.busy = False
        self._busy_label = ""
        self._last_health = None
        self._tab = 0
        self._last_vision = None
        self._last_idle = None
        self._last_ctx_cfg = None
        self._ctx_cfg = None
        self._ctx_native = None
        self._last_metrics = None
        self._req_sig = None
        self._upd_info = None
        self._found = []
        self._dirs = []
        self.float_win = float_win
        self._scan_log_t = 0.0
        self._fg_picked = False
        self._mon = {"running": False}       # 模型状态监视（日志跟随大模型动态）

        if float_win is not None:
            try:
                if float_win.poller:
                    float_win.poller.stop.set()
                float_win.poller = None
            except Exception:
                pass
            try:
                float_win.main_ref = self
            except Exception:
                pass

        self.tabs = Tabbed(self, ["控制台", "仪表盘", "连接", "设置"])
        self.tabs.on_change = self.on_tab
        self.page_ctl = _panel(self.tabs.area, "bg")
        self.page_dash = wx.ScrolledWindow(self.tabs.area)   # 内容多，允许滚动
        self.page_dash._bk = "bg"
        self.page_dash.SetBackgroundColour(C("bg"))
        self.page_dash.SetScrollRate(10, 10)
        self.page_conn = wx.ScrolledWindow(self.tabs.area)
        self.page_conn._bk = "bg"
        self.page_conn.SetBackgroundColour(C("bg"))
        self.page_conn.SetScrollRate(10, 10)
        self.page_set = wx.ScrolledWindow(self.tabs.area)
        self.page_set._bk = "bg"
        self.page_set.SetBackgroundColour(C("bg"))
        self.page_set.SetScrollRate(10, 10)
        self.tabs.AddPage(self.page_ctl, "控制台")
        self.tabs.AddPage(self.page_dash, "仪表盘")
        self.tabs.AddPage(self.page_conn, "连接")
        self.tabs.AddPage(self.page_set, "设置")

        self.build_control_page()
        self.build_dashboard_page()
        self.build_connect_page()
        self.build_settings_page()

        self.status = StatusLine(self)
        vs = wx.BoxSizer(wx.VERTICAL)
        vs.Add(self.tabs, 1, wx.EXPAND)
        vs.Add(self.status, 0, wx.EXPAND)
        self.SetSizer(vs)
        self.SetStatusText("就绪")

        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.poller = Poller(self.on_snapshot)
        self.poller.start()

        self.log(f"Strata 目录：{ctl.STRATA_DIR}")
        self.log(f"配置文件：{ctl.CONFIG.name if ctl.CONFIG else '未找到！'}")
        self.log(f"接入地址：{ctl.current_base()}")
        try:                                    # 打开时模型若已在跑，说清楚来源，避免误会成自动启动
            if ctl.server_running():
                self.log("检测到模型服务已经在后台运行（上次启动的还没停，不是本次打开才启动的）；"
                         "要停就点「关闭模型」", "info")
        except Exception:                        # noqa: BLE001
            pass
        apply_theme(self)
        set_alpha(self, THEME.alpha)
        if float_win is not None:
            self.cb_float.SetValue(True)
        wx.CallLater(1500, self.auto_connect)
        wx.CallLater(4000, self.auto_update_tick)
        wx.CallLater(800, self.boot_scan_conn)   # 启动就把连接页两个列表填上
        # 对外接口不开机自启：重启后默认关闭，要用就手动拨「对外服务」开关。

    def SetStatusText(self, text: str):
        self.status.set(text)

    # ================================================================ 控制台页
    def build_control_page(self):
        p = self.page_ctl
        root = wx.BoxSizer(wx.VERTICAL)

        box = Card(p, "状态")
        s = box.sizer
        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=18)
        grid.AddGrowableCol(1, 1)
        self.lbl_service = _text(box.body, "—")
        self.lbl_model = _text(box.body, "—")
        self.lbl_vision = _text(box.body, "—")
        self.lbl_idle = _text(box.body, "—")
        for title, ctrl in (("服务", self.lbl_service), ("模型", self.lbl_model),
                            ("视觉模型", self.lbl_vision), ("自动卸载", self.lbl_idle)):
            grid.Add(_text(box.body, title + "：", ck="muted", bold=True),
                     0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.EXPAND)
        s.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
        root.Add(box, 0, wx.EXPAND | wx.ALL, 10)

        acts = Card(p, "操作")
        ag = wx.GridSizer(cols=2, vgap=8, hgap=8)
        self.btn_start = Btn(acts.body, "启动模型", kind="primary")
        self.btn_start.SetToolTip("启动 Strata 引擎并加载模型；若引擎或模型还没装好，"
                                  "会自动跳到「连接」页的下载区让你先装")
        self.btn_stop = Btn(acts.body, "关闭模型", kind="danger")
        self.btn_unload = Btn(acts.body, "卸载模型（释放显存）")
        self.btn_web = Btn(acts.body, "打开网页界面")
        self.btn_vstart = Btn(acts.body, "启动视觉模型")
        self.btn_vstop = Btn(acts.body, "关闭视觉模型")
        self.btn_cache = Btn(acts.body, "清理缓存…")
        self.btn_idle = Btn(acts.body, "取消自动卸载")
        for b, fn in ((self.btn_start, self.on_start), (self.btn_stop, self.on_stop),
                      (self.btn_unload, self.on_unload), (self.btn_web, self.on_web),
                      (self.btn_vstart, self.on_vision_start),
                      (self.btn_vstop, self.on_vision_stop),
                      (self.btn_cache, self.on_cache), (self.btn_idle, self.on_disable_idle)):
            b.SetMinSize((-1, 42))
            b.Bind(wx.EVT_BUTTON, lambda e, f=fn: f())
            ag.Add(b, 0, wx.EXPAND)
        ag.AddSpacer(0)
        acts.sizer.Add(ag, 0, wx.EXPAND | wx.ALL, 8)
        self.cb_autoweb = Check(acts.body, "模型就绪后自动打开网页",
                                bool(ctl.load_settings().get("autoweb", True)))
        self.cb_autoweb.Bind(wx.EVT_CHECKBOX,
                             lambda e: ctl.save_settings(autoweb=self.cb_autoweb.IsChecked()))
        acts.sizer.Add(self.cb_autoweb, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.gauge = Bar(acts.body, 0, 100, 12)
        self.gauge.Hide()
        acts.sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(acts, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        log_card = Card(p, "日志")
        self.log_box = wx.TextCtrl(log_card.body,
                                   style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        self.log_box._bk, self.log_box._ck = "widget", "fg"
        self.log_box.SetBackgroundColour(C("widget"))
        self.log_box.SetForegroundColour(C("fg"))
        self.log_box.SetFont(FONT(9, mono=True))
        log_card.sizer.Add(self.log_box, 1, wx.EXPAND | wx.ALL, 6)
        root.Add(log_card, 1, wx.EXPAND | wx.ALL, 10)
        p.SetSizer(root)

    # ================================================================ 仪表盘页
    def build_dashboard_page(self):
        p = self.page_dash
        root = wx.BoxSizer(wx.VERTICAL)

        head = Card(p, "当前状态")
        hs = head.sizer
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_state = _text(head.body, "—", size=12, bold=True)
        self.lbl_state.SetMinSize((96, -1))       # 预留宽度，状态变化不挤压后面的文字
        self.lbl_state_sub = _text(head.body, "", ck="muted")
        row.Add(self.lbl_state, 0, wx.ALIGN_CENTER_VERTICAL)
        row.AddSpacer(12)
        row.Add(self.lbl_state_sub, 1, wx.ALIGN_CENTER_VERTICAL)
        hs.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.g_state = Bar(head.body, 0, 100, 10)
        hs.Add(self.g_state, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(head, 0, wx.EXPAND | wx.ALL, 10)

        cards = Card(p, "实时指标")
        grid = wx.FlexGridSizer(cols=4, vgap=8, hgap=8)
        for i in range(4):
            grid.AddGrowableCol(i, 1)
        self.cards = {}
        spec = [("speed", "速度"), ("gpu", "GPU 负载"), ("vram", "显存"), ("temp", "GPU 温度"),
                ("power", "功耗"), ("pcie", "PCIe"), ("cpu", "CPU"), ("disk", "磁盘读")]
        for key, title in spec:
            c = MetricCard(cards.body, title, key)
            self.cards[key] = c
            grid.Add(c, 1, wx.EXPAND)
        cards.sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        root.Add(cards, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        bars = Card(p, "占用")
        self.bars = {}
        for key, title in (("ctx", "上下文填充"), ("slots", "专家缓存（显存）"),
                           ("ram", "系统内存"), ("temp", "GPU 温度")):
            r = wx.BoxSizer(wx.HORIZONTAL)
            r.Add(_text(bars.body, title), 0, wx.ALIGN_CENTER_VERTICAL)
            r.AddStretchSpacer()
            v = _text(bars.body, "–", ck="muted")
            v.SetMinSize((170, -1))
            r.Add(v, 0, wx.ALIGN_CENTER_VERTICAL)
            bars.sizer.Add(r, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)
            g = Bar(bars.body, 0, 100, 12, "accent")
            bars.sizer.Add(g, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
            self.bars[key] = (g, v)
        root.Add(bars, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        facts = Card(p, "引擎")
        fg = wx.FlexGridSizer(cols=4, vgap=6, hgap=14)
        for i in range(4):
            fg.AddGrowableCol(i, 1)
        self.facts = {}
        for key in ("engine", "model", "ctx", "kv", "slots", "uptime", "requests", "output"):
            t = _text(facts.body, "", ck="muted", size=9)
            v = _text(facts.body, "–", size=9)
            v.SetMinSize((170, -1))
            fg.Add(t, 0, wx.ALIGN_CENTER_VERTICAL)
            fg.Add(v, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            self.facts[key] = (t, v)
        facts.sizer.Add(fg, 0, wx.EXPAND | wx.ALL, 8)
        root.Add(facts, 0, wx.EXPAND | wx.ALL, 10)

        recent = Card(p, "最近请求")
        self.req = SList(recent.body, 24)
        for _i, (h, w) in enumerate([("时间", 74), ("状态", 62), ("提示词", 66), ("复用", 56),
                                     ("输出", 56), ("预填充 t/s", 88), ("解码 t/s", 74),
                                     ("耗时", 58)]):
            self.req.InsertColumn(_i, h, w)
        self.req.SetMinSize((-1, 150))
        recent.sizer.Add(self.req, 1, wx.EXPAND | wx.ALL, 6)
        root.Add(recent, 1, wx.EXPAND | wx.ALL, 10)
        p.SetSizer(root)
        p.FitInside()

    # ================================================================ 连接页
    def build_connect_page(self):
        p = self.page_conn
        root = wx.BoxSizer(wx.VERTICAL)

        # ---- 单 Key 监控（实时请求 IP / 用量 / 内容预览 / 耗时 / 限速）
        mon = Card(p, "API Key 监控（按 Key 看实时请求与历史）")
        r = wx.BoxSizer(wx.HORIZONTAL)
        r.Add(_text(mon.body, "按 Key 筛选", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.ch_key = wx.Choice(mon.body, size=(180, 30))
        self.ch_key._bk, self.ch_key._ck = "widget", "fg"
        self.ch_key.SetBackgroundColour(C("widget"))
        self.ch_key.SetForegroundColour(C("fg"))
        self.ch_key.Bind(wx.EVT_CHOICE, lambda e: self.refresh_monitor(full=True))
        r.Add(self.ch_key, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        b = Btn(mon.body, "刷新", size=(70, 32))
        b.Bind(wx.EVT_BUTTON, lambda e: self.refresh_monitor(full=True))
        r.Add(b, 0, wx.ALIGN_CENTER_VERTICAL)
        mon.sizer.Add(r, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        r = wx.BoxSizer(wx.HORIZONTAL)
        r.Add(_text(mon.body, "上游模型服务", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        up = api_gate.upstream_cfg()
        self.txt_up_url = _entry(mon.body, up["url"], width=190)
        self.txt_up_key = _entry(mon.body, up["key"], width=130)
        r.Add(self.txt_up_url, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        r.Add(self.txt_up_key, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        b = Btn(mon.body, "保存上游", size=(88, 32))
        b.Bind(wx.EVT_BUTTON, lambda e: self.on_upstream_save())
        r.Add(b, 0, wx.RIGHT, 10)
        self.btn_gate_toggle = Btn(mon.body, "对外服务：已停止", size=(148, 32))
        self.btn_gate_toggle.Bind(wx.EVT_BUTTON, lambda e: self.on_gate_toggle())
        r.Add(self.btn_gate_toggle, 0)
        mon.sizer.Add(r, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        mon.sizer.Add(_text(mon.body,
                            "地址填上游的接口根地址（如 http://127.0.0.1:39099），留空 = 自动用上面「当前接入」的端口；"
                            "上游要鉴权就填它的 Key，留空 = 不带。改完点保存，即时生效。",
                            ck="muted", size=9), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        r = wx.BoxSizer(wx.HORIZONTAL)
        r.Add(_text(mon.body, "选中 Key 的 token 上限", ck="muted"),
              0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.spin_mon_quota = wx.SpinCtrl(mon.body, value="0", min=0, max=2000000000,
                                          size=(140, 30))
        self.spin_mon_quota._bk, self.spin_mon_quota._ck = "widget", "fg"
        self.spin_mon_quota.SetBackgroundColour(C("widget"))
        self.spin_mon_quota.SetForegroundColour(C("fg"))
        r.Add(self.spin_mon_quota, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        b = Btn(mon.body, "设定", size=(70, 32))
        b.Bind(wx.EVT_BUTTON, lambda e: self.on_quota_quick())
        r.Add(b, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 14)
        self.lbl_mon_ip = _text(mon.body, "最近请求 IP：–", ck="muted")
        self.lbl_mon_tok = _text(mon.body, "已用 token：–", ck="muted")
        for lab in (self.lbl_mon_ip, self.lbl_mon_tok):
            r.Add(lab, 0, wx.RIGHT, 18)
        mon.sizer.Add(r, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        r = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_mon_req = _text(mon.body, "请求 –", ck="muted")
        self.lbl_mon_lim = _text(mon.body, "限速触发 –", ck="muted")
        self.lbl_mon_ms = _text(mon.body, "最近耗时 –", ck="muted")
        for lab in (self.lbl_mon_req, self.lbl_mon_lim, self.lbl_mon_ms):
            r.Add(lab, 0, wx.RIGHT, 18)
        mon.sizer.Add(r, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        self.lst_hist = SList(mon.body, 22)
        for _i, (h, w) in enumerate([("时间", 62), ("Key", 84), ("请求 IP", 108), ("模型", 84),
                                     ("生成内容预览", 300), ("token", 82), ("耗时", 52),
                                     ("状态", 132)]):
            self.lst_hist.InsertColumn(_i, h, w)
        self.lst_hist.SetMinSize((-1, 190))
        mon.sizer.Add(self.lst_hist, 0, wx.EXPAND | wx.ALL, 8)
        root.Add(mon, 0, wx.EXPAND | wx.ALL, 10)
        self.mon_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.refresh_monitor(), self.mon_timer)
        pass  # self.mon_timer.Start(2000)
        wx.CallLater(700, lambda: self.refresh_monitor(full=True))

        cur = Card(p, "当前接入")
        self.lbl_conn = _text(cur.body, "–", size=12, bold=True)
        cur.sizer.Add(self.lbl_conn, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.lbl_check = _text(cur.body, "模型体检：开机后自动检查…", ck="muted", size=9)
        cur.sizer.Add(self.lbl_check, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        r1 = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_scan = Btn(cur.body, "扫描本机所有接口")
        self.btn_scan.SetMinSize((-1, 36))
        self.btn_scan.Bind(wx.EVT_BUTTON, lambda e: self.on_scan())
        r1.Add(self.btn_scan, 0, wx.RIGHT, 8)
        self.btn_use = Btn(cur.body, "接入选中的接口", kind="primary")
        self.btn_use.SetMinSize((-1, 36))
        self.btn_use.Bind(wx.EVT_BUTTON, lambda e: self.on_use_port())
        r1.Add(self.btn_use, 0, wx.RIGHT, 14)
        r1.Add(_text(cur.body, "手动端口", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.spin_port = wx.SpinCtrl(cur.body, value=str(ctl.PORT), min=1, max=65535,
                                     size=(100, 32))
        self.spin_port._bk, self.spin_port._ck = "widget", "fg"
        self.spin_port.SetBackgroundColour(C("widget"))
        self.spin_port.SetForegroundColour(C("fg"))
        r1.Add(self.spin_port, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        b = Btn(cur.body, "接入", size=(72, 36))
        b.Bind(wx.EVT_BUTTON, lambda e: self.set_port(self.spin_port.GetValue()))
        r1.Add(b, 0, wx.ALIGN_CENTER_VERTICAL)
        cur.sizer.Add(r1, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.lst_ports = SList(cur.body, 24)
        for _i, (h, w) in enumerate([("端口", 66), ("类型", 88), ("模型", 300),
                                     ("上下文", 84), ("状态", 78)]):
            self.lst_ports.InsertColumn(_i, h, w)
        self.lst_ports.SetMinSize((-1, 150))
        self.lst_ports.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self.on_use_port())
        cur.sizer.Add(self.lst_ports, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(cur, 0, wx.EXPAND | wx.ALL, 10)

        inst = Card(p, "Strata 安装目录（模型在哪个盘都能找）")
        self.lbl_dir = _text(inst.body, str(ctl.STRATA_DIR))
        inst.sizer.Add(self.lbl_dir, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.lbl_models = _text(inst.body, "", ck="muted")
        inst.sizer.Add(self.lbl_models, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        r2 = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_find = Btn(inst.body, "在所有盘里查找")
        self.btn_find.SetMinSize((-1, 36))
        self.btn_find.Bind(wx.EVT_BUTTON, lambda e: self.on_find_installs())
        r2.Add(self.btn_find, 0, wx.RIGHT, 8)
        b2 = Btn(inst.body, "使用选中的目录", kind="primary")
        b2.SetMinSize((-1, 36))
        b2.Bind(wx.EVT_BUTTON, lambda e: self.on_use_dir())
        r2.Add(b2, 0)
        inst.sizer.Add(r2, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.lst_dirs = SList(inst.body, 24)
        for _i, (h, w) in enumerate([("目录", 400), ("引擎", 68), ("server.py", 78),
                                     ("配置", 230)]):
            self.lst_dirs.InsertColumn(_i, h, w)
        self.lst_dirs.SetMinSize((-1, 130))
        self.lst_dirs.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self.on_use_dir())
        inst.sizer.Add(self.lst_dirs, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(inst, 0, wx.EXPAND | wx.ALL, 10)

        # ---- 引擎 与 模型下载
        dl = Card(p, "Strata 引擎 与 模型下载")
        r = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_engine = _text(dl.body, "引擎：检查中…")
        r.Add(self.lbl_engine, 1, wx.ALIGN_CENTER_VERTICAL)
        self.btn_chk2 = Btn(dl.body, "检查引擎更新", size=(130, 36))
        self.btn_chk2.Bind(wx.EVT_BUTTON, lambda e: self.check_update())
        r.Add(self.btn_chk2, 0, wx.RIGHT, 8)
        self.btn_upd2 = Btn(dl.body, "更新引擎", size=(110, 36), kind="primary")
        self.btn_upd2.Disable()
        self.btn_upd2.Bind(wx.EVT_BUTTON, lambda e: self.on_update())
        r.Add(self.btn_upd2, 0)
        dl.sizer.Add(r, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        dl.sizer.Add(_text(dl.body, "适配本机（64 GB 内存）的型号，选中后点下载，"
                                    "会在弹出的命令行窗口里完成选择与下载：",
                           ck="muted", size=9),
                     0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.lst_models = SList(dl.body, 26)
        for _i, (h, w) in enumerate([("型号", 110), ("说明", 380), ("下载", 70),
                                     ("需要内存", 80), ("状态", 100)]):
            self.lst_models.InsertColumn(_i, h, w)
        self.lst_models.SetMinSize((-1, 190))
        self.lst_models.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self.on_download_model())
        dl.sizer.Add(self.lst_models, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        r2 = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_dl = Btn(dl.body, "下载选中的模型", kind="primary")
        self.btn_dl.SetMinSize((-1, 38))
        self.btn_dl.Bind(wx.EVT_BUTTON, lambda e: self.on_download_model())
        r2.Add(self.btn_dl, 0, wx.RIGHT, 8)
        self.btn_wizard = Btn(dl.body, "打开完整安装向导…")
        self.btn_wizard.SetMinSize((-1, 38))
        self.btn_wizard.Bind(wx.EVT_BUTTON, lambda e: self.on_model_wizard())
        r2.Add(self.btn_wizard, 0)
        dl.sizer.Add(r2, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        dl.sizer.Add(_text(dl.body, "下载完向导会问上下文 / 视觉等设置并写好配置；"
                                    "下载过程别关那个黑色窗口。",
                           ck="muted", size=9),
                     0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(dl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.refresh_models()
        p.SetSizer(root)
        p.FitInside()

    # ================================================================ Key 监控
    def on_upstream_save(self):
        api_gate.save_upstream(self.txt_up_url.GetValue(), self.txt_up_key.GetValue())
        self.log("上游模型服务已保存（地址留空 = 跟随「当前接入」），即时生效", "ok")
        self.refresh_monitor(full=True)

    def on_gate_toggle(self):
        if api_gate.running():
            def job():
                api_gate.save(enabled=False)
                return api_gate.stop()
            self.run_async("停止对外 API", job)
        else:
            st = api_gate.load()

            def job():
                try:
                    msg = api_gate.start(mode=st["mode"], port=st["port"])
                    api_gate.save(enabled=True)
                    if st["mode"] == "public":
                        try:
                            msg += "；" + api_gate.public_start(st["port"])
                        except ctl.ApiError as e:
                            msg += f"；公网入口没开成：{e}"
                    return msg
                except ctl.ApiError as e:
                    return "对外 API 没起来：" + str(e)
            self.run_async("启动对外 API", job)
        wx.CallLater(1500, lambda: self.refresh_gate_status(full=True))

    def on_quota_quick(self):
        i = self.ch_key.GetSelection()
        ids = getattr(self, "_mon_ids", [])
        if i <= 0 or i - 1 >= len(ids):
            wx.MessageBox("先在「按 Key 筛选」里选一把 Key，再设它的 token 上限。",
                          "设定限额", wx.OK | wx.ICON_INFORMATION)
            return
        v = self.spin_mon_quota.GetValue()
        api_gate.key_update(ids[i - 1], token_limit=v)
        self.refresh_monitor(full=True)
        self.refresh_gate_status(full=True)
        self.log(f"该 Key 的 token 上限已设为 {v or '不限（0）'}", "ok")

    def _mon_kid(self) -> str:
        try:
            i = self.ch_key.GetSelection()
        except Exception:                    # noqa: BLE001
            return ""
        ids = getattr(self, "_mon_ids", [])
        return "" if i <= 0 or i - 1 >= len(ids) else ids[i - 1]

    def refresh_monitor(self, full: bool = False):
        try:
            ks = api_gate.keys()
            names = ["全部 Key"] + [f"{k['name']}" for k in ks]
            self._mon_ids = [k["id"] for k in ks]
            cur_i = self.ch_key.GetSelection()
            if full or self.ch_key.GetCount() != len(names):
                self.ch_key.Set(names)
                self.ch_key.SetSelection(max(0, min(cur_i, len(names) - 1)))
            kid = self._mon_kid()
            snap = api_gate.snapshot(kid=kid, limit=60)
            hist = snap["history"]
            cur = next((k for k in snap["keys"] if k["id"] == kid), None) if kid else None

            ip = (hist[0].get("ip") if hist else "") or (cur or {}).get("last_ip", "") or "–"
            used = cur["used_tokens"] if cur else sum(k["used_tokens"] for k in snap["keys"])
            cap = cur["token_limit"] if cur else 0
            set_label(self.lbl_mon_ip, f"最近请求 IP：{ip}")
            set_label(self.lbl_mon_tok,
                      f"已用 token：{kfmt(used)}" + (f" / {kfmt(cap)}" if cap else " / 不限"))
            reqs = cur["requests"] if cur else sum(k["requests"] for k in snap["keys"])
            lims = cur["limited"] if cur else sum(k["limited"] for k in snap["keys"])
            set_label(self.lbl_mon_req, f"请求 {reqs} 次")
            set_label(self.lbl_mon_lim, f"限速触发 {lims} 次")
            set_label(self.lbl_mon_ms, f"最近耗时 {int(hist[0].get('ms') or 0) if hist else 0} ms")
            run = api_gate.running()
            self.btn_gate_toggle.SetLabel("对外服务：运行中" if run else "对外服务：已停止")
            self.btn_gate_toggle.SetKind("danger" if run else "primary")

            sig = (kid, len(hist), hist[0].get("time") if hist else 0,
                   hist[0].get("status") if hist else "")
            if not full and sig == getattr(self, "_mon_sig", None):
                return
            self._mon_sig = sig
            self.lst_hist.DeleteAllItems()
            for rec in hist:
                t = time.strftime("%H:%M:%S", time.localtime(rec.get("time") or 0))
                model = rec.get("model") or ""
                model = model if len(model) <= 12 else model[:11] + "…"
                self.lst_hist.InsertItem(0, t)
                row = self.lst_hist.GetItemCount() - 1
                self.lst_hist.SetItem(row, 1, rec.get("key_name") or "-")
                self.lst_hist.SetItem(row, 2, rec.get("ip") or "-")
                self.lst_hist.SetItem(row, 3, model or "-")
                self.lst_hist.SetItem(row, 4, rec.get("reply") or rec.get("prompt") or "-")
                self.lst_hist.SetItem(row, 5, f"{rec.get('tokens_in', 0)}+{rec.get('tokens_out', 0)}")
                self.lst_hist.SetItem(row, 6, str(int(rec.get("ms") or 0)))
                st = rec.get("status") or ""
                self.lst_hist.SetItem(row, 7, st)
                if st != "ok":
                    bad = any(w in st for w in ("401", "额度", "QPS", "上下文", "超时"))
                    self.lst_hist.SetItemTextColour(row, C("bad") if bad else C("warn"))
        except Exception:                    # noqa: BLE001
            pass

    # ================================================================ 设置页
    def build_settings_page(self):
        p = self.page_set
        root = wx.BoxSizer(wx.VERTICAL)
        s = ctl.load_settings()

        # ---- 对外 API 接口
        gate = Card(p, "对外 API 接口（把模型给别人用）")
        self.cb_gate = Check(gate.body, "开启对外 API 接口（OpenAI / Anthropic 兼容，凭 API Key 使用）",
                             bool(s.get("api_enabled")))
        gate.sizer.Add(self.cb_gate, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        r = wx.BoxSizer(wx.HORIZONTAL)
        r.Add(_text(gate.body, "开放范围", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        mode = str(s.get("api_mode") or "lan")
        self.seg_gate = Seg(gate.body, ["仅本机", "局域网", "公网"],
                            {"local": 0, "lan": 1, "public": 2}.get(mode, 1))
        self.seg_gate.Bind(wx.EVT_RADIOBOX, lambda e: self.on_gate_mode())
        r.Add(self.seg_gate, 0, wx.RIGHT, 16)
        r.Add(_text(gate.body, "端口", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.spin_gate_port = wx.SpinCtrl(gate.body, value=str(int(s.get("api_port") or 8230)),
                                          min=1, max=65535, size=(100, 32))
        self.spin_gate_port._bk, self.spin_gate_port._ck = "widget", "fg"
        self.spin_gate_port.SetBackgroundColour(C("widget"))
        self.spin_gate_port.SetForegroundColour(C("fg"))
        r.Add(self.spin_gate_port, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 14)
        # 「重新生成」+「对外服务」放在端口旁边（同一行右侧）
        self.btn_gate_regen = Btn(gate.body, "重新生成", size=(92, 32))
        self.btn_gate_regen.SetToolTip("换一段新的随机后缀，旧地址立刻失效（公网模式才有）")
        self.btn_gate_regen.Bind(wx.EVT_BUTTON, lambda e: self.on_gate_regen())
        r.Add(self.btn_gate_regen, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        r.Add(_text(gate.body, "对外服务", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.tg_gate = Toggle(gate.body, False)          # 开关跟着真实服务走，不按配置显自启
        self.tg_gate.Bind(wx.EVT_CHECKBOX, lambda e: self.on_gate_toggle_switch())
        r.Add(self.tg_gate, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lbl_gate_run = _text(gate.body, "已停止", ck="muted", bold=True)
        r.Add(self.lbl_gate_run, 0, wx.ALIGN_CENTER_VERTICAL)
        gate.sizer.Add(r, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # ---- 地址行（只读输入框：点一下 = 全选 + 直接复制）
        r = wx.BoxSizer(wx.HORIZONTAL)
        r.Add(_text(gate.body, "接口地址", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.txt_api_addr = wx.TextCtrl(gate.body, value="", style=wx.TE_READONLY)
        self.txt_api_addr._bk, self.txt_api_addr._ck = "widget", "muted"
        self.txt_api_addr.SetBackgroundColour(C("widget"))
        self.txt_api_addr.SetForegroundColour(C("muted"))
        self.txt_api_addr.SetFont(FONT(9, mono=True))
        self.txt_api_addr.SetMinSize((260, 30))
        self.txt_api_addr.SetToolTip("点一下自动全选并复制；这就是发给别人的接口地址")
        self.txt_api_addr.Bind(wx.EVT_LEFT_UP, lambda e: self._addr_select())
        self.txt_api_addr.Bind(wx.EVT_SET_FOCUS, lambda e: self._addr_select())
        r.Add(self.txt_api_addr, 1, wx.ALIGN_CENTER_VERTICAL)
        gate.sizer.Add(r, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        r = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_gate_apply = Btn(gate.body, "保存并生效", size=(130, 38), kind="primary")
        self.btn_gate_apply.Bind(wx.EVT_BUTTON, lambda e: self.on_gate_apply())
        r.Add(self.btn_gate_apply, 0, wx.RIGHT, 12)
        self.btn_gate_router = Btn(gate.body, "打开路由器设置", size=(150, 38))
        self.btn_gate_router.SetToolTip("浏览器打开路由器管理页：开 UPnP，或在「端口转发」里把 TCP 端口转到本机（做一次即可，做好后状态自动变好）")
        self.btn_gate_router.Bind(wx.EVT_BUTTON, lambda e: self.on_open_router())
        r.Add(self.btn_gate_router, 0, wx.RIGHT, 12)
        self.btn_gate_cf = Btn(gate.body, "Cloudflare 隧道配置", size=(178, 38))
        self.btn_gate_cf.SetToolTip(
            "用你自己的 Cloudflare Zero Trust 账号建命名隧道：填 Tunnel Token 和自定义域名，"
            "公网地址固定不变（不再依赖路由器 UPnP / 端口转发）")
        self.btn_gate_cf.Bind(wx.EVT_BUTTON, lambda e: self.on_cf_config())
        r.Add(self.btn_gate_cf, 0, wx.RIGHT, 12)
        self.lbl_gate = _text(gate.body, "", ck="muted")
        r.Add(self.lbl_gate, 1, wx.ALIGN_CENTER_VERTICAL)
        gate.sizer.Add(r, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # ---- 上下文满自动清理（总开关：只在对外 API 开着时生效）
        self.cb_gate_clear = Check(
            gate.body, "上下文用满后自动清理缓存（仅对外 API 开启时生效；清完自动重新驻留显存）",
            bool(s.get("api_auto_clear_ctx")))
        self.cb_gate_clear.SetToolTip(
            "只在「对外服务」开关打开时工作：上下文用量 ≥98% 且模型空闲时，快速回收引擎并立刻重新加载"
            "（约 20-30 秒，趁空闲做），清完后模型驻留显存，新请求直接用。对外接口没开时完全不介入")
        self.cb_gate_clear.Bind(wx.EVT_CHECKBOX, lambda e: self.on_gate_clearctx())
        gate.sizer.Add(self.cb_gate_clear, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        gate.sizer.Add(_text(gate.body,
                             "仅本机 / 局域网：网关就地监听，地址直接可用。\n"
                             "公网：内置服务器 + 路由器 UPnP 自动开端口，"
                             "地址形如 http://公网IP:端口/随机后缀/v1（点「重新生成」换后缀）。\n"
                             "路由器 UPnP 没开时映射会失败：进路由器管理页打开 UPnP（然后关-开一次「对外服务」），"
                             "或在「端口转发」里手动把 TCP 端口转到本机；转发后程序会自动检测并显示「外网端口已通」。\n"
                             "「对外服务」开关点了立刻生效，不用再点「保存并生效」；"
                             "对外接口不开机自启，电脑重启后默认关闭。\n"
                             "随机后缀是暗门，外人只能用你给的完整地址；"
                             "把开关拨到关，会连路由器的端口映射一起撤销。",
                             ck="muted", size=9),
                       0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # ---- 每个 Key 的独立配置
        gate.sizer.Add(_text(gate.body, "API Key（每个 Key 独立额度 / 上下文 / 限速，点一行选中后改下面的配置）",
                             ck="muted", size=9), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        self.lst_keys = SList(gate.body, 24)
        for _i, (h, w) in enumerate([("名称", 92), ("Key", 150), ("IP 地址", 110),
                                     ("已用 / 上限", 116), ("上下文", 66), ("QPS", 44),
                                     ("秒数", 46), ("限速", 44), ("启用", 52)]):
            self.lst_keys.InsertColumn(_i, h, w)
        self.lst_keys.switch_col = 8           # 最后一列画成拨动开关：点一下直接启停
        self.lst_keys.on_switch = self.on_key_switch_row
        self.lst_keys.SetMinSize((-1, 208))
        self.lst_keys.SetToolTip("点一行选中改配置；点行尾开关直接启用/停用该 Key；滚轮翻页")
        self.lst_keys.Bind(wx.EVT_LEFT_UP, self._on_key_row_up)
        self.lst_keys.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self.on_key_pick())
        gate.sizer.Add(self.lst_keys, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Key 码框：选中哪把就显示哪把的完整 Key 码，点一下 = 全选 + 直接复制
        kr2 = wx.BoxSizer(wx.HORIZONTAL)
        kr2.Add(_text(gate.body, "Key 码", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.txt_key_secret = wx.TextCtrl(
            gate.body, value="（在上面列表点一行选中 Key，点这里就复制完整 Key 码）",
            style=wx.TE_READONLY)
        self.txt_key_secret._bk, self.txt_key_secret._ck = "widget", "muted"
        self.txt_key_secret.SetBackgroundColour(C("widget"))
        self.txt_key_secret.SetForegroundColour(C("muted"))
        self.txt_key_secret.SetFont(FONT(9, mono=True))
        self.txt_key_secret.SetMinSize((260, 30))
        self.txt_key_secret.SetToolTip("点一下自动全选并复制完整 Key 码，不用再按 Ctrl+C")
        self.txt_key_secret.Bind(wx.EVT_LEFT_UP, lambda e: self._secret_copy())
        self.txt_key_secret.Bind(wx.EVT_SET_FOCUS, lambda e: self._secret_copy())
        kr2.Add(self.txt_key_secret, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        gate.sizer.Add(kr2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        kr = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_key_add = Btn(gate.body, "新增 Key", size=(92, 34), kind="primary")
        self.btn_key_add.Bind(wx.EVT_BUTTON, lambda e: self.on_key_add())
        self.btn_key_copy = Btn(gate.body, "复制 Key", size=(88, 34))
        self.btn_key_copy.Bind(wx.EVT_BUTTON, lambda e: self.on_key_copy())
        self.btn_key_reset = Btn(gate.body, "重置用量", size=(88, 34))
        self.btn_key_reset.Bind(wx.EVT_BUTTON, lambda e: self.on_key_reset())
        self.btn_key_del = Btn(gate.body, "删除", size=(72, 34), kind="danger")
        self.btn_key_del.Bind(wx.EVT_BUTTON, lambda e: self.on_key_delete())
        self.btn_key_ban = Btn(gate.body, "拉黑该 IP", size=(98, 34), kind="danger")
        self.btn_key_ban.SetToolTip("把选中 Key 最近一次请求的来源 IP 拉黑：它以后所有请求直接 403")
        self.btn_key_ban.Bind(wx.EVT_BUTTON, lambda e: self.on_key_ban())
        self.btn_key_def = Btn(gate.body, "一键默认", size=(92, 34))
        self.btn_key_def.SetToolTip("填入通用默认值：token 2000 · 上下文 60000 · QPS 1（串行排队）· 时长不限")
        self.btn_key_def.Bind(wx.EVT_BUTTON, lambda e: self.on_key_defaults())
        for b in (self.btn_key_add, self.btn_key_copy, self.btn_key_reset,
                  self.btn_key_del, self.btn_key_ban, self.btn_key_def):
            kr.Add(b, 0, wx.RIGHT, 6)
        gate.sizer.Add(kr, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        kg = wx.FlexGridSizer(cols=4, vgap=6, hgap=10)
        kg.AddGrowableCol(1, 1)
        kg.AddGrowableCol(3, 1)
        kg.Add(_text(gate.body, "名称", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_key_name = _entry(gate.body, "", width=120)
        kg.Add(self.txt_key_name, 0, wx.EXPAND)
        kg.Add(_text(gate.body, "token 上限", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_key_quota = wx.SpinCtrl(gate.body, value="2000", min=0, max=2000000000,
                                          size=(124, 30))
        self.spin_key_quota.SetToolTip("0 = 不限；默认 2000")
        kg.Add(self.spin_key_quota, 0)
        kg.Add(_text(gate.body, "单请求上下文", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_key_ctx = wx.SpinCtrl(gate.body, value="60000", min=0, max=2000000,
                                        size=(124, 30))
        kg.Add(self.spin_key_ctx, 0)
        kg.Add(_text(gate.body, "QPS", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_key_qps = wx.SpinCtrl(gate.body, value="1", min=0, max=10000, size=(124, 30))
        self.spin_key_qps.SetToolTip("默认 1：同一把 Key 的请求串行排队，上一个完整返回后才处理下一个")
        kg.Add(self.spin_key_qps, 0)
        kg.Add(_text(gate.body, "秒上限", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_key_secs = wx.SpinCtrl(gate.body, value="0", min=0, max=86400, size=(124, 30))
        kg.Add(self.spin_key_secs, 0)
        for sp in (self.spin_key_quota, self.spin_key_ctx, self.spin_key_qps,
                   self.spin_key_secs):
            sp._bk, sp._ck = "widget", "fg"
            sp.SetBackgroundColour(C("widget"))
            sp.SetForegroundColour(C("fg"))
        # 选中 Key 后改「token 上限 / 单次请求上下文」：不用点保存，600ms 后即时生效
        self.spin_key_quota.Bind(wx.EVT_SPINCTRL, self.on_key_live_edit)
        self.spin_key_ctx.Bind(wx.EVT_SPINCTRL, self.on_key_live_edit)
        self.spin_key_quota.Bind(wx.EVT_TEXT, self.on_key_live_edit)
        self.spin_key_ctx.Bind(wx.EVT_TEXT, self.on_key_live_edit)
        gate.sizer.Add(kg, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        gate.sizer.Add(_text(gate.body,
                             "默认值：token 2000 · 上下文 60000 · QPS 1 · 时长不限（0 = 不限），"
                             "点「一键默认」直接填入。token 用量按上游返回的 usage 计，没有 usage 按字数估算。\n"
                             "QPS = 1 时同一把 Key 的请求串行排队：上一个请求完整返回后才处理下一个"
                             "（排队超过 8 个返回 429）。\n"
                             "选中 Key 后直接改 token 上限 / 单次请求上下文，改动即时保存并同步到列表。",
                             ck="muted", size=9), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)

        rr = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_key_save = Btn(gate.body, "保存该 Key 配置", size=(140, 36), kind="primary")
        self.btn_key_save.Bind(wx.EVT_BUTTON, lambda e: self.on_key_save())
        rr.Add(self.btn_key_save, 0, wx.RIGHT, 8)
        self.btn_key_enable = Btn(gate.body, "停用该 Key", size=(110, 36))
        self.btn_key_enable.Bind(wx.EVT_BUTTON, lambda e: self.on_key_toggle())
        rr.Add(self.btn_key_enable, 0, wx.RIGHT, 10)
        self.lbl_key_tip = _text(gate.body, "未选中 Key（填好参数直接保存 = 新增一个）", ck="muted", size=9)
        rr.Add(self.lbl_key_tip, 1, wx.ALIGN_CENTER_VERTICAL)
        gate.sizer.Add(rr, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 8)

        self.cb_gate.Bind(wx.EVT_CHECKBOX, lambda e: self.on_gate_master())

        root.Add(gate, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.gate_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.refresh_gate_status(), self.gate_timer)
        self.gate_timer.Start(3000)             # 地址/开关/状态跟着真实服务走
        try:
            n = api_gate.rename_generic_keys()  # 「Key N」→ 按配置起的功能名
            if n:
                self.log(f"已把 {n} 把通用名的 Key 按功能重命名（可选中后自己改）", "info")
        except Exception:                        # noqa: BLE001
            pass
        self.sync_gate_controls()
        self.refresh_gate_status(full=True)

        # ---- 外观
        look = Card(p, "外观")
        r = wx.BoxSizer(wx.HORIZONTAL)
        r.Add(_text(look.body, "主题", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.rb_theme = Seg(look.body, ["白色", "黑色", "自定义"],
                            {"light": 0, "dark": 1}.get(THEME.name, 2))
        self.rb_theme.Bind(wx.EVT_RADIOBOX, lambda e: self.on_theme())
        r.Add(self.rb_theme, 0)
        look.sizer.Add(r, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        look.sizer.Add(_text(look.body, "自定义颜色（点色块自己挑）", ck="muted", size=9),
                       0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sw = wx.WrapSizer(wx.HORIZONTAL)
        self.swatches = {}
        for label, key in (("背景", "bg"), ("文字", "fg"), ("强调色", "accent"), ("卡片", "card")):
            w = Swatch(look.body, label, key)
            w.Bind(wx.EVT_BUTTON, lambda e, k=key: self.on_pick_colour(k))
            self.swatches[key] = w
            sw.Add(w, 0, wx.RIGHT | wx.BOTTOM, 8)
        look.sizer.Add(sw, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        look.sizer.Add(_text(look.body,
                             "选「自定义」后，改背景会按明暗自动配好文字、卡片和边框；"
                             "单独改过「文字」就以你选的为准。",
                             ck="muted", size=9),
                       0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        r = wx.BoxSizer(wx.HORIZONTAL)
        r.Add(_text(look.body, "整体透明度", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.sl_alpha = HSlider(look.body, THEME.alpha, 60, 255)
        self.sl_alpha.Bind(wx.EVT_SLIDER, lambda e: self.on_alpha())
        r.Add(self.sl_alpha, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.lbl_alpha = _text(look.body, f"{int(THEME.alpha * 100 / 255)}%", ck="muted")
        r.Add(self.lbl_alpha, 0, wx.ALIGN_CENTER_VERTICAL)
        look.sizer.Add(r, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.cb_float = Check(look.body, "显示悬浮窗（只显示仪表盘数据的小窗）",
                              bool(s.get("float_on")))
        self.cb_float.Bind(wx.EVT_CHECKBOX, lambda e: self.on_float_toggle())
        look.sizer.Add(self.cb_float, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(look, 0, wx.EXPAND | wx.ALL, 10)

        # ---- 自启动
        auto = Card(p, "后台自启动")
        self.cb_auto = Check(auto.body, "开机时自动启动本控制台", ctl.autostart_enabled())
        self.cb_auto.Bind(wx.EVT_CHECKBOX, lambda e: self.on_autostart())
        auto.sizer.Add(self.cb_auto, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        r = wx.BoxSizer(wx.HORIZONTAL)
        r.Add(_text(auto.body, "启动后显示", ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.rb_start = Seg(auto.body, ["完整控制台", "只显示悬浮窗"],
                            1 if str(s.get("autostart_mode")) == "float" else 0)
        self.rb_start.Bind(wx.EVT_RADIOBOX, lambda e: self.on_autostart())
        r.Add(self.rb_start, 0)
        auto.sizer.Add(r, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.lbl_auto = _text(auto.body, "", ck="muted", size=9)
        self.lbl_auto.Wrap(700)
        auto.sizer.Add(self.lbl_auto, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(auto, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.refresh_autostart_label()

        # ---- 上下文
        ctxb = Card(p, "上下文长度（--max-context）")
        self.lbl_ctx_now = _text(ctxb.body, "当前生效：–")
        self.lbl_ctx_cfg = _text(ctxb.body, "配置里：–")
        self.lbl_ctx_native = _text(ctxb.body, "模型上限：–")
        for lab in (self.lbl_ctx_now, self.lbl_ctx_cfg, self.lbl_ctx_native):
            ctxb.sizer.Add(lab, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        ctxb.sizer.AddSpacer(10)
        self.ctx_slider = HSlider(ctxb.body, 32768, 4096, 131072)
        self.ctx_slider.SetLineSize(1024)
        self.ctx_slider.SetPageSize(8192)
        self.ctx_slider.Bind(wx.EVT_SLIDER, self.on_ctx_slide)
        ctxb.sizer.Add(self.ctx_slider, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.lbl_ctx_pick = _text(ctxb.body, "已选：–", size=12, bold=True)
        ctxb.sizer.Add(self.lbl_ctx_pick, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        presets = wx.BoxSizer(wx.HORIZONTAL)
        for n in (16384, 32768, 65536, 100000, 131072):
            b = Btn(ctxb.body, f"{n // 1024}K" if n % 1024 == 0 else f"{n // 1000}K",
                    size=(68, 32))
            b.Bind(wx.EVT_BUTTON, lambda e, v=n: self.set_ctx_slider(v))
            presets.Add(b, 0, wx.RIGHT | wx.BOTTOM, 6)
        ctxb.sizer.Add(presets, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.btn_ctx_apply = Btn(ctxb.body, "应用并重启服务", kind="primary")
        self.btn_ctx_apply.SetMinSize((-1, 38))
        self.btn_ctx_apply.Bind(wx.EVT_BUTTON, lambda e: self.on_ctx_apply())
        ctxb.sizer.Add(self.btn_ctx_apply, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        ctxb.sizer.Add(_text(ctxb.body,
                             "上下文越大，KV 缓存占用越多：超出 --kv-resident（32768）的部分走"
                             "内存 / PCIe，长文能装下但速度会降。改完要重启服务才生效。",
                             ck="muted", size=9),
                       0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(ctxb, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # ---- 更新
        ub = Card(p, "Strata 更新")
        self.cb_upd = Check(ub.body, "发现新版本就自动更新（服务没在跑的时候才装）",
                            bool(s.get("auto_update")))
        self.cb_upd.Bind(wx.EVT_CHECKBOX,
                         lambda e: ctl.save_settings(auto_update=self.cb_upd.IsChecked()))
        ub.sizer.Add(self.cb_upd, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.lbl_upd = _text(ub.body, "还没检查过。", ck="muted", size=9)
        ub.sizer.Add(self.lbl_upd, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        ur = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_check = Btn(ub.body, "检查更新", size=(120, 38))
        self.btn_update = Btn(ub.body, "更新 Strata", size=(120, 38), kind="primary")
        self.btn_update.Disable()
        self.btn_check.Bind(wx.EVT_BUTTON, lambda e: self.check_update())
        self.btn_update.Bind(wx.EVT_BUTTON, lambda e: self.on_update())
        ur.Add(self.btn_check, 0, wx.RIGHT, 8)
        ur.Add(self.btn_update, 0)
        ub.sizer.Add(ur, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        ub.sizer.Add(_text(ub.body, "更新会：停服务 → 拉源码（镜像）→ 下引擎包并校验 → "
                                    "setup.py 安装。旧引擎留在 engine/.previous，可回退。",
                           ck="muted", size=9),
                     0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(ub, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        p.SetSizer(root)
        p.FitInside()

    # ================================================================ 对外 API
    def _gate_mode(self) -> str:
        return ("local", "lan", "public")[max(0, min(2, self.seg_gate.GetSelection()))]

    def _clip_copy(self, text: str, what: str = "内容") -> None:
        if not text:
            self.log(f"没有可复制的{what}", "warn")
            self.set_status(f"没有可复制的{what}")
            return
        last = ""
        for _i in range(4):                    # Windows 剪贴板常被别的程序占着，多试几次
            try:
                if wx.TheClipboard.Open():
                    try:
                        wx.TheClipboard.SetData(wx.TextDataObject(text))
                        wx.TheClipboard.Flush()          # 立刻固化，程序退出也不丢
                        ok = True
                    except Exception as e:
                        ok = False
                        last = str(e)
                    finally:
                        wx.TheClipboard.Close()
                    if ok:
                        self.log(f"{what}已复制到剪贴板（{len(text)} 字符）", "ok")
                        self.set_status(f"{what}已复制")
                        return
            except Exception as e:
                last = str(e)
            time.sleep(0.12)
        # 兜底：绕过 wx，直接写系统剪贴板
        try:
            self._win32_copy(text)
            self.log(f"{what}已复制到剪贴板（系统直写，{len(text)} 字符）", "ok")
            self.set_status(f"{what}已复制")
        except Exception as e:
            self.log(f"复制失败（{last or e}）。已把内容显示到状态栏，请手动复制", "err")
            self.set_status(f"{what}：{text}")

    @staticmethod
    def _win32_copy(text: str) -> None:
        """不经过 wx 的剪贴板直写（CF_UNICODETEXT）。"""
        import ctypes
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        # 64 位下指针/句柄必须显式声明，否则 GlobalLock 返回值被截断成 int
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.restype = ctypes.c_void_p
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not u32.OpenClipboard(0):
            raise OSError("OpenClipboard 失败")
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                raise OSError("GlobalAlloc 失败")
            p = k32.GlobalLock(h)
            if not p:
                raise OSError("GlobalLock 失败")
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(CF_UNICODETEXT, h):
                raise OSError("SetClipboardData 失败")
        finally:
            u32.CloseClipboard()

    # ---- 地址 / 模式
    def _addr_select(self):
        """只读输入框：点一下就全选并直接复制（不用再按 Ctrl+C）。"""
        try:
            self.txt_api_addr.SetSelection(-1, -1)
            text = self.txt_api_addr.GetValue()
            if text and not text.startswith(("生成", "（")):
                self._clip_copy(text, "接口地址")
        except Exception:
            pass

    def _secret_copy(self):
        """Key 码框：点一下 = 全选 + 直接复制完整 Key 码。"""
        try:
            text = self.txt_key_secret.GetValue()
            if not text or text.startswith("（"):
                self.log("先在上面列表点一行选中 Key，再点这里复制", "warn")
                return
            self.txt_key_secret.SetSelection(-1, -1)
            self._clip_copy(text, "Key 码")
        except Exception:
            pass

    def _set_addr(self, text: str, ck: str = "fg"):
        try:
            text = text or ""
            if self.txt_api_addr.GetValue() != text:
                self.txt_api_addr.SetValue(text)
            self.txt_api_addr._ck = ck
            self.txt_api_addr.SetForegroundColour(C(ck))
            self.txt_api_addr.Refresh()
        except Exception:
            pass

    def sync_gate_controls(self):
        """总开关（开放对外 API 接口）没勾上时，Key 相关的按钮一律置灰。"""
        try:
            on = self.cb_gate.IsChecked()
        except Exception:
            on = False
        for b in (getattr(self, "btn_key_add", None), getattr(self, "btn_key_copy", None),
                  getattr(self, "btn_key_reset", None), getattr(self, "btn_key_del", None),
                  getattr(self, "btn_key_save", None), getattr(self, "btn_key_enable", None),
                  getattr(self, "btn_key_ban", None), getattr(self, "btn_key_def", None)):
            if b is not None:
                b.Enable(on)
        if not on:
            # 明确告诉用户为什么这些按钮是灰的，免得点了「没反应」以为坏了
            try:
                set_label(self.lbl_key_tip,
                          "「开启对外 API 接口」总开关没勾：Key 管理按钮暂不可用，勾上后即可新增 / 删除",
                          "warn")
            except Exception:
                pass
        try:
            self.tg_gate.Enable(on)
        except Exception:
            pass
        try:
            self.spin_gate_port.Enable(True)
        except Exception:
            pass
        try:
            self.lbl_key_tip.SetLabel(self.lbl_key_tip.GetLabel())   # 触发重排
        except Exception:
            pass

    def on_gate_master(self):
        """总开关：只管「允不允许对外 + 能不能编辑 Key」。
        勾上只是解锁（启动必须手动拨「对外服务」开关）；取消则立刻停掉对外服务。"""
        on = self.cb_gate.IsChecked()
        self.sync_gate_controls()
        if on:
            self.log("已允许对外：要启动请拨右上角「对外服务」开关（不会自己启动）", "info")
            return
        self.tg_gate.SetValue(False)
        self._set_running_look(False)
        self._set_addr("", "muted")
        self.log("总开关已关：立刻停止对外接口（含路由器端口映射）", "warn")
        self._apply_gate(False)

    def on_gate_toggle_switch(self):
        """拨动开关：开=启动端口，关=停止（连端口映射一起撤）。"""
        want = self.tg_gate.GetValue()
        if want and not self.cb_gate.IsChecked():
            self.cb_gate.SetValue(True)
            self.sync_gate_controls()
        self._set_running_look(want)
        if not want:
            self.log("对外服务已停止", "warn")
            self.cb_gate.SetValue(False)          # 拨到关 = 不再对外，总开关一起落下
            self.sync_gate_controls()
            self._set_addr("", "muted")
            self._apply_gate(False)
            return
        if not [k for k in api_gate.keys() if k["enabled"]]:
            wx.MessageBox("还没有可用的 API Key，先点「新增 Key」。", "对外 API 接口",
                          wx.OK | wx.ICON_INFORMATION)
            self.tg_gate.SetValue(False)
            self._set_running_look(False)
            return
        self._apply_gate(True)

    def _set_running_look(self, running: bool):
        try:
            self.lbl_gate_run.SetLabel("运行中" if running else "已停止")
            self.lbl_gate_run._ck = "ok" if running else "muted"
            self.lbl_gate_run.SetForegroundColour(C("ok" if running else "muted"))
            self.lbl_gate_run.Refresh()
        except Exception:
            pass

    def _apply_gate(self, enabled: bool):
        """真正落盘 + 启停端口，并把界面（地址/开关/状态）同步过去。"""
        mode = self._gate_mode()
        port = self.spin_gate_port.GetValue()
        if enabled:
            self._set_addr("生成中…" if mode == "public" else api_gate.public_base(port, mode), "muted")

        def job():
            return api_gate.apply(enabled, mode, port)
        self.run_async("应用对外 API 设置", job)
        wx.CallLater(5000 if (enabled and mode == "public") else 1200,
                     lambda: self.refresh_gate_status(full=True))

    def on_gate_mode(self):
        """切模式先把地址清空，避免旧地址留在界面上误导人。"""
        self._set_addr("", "muted")
        if self._gate_mode() != "public" and api_gate.public_info()["active"]:
            def job():
                api_gate.public_stop(silent=False)
            self.run_async("撤销公网端口映射", job)
        try:
            self.btn_gate_regen.Enable(self._gate_mode() == "public")
        except Exception:
            pass
        self.log(f"开放范围切到「{['仅本机', '局域网', '公网'][self.seg_gate.GetSelection()]}」，"
                 f"点「保存并生效」应用", "info")

    def on_gate_regen(self):
        """换一段新的随机后缀（旧地址立刻失效）。只有公网模式有后缀。"""
        if self._gate_mode() != "public":
            wx.MessageBox("「仅本机 / 局域网」的地址是固定的，没有随机后缀可换。\n"
                          "要随机后缀请把开放范围切到「公网」。", "重新生成地址",
                          wx.OK | wx.ICON_INFORMATION)
            return
        if not api_gate.running():
            wx.MessageBox("先拨开「对外服务」（或点「保存并生效」）再换后缀。", "公网地址",
                          wx.OK | wx.ICON_INFORMATION)
            return
        self._set_addr("重新生成中…", "muted")

        def job():
            api_gate.new_suffix()
            api_gate.public_stop(silent=True)
            return api_gate.public_start()
        self.run_async("重新生成公网地址", job)
        wx.CallLater(4000, lambda: self.refresh_gate_status(full=True))

    def on_gate_apply(self):
        enabled = self.cb_gate.IsChecked()
        if enabled and not [k for k in api_gate.keys() if k["enabled"]]:
            wx.MessageBox("还没有可用的 API Key，先点「新增 Key」。", "对外 API 接口",
                          wx.OK | wx.ICON_INFORMATION)
            return
        self.tg_gate.SetValue(enabled)
        self._set_running_look(enabled)
        self._apply_gate(enabled)

    # 对外接口不再开机自启：想开就手动拨「对外服务」开关，重启电脑后默认是关的。

    # ---- Key 管理
    def _on_key_row_up(self, e):
        """松开鼠标：同步把点的行回填到表单。SList 自己在 DOWN 上管选中，
        这里只负责回填；必须 Skip，否则会把列表内部的 UP 处理器阻断。"""
        wx.CallAfter(self.on_key_pick)
        e.Skip()

    def on_key_switch_row(self, row: int):
        """点行尾开关：直接启用/停用这一行的 Key（一个 Key 一个开关）。"""
        try:
            ks = api_gate.keys()
            if not (0 <= row < len(ks)):
                return
            k = ks[row]
            nk = api_gate.key_update(k["id"], enabled=not k["enabled"])
            self.refresh_gate_status(full=True)
            cur = self._sel_key()
            if cur and cur["id"] == k["id"]:
                self._show_key(nk or k)
            self.log(f"「{k['name']}」已{'启用' if nk and nk['enabled'] else '停用'}"
                     f"（行内开关）", "ok" if nk and nk["enabled"] else "warn")
        except Exception as e:                    # noqa: BLE001
            self.log(f"切换 Key 开关出错：{e}", "err")

    def on_open_router(self):
        """一键打开路由器管理页：UPnP / 端口转发只能在这里做，软件代做不了。"""
        gw = api_gate.public_info().get("gateway") or api_gate.gateway_ip()
        port = self.spin_gate_port.GetValue()
        local = api_gate.lan_ip()
        if not gw:
            wx.MessageBox("没拿到网关地址，请手动在浏览器输入路由器管理页地址（一般是 192.168.1.1）。",
                          "打开路由器设置", wx.OK | wx.ICON_INFORMATION)
            return
        try:
            os.startfile(f"http://{gw}")      # noqa: S606 - Windows 标准方式
        except Exception:                     # noqa: BLE001
            try:
                import webbrowser
                webbrowser.open(f"http://{gw}")
            except Exception as e:            # noqa: BLE001
                self.log(f"打不开浏览器：{e}；请手动访问 http://{gw}", "err")
                return
        self.log(f"已打开路由器管理页 http://{gw}：① 找到 UPnP 设置打开，然后关-开一次「对外服务」；"
                 f"② 或在「端口转发」添加：外部 TCP {port} → {local}:{port}。"
                 "做好后状态会自动变成「外网端口已通」", "info")

    def on_cf_config(self):
        """Cloudflare 隧道配置页：Tunnel Token + 自定义域名，用用户自己的账号建命名隧道。"""
        cfg = api_gate.cf_cfg()
        dlg = wx.Dialog(self, title="Cloudflare 隧道配置", size=(560, 340))
        dlg.SetBackgroundColour(C("bg"))
        vs = wx.BoxSizer(wx.VERTICAL)
        vs.Add(_text(dlg, "用你自己的 Cloudflare Zero Trust 账号建命名隧道：地址固定不变，\n"
                          "不再依赖路由器 UPnP / 端口转发。留空则继续用自动隧道（随机地址）。",
                     ck="muted", size=9), 0, wx.ALL, 12)

        def row(label):
            r = wx.BoxSizer(wx.HORIZONTAL)
            r.Add(_text(dlg, label, ck="muted"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            t = wx.TextCtrl(dlg, size=(-1, 30))
            t.SetBackgroundColour(C("widget"))
            t.SetForegroundColour(C("fg"))
            r.Add(t, 1, wx.ALIGN_CENTER_VERTICAL)
            vs.Add(r, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
            return t
        t_tok = row("Tunnel Token")
        t_tok.SetValue(cfg["token"])
        t_tok.SetHint("在 Zero Trust → Networks → Tunnels 里创建隧道后复制 Token")
        t_dom = row("自定义域名")
        t_dom.SetValue(cfg["domain"])
        t_dom.SetHint("例如 api.example.com（需已托管在该 Cloudflare 账号，且在隧道 Public Hostname 里绑好）")
        vs.Add(_text(dlg, "获取步骤：Cloudflare 控制台 → Zero Trust → Networks → Tunnels →\n"
                          "Create a tunnel → 复制 Token；再在该隧道的 Public Hostname 里把你的域名\n"
                          "指向 http://127.0.0.1:8230（端口以本页「端口」为准）。域名要已托管在 Cloudflare。",
                     ck="muted", size=9), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        hs = wx.BoxSizer(wx.HORIZONTAL)
        hs.AddStretchSpacer(1)
        b_cancel = Btn(dlg, "取消", size=(96, 34))
        b_cancel.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CANCEL))
        hs.Add(b_cancel, 0, wx.RIGHT, 10)
        b_ok = Btn(dlg, "保存并应用", size=(120, 34), kind="primary")
        hs.Add(b_ok, 0, wx.RIGHT, 12)
        vs.Add(hs, 0, wx.BOTTOM, 14)
        dlg.SetSizer(vs)
        dlg.CentreOnParent()

        def apply(_e=None):
            tok = t_tok.GetValue().strip()
            dom = t_dom.GetValue().strip().lower().rstrip("/")
            if tok and not dom:
                wx.MessageBox("填了 Tunnel Token 就要同时填自定义域名（命名隧道的公网地址就是它）；"
                              "两个都留空则继续用自动隧道。", "Cloudflare 隧道配置",
                              wx.OK | wx.ICON_WARNING)
                return
            api_gate.save_cf(token=tok, domain=dom)
            dlg.EndModal(wx.ID_OK)
            if tok and dom:
                self.log(f"Cloudflare 隧道配置已保存：域名 {dom}。地址固定为 https://{dom}/…/v1", "ok")
            else:
                self.log("Cloudflare 隧道配置已清空：对外服务将用自动隧道（trycloudflare 随机地址）", "info")
            # 网关正在对外服务 → 立即切换隧道模式（关-开一次公网入口，地址按新配置生成）
            try:
                if api_gate.running() and api_gate.public_info()["active"]:
                    port = self.spin_gate_port.GetValue()
                    api_gate.public_stop(silent=True)
                    msg = api_gate.public_start(port)
                    self.log(msg, "ok")
                    self.refresh_gate_status(full=True)
            except Exception as e:                # noqa: BLE001
                self.log(f"应用 Cloudflare 隧道配置失败：{e}；可关-开一次「对外服务」重试", "err")
        b_ok.Bind(wx.EVT_BUTTON, apply)
        dlg.ShowModal()
        dlg.Destroy()

    def _sel_key(self):
        i = self.lst_keys.GetFirstSelected()
        ks = api_gate.keys()
        if 0 <= i < len(ks):
            return ks[i]
        return None

    def _key_params(self) -> dict:
        """把下面那排输入框读成一份 Key 配置。"""
        def num(w, cast=int):
            try:
                return cast(w.GetValue() or 0)
            except Exception:
                return cast(0)
        return {"name": self.txt_key_name.GetValue().strip(),
                "token_limit": num(self.spin_key_quota),
                "context_limit": num(self.spin_key_ctx),
                "qps": num(self.spin_key_qps, float),
                "max_seconds": num(self.spin_key_secs)}

    def _show_key(self, k: dict):
        """把某个 Key 的参数回填到输入框，并选中它那一行。"""
        if not k:
            return
        self.txt_key_name.SetValue(k["name"])
        self.spin_key_quota.SetValue(int(k["token_limit"]))
        self.spin_key_ctx.SetValue(int(k["context_limit"]))
        self.spin_key_qps.SetValue(int(k["qps"]))
        self.spin_key_secs.SetValue(int(k["max_seconds"]))
        self.btn_key_enable.SetLabel("停用该 Key" if k["enabled"] else "启用该 Key")
        try:                                  # Key 码框跟着选中走：显示完整 Key 码
            self.txt_key_secret.SetValue(k["secret"])
            self.txt_key_secret._ck = "fg"
            self.txt_key_secret.SetForegroundColour(C("fg"))
            self.txt_key_secret.Refresh()
        except Exception:                     # noqa: BLE001
            pass
        ips = [x for x in (k.get("ips") or []) if x] or ([k["last_ip"]] if k.get("last_ip") else [])
        ip = ips[0] if ips else ""
        try:
            if ip:
                banned = api_gate.is_blocked(ip)
                self.btn_key_ban.SetLabel("解除拉黑" if banned else "拉黑该 IP")
                self.btn_key_ban.SetToolTip(
                    f"选中 Key 最近来源 IP：{ip}" + ("（已拉黑，点击解除）" if banned else "（点击拉黑）"))
            else:
                self.btn_key_ban.SetLabel("拉黑该 IP")
                self.btn_key_ban.SetToolTip("该 Key 还没有请求记录，没有 IP 可拉黑")
        except Exception:
            pass
        set_label(self.lbl_key_tip,
                  f"已选中「{k['name']}」：已用 {kfmt(k['used_tokens'])}"
                  f" / {kfmt(k['token_limit']) if k['token_limit'] else '不限'} token"
                  + (f" · 来源 IP {ip}" if ip else ""), "muted")
        self.refresh_key_list(select_id=k["id"])

    def on_key_defaults(self):
        """一键填入通用默认值：token 2000 · 上下文 60K · QPS 1（串行排队）· 时长不限。"""
        self.spin_key_quota.SetValue(2000)
        self.spin_key_ctx.SetValue(60000)
        self.spin_key_qps.SetValue(1)
        self.spin_key_secs.SetValue(0)
        k = self._sel_key()
        set_label(self.lbl_key_tip,
                  "已填入默认：token 2000 · 上下文 60000 · QPS 1（串行排队）· 时长不限"
                  + ("（点「保存该 Key 配置」应用到选中的 Key）" if k else "（新增 Key 直接用这组值）"),
                  "muted")
        self.log("已填入通用默认数值：token 2000、上下文 60000、QPS 1（串行排队）、时长不限", "info")

    def on_key_ban(self):
        """把选中 Key 最近一次请求的来源 IP 拉黑 / 解除。"""
        k = self._sel_key()
        if not k:
            self.log("先在列表里点一行选中 Key", "warn")
            return
        ips = [x for x in (k.get("ips") or []) if x] or ([k["last_ip"]] if k.get("last_ip") else [])
        if not ips:
            wx.MessageBox("这把 Key 还没有请求记录，没有可拉黑的 IP。", "拉黑 IP",
                          wx.OK | wx.ICON_INFORMATION)
            return
        ip = ips[0]
        banned = api_gate.is_blocked(ip)
        api_gate.block_ip(ip, block=not banned)
        nk = next((x for x in api_gate.keys() if x["id"] == k["id"]), k)
        self.refresh_gate_status(full=True)
        self._show_key(nk)
        bl = api_gate.blocked_ips()
        if banned:
            self.log(f"已解除 {ip} 的拉黑（黑名单 {len(bl)} 个）", "ok")
        else:
            self.log(f"已拉黑 {ip}：这个地址以后的所有请求直接 403（黑名单 {len(bl)} 个）", "warn")

    def on_key_pick(self):
        k = self._sel_key()
        if not k:
            return
        self._show_key(k)

    def on_key_add(self):
        """按下面的参数新建一把 Key，直接追加到列表并选中。"""
        p = self._key_params()
        name = p.pop("name") or f"Key {len(api_gate.keys()) + 1}"
        k = api_gate.key_add(name, **p)
        self._clip_copy(k["secret"], "新 Key")
        self._show_key(k)
        self.refresh_gate_status(full=False)
        self.log(f"已新增 API Key「{k['name']}」：额度 {p['token_limit'] or '不限'}，"
                 f"上下文 {p['context_limit'] or '不限'}，QPS {p['qps'] or '不限'}，"
                 f"时长 {p['max_seconds'] or '不限'} 秒（Key 已复制到剪贴板）", "ok")

    def on_key_copy(self):
        k = self._sel_key()
        if not k:
            wx.MessageBox("先在列表里点一行选中 Key，再点「复制 Key」；\n"
                          "或者直接点列表下面的「Key 码」框，点一下就复制。",
                          "复制 Key", wx.OK | wx.ICON_INFORMATION)
            return
        self._clip_copy(k["secret"], f"Key「{k['name']}」")

    def on_key_reset(self):
        k = self._sel_key()
        if not k:
            self.log("先在列表里点一行选中 Key", "warn")
            return
        api_gate.key_reset(k["id"])
        self.refresh_gate_status(full=True)
        self.log(f"已重置「{k['name']}」的用量统计", "ok")

    def on_key_delete(self):
        try:
            k = self._sel_key()
            if not k:
                wx.MessageBox("请先在上方列表里点一行选中要删除的 Key，再点「删除」。",
                              "删除 Key", wx.OK | wx.ICON_INFORMATION)
                return
            if wx.MessageBox(f"删除 Key「{k['name']}」？\n\n删除后这个 Key 码立即失效，"
                             "再有人拿它请求会被直接拒绝。", "删除 Key",
                             wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
                return
            if not api_gate.key_delete(k["id"]):
                self.log("删除失败：列表和磁盘上的 Key 表对不上（可能刚被其他操作改过），已刷新，请重试", "err")
                self.refresh_gate_status(full=True)
                return
            # 删完把表单清空，别再留着已删 Key 的参数误导人
            self.txt_key_name.SetValue("")
            self.txt_key_secret.SetValue("（在上面列表点一行选中 Key，点这里就复制完整 Key 码）")
            self.txt_key_secret._ck = "muted"
            self.txt_key_secret.SetForegroundColour(C("muted"))
            set_label(self.lbl_key_tip, "已删除；表单已清空，可点「新增 Key」建新的", "muted")
            self.refresh_gate_status(full=True)
            self.log(f"已删除 Key「{k['name']}」：这个 Key 码已失效，外部用它请求会立刻被拒绝", "warn")
        except Exception as e:                    # noqa: BLE001
            self.log(f"删除 Key 出错：{e}", "err")

    def on_key_toggle(self):
        k = self._sel_key()
        if not k:
            self.log("先在列表里点一行选中 Key", "warn")
            return
        nk = api_gate.key_update(k["id"], enabled=not k["enabled"])
        self.refresh_gate_status(full=True)
        self._show_key(nk or k)                # 回填 + 刷新按钮文字
        self.log(f"「{k['name']}」已{'停用' if k['enabled'] else '启用'}", "ok")

    def on_key_live_edit(self, _evt=None):
        """选中 Key 后改 token 上限 / 单次请求上下文：防抖后即时保存。"""
        if getattr(self, "_live_edit_timer", None) is None:
            t = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, lambda e: self._live_key_save(), t)
            self._live_edit_timer = t
        if not self._live_edit_timer.IsRunning():
            self._live_edit_timer.Start(600, oneShot=True)

    def _live_key_save(self):
        k = self._sel_key()
        if not k:
            return                                   # 没选中 = 在给新 Key 填参数，不落盘
        q = int(self.spin_key_quota.GetValue() or 0)
        c = int(self.spin_key_ctx.GetValue() or 0)
        if q == int(k["token_limit"]) and c == int(k["context_limit"]):
            return
        try:
            api_gate.key_update(k["id"], token_limit=q, context_limit=c)
        except Exception as e:                       # noqa: BLE001
            self.log(f"实时保存失败：{e}", "err")
            return
        self.refresh_gate_status(full=True)          # 列表额度列同步（保持选中行）
        self.log(f"已实时保存「{k['name']}」：token 上限 {kfmt(q)}，"
                 f"单次上下文 {kfmt(c) if c else '不限'}", "ok")

    def on_key_save(self):
        """选中了就是改它；没选中就用下面的参数新建一个（并追加到列表）。"""
        p = self._key_params()
        k = self._sel_key()
        if k is None:
            name = p.pop("name") or f"Key {len(api_gate.keys()) + 1}"
            new = api_gate.key_add(name, **p)
            self._clip_copy(new["secret"], "新 Key")
            self._show_key(new)
            self.log(f"已按填写的参数新增 Key「{new['name']}」：额度 {p['token_limit'] or '不限'}，"
                     f"上下文 {p['context_limit'] or '不限'}，QPS {p['qps'] or '不限'}，"
                     f"时长 {p['max_seconds'] or '不限'} 秒", "ok")
            return
        nk = api_gate.key_update(
            k["id"],
            name=(p["name"] or k["name"]),
            token_limit=p["token_limit"],
            context_limit=p["context_limit"],
            qps=p["qps"],
            max_seconds=p["max_seconds"])
        self.refresh_gate_status(full=True)
        self._show_key(nk or k)
        self.log(f"「{p['name'] or k['name']}」配置已保存：额度 {p['token_limit'] or '不限'}，"
                 f"上下文 {p['context_limit'] or '不限'}，QPS {p['qps'] or '不限'}，"
                 f"时长 {p['max_seconds'] or '不限'} 秒", "ok")

    # ---- 状态刷新
    def refresh_gate_status(self, full: bool = False):
        try:
            info = api_gate.public_info()
            mode = api_gate.load()["mode"]
            live = api_gate.running()
            if live:
                addr = api_gate.public_base()
                gate_warn = ""
                # 模型本体没启动时，所有请求都会 502——提前把原因写在状态行上
                model_warn = ""
                if getattr(self, "_last_health", None) is None and getattr(self, "_busy_label", "") == "":
                    model_warn = "｜模型没启动：回「控制台」页点「启动模型」，否则 API 一律返回 502"
                if mode == "public":
                    if info.get("tunnel") and addr:
                        self._set_addr(addr, "ok")
                        if info.get("tunnel_mode") == "token":
                            gate_warn = "（Cloudflare 隧道·你的账号：地址固定不变，隧道断线会自动重连）"
                        else:
                            gate_warn = "（隧道模式：流量经第三方中继 Cloudflare，已自动打通；隧道断线会自动重连换地址）"
                            if not info.get("cf_configured"):
                                # 默认公网链路（UPnP/端口转发）没通才落到自动隧道：提醒配自己的凭证
                                gate_warn += ("｜当前默认公网链路未连通，已切换至 Cloudflare 隧道模式，"
                                              "请点击上方按钮配置你自己的 Cloudflare Zero Trust 账号凭证"
                                              "以启用对外服务")
                    elif info["active"] and addr:
                        self._set_addr(addr, "ok")
                        if info["mapped"]:
                            gate_warn = ""
                        elif info.get("reachable"):
                            # 路由器 UPnP 没开，但端口已经转发通了（手动转发/直通）
                            gate_warn = "（路由器没开 UPnP，端口已转发，外网可达）"
                        else:
                            api_gate.reachability_async()   # 后台探测，60 秒冷却
                            why = info.get("error") or "路由器端口映射没成功"
                            gate_warn = f"（{why}，外网暂时访问不到）"
                            if not getattr(self, "_gate_hint_logged", False):
                                self._gate_hint_logged = True
                                lan_test = (f"http://{info['local_ip']}:"
                                            f"{self.spin_gate_port.GetValue()}/{api_gate.suffix()}/v1")
                                v6 = api_gate.global_ipv6()
                                v6msg = (f"；本机有公网 IPv6（{v6}），外网也可直接用 {api_gate.v6_base()} 访问"
                                         "（对方网络同样要支持 IPv6）") if v6 else \
                                        "；你的网络还没有 IPv6，路由器管理页把「IPv6」开关打开即可免端口转发直连"
                                self.log(f"路由器端口没通（{why}）。"
                                         + (info.get("hint") or api_gate._forward_hint(
                                             self.spin_gate_port.GetValue(), info["local_ip"]))
                                         + f"。本机或同一 WiFi 的设备现在就能连：{lan_test}"
                                           "（请求带 Authorization: Bearer <你的API Key>）"
                                         + v6msg, "warn")
                    else:
                        # 公网地址没生成也别空着：回落到局域网地址，内网照样能用
                        try:
                            lan = api_gate.public_base(self.spin_gate_port.GetValue(), "lan")
                        except Exception:         # noqa: BLE001
                            lan = api_gate.public_base(None, "lan")
                        self._set_addr(lan, "muted")
                        why = info["error"] or "没拿到公网 IP（宽带可能是运营商大内网）"
                        gate_warn = f"｜公网地址没生成：{why}。地址框里是局域网地址，内网可先用"
                else:
                    self._set_addr(addr, "ok")
                    gate_warn = ""
                st = api_gate.stats
                last = time.strftime("%H:%M:%S", time.localtime(st["last_at"])) if st["last_at"] else "–"
                mapinfo = ""
                if mode == "public":
                    if info["tunnel"]:
                        mapinfo = ("，你的 Cloudflare 隧道（地址固定）"
                                   if info.get("tunnel_mode") == "token"
                                   else "，隧道模式（已自动打通）")
                    elif info["mapped"]:
                        mapinfo = "，路由器映射已生效"
                    elif info.get("reachable"):
                        mapinfo = "，外网端口已通"
                    else:
                        mapinfo = "，路由器映射未生效"
                if mode != "public" or info["mapped"] or info.get("reachable"):
                    self._gate_hint_logged = False    # 恢复后允许下次再提示
                tag = "warn" if (gate_warn or model_warn) else "ok"
                set_label(self.lbl_gate,
                          f"运行中（{api_gate.MODES.get(mode, mode)}）{mapinfo}　|　"
                          f"{st['requests']} 次请求，拒绝 {st['denied']}，限速 {st['limited']}，"
                          f"最近 {last}{gate_warn}{model_warn}", tag)
            else:
                if mode != "public":
                    # 本地/局域网地址是固定的：服务没开也先给出来，切到开就能用
                    self._set_addr(api_gate.public_base(), "muted")
                else:
                    try:
                        lan = api_gate.public_base(self.spin_gate_port.GetValue(), "lan")
                    except Exception:             # noqa: BLE001
                        lan = api_gate.public_base(None, "lan")
                    self._set_addr(lan, "muted")
                set_label(self.lbl_gate, "对外服务已关闭（拨动开关即可启动）", "muted")
            self._set_running_look(live)
            if not self.busy:                  # 忙的时候别和用户正在点的开关打架
                self.tg_gate.SetValue(live)
                if full:
                    enabled = api_gate.load()["enabled"]
                    self.cb_gate.SetValue(enabled)
                    self.sync_gate_controls()
                    self.tg_gate.SetValue(live if enabled else False)
            try:
                self.btn_gate_regen.Enable(mode == "public")
            except Exception:
                pass
            if full or not self.lst_keys.GetItemCount():
                self.refresh_key_list()
        except Exception:                    # noqa: BLE001
            pass

    def refresh_key_list(self, select_id: str = ""):
        try:
            ks = api_gate.keys()
            keep = select_id or ""
            if not keep:
                cur = self._sel_key()
                keep = cur["id"] if cur else ""
            sel = self.lst_keys.GetFirstSelected()
            self.lst_keys.Freeze()
            self.lst_keys.DeleteAllItems()
            row = -1
            for i, k in enumerate(ks):
                quota = f"{kfmt(k['used_tokens'])} / {kfmt(k['token_limit']) if k['token_limit'] else '不限'}"
                ips = [x for x in (k.get("ips") or []) if x] or ([k["last_ip"]] if k.get("last_ip") else [])
                ip_txt = "–"
                if ips:
                    ip_txt = ips[0] + (f" 等{len(ips)}个" if len(ips) > 1 else "")
                    if api_gate.is_blocked(ips[0]):
                        ip_txt += "（已拉黑）"
                self.lst_keys.InsertItem(i, k["name"])
                self.lst_keys.SetItem(i, 1, k.get("secret_hint") or (k["secret"][:14] + "…"))
                self.lst_keys.SetItem(i, 2, ip_txt)
                self.lst_keys.SetItem(i, 3, quota)
                self.lst_keys.SetItem(i, 4, f"{k['context_limit'] or '不限'}")
                self.lst_keys.SetItem(i, 5, f"{k['qps']:g}" if k["qps"] else "不限")
                self.lst_keys.SetItem(i, 6, f"{k['max_seconds'] or '不限'}")
                self.lst_keys.SetItem(i, 7, str(k["limited"] or 0))
                self.lst_keys.SetItem(i, 8, "启用" if k["enabled"] else "停用")
                if keep and k["id"] == keep:
                    row = i
            if row < 0 and 0 <= sel < len(ks):
                row = sel
            if row >= 0:
                self.lst_keys.Select(row)
                self.lst_keys.EnsureVisible(row)
            self.lst_keys.Thaw()
        except Exception:                    # noqa: BLE001
            try:
                self.lst_keys.Thaw()
            except Exception:
                pass

    # ================================================================ 日志 / 状态
    def log(self, text: str, tag: str = ""):
        stamp = time.strftime("%H:%M:%S")
        prefix = {"ok": "[OK] ", "err": "[错误] ", "info": "[..] ", "warn": "[!] "}.get(tag, "")
        self.log_box.AppendText(f"{stamp}  {prefix}{text}\n")
        self.log_box.ShowPosition(self.log_box.GetLastPosition())
        n = self.log_box.GetNumberOfLines()
        if n > 400:                              # 不让它无限长，否则拖动/滚动都会变钝
            try:
                self.log_box.Remove(0, self.log_box.XYToPosition(0, n - 400))
            except Exception:
                pass

    def set_status(self, text: str):
        self.SetStatusText(text)

    def on_tab(self):
        self._tab = self.tabs.GetSelection()
        if self._tab == 1 and self._last_metrics:
            self.render_dashboard(self._last_metrics)
        if self._tab == 2:
            self.refresh_model_check()
        self.refresh_now()

    def refresh_now(self):
        try:
            self.poller.tick = 0               # 归零：这次会把 vision / 配置一起重新读
            self.poller.wake.set()
        except Exception:
            pass

    def sync_actions(self, running: bool, loaded: bool, vision) -> None:
        """按钮的可用性和高亮都跟着服务真实状态走：
        服务一停，「关闭模型」立刻变回普通按钮，不会再红着骗人；
        没在跑的时候「启动模型」自己亮成主按钮。"""
        busy = bool(self.busy)
        plan = (
            (self.btn_start, (not running) and not busy, "primary" if not running else "normal"),
            (self.btn_stop, running and not busy, "danger" if running else "normal"),
            (self.btn_unload, bool(loaded) and not busy, "normal"),
            (self.btn_vstart, running and not vision and not busy, "normal"),
            (self.btn_vstop, bool(vision) and not busy, "normal"),
            (self.btn_ctx_apply, not busy, None),        # 保持原来的主按钮样式
        )
        for b, on, kind in plan:
            try:
                if bool(b.enabled) != bool(on):
                    b.Enable(on)
                if kind is not None and b.kind != kind:
                    b.SetKind(kind)
            except Exception:
                pass

    def _log_model_changes(self, h, m):
        """状态一变就写日志 + 30 秒心跳，让「日志」跟上大模型的所有动态。"""
        mon = self._mon
        running = h is not None
        loaded = bool(h and h.get("loaded"))
        model = (h or {}).get("model") or ""

        if mon.get("running") != running:
            self.log(f"服务{'已启动（' + ctl.current_base() + '）' if running else '已停止'}",
                     "ok" if running else "err")
            mon["running"] = running
            mon.pop("loaded", None)
            mon.pop("state", None)
            mon["warned_hot"] = False
            mon["hb"] = 0.0
        if running and mon.get("loaded") != loaded:
            if loaded:
                self.log("模型已加载" + (f"：{model}" if model else "") + "（占用显存）", "ok")
            else:
                self.log("模型已卸载（显存已释放）", "warn")
            mon["loaded"] = loaded
            mon.pop("state", None)
            mon["hb"] = 0.0
        if not (running and loaded):
            return

        live = (m or {}).get("live") or {}
        state = live.get("state") or "idle"
        if mon.get("state") != state:
            if state == "generating":
                sp = live.get("tok_s")
                self.log("大模型：开始生成回复" +
                         (f"（{sp:.1f} tok/s）" if isinstance(sp, (int, float)) and sp else ""),
                         "info")
            elif state == "reading":
                self.log("大模型：正在读入提示词", "info")
            elif state == "queued":
                self.log(f"大模型：请求排队（{live.get('queued') or 1} 个）", "info")
            elif state == "idle" and mon.get("state") in ("generating", "reading", "queued"):
                r0 = ((m or {}).get("requests") or [{}])[0] or {}
                det = []
                if r0.get("prompt_tokens"):
                    det.append(f"输入 {r0['prompt_tokens']} tok")
                if r0.get("output_tokens"):
                    det.append(f"输出 {r0['output_tokens']} tok")
                if r0.get("decode_tok_s"):
                    det.append(f"{r0['decode_tok_s']:.1f} tok/s")
                self.log("大模型：生成结束" + ("（" + "，".join(det) + "）" if det else ""), "ok")
            mon["state"] = state

        hw = (m or {}).get("hardware") or {}
        temp = hw.get("gpu_temp")
        try:
            temp = float(temp) if temp else 0.0
        except (TypeError, ValueError):
            temp = 0.0
        if temp >= 85 and not mon.get("warned_hot"):
            self.log(f"GPU 温度偏高：{temp:.0f} °C", "warn")
            mon["warned_hot"] = True
        elif temp and temp < 80:
            mon["warned_hot"] = False

        now = time.time()
        if now - mon.get("hb", 0.0) >= 30.0:      # 心跳：把关键状态定期报一遍
            mon["hb"] = now
            tot = (m or {}).get("totals") or {}
            nreq = tot.get("requests", tot.get("n", tot.get("total")))
            eng = (m or {}).get("engine") or {}
            self.log("心跳：模型 {} · 显存 {:.1f} G · GPU {:.0f}°C · GPU 负载 {:.0f}% · 速度 {} tok/s · 上下文 {}"
                     .format(model or "–",
                             gb(hw.get("gpu_mem_used")) if hw.get("gpu_mem_used") else 0.0,
                             temp, hw.get("gpu_util") or 0,
                             fmt(live.get("tok_s"), 1) if state == "generating" else "0.0",
                             fmt(eng.get("max_context") or eng.get("context") or "–")),
                     "info")
            if nreq is not None:
                self.log(f"心跳：累计请求 {nreq} 次", "info")

    def on_snapshot(self, snap: dict):
        """只把后台线程算好的结果画到界面上——这里不做任何 I/O。"""
        h = snap.get("health")
        m = snap.get("metrics")
        self._last_health = h
        if "vision" in snap:
            self._last_vision = snap["vision"]
        if "idle" in snap:
            self._last_idle = snap["idle"]
        if "ctx_cfg" in snap:
            self._last_ctx_cfg = snap["ctx_cfg"]
        vision = self._last_vision or []
        running = h is not None
        loaded = bool(h and h.get("loaded"))
        self._log_model_changes(h, m)

        base = ctl.current_base()
        model = snap.get("model") or (h or {}).get("model") or ""
        busy_start = self.busy and str(getattr(self, "_busy_label", "")).startswith("启动模型")
        if running:
            set_label(self.lbl_service, f"运行中（{base}）", "ok")
        elif busy_start:
            set_label(self.lbl_service, f"正在启动（拉起进程，端口 {ctl.PORT}）", "warn")
        else:
            set_label(self.lbl_service, "已停止", "bad")
        if loaded:
            set_label(self.lbl_model, f"已加载{f'：{model}' if model else ''}（占用显存）", "ok")
        elif running:
            set_label(self.lbl_model, "未加载（空闲卸载 / 正在加载）", "warn")
        else:
            set_label(self.lbl_model, "未加载", "muted")
        if vision:
            set_label(self.lbl_vision, f"运行中（PID {', '.join(map(str, vision))}）", "ok")
        else:
            set_label(self.lbl_vision, "未运行" + ("" if running else "（服务未启动）"), "muted")
        idle = self._last_idle
        if idle is None:
            set_label(self.lbl_idle, "—")
        elif idle <= 0:
            set_label(self.lbl_idle, "已关闭（永不动卸载）", "ok")
        else:
            set_label(self.lbl_idle, f"开启：空闲 {idle:g} 秒后卸载", "warn")

        if self._tab == 2:
            if running:
                set_label(self.lbl_conn, f"{base}　已连上　模型：{model or '（读不到名字）'}", "ok")
            else:
                set_label(self.lbl_conn, f"{base}　连不上（服务没在跑）", "bad")

        self.sync_actions(running, loaded, vision)

        if self.float_win:
            try:
                self.float_win.update(snap)
            except Exception:
                pass

        if m is None:
            return
        self._last_metrics = m
        self._maybe_auto_clear_ctx(m)        # 上下文用满自动清理（开关键在设置页）
        if self._tab == 1:                       # 只在仪表盘可见时刷新，省掉后台重绘
            self.render_dashboard(m)
        if self._tab == 3:
            eng = m.get("engine") or {}
            set_label(self.lbl_ctx_now, f"当前生效：{fmt(eng.get('max_context'))} tokens")
        nat = (m.get("engine") or {}).get("context") or (m.get("engine") or {}).get("max_context")
        if nat and nat != self._ctx_native:
            self._ctx_native = int(nat)
            self.ctx_slider.SetMax(min(int(nat), 262144))
            set_label(self.lbl_ctx_native, f"模型上限：{fmt(self._ctx_native)} tokens")
        if self._last_ctx_cfg and self._ctx_cfg != self._last_ctx_cfg:
            self._ctx_cfg = self._last_ctx_cfg
            self.set_ctx_slider(self._ctx_cfg)
            set_label(self.lbl_ctx_cfg, f"配置里：{fmt(self._ctx_cfg)} tokens（重启后生效）")

    # ---------------------------------------------------------------- 仪表盘渲染
    def render_dashboard(self, m: dict):
        live = m.get("live") or {}
        hw = m.get("hardware") or {}
        st = m.get("hardware_static") or {}
        eng = m.get("engine") or {}
        hist = m.get("history") or {}
        reqs = m.get("requests") or []
        tot = m.get("totals") or {}

        state = live.get("state") or "idle"
        label, detail, pct = "等待请求", "", 0
        if live.get("queued"):
            label = f"排队中（{live['queued']}）"
        if state == "reading":
            label = "读取提示词"
            if live.get("prompt_total"):
                pct = 100 * (live.get("prompt_read") or 0) / live["prompt_total"]
                detail = f"{fmt(live.get('prompt_read'))} / {fmt(live.get('prompt_total'))} tokens · {pct:.0f}%"
            else:
                detail = f"{fmt(live.get('prompt_tokens'))} tokens"
        elif state == "generating":
            label = "生成中"
            pct = min(100, 100 * (live.get("generated") or 0) / live["max_tokens"]) if live.get("max_tokens") else 0
            detail = f"{fmt(live.get('generated'))} tokens · {fmt(live.get('tok_s'), 1)} t/s"
        elif reqs:
            r = reqs[0]
            detail = f"上次：{fmt(r.get('output_tokens'))} tokens" + (
                f" · {fmt(r.get('decode_tok_s'), 1)} t/s" if r.get("decode_tok_s") else "")
        set_label(self.lbl_state, label, {"reading": "warn", "generating": "ok"}.get(state, "muted"))
        set_label(self.lbl_state_sub, detail, "muted")
        self.g_state.SetValue(int(max(0, min(100, pct))))

        last = reqs[0] if reqs else None
        speed = live.get("tok_s") if state == "generating" else (last or {}).get("decode_tok_s")
        prefill = live.get("prefill_tok_s_mean") if state != "idle" else None
        sub_speed = "解码（正在生成）" if state == "generating" else "解码（上次请求）"
        if prefill is not None:
            sub_speed += f" · 预填充 {fmt(prefill)} t/s"
        self.cards["speed"].set(fmt(speed, 1) + " t/s", sub_speed, hist.get("tok_s"))
        self.cards["gpu"].set(f"{fmt(hw.get('gpu_util'))} %", st.get("gpu_name") or "",
                              hist.get("gpu_util"), 100)
        self.cards["vram"].set(f"{gb(hw.get('gpu_mem_used')):.1f} GB",
                               f"/ {gb(hw.get('gpu_mem_total')):.1f} GB"
                               + (f" · {fmt(eng.get('expert_slots'))} 专家缓存" if eng.get("expert_slots") else ""),
                               hist.get("gpu_mem_used"), hw.get("gpu_mem_total"))
        self.cards["temp"].set(f"{fmt(hw.get('gpu_temp'))} °C", "", hist.get("gpu_temp"), 90)
        self.cards["power"].set(f"{fmt(hw.get('gpu_power'))} W",
                                f"上限 {fmt(hw.get('gpu_power_limit'))} W" if hw.get("gpu_power_limit") else "",
                                hist.get("gpu_power"), hw.get("gpu_power_limit"))
        gen = hw.get("gpu_pcie_gen_max") or hw.get("gpu_pcie_gen")
        self.cards["pcie"].set(f"Gen{gen}" if gen else "–",
                               f"x{hw.get('gpu_pcie_width')}" if hw.get("gpu_pcie_width") else "",
                               hist.get("gpu_pcie_rx_mb"))
        self.cards["cpu"].set(f"{fmt(hw.get('cpu'))} %",
                              (f"{st.get('cores')} 核 · {st.get('threads')} 线程" if st.get("threads") else ""),
                              hist.get("cpu"), 100)
        dr = hw.get("disk_read_mb")
        if dr is None:
            self.cards["disk"].set("–", "需要 psutil", None)
        else:
            self.cards["disk"].set(f"{dr / 1024:.2f} GB/s" if dr >= 1000 else f"{dr:.1f} MB/s",
                                   f"写 {fmt(hw.get('disk_write_mb'), 1)} MB/s", hist.get("disk_read_mb"))

        ctx = eng.get("max_context") or 0
        used = 0
        if state != "idle":
            used = (live.get("prompt_tokens") or 0) + (live.get("generated") or 0)
        elif last:
            used = (last.get("prompt_tokens") or 0) + (last.get("output_tokens") or 0)
        self.set_bar("ctx", (100.0 * used / ctx) if ctx else 0,
                     f"{kfmt(used)}/{kfmt(ctx)}（{100.0 * used / ctx:.0f}%）" if ctx else "–")
        cache_b = (eng.get("expert_cache_mib") or 0) * 2 ** 20
        self.set_bar("slots", (100.0 * cache_b / hw["gpu_mem_total"]) if hw.get("gpu_mem_total") else 0,
                     f"{fmt(eng.get('expert_slots'))} / {gb(cache_b):.1f} GB" if eng.get("expert_slots") else "–")
        self.set_bar("ram", (100.0 * hw["ram_used"] / hw["ram_total"]) if hw.get("ram_total") else 0,
                     f"{gb(hw.get('ram_used')):.1f} / {gb(hw.get('ram_total')):.0f} GB"
                     if hw.get("ram_total") else "–")
        self.set_bar("temp", min(100, hw.get("gpu_temp") or 0), f"{fmt(hw.get('gpu_temp'))} °C")

        kv = {"int8": "8 位", "q4_0": "4 位", "fp16": "16 位"}.get(eng.get("kv"), eng.get("kv"))
        up = (time.time() - tot["since"]) if tot.get("since") else None
        self.set_fact("engine", "引擎", f"v{eng.get('version') or '?'}")
        self.set_fact("model", "模型", eng.get("model") or "–")
        self.set_fact("ctx", "上下文", f"{fmt(eng.get('max_context'))} tokens")
        self.set_fact("kv", "KV 缓存", f"{kv or '–'}"
                      + (f"，显存常驻 {fmt(eng.get('kv_resident'))}" if eng.get("kv_resident") else "，全在显存"))
        self.set_fact("slots", "专家缓存", f"{fmt(eng.get('expert_slots'))} 个 / {gb(cache_b):.1f} GB")
        self.set_fact("uptime", "已运行", f"{up / 3600:.1f} 小时" if up else "–")
        read_t = (tot.get("prompt_tokens") or 0) - (tot.get("reused") or 0)
        self.set_fact("requests", "请求", f"{fmt(tot.get('requests'))} 个 · 读 {fmt(read_t)} / "
                                          f"复用 {fmt(tot.get('reused'))} tokens")
        o_speed = tot["output_tokens"] / (tot["decode_ms"] / 1000) if (
            tot.get("decode_ms") and tot.get("output_tokens")) else None
        self.set_fact("output", "输出", f"{fmt(tot.get('output_tokens'))} tokens"
                      + (f" · 平均 {o_speed:.1f} t/s" if o_speed else ""))

        sig = tuple((r.get("time"), r.get("prompt_tokens"), r.get("output_tokens")) for r in reqs[:8])
        if sig != self._req_sig:
            self._req_sig = sig
            self.req.Freeze()
            self.req.DeleteAllItems()
            for r in reqs[:8]:
                i = self.req.InsertItem(self.req.GetItemCount(),
                                        time.strftime("%H:%M:%S", time.localtime(r.get("time") or 0)))
                self.req.SetItem(i, 1, {"stop": "完成", "length": "到上限", "cancel": "已停止",
                                        "disconnect": "断开", "error": "出错"}.get(r.get("finish"), "–"))
                self.req.SetItem(i, 2, fmt(r.get("prompt_tokens")))
                self.req.SetItem(i, 3, fmt(r.get("reused")))
                self.req.SetItem(i, 4, fmt(r.get("output_tokens")))
                fresh = None
                if r.get("prompt_tokens") is not None:
                    fresh = max(0, r["prompt_tokens"] - (r.get("reused") or 0))
                self.req.SetItem(i, 5, fmt(fresh / (r["prompt_ms"] / 1000)) if fresh and r.get("prompt_ms") else "–")
                self.req.SetItem(i, 6, fmt(r.get("decode_tok_s"), 1))
                self.req.SetItem(i, 7, f"{fmt(r.get('duration_s'), 1)} s")
            self.req.Thaw()

    def set_bar(self, key, pct, text):
        g, v = self.bars[key]
        set_label(v, text, "muted")
        g.SetValue(int(max(0, min(100, pct))))

    def set_fact(self, key, title, value):
        t, v = self.facts[key]
        set_label(t, title, "muted")
        set_label(v, value)

    # ================================================================ 异步动作
    def run_async(self, label: str, fn, done_text: str | None = None, determinate: bool = False):
        if self.busy:
            return
        self.busy = True
        self._busy_label = label
        for b in (self.btn_start, self.btn_stop, self.btn_unload, self.btn_vstart,
                  self.btn_vstop, self.btn_cache, self.btn_idle, self.btn_ctx_apply,
                  self.btn_update, self.btn_check, self.btn_scan, self.btn_find,
                  self.btn_chk2, self.btn_upd2, self.btn_dl, self.btn_wizard,
                  self.btn_gate_apply):
            b.Enable(False)
        self.gauge.Show()
        if determinate:
            self.gauge.SetValue(0)
        else:
            self.gauge.Pulse()
        self.set_status(label + " …")
        self.log(label + " …")
        self.refresh_now()                     # 状态文字立刻跟着改成「正在…」

        def work():
            try:
                res = fn()
                err = None
            except Exception as e:            # noqa: BLE001 - 一律显示给用户
                res, err = None, e
            wx.CallAfter(self._finished, label, res, err, done_text)

        threading.Thread(target=work, daemon=True).start()

    def _finished(self, label, res, err, done_text):
        self.gauge.Stop()
        self.gauge.Hide()
        self.busy = False
        self._busy_label = ""
        if err is not None:
            self.log(f"{label}失败：{err}", "err")
            self.set_status("出错：" + str(err)[:80])
        else:
            text = res if isinstance(res, str) else (done_text or "完成")
            self.log(text, "ok")
            self.set_status("完成")
        self.btn_check.Enable()
        self.btn_chk2.Enable()
        self.btn_scan.Enable()
        self.btn_find.Enable()
        self.btn_gate_apply.Enable()
        self.btn_dl.Enable()
        self.btn_wizard.Enable()
        self.btn_update.Enable(bool(self._upd_info and self._upd_info.get("has_update")))
        self.btn_upd2.Enable(bool(self._upd_info and self._upd_info.get("has_update")))
        # 操作一结束就按缓存的真实状态把按钮回显一遍，不等下一轮轮询
        h = getattr(self, "_last_health", None)
        self.sync_actions(h is not None, bool(h and h.get("loaded")), self._last_vision or [])
        if str(label).startswith("启动模型"):
            self.refresh_model_check()         # 起失败时把端口/文件情况重新报一遍
        wx.CallLater(0, self.refresh_now)

    def set_progress(self, frac: float, text: str):
        self.gauge.SetValue(int(max(0, min(100, frac * 100))))
        if text:
            self.set_status(text)

    # ================================================================ 连接页动作
    def auto_connect(self):
        """启动时：端口上没服务就自己扫一遍，找到 Strata 就接上去。"""
        if self.busy:
            wx.CallLater(2000, self.auto_connect)
            return
        if ctl.health(timeout=1.5) is not None:
            return

        def job():
            try:
                found = ctl.discover_servers()
            except Exception as e:              # noqa: BLE001
                found = []
                self.log(f"扫描失败：{e}", "err")
            wx.CallAfter(self._auto_connect_done, found)
        threading.Thread(target=job, daemon=True).start()

    def _auto_connect_done(self, found):
        if not found:
            self.log("没在本机找到 Strata 服务——8080 可能被别的程序占了。"
                     "去「连接」页点扫描，或用「启动模型」自己起一个。", "err")
            return
        pick = next((f for f in found if f["kind"] == "strata"), None)
        if pick is None:
            names = "、".join(f"{f['port']}（{f['kind']}）" for f in found[:5])
            self.log(f"本机这些端口上有服务，但没有 Strata：{names}。"
                     f"要接别的模型就到「连接」页手动接入。", "err")
            return
        self.set_port(pick["port"],
                      why=f"自动接入 {pick['port']}：{pick.get('model') or 'Strata'}")

    def on_gate_clearctx(self):
        """上下文用满后自动清理的总开关（只在对外 API 开着时生效）。"""
        on = self.cb_gate_clear.IsChecked()
        ctl.save_settings(api_auto_clear_ctx=on)
        self.log("已开启「上下文用满自动清理」：只在对外服务开启时工作，用量 ≥98% 且空闲时"
                 "快速回收引擎并自动重新驻留" if on
                 else "已关闭「上下文用满自动清理」", "ok" if on else "info")

    def _maybe_auto_clear_ctx(self, m):
        """仅当对外 API 网关正在运行时生效：上下文用量 ≥98% 且模型空闲 →
        快速回收引擎并立刻重新加载，模型重新驻留显存、缓存清空。
        对外接口没开时本功能完全不介入（不卸载、不清理、不影响任何使用）。"""
        try:
            if not ctl.load_settings().get("api_auto_clear_ctx"):
                self._ctx_clear_hit = False
                return
        except Exception:
            return
        if not api_gate.running():               # 只在对外服务开着时工作
            return
        if getattr(self, "_ctx_clear_busy", False):
            return                               # 正在回收，别叠加
        eng = (m or {}).get("engine") or {}
        live = (m or {}).get("live") or {}
        last = ((m or {}).get("requests") or [{}])[0] or {}
        ctx = eng.get("max_context") or eng.get("context") or 0
        state = live.get("state") or "idle"
        if state != "idle":
            used = (live.get("prompt_tokens") or 0) + (live.get("generated") or 0)
        else:
            used = (last.get("prompt_tokens") or 0) + (last.get("output_tokens") or 0)
        try:
            ctx, used = float(ctx or 0), float(used or 0)
        except (TypeError, ValueError):
            return
        if not ctx or not used:
            return
        if used / ctx < 0.98:
            self._ctx_clear_hit = False          # 用量降下来了（比如已重载），重新武装
            return
        now = time.time()
        if getattr(self, "_ctx_clear_at", 0.0) and now - self._ctx_clear_at < 60:
            return                               # 冷却 60 秒，避免连环回收
        if state != "idle":
            return                               # 生成中不动作
        self._ctx_clear_at = now
        self._ctx_clear_hit = True
        self._ctx_clear_busy = True
        t0 = time.time()

        def job():
            try:
                try:
                    r1 = ctl.unload_model()
                except Exception as e:           # noqa: BLE001
                    wx.CallAfter(self.log, f"自动清理上下文失败（卸载）：{e}", "err")
                    return
                # 立刻重载：模型重新驻留显存，缓存是干净的，新请求直接用
                try:
                    r2 = ctl.load_model()
                except Exception as e:           # noqa: BLE001
                    wx.CallAfter(self.log,
                                 f"上下文已清空（{r1}），自动重载失败：{e}。"
                                 f"下一个请求会自动加载", "warn")
                    return
                deadline = time.time() + 300
                while time.time() < deadline:
                    h = ctl.health(timeout=3.0)
                    if h and h.get("loaded"):
                        break
                    time.sleep(2)
                took = time.time() - t0
                wx.CallAfter(self.log,
                             f"上下文已用满（{kfmt(int(used))}/{kfmt(int(ctx))}），"
                             f"已自动清理并重新驻留显存（用时 {took:.0f} 秒）", "warn")
            finally:
                self._ctx_clear_busy = False
        threading.Thread(target=job, daemon=True).start()

    def boot_scan_conn(self):
        """启动时静默把连接页两个列表填上，不再一片空白。"""
        def job():
            try:
                found = ctl.discover_servers()
            except Exception:                  # noqa: BLE001
                found = []
            try:
                hits = ctl.find_strata_installs(budget_s=10.0)
            except Exception:                  # noqa: BLE001
                hits = []
            try:
                cur = str(ctl.STRATA_DIR or "")
                if cur and not any(h["dir"].lower() == cur.lower() for h in hits):
                    hits.insert(0, ctl.describe_install(Path(cur)))
            except Exception:                  # noqa: BLE001
                pass
            wx.CallAfter(self.show_ports, found)
            wx.CallAfter(self.show_dirs, hits)
            wx.CallAfter(self.refresh_model_check)
        threading.Thread(target=job, daemon=True).start()

    def refresh_model_check(self):
        """连接页那行「模型体检」：配置 / 模型 / 引擎 / 端口到底什么情况，一眼能看到。"""
        def job():
            try:
                info = ctl.inspect_model()
            except Exception as e:             # noqa: BLE001
                wx.CallAfter(set_label, self.lbl_check, f"模型体检失败：{e}", "bad")
                return
            cp = ctl.config_port()
            state = ctl.port_state(cp)
            kind = {"free": "空闲", "strata": "Strata 已在跑", "other": "被别的程序占了"}[state]
            if not info.get("config"):
                wx.CallAfter(set_label, self.lbl_check,
                             f"模型体检：{ctl.STRATA_DIR} 下没有 strata-*.json 配置", "bad")
                return
            txt = (f"配置 {Path(info['config']).name} · 模型 {info.get('model_name') or '?'}"
                   f" · 引擎 {info.get('engine_ver') or '?'} · 端口 {cp}（{kind}）")
            if info.get("missing"):
                txt += " · 缺文件：" + "、".join(Path(x).name for x in info["missing"][:3])
                wx.CallAfter(set_label, self.lbl_check, txt, "bad")
            elif info.get("ok"):
                wx.CallAfter(set_label, self.lbl_check, txt, "ok" if state != "other" else "warn")
            else:
                wx.CallAfter(set_label, self.lbl_check, txt, "warn")
        threading.Thread(target=job, daemon=True).start()

    def on_scan(self):
        def job():
            n = len(ctl.listening_ports())
            self.log(f"本机有 {n} 个端口在监听，逐个问 /health …")
            found = ctl.discover_servers()
            wx.CallAfter(self.show_ports, found)
            if not found:
                return "没找到任何 HTTP 服务（模型服务没启动？）"
            kinds = {}
            for f in found:
                kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
            return "找到 " + "、".join(f"{v} 个 {k}" for k, v in kinds.items())
        self.run_async("扫描本机接口", job)

    def show_ports(self, found):
        self._found = found or []
        self.lst_ports.Freeze()
        self.lst_ports.DeleteAllItems()
        for f in self._found:
            i = self.lst_ports.InsertItem(self.lst_ports.GetItemCount(), str(f["port"]))
            self.lst_ports.SetItem(i, 1, {"strata": "Strata 大模型", "llamacpp": "llama.cpp",
                                          "ollama": "Ollama", "openai": "OpenAI 兼容",
                                          "http": "其它 HTTP"}.get(f["kind"], f["kind"]))
            self.lst_ports.SetItem(i, 2, f.get("model") or "–")
            self.lst_ports.SetItem(i, 3, fmt(f.get("ctx")))
            self.lst_ports.SetItem(i, 4, "已加载" if f.get("loaded") else
                                   ("未加载" if f.get("loaded") is False else "–"))
            if f["kind"] == "strata":
                self.lst_ports.SetItemTextColour(i, C("ok"))
        self.lst_ports.Thaw()
        if self.lst_ports.GetItemCount():
            self.lst_ports.Select(0)

    def on_use_port(self):
        idx = self.lst_ports.GetFirstSelected()
        if idx < 0 or not self._found:
            wx.MessageBox("先在列表里选一个（点「扫描本机所有接口」刷新）。", "接入",
                          wx.OK | wx.ICON_INFORMATION)
            return
        self.set_port(self._found[idx]["port"])

    def set_port(self, port: int, why: str = ""):
        try:
            port = int(str(port).strip())
        except (TypeError, ValueError):
            self.log(f"「{port}」不是端口数字", "err")
            return
        ctl.set_port(port)
        ctl.save_settings(port=port)
        try:
            self.spin_port.SetValue(int(port))
        except Exception:
            self.spin_port.SetValue(str(port))
        h = ctl.health(timeout=2.0)
        msg = (why or f"已接入 {ctl.current_base()}") + "　" + \
              (f"连上了：{(h or {}).get('model', '')}" if h else "这个端口上没响应")
        self.log(msg, "ok" if h else "err")
        set_label(self.lbl_conn, f"{ctl.current_base()}　{'已连上' if h else '连不上'}")
        self.refresh_now()

    def on_find_installs(self):
        def job():
            self._scan_log_t = 0.0

            def prog(_n, path):
                now = time.time()
                if now - self._scan_log_t > 2.0:
                    self._scan_log_t = now
                    self.log(f"  正在看：{path}")
            hits = ctl.find_strata_installs(progress=prog)
            wx.CallAfter(self.show_dirs, hits)
            if not hits:
                return "没在别的盘上找到 Strata 目录"
            return "找到 " + "、".join(h["dir"] for h in hits[:3])
        self.run_async("在所有盘里查找 Strata", job)

    def show_dirs(self, hits):
        self._dirs = hits or []
        self.lst_dirs.Freeze()
        self.lst_dirs.DeleteAllItems()
        for h in self._dirs:
            i = self.lst_dirs.InsertItem(self.lst_dirs.GetItemCount(), h["dir"])
            self.lst_dirs.SetItem(i, 1, h["engine"])
            self.lst_dirs.SetItem(i, 2, "有" if h["server"] else "无")
            self.lst_dirs.SetItem(i, 3, ", ".join(h["configs"]) or "–")
        self.lst_dirs.Thaw()
        if self.lst_dirs.GetItemCount():
            self.lst_dirs.Select(0)

    def on_use_dir(self):
        idx = self.lst_dirs.GetFirstSelected()
        if idx < 0 or not self._dirs:
            wx.MessageBox("先在列表里选一个目录（点「在所有盘里查找」刷新）。", "Strata 目录",
                          wx.OK | wx.ICON_INFORMATION)
            return
        d = self._dirs[idx]["dir"]
        if not ctl.set_strata_dir(d):
            self.log(f"{d} 看着不像 Strata 目录（没有 serve/server.py，也没有 engine/）", "err")
            return
        ctl.save_settings(strata_dir=d)
        set_label(self.lbl_dir, str(ctl.STRATA_DIR))
        self.log(f"已切换到 {ctl.STRATA_DIR}（配置："
                 f"{ctl.CONFIG.name if ctl.CONFIG else '无'}）", "ok")
        paths = ctl.config_model_paths()
        if paths:
            drives = sorted({str(v)[:2] for v in paths.values()})
            set_label(self.lbl_models, "模型文件在：" + "、".join(drives) + "（"
                      + (paths.get("--native") or paths.get("--pack") or "") + "）", "muted")
        else:
            set_label(self.lbl_models, "这个目录里没读到模型路径", "muted")
        self.refresh_model_check()
        self.refresh_now()

    # ================================================================ 引擎与模型下载
    def refresh_models(self):
        self._model_rows = []
        self.lst_models.DeleteAllItems()
        try:
            ver = ctl.engine_version()
            set_label(self.lbl_engine, f"引擎 v{ver or '?'}　·　点「检查引擎更新」看有没有新版")
        except Exception:
            set_label(self.lbl_engine, "引擎：未知版本")
        inst = installed_model_key()
        ram = total_ram_gb()
        try:
            for m in parse_models():
                if not m["ok_family"]:
                    continue
                self._model_rows.append(m)
                if m["key"].upper() == inst and inst:
                    status, col = "已安装", C("ok")
                elif m["ram_gb"] and m["ram_gb"] > ram:
                    status, col = "内存不够", C("muted")
                elif m["key"] == "IQ3_S":
                    status, col = "推荐", C("accent")
                else:
                    status, col = "可装", C("fg")
                i = self.lst_models.InsertItem(self.lst_models.GetItemCount(), m["key"])
                self.lst_models.SetItem(i, 1, m["about"])
                self.lst_models.SetItem(i, 2, f"{m['download_gb']:.0f} GB" if m["download_gb"] else "–")
                self.lst_models.SetItem(i, 3, f"{m['ram_gb']:.0f} GB" if m["ram_gb"] else "–")
                self.lst_models.SetItem(i, 4, status)
                self.lst_models.SetItemTextColour(i, col)
            if self.lst_models.GetItemCount():
                self.lst_models.Select(0)
        except Exception as e:                     # noqa: BLE001
            self.log(f"读模型清单失败：{e}", "err")

    def on_download_model(self):
        idx = self.lst_models.GetFirstSelected()
        if idx < 0 or idx >= len(self._model_rows):
            wx.MessageBox("先在列表里选一个型号。", "下载模型", wx.OK | wx.ICON_INFORMATION)
            return
        m = self._model_rows[idx]
        ram = total_ram_gb()
        if m["ram_gb"] and m["ram_gb"] > ram:
            if wx.MessageBox(f"{m['key']} 建议 {m['ram_gb']:.0f} GB 内存，本机 {ram:.0f} GB，"
                             f"装上大概率跑不动。仍要打开下载向导吗？",
                             "下载模型", wx.YES_NO | wx.ICON_WARNING) != wx.YES:
                return
        try:
            msg = launch_model_wizard(m["key"])
            self.log(msg, "ok")
            self.set_status(msg)
        except Exception as e:                     # noqa: BLE001
            self.log(f"打不开下载向导：{e}", "err")

    def on_model_wizard(self):
        try:
            msg = launch_model_wizard()
            self.log(msg, "ok")
            self.set_status(msg)
        except Exception as e:                     # noqa: BLE001
            self.log(f"打不开安装向导：{e}", "err")

    # ================================================================ 外观
    def on_theme(self):
        name = ("light", "dark", "custom")[self.rb_theme.GetSelection()]
        if name == "custom":
            if THEME.name != "custom":          # 刚切到自定义：从当前这套颜色起步
                THEME.c = dict(THEME.c)
                THEME.name = "custom"
                self.sync_custom()
            else:
                self.sync_custom()
            THEME.save()
            self.after_theme()
            return
        THEME.set(name)
        THEME.save()
        self._fg_picked = False
        self.after_theme()

    def sync_custom(self):
        """自定义模式下：按背景明暗把卡片 / 边框 / 次要文字一起算出来。"""
        bg = K("bg")
        dark_bg = lum(bg) < 128
        fg = K("fg") if self._fg_picked else ((236, 241, 248) if dark_bg else (24, 27, 33))
        card = mixt(bg, fg, 0.085)
        THEME.c.update({
            "fg": fg, "widget_fg": fg,
            "card": card, "panel": card,
            "widget": mixt(bg, fg, 0.15),
            "border": mixt(bg, fg, 0.22),
            "track": mixt(bg, fg, 0.16),
            "muted": mixt(bg, fg, 0.60),
        })
        THEME.name = "custom"

    def on_pick_colour(self, key: str):
        dlg = wx.ColourDialog(self)
        dlg.GetColourData().SetColour(C(key))
        try:
            ok = dlg.ShowModal() == wx.ID_OK
            col = dlg.GetColourData().GetColour()
        finally:
            dlg.Destroy()
        if not ok:
            return
        rgb = (col.Red(), col.Green(), col.Blue())
        if THEME.name != "custom":
            THEME.c = dict(THEME.c)
        if key == "fg":
            self._fg_picked = True
        THEME.c[key] = rgb
        if key == "bg":
            THEME.c["panel"] = rgb
            self.after_theme()
        self.sync_custom()
        if key == "bg":
            pass
        self.rb_theme.SetSelection(2)
        THEME.save()
        self.after_theme()

    def after_theme(self):
        apply_theme(self)
        set_alpha(self, THEME.alpha)
        if self.float_win:
            self.float_win.retheme()
        for w in self.swatches.values():
            w.Refresh()
        self.tabs.bar.Refresh()
        self.refresh_now()

    def on_alpha(self):
        THEME.alpha = self.sl_alpha.GetValue()
        set_label(self.lbl_alpha, f"{int(THEME.alpha * 100 / 255)}%", "muted")
        set_alpha(self, THEME.alpha)
        if self.float_win:
            set_alpha(self.float_win, THEME.alpha)
        THEME.save()

    def on_float_toggle(self):
        on = self.cb_float.IsChecked()
        ctl.save_settings(float_on=on)
        if on:
            if not self.float_win:
                f = FloatFrame(None)          # 不挂为子窗口：主窗口怎么关都不影响悬浮窗
                f.main_ref = self
                f.Show()
                f.retheme()
                self.float_win = f
                if self._last_metrics:
                    self.float_win.update({"health": None, "metrics": self._last_metrics})
                self.refresh_now()
        elif self.float_win:
            self.float_win.Destroy()
            self.float_win = None

    def on_float_closed(self):
        self.float_win = None
        try:
            self.cb_float.SetValue(False)
        except Exception:
            pass
        ctl.save_settings(float_on=False)

    def on_autostart(self):
        try:
            mode = "float" if self.rb_start.GetSelection() == 1 else "main"
            ctl.save_settings(autostart_mode=mode)
            msg = ctl.set_autostart(self.cb_auto.IsChecked(),
                                    "--float" if mode == "float" else "")
            self.log(msg, "ok")
        except Exception as e:                 # noqa: BLE001
            self.log(f"改自启动失败：{e}", "err")
        self.refresh_autostart_label()

    def refresh_autostart_label(self):
        v = ctl.autostart_value()
        set_label(self.lbl_auto, ("当前注册表里的启动命令：" + v) if v else "现在没有开机自启。",
                  "muted")
        try:
            self.lbl_auto.Wrap(700)
        except Exception:
            pass

    # ================================================================ 上下文
    def set_ctx_slider(self, n: int):
        lo = self.ctx_slider.GetMin()
        hi = self.ctx_slider.GetMax()
        self.ctx_slider.SetValue(int(max(lo, min(hi, n))))
        self.on_ctx_slide(None)

    def on_ctx_slide(self, _evt):
        n = self.ctx_slider.GetValue()
        set_label(self.lbl_ctx_pick, f"已选：{fmt(n)} tokens（{n / 1024:.0f}K）")
        same = (self._last_ctx_cfg == n)
        set_label(self.lbl_ctx_cfg, f"配置里：{fmt(self._last_ctx_cfg)} tokens（重启后生效）"
                  if self._last_ctx_cfg else "配置里：–",
                  "ok" if same else "warn")

    def on_ctx_apply(self):
        n = self.ctx_slider.GetValue()
        if self._last_ctx_cfg == n:
            wx.MessageBox(f"配置里已经是 {fmt(n)} 了，不用改。", "上下文长度",
                          wx.OK | wx.ICON_INFORMATION)
            return
        if wx.MessageBox(f"把 --max-context 设为 {fmt(n)} tokens？\n\n"
                         f"会写入 {ctl.CONFIG.name}（自动备份），然后重启服务生效。",
                         "上下文长度", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return

        def job():
            changed, msg = ctl.set_max_context(n)
            self.log(msg)
            if not changed:
                return msg
            if not ctl.server_running():
                return msg + "（服务没在运行，下次启动就是新值）"
            self.log("正在重启服务 …")
            ctl.stop_server()
            ctl.start_server(log_sink=lambda line: wx.CallAfter(self.log, line))
            deadline = time.time() + 900
            while time.time() < deadline:
                h = ctl.health(timeout=3.0)
                if h and h.get("loaded"):
                    return f"{msg}；已重启，上下文现在是 {fmt(n)}"
                time.sleep(4)
            return msg + "；服务已重启但没等到加载完成"
        self.run_async(f"设置上下文为 {fmt(n)}", job)

    # ================================================================ 更新
    def auto_update_tick(self):
        if not ctl.load_settings().get("auto_update"):
            return
        if self.busy:
            wx.CallLater(5000, self.auto_update_tick)
            return

        def job():
            try:
                info = ctl.update_check()
            except Exception as e:              # noqa: BLE001
                self.log(f"自动检查更新失败：{e}", "err")
                return
            wx.CallAfter(self.show_update, info)
            if not info.get("has_update"):
                return
            if ctl.server_running():
                self.log(f"有新版本 {info.get('latest')}，但服务正在跑——"
                         f"先关掉服务，或手动点「更新 Strata」。", "warn")
                return
            self.log(f"发现新版本 {info.get('latest')}，服务没在跑，开始自动更新 …", "ok")
            wx.CallAfter(self.do_update, info)
        threading.Thread(target=job, daemon=True).start()

    def check_update(self):
        if self.busy:
            return

        def job():
            info = ctl.update_check()
            wx.CallAfter(self.show_update, info)
            if info.get("error"):
                return f"检查更新失败：{info['error']}"
            if info.get("has_update"):
                bits = []
                if info.get("remote") and info["remote"] != info["local"]:
                    bits.append(f"源码 {info['local']} → {info['remote']}"
                                + (f"（落后 {info['behind']} 个提交）" if info.get("behind") else ""))
                if info.get("latest") and info["latest"] != info["engine"]:
                    bits.append(f"引擎 {info['engine']} → {info['latest']}")
                return "有新版本：" + "；".join(bits)
            return f"已是最新：源码 {info.get('local')}，引擎 {info.get('engine')}"
        self.run_async("检查更新", job)

    def show_update(self, info: dict):
        self._upd_info = info
        has = bool(info.get("has_update"))
        if info.get("error"):
            set_label(self.lbl_upd, f"检查失败：{info['error']}（连不上 GitHub 就等会儿再试）", "bad")
            set_label(self.lbl_engine, f"引擎 v{ctl.engine_version() or '?'}　·　检查失败", "bad")
            self.btn_update.Disable()
            self.btn_upd2.Disable()
            return
        lines = [f"源码：本地 {info.get('local')} · 上游 {info.get('remote')}"
                 + (f" · 落后 {info.get('behind')} 个提交" if info.get("behind") else "")]
        lines.append(f"引擎：已装 {info.get('engine')} · 最新 {info.get('latest')}"
                     + (f"（{str(info.get('published'))[:10]}）" if info.get("published") else ""))
        eng_line = (f"引擎 v{info.get('engine') or '?'}　·　"
                    + (f"有新版 {info.get('latest')}" if has else "已是最新"))
        if has:
            lines.append("有新版本，点「更新 Strata」即可（会先关掉服务）。")
            set_label(self.lbl_upd, "\n".join(lines), "warn")
            set_label(self.lbl_engine, eng_line, "warn")
            self.btn_update.Enable()
            self.btn_upd2.Enable()
        else:
            lines.append("已经是最新了。")
            set_label(self.lbl_upd, "\n".join(lines), "ok")
            set_label(self.lbl_engine, eng_line, "ok")
            self.btn_update.Disable()
            self.btn_upd2.Disable()

    def on_update(self):
        info = self._upd_info or {}
        if not info.get("has_update"):
            wx.MessageBox("没检测到新版本，先点「检查更新」。", "更新 Strata",
                          wx.OK | wx.ICON_INFORMATION)
            return
        if ctl.server_running():
            if wx.MessageBox("更新要替换引擎文件，得先关掉模型服务。\n\n现在关闭并继续更新吗？",
                             "更新 Strata", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
                return
        elif wx.MessageBox(f"把 Strata 更新到 {info.get('latest')}？\n\n"
                           f"源码 {info.get('local')} → {info.get('remote')}，"
                           f"引擎 {info.get('engine')} → {info.get('latest')}。\n"
                           f"更新过程中不能用模型。", "更新 Strata",
                           wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        self.do_update(info)

    def do_update(self, info: dict):
        def job():
            if ctl.server_running():
                self.log("先关闭服务 …")
                ctl.stop_server()
            res = ctl.perform_update(sink=lambda line: wx.CallAfter(self.log, line),
                                     progress=lambda f, t: wx.CallAfter(self.set_progress, f, t))
            try:
                wx.CallAfter(self.show_update, ctl.update_check())
            except Exception:
                pass
            return res
        self.run_async("更新 Strata（要下 165 MB，别关窗口）", job, determinate=True)

    # ================================================================ 模型动作
    def _port_followed(self, old: int, new: int):
        """起模型后把「接入端口」校正回配置端口，并让界面跟着走。"""
        try:
            self.spin_port.SetValue(int(new))
        except Exception:
            pass
        ctl.save_settings(port=int(new))
        self.log(f"接入端口 {old} → {new}（跟着模型配置走，不然连不上自己起的服务）", "warn")
        set_label(self.lbl_conn, f"{ctl.current_base()}　启动中…", "warn")

    def on_start(self):
        # 前置体检：Strata 引擎 / 模型没装好就启动不了，自动跳到「连接」页的下载区
        try:
            info = ctl.inspect_model()
        except Exception:                     # noqa: BLE001
            info = {}
        if not info.get("ok"):
            self.tabs.SetSelection(2)         # 「连接」页：安装目录 + 引擎与模型下载
            if not info.get("config"):
                why = "还没安装 Strata（安装目录里没有任何模型配置）"
            elif not info.get("engine"):
                why = "Strata 引擎（engine/strata.exe）还没下载"
            else:
                why = "模型文件不齐：" + "；".join(
                    Path(x).name for x in (info.get("missing") or [])[:3])
            self.log(f"现在启动不了：{why}。已自动跳到「连接」页——"
                     "在「Strata 引擎 与 模型下载」里选一个适配本机的型号点「下载选中的模型」"
                     "（没有引擎就先点「打开完整安装向导…」），装好再回来点「启动模型」", "warn")
            wx.MessageBox(f"现在启动不了：{why}。\n\n"
                          "已自动跳到「连接」页：\n"
                          "① 没有引擎 → 点「打开完整安装向导…」先把 Strata 装好；\n"
                          "② 在「Strata 引擎 与 模型下载」列表里选一个适配本机的型号，"
                          "点「下载选中的模型」，在弹出的黑色窗口里等下载完成；\n"
                          "③ 装好后回到「控制台」页再点「启动模型」。",
                          "先下载引擎和模型", wx.OK | wx.ICON_INFORMATION)
            return
        auto_web = self.cb_autoweb.IsChecked()

        def job():
            old, now = ctl.ensure_config_port()
            if now != old:
                wx.CallAfter(self._port_followed, old, now)
            info = ctl.inspect_model()
            if info.get("config"):
                wx.CallAfter(self.log,
                             f"体检：配置 {Path(info['config']).name}"
                             f" · 模型 {info.get('model_name') or '?'}"
                             f" · 引擎 {info.get('engine_ver') or '?'}"
                             f" · 端口 {ctl.PORT}")
            if info.get("missing"):
                raise ctl.ApiError("模型文件不齐：" + "；".join(Path(x).name for x in info["missing"][:4]))
            proc = ctl.start_server(log_sink=lambda line: wx.CallAfter(self.log, line))
            deadline = time.time() + 600
            while time.time() < deadline:
                h = ctl.health(timeout=3.0)
                if h and h.get("loaded"):
                    name = ctl.served_model(timeout=3.0) or info.get("model_name") or ""
                    wx.CallAfter(self.refresh_now)
                    if auto_web:
                        wx.CallAfter(self.open_web_page, "启动完成")
                    return f"模型已启动：{name or '（服务已在跑）'}　{ctl.current_base()}"
                if proc is not None and proc.poll() is not None and h is None:
                    return (f"服务进程退出了（代码 {proc.returncode}）——看下面日志最后几行；"
                            f"常见原因：端口 {ctl.PORT} 被占、模型文件缺、显存不够")
                time.sleep(2)
            return "已启动服务，但 10 分钟内没等到加载完成——看日志里最后几行"
        self.run_async("启动模型（加载需要 1-3 分钟）", job)

    def on_stop(self):
        if wx.MessageBox("确定关闭整个模型服务吗？\n（显存会释放，正在处理的请求会中断）",
                         "关闭模型", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        self.run_async("关闭模型", ctl.stop_server)

    def on_unload(self):
        self.run_async("卸载模型", ctl.unload_model)

    def open_web_page(self, why: str = "") -> None:
        url = f"http://localhost:{ctl.PORT}/"
        if not ctl.server_running():
            msg = "模型服务没在运行，先点「启动模型」。" + (f"（{why}）" if why else "")
            self.log(msg, "err")
            wx.MessageBox(msg, "打开网页界面", wx.OK | wx.ICON_INFORMATION)
            return
        try:
            how = ctl.open_url(url)
            self.log(f"已用 {how} 打开 {url}", "ok")
        except Exception as e:                 # noqa: BLE001
            self.log(f"打开失败：{e}", "err")
            wx.MessageBox(f"打不开浏览器：{e}\n\n请手动在浏览器里打开 {url}",
                          "打开网页界面", wx.OK | wx.ICON_WARNING)

    def on_web(self):
        self.open_web_page()

    def on_vision_start(self):
        self.run_async("启动视觉模型", ctl.start_vision)

    def on_vision_stop(self):
        self.run_async("关闭视觉模型", ctl.stop_vision)

    def on_disable_idle(self):
        def job():
            changed, msg = ctl.disable_idle_unload()
            if changed and ctl.server_running():
                wx.CallAfter(self.ask_restart, msg)
            return msg
        self.run_async("取消自动卸载", job)

    def ask_restart(self, msg):
        if wx.MessageBox(msg + "\n\n配置改动要重启服务才生效。现在重启吗？",
                         "取消自动卸载", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            self.restart_service("已重启，自动卸载已关闭")

    def restart_service(self, done: str):
        def job():
            ctl.stop_server()
            ctl.start_server(log_sink=lambda line: wx.CallAfter(self.log, line))
            deadline = time.time() + 900
            while time.time() < deadline:
                h = ctl.health(timeout=3.0)
                if h and h.get("loaded"):
                    return done
                time.sleep(4)
            return "已重启服务，但没等到加载完成"
        self.run_async("重启服务（重新加载要 1-3 分钟）", job)

    def on_cache(self):
        rep = ctl.cache_report()
        msg = (f"可清理内容：\n\n"
               f"• 图片编码缓存：{len(rep['vision_dirs'])} 个临时目录，{human(rep['vision_bytes'])}\n"
               f"• 日志文件：{len(rep['logs'])} 个，{human(rep['log_bytes'])}\n\n"
               f"注意：模型内部的提示词/KV 缓存在显存里，要用「卸载模型」释放。\n\n确定清理吗？")
        if wx.MessageBox(msg, "清理缓存", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return

        def job():
            r = ctl.clear_cache(vision=True, logs=True)
            if not r["notes"]:
                return "没有需要清理的内容"
            return f"清理完成，释放约 {human(r['freed'])}（" + "；".join(r["notes"]) + "）"
        self.run_async("清理缓存", job)

    # ================================================================ 关闭
    def on_close(self, evt=None, force: bool = False):
        # 悬浮窗开着时：关主窗口 = 收进悬浮窗（磁悬），程序继续在后台跑
        if not force and self.float_win is not None:
            self.Hide()
            return
        running = ctl.server_running()
        if running:
            dlg = wx.MessageDialog(
                self,
                "模型服务还在运行。\n\n要一起关掉吗？\n（选「否」= 只关这个窗口，服务继续在后台跑）",
                "退出", wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION)
            dlg.SetYesNoCancelLabels("关掉服务并退出", "只退出窗口", "取消")
            r = dlg.ShowModal()
            dlg.Destroy()
            if r == wx.ID_CANCEL:
                return
            if r == wx.ID_YES:
                threading.Thread(target=ctl.stop_server, daemon=True).start()
                time.sleep(0.5)
        self.shutdown()
        if self.float_win:
            try:
                self.float_win.Destroy()
            except Exception:
                pass
        self.Destroy()

    def shutdown(self):
        try:
            api_gate.stop()
        except Exception:                    # noqa: BLE001
            pass
        for attr in ("gate_timer", "mon_timer"):
            try:
                getattr(self, attr).Stop()
            except Exception:                # noqa: BLE001
                pass
        try:
            self.poller.stop.set()
        except Exception:
            pass
        try:
            self.gauge.Stop()
        except Exception:
            pass


# ------------------------------------------------------------------ 入口

def take_lock() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return False
    sock.listen(1)
    return True


def boot_settings():
    """把上次记住的目录 / 端口 / 主题先恢复。"""
    s = THEME.load()
    d = s.get("strata_dir")
    if d:
        try:
            ctl.set_strata_dir(d)
        except Exception:
            pass
    p = s.get("port")
    if p:
        try:
            ctl.set_port(int(p))
        except Exception:
            pass
    # 上次「接入」的端口没响应、而模型自己配置的端口上确实有服务，就以模型端口为准
    try:
        if ctl.health(timeout=1.0) is None:
            cp = ctl.config_port()
            if cp != ctl.PORT and ctl.health_for(cp, timeout=1.0) is not None:
                ctl.set_port(cp)
    except Exception:
        pass
    return s


def run_float_app():
    """只加载悬浮窗模块：开机自启时用它，内存和 CPU 都更省。"""
    app = wx.App(False)
    app.SetAppName("StrataControl")
    if not take_lock():
        wx.MessageBox("Strata 控制台已经在运行了。", APP_TITLE, wx.OK | wx.ICON_INFORMATION)
        return
    boot_settings()
    f = FloatFrame()
    f.Show()
    if not ctl.load_settings().get("float_on"):
        ctl.save_settings(float_on=True)
    poller = Poller(f.update)
    poller.start()
    f.poller = poller
    app.SetTopWindow(f)
    app.MainLoop()
    os._exit(0)


def main():
    app = wx.App(False)
    app.SetAppName("StrataControl")
    if not take_lock():
        wx.MessageBox("Strata 控制台已经在运行了。", APP_TITLE, wx.OK | wx.ICON_INFORMATION)
        return
    s = boot_settings()
    frame = MainFrame()
    frame.Centre()
    frame.Show()
    if s.get("float_on"):
        def show_float():
            frame.cb_float.SetValue(True)
            frame.on_float_toggle()
        wx.CallLater(600, show_float)
    app.MainLoop()
    os._exit(0)


def selftest() -> int:
    """无界面自检（打包后的 exe 也能跑）：把关键状态写到 selftest.txt。"""
    out = []
    try:
        out.append(f"config = {ctl.CONFIG}")
        out.append(f"python = {ctl.find_python()}")
        out.append(f"server.py exists = {ctl.SERVER_PY.is_file()}")
        out.append(f"health = {ctl.health(timeout=3.0)}")
        out.append(f"metrics ok = {ctl.metrics() is not None}")
        out.append(f"engine pids = {ctl.engine_pids()}")
        out.append(f"vision pids = {ctl.vision_pids()}")
        out.append(f"port pids = {ctl.port_pids()}")
        out.append(f"listening = {ctl.listening_ports()[:20]}")
        out.append(f"discovered = {ctl.discover_servers()[:6]}")
        out.append(f"installs = {[h['dir'] for h in ctl.find_strata_installs(budget_s=12.0)][:5]}")
        out.append(f"idle_unload_s = {ctl.idle_unload_seconds()}")
        out.append(f"max_context = {ctl.max_context_setting()}")
        out.append(f"commit = {ctl.local_commit()} / engine {ctl.engine_version()}")
        out.append(f"update = {ctl.update_check()}")
        out.append(f"settings = {ctl.settings_path()}")
        out.append(f"settings body = {ctl.load_settings()}")
        out.append(f"autostart = {ctl.autostart_enabled()}")
        rep = ctl.cache_report()
        out.append(f"cache = {len(rep['vision_dirs'])} dirs / {rep['vision_bytes']} B, "
                   f"logs {len(rep['logs'])} / {rep['log_bytes']} B")
        out.append(f"wx = {wx.version()}")
        out.append("RESULT = OK")
        rc = 0
    except Exception as e:                    # noqa: BLE001
        out.append(f"RESULT = FAIL: {e}")
        rc = 1
    try:
        Path(ctl.settings_path()).parent.joinpath("selftest.txt").write_text(
            "\n".join(out), encoding="utf-8")
    except Exception:
        pass
    return rc


def smoke() -> int:
    """开窗渲染几秒再自关，确认各页签都建得起来（打包后也能测）。"""
    result = {"ok": False, "err": ""}
    app = wx.App(False)
    frame = None
    try:
        boot_settings()
        frame = MainFrame()
        frame.Centre()
        frame.Show()
        for i in (1, 2, 3):
            frame.tabs.SetSelection(i)
            wx.YieldIfNeeded()
        frame.tabs.SetSelection(0)
        # 两套主题都刷一遍，确认自绘控件在深浅两色下都不炸
        for name in ("dark", "light"):
            THEME.set(name)
            frame.after_theme()
            wx.YieldIfNeeded()
        result["ok"] = True
    except Exception as e:                     # noqa: BLE001
        import traceback
        result["err"] = repr(e) + "\n" + traceback.format_exc()
    keep = {"frame": frame}
    out_dir = Path(ctl.settings_path()).parent

    def done():
        try:
            out_dir.joinpath("smoke.txt").write_text(
                f"UI {'OK' if result['ok'] else 'FAIL'} {result['err']}", encoding="utf-8")
        except Exception:
            pass
        try:
            keep["frame"].shutdown()
            keep["frame"].Destroy()
        except Exception:
            pass
        app.ExitMainLoop()

    wx.CallLater(4000, done)
    app.MainLoop()
    os._exit(0 if result["ok"] else 1)


def smoke_float() -> int:
    """只开悬浮窗渲染几秒，确认它单独也能起来、并且真的画出了数据。"""
    result = {"ok": False, "err": "", "dump": ""}
    app = wx.App(False)
    f = None
    try:
        boot_settings()
        f = FloatFrame()
        f.Show()
        f.update({"health": None, "metrics": None})
        sysi = ctl.sys_sample()               # 模型没跑时悬浮窗靠它活着
        m = ctl.metrics(timeout=3.0)
        result["err"] = (f"metrics_none={m is None} sys_cpu={sysi.get('cpu')} "
                         f"sys_ram={bool(sysi.get('ram'))} sys_gpu={bool(sysi.get('gpu'))}")
        f.update({"health": None, "metrics": None, "sys": sysi})
        if m:
            f.update({"health": {"loaded": True}, "metrics": m})
        result["dump"] = " | ".join(f"{k}={v.value}" for k, v in f.minis.items())
        THEME.set("dark")
        f.retheme()
        wx.YieldIfNeeded()
        result["ok"] = True
    except Exception as e:                     # noqa: BLE001
        import traceback
        result["err"] = repr(e) + "\n" + traceback.format_exc()
    out_dir = Path(ctl.settings_path()).parent

    def done():
        try:
            out_dir.joinpath("smoke_float.txt").write_text(
                f"FLOAT {'OK' if result['ok'] else 'FAIL'} {result['err']}\n"
                f"minis: {result['dump']}", encoding="utf-8")
        except Exception:
            pass
        try:
            f.Destroy()
        except Exception:
            pass
        app.ExitMainLoop()
    wx.CallLater(2500, done)
    app.MainLoop()
    os._exit(0 if result["ok"] else 1)


def openweb_probe() -> int:
    """测打包后的 exe 能不能真的唤起浏览器：结果写入 openweb.txt。"""
    lines = []
    try:
        lines.append(f"server_running = {ctl.server_running()}")
        try:
            how = ctl.open_url(f"http://localhost:{ctl.PORT}/")
            lines.append(f"opened via {how}")
            rc = 0
        except Exception as e:                 # noqa: BLE001
            lines.append(f"FAILED: {e}")
            rc = 1
    except Exception as e:                     # noqa: BLE001
        lines.append(f"FAILED: {e}")
        rc = 1
    try:
        Path(ctl.settings_path()).parent.joinpath("openweb.txt").write_text(
            "\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    os._exit(rc)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--smoke" in sys.argv:
        sys.exit(smoke())
    if "--smokefloat" in sys.argv:
        sys.exit(smoke_float())
    if "--openweb" in sys.argv:
        openweb_probe()
    if "--float" in sys.argv:
        run_float_app()
    main()
