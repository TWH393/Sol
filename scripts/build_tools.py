"""Build the common Docker tool image only; never start Sol or execute model code."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
workers = root / "companion/workers"
digest = hashlib.sha256(b"".join((workers / name).read_bytes() for name in ("Dockerfile", "session.py", "client.py"))).hexdigest()[:12]
image = "sol-tools:"+digest
docker = shutil.which("docker")
if not docker:
    sys.exit("Docker Desktop is not installed. No app was started.")
log = root / "build/docker-tools-build.log"
log.parent.mkdir(parents=True, exist_ok=True)
print("Building "+image+"; log: "+str(log), flush=True)
with log.open("wb") as output:
    code = subprocess.call([docker, "build", "--progress=plain", "-t", image, str(workers)], stdout=output, stderr=subprocess.STDOUT)
if code:
    print(log.read_text("utf-8", errors="replace")[-2500:])
    sys.exit(code)
print("Built "+image+". No app, QQ or model task was started.")
