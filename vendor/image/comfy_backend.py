"""Per-job local ComfyUI runner using the user's existing model installation."""
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path

COMFY = Path(__file__).resolve().parents[2] / 'runtime/images/ComfyUI'
MODEL = 'qwen_image_2.1_int8_convrot.safetensors'
ENCODER = 'qwen3vl_8b_int8_convrot.safetensors'
VAE = 'qwen_image_2.1_vae_bf16.safetensors'
MODELS = dict.fromkeys(('general','realistic','anime'),MODEL)
REQUIRED_FILES = [('diffusion_models',MODEL),('text_encoders',ENCODER),('vae',VAE)]
STEPS = 40


def worker_error(log_path, offset=0):
    """Report this launch's useful error, without replaying old attempts/help."""
    try:
        with log_path.open('rb') as source:
            source.seek(0, 2)
            source.seek(max(offset, source.tell()-16000))
            lines = [line.strip() for line in source.read().decode('utf-8', errors='replace').splitlines() if line.strip()]
    except OSError:
        return '未能读取本次绘图进程日志'
    useful = [line for line in lines if 'error:' in line.lower() or line.startswith(('RuntimeError:', 'ImportError:', 'ModuleNotFoundError:', 'OSError:', 'ValueError:', 'PermissionError:'))]
    return (useful[-1] if useful else lines[-1] if lines else '本次进程未输出具体错误')[:1200]


def workflow(prompt, style, width, height, seed, prefix):
    if style not in MODELS:
        raise ValueError('未知生图风格。')
    suffix={'general':'','anime':'\nAnime illustration, expressive line art and cel shading.',
            'realistic':'\nPhotorealistic image, natural lighting and realistic materials.'}[style]
    return {
        '1': {'class_type':'UNETLoader','inputs':{'unet_name':MODEL,'weight_dtype':'default'}},
        '2': {'class_type':'CLIPLoader','inputs':{'clip_name':ENCODER,'type':'qwen_image','device':'default'}},
        '3': {'class_type':'VAELoader','inputs':{'vae_name':VAE}},
        '4': {'class_type':'TextEncodeQwenImage21','inputs':{'clip':['2',0],
              'prompt':prompt+suffix,'negative_prompt':'','resolution':1024}},
        '6': {'class_type': 'EmptyLatentImage',
              'inputs': {'width': width, 'height': height, 'batch_size': 1}},
        '7': {'class_type':'SamplerCustom','inputs':{'model':['10',0],'add_noise':True,
              'noise_seed':seed,'cfg':1.0,'positive':['4',0],'negative':['4',1],
              'sampler':['11',0],'sigmas':['12',0],'latent_image':['6',0]}},
        '8': {'class_type': 'VAEDecode', 'inputs': {'samples': ['7', 0], 'vae': ['3', 0]}},
        '9': {'class_type': 'SaveImage', 'inputs': {'images': ['8', 0], 'filename_prefix': prefix}},
        '10': {'class_type':'QwenImage21Cache','inputs':{'model':['1',0],'device':'auto','dtype':'default'}},
        '11': {'class_type':'KSamplerSelect','inputs':{'sampler_name':'euler'}},
        '12': {'class_type':'BasicScheduler','inputs':{'model':['10',0],'scheduler':'simple','steps':STEPS,'denoise':1.0}},
    }


def generate(spec, output, progress):
    style = spec.style
    if style not in MODELS: raise ValueError('未知生图风格。')
    needed = [COMFY/'models'/folder/name for folder,name in REQUIRED_FILES]
    missing = [p.name for p in needed if not p.is_file()]
    if missing:
        raise RuntimeError('所选本地模型文件缺失：'+ '、'.join(missing))
    # Dedicated ephemeral server: never reuse or stop the user's own ComfyUI.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    host = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def api(path, payload=None):
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(host+path, data=data, headers={'Content-Type': 'application/json'})
        try:
            with opener.open(req, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(6000).decode('utf-8', errors='replace')
            raise RuntimeError(f'绘图引擎接口{path}返回HTTP {exc.code}：'+detail[:1000]) from exc
    image_id = uuid.uuid4().hex
    seed = secrets.randbelow(2**31)
    started = time.monotonic()
    folder = output.parent/'jobs'
    folder.mkdir(exist_ok=True)
    import tempfile
    with tempfile.TemporaryDirectory(prefix='comfy-', dir=folder) as temp:
        runtime_root = Path(temp).resolve()
        directories = {name: runtime_root/name for name in ('input', 'temp', 'user')}
        # ComfyUI validates --user-directory during argument parsing, before
        # it creates runtime folders. All paths must exist before Popen.
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        progress('启动本地绘图引擎', model=MODELS[style])
        log_path=output.parent/'comfy-worker.log'
        if log_path.exists() and log_path.stat().st_size>8*1024*1024:
            log_path.replace(output.parent/'comfy-worker.previous.log')
        with log_path.open('ab') as log:
            log_offset = log.tell()
            env = dict(os.environ)
            env.pop('PYTHONHOME', None)
            env.pop('PYTHONPATH', None)
            env.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1')
            worker = subprocess.Popen([str(COMFY/'.venv/Scripts/python.exe'), '-u', str(COMFY/'main.py'),
                '--listen', '127.0.0.1', '--port', str(port), '--disable-auto-launch',
                '--output-directory', str(runtime_root), '--input-directory', str(directories['input']),
                '--temp-directory', str(directories['temp']), '--user-directory', str(directories['user']),
                '--disable-all-custom-nodes'], cwd=COMFY,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline = started+150
                while True:
                    if worker.poll() is not None:
                        raise RuntimeError(f'本地绘图引擎启动失败（退出码{worker.returncode}）：'+worker_error(log_path, log_offset))
                    try:
                        api('/system_stats')
                        break
                    except OSError:
                        if time.monotonic()>deadline:
                            raise RuntimeError('本地绘图引擎启动超时，最后日志：'+worker_error(log_path, log_offset))
                        time.sleep(1)
                progress('加载并生成图片', model='Qwen-Image-2.1 标准版 INT8', total=STEPS)
                reply=api('/prompt', {'prompt': workflow(spec.prompt, style, spec.width, spec.height, seed, image_id)})
                prompt_id=reply['prompt_id']
                while time.monotonic()-started<780:
                    if worker.poll() is not None:
                        raise RuntimeError(f'本地绘图引擎异常退出（退出码{worker.returncode}）：'+worker_error(log_path, log_offset))
                    history=api('/history/'+prompt_id).get(prompt_id)
                    if history:
                        if history.get('status', {}).get('status_str') == 'error':
                            raise RuntimeError('所选模型生成失败：'+worker_error(log_path, log_offset))
                        images=history.get('outputs', {}).get('9', {}).get('images', [])
                        if images:
                            item=images[0]
                            source=(Path(temp)/item.get('subfolder','')/item['filename']).resolve()
                            if not source.is_relative_to(Path(temp).resolve()) or not source.is_file():
                                raise RuntimeError('模型返回的图片文件无效。')
                            shutil.copy2(source, output/(image_id+'.png'))
                            return {'id':image_id, 'model':MODELS[style].removesuffix('.safetensors'),
                                'width':spec.width, 'height':spec.height, 'seed':seed,
                                'seconds':round(time.monotonic()-started,1)}
                    time.sleep(.5)
                raise RuntimeError('本地模型生成超时。')
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    try: worker.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        worker.kill(); worker.wait()
