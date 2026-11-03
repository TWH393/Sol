"""Local subtitle, audio and scene preparation; no cloud inference."""
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

request = Path(sys.argv[1]).resolve()
job = json.loads(request.read_text("utf-8"))
folder = request.parent


def command(*args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=1200)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-1200:])
    return result


segments, media, title = [], Path(job["source"]) if job.get("source") else None, "视频"
if job.get("url"):
    import yt_dlp
    host = (urlparse(job["url"]).hostname or "").lower()
    allowed = ("bilibili.com", "b23.tv", "youtube.com", "youtu.be", "x.com", "twitter.com", "douyin.com")
    if not any(host == d or host.endswith("."+d) for d in allowed):
        raise ValueError("视频链接平台不在支持范围内")
    options = {"quiet": True, "noplaylist": True, "socket_timeout": 25, "retries": 1, "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best", "outtmpl": str(folder / "media.%(ext)s"), "ffmpeg_location": job["ffmpeg"], "max_filesize": job["max_mb"]*1024*1024, "js_runtimes": {"node": {"path": job.get("node", "node")}}}
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(job["url"], download=False)
        if info.get("is_live") or info.get("duration", 0) > job["max_minutes"]*60:
            raise ValueError("直播或视频时长不符合设置")
        title = info.get("title", title)
        if not job.get("download_only"):
            for tracks in (info.get("subtitles", {}), info.get("automatic_captions", {})):
                languages = sorted(tracks, key=lambda x: (not x.startswith("zh"), not x.startswith("en")))
                for lang in languages:
                    track = next((t for t in tracks[lang] if t.get("ext") == "json3"), None)
                    if not track:
                        continue
                    with downloader.urlopen(track["url"]) as response:
                        raw = json.loads(response.read(12*1024*1024))
                    segments = [{"start": e.get("tStartMs", 0)/1000, "end": (e.get("tStartMs", 0)+e.get("dDurationMs", 0))/1000, "text": "".join(s.get("utf8", "") for s in e.get("segs", []))} for e in raw.get("events", []) if e.get("segs")]
                    if segments:
                        break
                if segments:
                    break
        if job.get("download_only") or job.get("visual") or not segments:
            info = downloader.extract_info(job["url"], download=True)
            media = next((p for p in folder.glob("media.*") if p.suffix not in {".part", ".ytdl"}), None)
duration = max((s["end"] for s in segments), default=0)
if media:
    import av
    with av.open(str(media)) as container:
        duration = float(container.duration or 0)/av.time_base
    if not 0 < duration <= job["max_minutes"]*60 or media.stat().st_size > job["max_mb"]*1024*1024:
        raise ValueError("视频大小或时长不符合设置")
if job.get("download_only"):
    if not media:
        raise ValueError("未取得视频文件")
    result = {"media": str(media)}
else:
    if not segments:
        import numpy as np
        from faster_whisper import WhisperModel
        audio = folder / "audio.f32"
        command(job["ffmpeg"], "-nostdin", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", str(audio))
        samples = np.memmap(audio, dtype="<f4", mode="r")
        model = WhisperModel(job.get("whisper_model") or "small", device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
        parts, _ = model.transcribe(samples, beam_size=3, vad_filter=True)
        segments = [{"start": p.start, "end": p.end, "text": p.text} for p in parts if p.text.strip()]
        del samples, model
    frames = []
    if job.get("visual") and media:
        log = command(job["ffmpeg"], "-nostdin", "-y", "-i", str(media), "-vf", "select='gt(scene,0.35)',showinfo,scale=960:-2", "-vsync", "vfr", "-frames:v", "18", str(folder / "frame-%03d.jpg"))
        times = [float(t) for t in re.findall(r"pts_time:([0-9.]+)", log.stderr.decode(errors="replace"))]
        for index, path in enumerate(sorted(folder.glob("frame-*.jpg"))):
            frames.append({"time": times[index] if index < len(times) else None, "path": str(path)})
        if not frames:
            for index in range(6):
                stamp = max(0, duration*(index+.5)/6)
                path = folder / f"sample-{index}.jpg"
                command(job["ffmpeg"], "-nostdin", "-y", "-ss", str(stamp), "-i", str(media), "-frames:v", "1", "-vf", "scale=960:-2", str(path))
                frames.append({"time": stamp, "path": str(path)})
    result = {"title": title, "duration": duration, "media": str(media) if media else None, "segments": segments, "frames": frames, "analysis_scope": "字幕/本地转写；画面按场景或时间抽样，非逐帧分析"}
(folder / "result.json").write_text(json.dumps(result, ensure_ascii=False), "utf-8")
