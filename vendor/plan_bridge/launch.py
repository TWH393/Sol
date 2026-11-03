"""Managed bootstrap: startup stages and bounded stall diagnostics, never inference."""
from __future__ import annotations

import faulthandler
import json
import os
from pathlib import Path
import runpy
import time

root = Path(os.environ["QQ_PLAN_DATA_DIR"])
root.mkdir(parents=True, exist_ok=True)


def stage(text):
    (root / "startup.json").write_text(json.dumps({"pid": os.getpid(), "stage": text, "time": time.time()}, ensure_ascii=False), "utf-8")
    print("Sol GPT bridge: " + text, flush=True)


stage("加载登录桥组件")
faulthandler.enable()
faulthandler.dump_traceback_later(25, repeat=False)
try:
    runpy.run_path(str(Path(__file__).with_name("bridge.py")), run_name="__main__")
except BaseException as exc:
    stage("启动失败：" + type(exc).__name__)
    raise
finally:
    faulthandler.cancel_dump_traceback_later()
