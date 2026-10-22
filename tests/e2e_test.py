# -*- coding: utf-8 -*-
"""端到端验证：启动 → 加载 → 视觉启停 → 卸载 → 关闭。"""
import time

import strata_ctl as ctl


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def wait_loaded(limit=480):
    t0 = time.time()
    while time.time() - t0 < limit:
        h = ctl.health(timeout=3.0)
        if h and h.get("loaded"):
            log(f"加载完成（{time.time() - t0:.0f}s）")
            return True
        time.sleep(5)
    log("等待超时")
    return False


log("idle_unload_s =", ctl.idle_unload_seconds())
log("启动服务 ...")
ctl.start_server(log_sink=lambda line: log("  srv|", line[:150]))
ok = wait_loaded()
log("health =", ctl.health())
log("engine pids =", ctl.engine_pids(), "vision pids =", ctl.vision_pids())

log("--- /load（已加载，应立即返回）")
try:
    log(ctl.load_model())
except Exception as e:
    log("load 失败:", e)

log("--- 关闭视觉模型")
try:
    log(ctl.stop_vision())
except Exception as e:
    log("stop_vision 失败:", e)
time.sleep(1)
log("vision pids =", ctl.vision_pids())

log("--- 启动视觉模型")
try:
    log(ctl.start_vision())
except Exception as e:
    log("start_vision 失败:", e)
log("vision pids =", ctl.vision_pids())

log("--- 卸载模型")
try:
    log(ctl.unload_model())
except Exception as e:
    log("unload 失败:", e)
time.sleep(2)
log("卸载后 health =", ctl.health())

log("--- 关闭服务")
log(ctl.stop_server())
time.sleep(2)
log("收尾：engine =", ctl.engine_pids(), "vision =", ctl.vision_pids(), "port =", ctl.port_pids())
log("DONE")
