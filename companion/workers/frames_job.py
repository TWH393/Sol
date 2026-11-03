"""Decode only the requested video timestamps; no ASR or model invocation."""
import json
import subprocess
import sys
from pathlib import Path

request = Path(sys.argv[1]).resolve()
job = json.loads(request.read_text("utf-8"))
folder = request.parent
import av
with av.open(job["source"]) as video:
    duration = float(video.duration or 0)/av.time_base
frames = []
for index, stamp in enumerate(job["times"]):
    stamp = float(stamp)
    if not 0 <= stamp < duration:
        raise ValueError("抽帧时间超出视频范围")
    path = folder / f"frame-{index}.jpg"
    process = subprocess.run([job["ffmpeg"],"-nostdin","-y","-ss",str(stamp),"-i",job["source"],"-frames:v","1","-vf","scale=1280:-2",str(path)],capture_output=True,timeout=60)
    if process.returncode or not path.is_file():
        raise RuntimeError("视频片段解码失败："+process.stderr.decode(errors="replace")[-500:])
    frames.append({"path":str(path),"time":stamp})
(folder / "result.json").write_text(json.dumps({"frames":frames,"duration":duration}),"utf-8")
