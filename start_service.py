# -*- coding: utf-8 -*-
"""独立拉起 Strata 服务：本脚本启动完就退出，服务进程与它脱钩。"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for k in ("PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)

import strata_ctl as ctl

STRATA = r"F:\Qwen3.8-27b\qwen27b\Strata"
ctl.set_strata_dir(STRATA)
print("config:", ctl.CONFIG)
print("server.py:", ctl.SERVER_PY)

log = open(ctl.STRATA_DIR / "strata-launcher-server.log", "ab", buffering=0)
proc = subprocess.Popen(
    [str(ctl.find_python()), str(ctl.SERVER_PY), "--engine", "strata",
     "--config", str(ctl.CONFIG), "--port", "8080"],
    cwd=str(ctl.STRATA_DIR), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
    creationflags=0x00000008 | 0x00000200,   # DETACHED_PROCESS | NEW_PROCESS_GROUP
)
print("pid:", proc.pid)
open(r"F:\Qwen3.8-27b\qwen27b\StrataLauncher\_server_pid.txt", "w").write(str(proc.pid))
