"""Container-only session server, retaining globals for one task."""
import base64
import contextlib
import hashlib
import io
import json
import os
import resource
import signal
import socketserver
import subprocess
import tempfile
import importlib
import site
import traceback
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

os.chdir("/work")
Path("input").mkdir(exist_ok=True)
Path("output").mkdir(exist_ok=True)
resource.setrlimit(resource.RLIMIT_FSIZE, (100*1024*1024, 100*1024*1024))
namespace = {"__name__": "__main__"}
sent_files = {}
input_manifest = {}


class Output(io.StringIO):
    def write(self, text):
        if self.tell()+len(text) > 100000:
            raise RuntimeError("输出过多，请保存到文件")
        return super().write(text)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        data = self.rfile.readline(160*1024*1024)
        try:
            payload = json.loads(data)
            for item in payload.get("files", []):
                name = item["name"]
                if Path(name).name != name or name in {".", ".."}:
                    raise ValueError("Invalid input name")
                Path("input", name).write_bytes(base64.b64decode(item["data"], validate=True))
                if item.get("id"):
                    input_manifest[item["id"]] = "input/"+name
            Path("input-manifest.json").write_text(json.dumps(input_manifest, ensure_ascii=False), encoding="utf-8")
            output = Output()
            def expired(*_):
                raise TimeoutError("Python execution exceeded its limit")
            signal.signal(signal.SIGALRM, expired)
            timeout = max(1, min(600, int(payload.get("timeout", 180))))
            signal.alarm(timeout+2)
            try:
                if payload.get("mode") == "shell":
                    with tempfile.TemporaryFile() as log:
                        process = subprocess.Popen(["/bin/bash", "-o", "pipefail", "-c", payload["command"]], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                        try:
                            process.wait(timeout=timeout)
                        finally:
                            # Reap descendants too, including commands left in the background.
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait()
                        log.seek(0)
                        output.write(log.read(90000).decode("utf-8", errors="replace"))
                        if process.returncode:
                            raise RuntimeError("Shell exit "+str(process.returncode)+"\n"+output.getvalue())
                else:
                    importlib.invalidate_caches()
                    site.addsitedir(site.getusersitepackages())
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        exec(compile(payload["code"], "<task>", "exec"), namespace)
                result = {"ok": True, "stdout": output.getvalue(), "files": []}
            finally:
                signal.alarm(0)
            total = 0
            for path in Path("output").iterdir():
                if path.is_file() and not path.is_symlink():
                    if path.suffix.lower() in {".docx", ".pptx", ".xlsx"}:
                        with zipfile.ZipFile(path) as package:
                            if package.testzip():
                                raise ValueError("Office产物归档损坏："+path.name)
                            ET.fromstring(package.read("[Content_Types].xml"))
                    elif path.suffix.lower() == ".pdf":
                        import fitz
                        with fitz.open(path) as document:
                            if not len(document):
                                raise ValueError("生成的PDF没有页面")
                    total += path.stat().st_size
                    if total > 100*1024*1024:
                        raise ValueError("Outputs exceed 100MB")
                    raw = path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    if sent_files.get(path.name) != digest:
                        result["files"].append({"name": path.name, "data": base64.b64encode(raw).decode()})
                        sent_files[path.name] = digest
        except BaseException as exc:
            result = {"ok": False, "error": traceback.format_exc(limit=8), "files": [], "missing_module": exc.name if isinstance(exc, ModuleNotFoundError) else None}
        self.wfile.write(json.dumps(result).encode()+b"\n")


class Server(socketserver.UnixStreamServer):
    allow_reuse_address = True


with Server("/tmp/companion.sock", Handler) as server:
    server.serve_forever()
