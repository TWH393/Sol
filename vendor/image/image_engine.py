"""Authenticated loopback drawing service, isolated from AstrBot's runtime."""
import asyncio
import base64
import io
import gc
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import traceback
import types
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from typing import Literal
import uvicorn

ROOT = Path(os.environ.get('COMPANION_IMAGE_RESOURCES', str(Path(__file__).resolve().parents[2] / 'runtime/images')))
OUTPUT = Path(os.environ.get('COMPANION_IMAGE_OUTPUT', str(ROOT / 'output')))
STATE = Path(os.environ.get('COMPANION_IMAGE_STATE', str(OUTPUT.parent)))
STATE.mkdir(parents=True, exist_ok=True)
OUTPUT.mkdir(exist_ok=True)
KEY = ''  # Worker-only adapter; no HTTP listener is launched.
PORT = 24663
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
lock = asyncio.Lock()
pipeline = None
pipeline_kind = None

def save_progress(stage, **fields):
    path=STATE/'progress.json'
    try: current=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError): current={}
    job=fields.pop('job_id',None) or os.environ.get('QQ_IMAGE_JOB_ID')
    if job and current.get('job_id')!=job: current={'job_id':job}
    current.update(stage=stage,updated=time.time(),**fields)
    temp=path.with_name('progress-'+str(os.getpid())+'.tmp')
    temp.write_text(json.dumps(current,ensure_ascii=False),encoding='utf-8')
    os.replace(temp,path)

def step_progress(pipe,index,timestep,values):
    total=len(pipe.scheduler.timesteps)
    save_progress('生成中' if index+1<total else '解码图片',step=index+1,total=total)
    return values

def run_job(kind, spec):
    """Run GPU work in an expendable process; exit frees allocator and bnb state."""
    temp=STATE/'jobs'
    temp.mkdir(exist_ok=True)
    job_id=uuid.uuid4().hex
    save_progress('启动工作进程',job_id=job_id,kind=kind,started=time.time(),step=0,total=0,finished=None)
    with tempfile.TemporaryDirectory(prefix='job-',dir=temp) as folder:
        folder=Path(folder)
        request=folder/'request.json'
        result=folder/'result.json'
        request.write_text(spec.model_dump_json(),encoding='utf-8')
        log_path=STATE/'worker-error.log'
        if log_path.exists() and log_path.stat().st_size>8*1024*1024:
            log_path.replace(STATE/'worker-error.previous.log')
        with log_path.open('ab') as log:
            worker=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),
                '--worker',kind,str(request),str(result)],cwd=STATE,
                env={**os.environ,'QQ_IMAGE_JOB_ID':job_id},
                stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                code=worker.wait(timeout=900)
            except subprocess.TimeoutExpired:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(worker.pid),'/T','/F'],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                else: worker.kill()
                worker.wait()
                raise RuntimeError('图片处理超时，工作进程已关闭。')
        if code!=0 or not result.is_file():
            raise RuntimeError('图片工作进程失败，请检查worker-error.log。')
        data=json.loads(result.read_text(encoding='utf-8'))
        save_progress('已完成，资源已释放',finished=time.time())
        return data

def enable_quantized_moves(model):
    """Keep bnb's non-buffer quantization state aligned during parent .to()."""
    from bitsandbytes.nn import Linear8bitLt
    original_to=model.to
    def sync(layer):
        device=layer.weight.device
        if layer.weight.CB is not None: layer.weight.CB=layer.weight.data
        if layer.weight.SCB is not None: layer.weight.SCB=layer.weight.SCB.to(device)
        if layer.state.CB is not None: layer.state.CB=layer.weight.data
        if layer.state.SCB is not None: layer.state.SCB=layer.state.SCB.to(device)
    for layer in model.modules():
        if isinstance(layer,Linear8bitLt):
            layer.register_forward_pre_hook(lambda module,inputs: sync(module))
    def move(this,*args,**kwargs):
        result=original_to(*args,**kwargs)
        for layer in this.modules():
            if not isinstance(layer,Linear8bitLt): continue
            sync(layer)
        return result
    model.to=types.MethodType(move,model)
    return model

def release_pipeline():
    global pipeline, pipeline_kind
    import torch
    if pipeline is not None:
        pipeline.maybe_free_model_hooks()
        pipeline = None
    pipeline_kind = None
    gc.collect()
    torch.cuda.empty_cache()


@app.middleware('http')
async def local_only(request, call_next):
    if (request.headers.get('host') != f'127.0.0.1:{PORT}' or
            (request.client and request.client.host != '127.0.0.1') or
            request.headers.get('origin') not in (None, f'http://127.0.0.1:{PORT}')):
        return JSONResponse({'detail': 'Loopback only'}, status_code=403)
    if request.url.path not in ('/health','/'):
        if not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + KEY):
            return JSONResponse({'detail': 'Invalid local key'}, status_code=401)
    return await call_next(request)


class Drawing(BaseModel):
    prompt: str = Field(min_length=2, max_length=3000)
    width: int = 1024
    height: int = 1024
    style: Literal['general', 'realistic', 'anime'] = 'general'


def generate_image(spec):
    from comfy_backend import generate
    return generate(spec, OUTPUT, save_progress)


class Editing(BaseModel):
    prompt: str = Field(min_length=2, max_length=3000)
    image_base64: str = Field(max_length=28000000)


def decode_input(encoded):
    from PIL import Image, ImageOps
    try:
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError('原图超过20MB。')
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 16000000:
                raise ValueError('原图超过1600万像素。')
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((1024,1024))
            return image.copy()
    except Exception as exc:
        raise ValueError('原图无效、过大或无法读取。') from exc


