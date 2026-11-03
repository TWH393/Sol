"""Bounded bootstrap validation of Sol's own QQ loader. Never scans or sends messages."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]


def run(qq):
    build = ROOT / "build/qq-native/Release"
    import shutil
    shutil.copy2(ROOT / "qq-native/bootstrap.cjs", build / "bootstrap.cjs")
    client = ROOT / "state/qq/sol-client"
    profile = ROOT / "state/qq/sol-profile"
    port = 24766
    if any(c.laddr and c.laddr.port == port and c.status == "LISTEN" for c in psutil.net_connections("tcp")):
        raise RuntimeError("本地原型端口被占用，未接入已有进程")
    key = secrets.token_urlsafe(32)
    env = {**os.environ, "SOL_QQ_RPC_KEY": key}
    result = {"driver": "native", "version": "9.9.36-53644", "phase": "not_ready", "login_attempted": False,
              "messages_sent": 0, "third_party_authorization": False, "program_files_patched": False}
    log = (ROOT / "state/qq/native-loader.log").open("ab")
    process = subprocess.Popen([str(build / "sol-qq-launcher.exe"), str(client / "QQ.exe"), str(profile), qq],
                               stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                               creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        with httpx.Client(trust_env=False, timeout=1) as http:
            for _ in range(25):
                if process.poll() is not None:
                    result.update(reason="独立QQ加载器退出，请查看native-loader.log和loader-stage.log", exit_code=process.returncode)
                    break
                try:
                    r = http.get(f"http://127.0.0.1:{port}/status", headers={"X-Sol-QQ": key})
                    if r.is_success:
                        data = r.json()
                        if not psutil.Process(data["pid"]).exe().startswith(str(client)):
                            raise RuntimeError("QQ响应不属于独立副本")
                        result.update(phase="native_bootstrap_running", actual=data.get("actual"), online=data.get("online"), stage=data.get("stage"), profile_isolated=Path(data["profile"]) == profile)
                        if not result["profile_isolated"]:
                            raise RuntimeError("资料目录没有隔离，拒绝继续")
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                result["reason"] = "Sol加载器已启动但接入代码尚未响应，需要继续适配当前QQ版本"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        log.close()
    (ROOT / "state/qq/native-loader-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(); parser.add_argument("--qq", required=True)
    run(parser.parse_args().qq)
