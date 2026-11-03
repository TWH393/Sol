"""Image job entrypoint; the process and its child renderer are task-owned."""
import sys

# Record entry before importing even stdlib helpers, so startup failures are
# distinguishable from adapter/model loading without launching another job.
with open(sys.argv[1]+".started", "w", encoding="utf-8") as entered:
    entered.write("image_job entered\n"+sys.version)

import json
import os
from pathlib import Path
from types import SimpleNamespace

request = Path(sys.argv[1]).resolve()
job = json.loads(request.read_text("utf-8"))
folder = request.parent
def progress(stage, **fields):
    temporary = folder / "progress.tmp"
    temporary.write_text(json.dumps({"stage": stage, "pid": os.getpid(), **fields}, ensure_ascii=False), "utf-8")
    temporary.replace(folder / "progress.json")

progress("图片调度脚本已启动")
vendor = Path(__file__).resolve().parents[2] / "vendor/image"
sys.path.insert(0, str(vendor))
action = job["action"]
output = folder / "output.png"
if action == "generate":
    progress("加载本地绘图适配器")
    import comfy_backend
    comfy_backend.COMFY = Path(job["comfy"])
    result = comfy_backend.generate(SimpleNamespace(**job), folder, progress)
    output = folder / (result["id"]+".png")
elif action == "cutout":
    os.environ["U2NET_HOME"] = str(Path(job["resources"]) / "models/rembg")
    import cutout_worker
    cutout_worker.main(job["source"], str(output), "general")
elif action == "erase":
    os.environ["COMPANION_INPAINT_MODEL"] = str(Path(job["resources"]) / "models/inpaint/big-lama.pt")
    import inpaint_worker
    inpaint_worker.main(job["source"], str(output), job["mask"])
elif action == "edit":
    os.environ["COMPANION_IMAGE_RESOURCES"] = job["resources"]
    os.environ["COMPANION_IMAGE_OUTPUT"] = str(folder)
    os.environ["COMPANION_IMAGE_STATE"] = str(folder)
    import image_engine
    import base64
    spec = image_engine.Editing(prompt=job["prompt"], image_base64=base64.b64encode(Path(job["source"]).read_bytes()).decode())
    result = image_engine.edit_image(spec, image_engine.decode_input(spec.image_base64))
    output = folder / (result["id"]+".png")
else:
    raise ValueError("Unknown image operation")
(folder / "result.json").write_text(json.dumps({"path": str(output)}), "utf-8")
progress("图片产物已写入")