def edit_image(spec, original):
    global pipeline, pipeline_kind
    import torch
    from diffusers import QwenImageEditPlusPipeline, QwenImageTransformer2DModel, BitsAndBytesConfig
    from transformers import Qwen2_5_VLForConditionalGeneration, BitsAndBytesConfig as TextQuant
    if pipeline_kind != 'edit':
        save_progress('加载编辑模型')
        release_pipeline()
        model = str(ROOT / 'models/Qwen-Image-Edit-2511')
        transformer = QwenImageTransformer2DModel.from_pretrained(
            model, subfolder='transformer', torch_dtype=torch.bfloat16,
            quantization_config=BitsAndBytesConfig(load_in_8bit=True), local_files_only=True)
        enable_quantized_moves(transformer).to('cpu')
        torch.cuda.empty_cache()
        save_progress('加载图像与文字编码器')
        encoder = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            model, subfolder='text_encoder', dtype=torch.bfloat16,
            quantization_config=TextQuant(load_in_8bit=True), local_files_only=True)
        enable_quantized_moves(encoder).to('cpu')
        torch.cuda.empty_cache()
        pipeline = QwenImageEditPlusPipeline.from_pretrained(
            model, transformer=transformer, text_encoder=encoder,
            torch_dtype=torch.bfloat16, local_files_only=True)
        pipeline.enable_model_cpu_offload()
        pipeline.vae.enable_tiling()
        pipeline.set_progress_bar_config(disable=True)
        pipeline_kind = 'edit'
    started = time.monotonic()
    width = max(64, round(original.width/16)*16)
    height = max(64, round(original.height/16)*16)
    save_progress('编码原图与编辑要求',step=0,total=0)
    with torch.inference_mode():
        result = pipeline(image=[original], prompt=spec.prompt, width=width, height=height,
                          true_cfg_scale=4.0, negative_prompt=' ', num_inference_steps=40,
                          guidance_scale=1.0, num_images_per_prompt=1,
                          callback_on_step_end=step_progress,
                          generator=torch.Generator('cuda').manual_seed(secrets.randbelow(2**31))).images[0]
    image_id=uuid.uuid4().hex
    result.save(OUTPUT/(image_id+'.png'))
    pipeline.maybe_free_model_hooks()
    torch.cuda.empty_cache()
    return {'id':image_id,'model':'Qwen-Image-Edit-2511','width':width,'height':height,
            'seconds':round(time.monotonic()-started,1)}


@app.post('/edit')
async def edit(spec: Editing):
    try:
        original=await asyncio.to_thread(decode_input,spec.image_base64)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    if lock.locked(): raise HTTPException(429,'正在处理另一张图片，请稍后再试。')
    async with lock:
        try:
            original.close()
            return await asyncio.to_thread(run_job,'edit',spec)
        except Exception as exc:
            save_progress('失败，工作进程已结束',finished=time.time())
            print('Local editing failed: '+type(exc).__name__,flush=True)
            traceback.print_exc()
            raise HTTPException(500,'原图编辑失败，请管理员检查绘图日志。') from exc


@app.get('/health')
async def health():
    try: progress=json.loads((ROOT/'progress.json').read_text(encoding='utf-8'))
    except (OSError,ValueError): progress={}
    return {'ok': True, 'model': 'ComfyUI-local', 'busy': lock.locked(),
            'progress':progress,
            'edit_supported':True,
            'resource_mode':'isolated-worker-per-request',
            'models': dict.fromkeys(('general','realistic','anime'),'Qwen-Image-2.1 标准版 INT8'),
            'model_files_ready': all((ROOT/'ComfyUI/models'/folder/name).is_file()
                for folder,name in __import__('comfy_backend').REQUIRED_FILES)}


@app.post('/generate')
async def generate(spec: Drawing):
    if any(x < 512 or x > 1536 or x % 64 for x in (spec.width, spec.height)) or spec.width * spec.height > 1572864:
        raise HTTPException(400, '图片边长需为512–1536的64倍数，最多150万像素。')
    if lock.locked():
        raise HTTPException(429, '正在画另一张图，请稍后再试。')
    async with lock:
        try:
            return await asyncio.to_thread(run_job, 'draw', spec)
        except Exception as exc:
            save_progress('失败，工作进程已结束',finished=time.time())
            # Never log prompts or return internal paths to chat users.
            print('Local drawing failed: ' + type(exc).__name__, flush=True)
            traceback.print_exc()
            raise HTTPException(500, '本地绘图失败，请管理员检查绘图日志。') from exc


@app.get('/images/{image_id}')
async def image(image_id: str):
    if len(image_id) != 32 or any(c not in '0123456789abcdef' for c in image_id):
        raise HTTPException(404)
    path = OUTPUT / (image_id + '.png')
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type='image/png')

@app.get('/')
async def dashboard():
    return HTMLResponse((Path(__file__).parent/'progress.html').read_text(encoding='utf-8'),headers={'Cache-Control':'no-store'})


if __name__ == '__main__':
    if len(sys.argv)==5 and sys.argv[1]=='--worker':
        kind,request_path,result_path=sys.argv[2:]
        payload=json.loads(Path(request_path).read_text(encoding='utf-8'))
        if kind=='draw':
            result=generate_image(Drawing(**payload))
        elif kind=='edit':
            spec=Editing(**payload)
            result=edit_image(spec,decode_input(spec.image_base64))
        else: raise ValueError('Unknown worker kind')
        Path(result_path).write_text(json.dumps(result),encoding='utf-8')
        # Bnb/offload hooks can retain cyclic GPU references. Process termination
        # is the guaranteed resource boundary; no warm model survives this job.
    else:
        uvicorn.run(app, host='127.0.0.1', port=PORT, access_log=False, log_level='warning')
