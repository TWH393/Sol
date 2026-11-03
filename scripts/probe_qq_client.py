"""One bounded capability probe of the isolated QQ copy; no login, message send or patching."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]


def probe():
    client = ROOT / "state/qq/sol-client"
    data = json.loads((client / "sol-client.json").read_text("utf-8"))
    profile = Path(data["profile"])
    for path in (profile, profile / "Roaming", profile / "Local"):
        path.mkdir(parents=True, exist_ok=True)
    port = 24765
    if any(c.laddr.port == port for c in psutil.net_connections("tcp") if c.laddr and c.status == "LISTEN"):
        raise RuntimeError("原型调试端口已占用，未连接其他进程")
    env = {**os.environ, "USERPROFILE": str(profile), "APPDATA": str(profile / "Roaming"), "LOCALAPPDATA": str(profile / "Local")}
    log_path = profile.parent / "native-probe.log"
    result = {"version": data["version"], "status": "not_ready", "login_attempted": False,
              "messages_sent": 0, "third_party_authorization": False, "program_files_patched": False}
    with log_path.open("ab") as log:
        process = subprocess.Popen([str(client / "QQ.exe"), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", "--user-data-dir="+str(profile)],
                                   cwd=client, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        with httpx.Client(trust_env=False, timeout=1) as http:
            for _ in range(15):
                candidates = [p for p in psutil.process_iter(["pid", "exe"]) if p.info["exe"] and Path(p.info["exe"]).is_relative_to(client)]
                owned = {p.pid for p in candidates}
                listeners = [c for c in psutil.net_connections("tcp") if c.laddr and c.laddr.port == port and c.status == "LISTEN"]
                if listeners and listeners[0].pid not in owned:
                    raise RuntimeError("调试监听器不属于独立QQ，已拒绝接入")
                if listeners:
                    response = http.get(f"http://127.0.0.1:{port}/json/list")
                    response.raise_for_status()
                    result.update(status="renderer_debug_available", targets=len(response.json()), owned_pid=listeners[0].pid)
                    break
                time.sleep(1)
            else:
                result["reason"] = "15秒内未发现独立QQ的调试监听器，启动配置或客户端适配仍需排查；尚未扫码或收发。"
                result["launcher_exit_code"] = process.poll()
    finally:
        # Only exact program copies in this prototype directory, never the user's installed QQ.
        for p in psutil.process_iter(["pid", "exe"]):
            if p.info["exe"] and Path(p.info["exe"]).is_relative_to(client):
                try:
                    p.terminate()
                except psutil.NoSuchProcess:
                    pass
    (profile.parent / "native-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    probe()
