"""One CPU-only background removal job; process exit releases all model memory."""
import json
import hashlib
import os
import sys
from pathlib import Path

def main(source, output, kind):
    os.environ.setdefault('OMP_NUM_THREADS', '2')
    from PIL import Image, ImageChops, ImageOps
    from rembg import new_session, remove
    models={'general':'birefnet-general-lite','anime':'birefnet-general-lite'}
    if kind not in models:raise ValueError('未知抠图类型。')
    model=models[kind]
    if not (Path(os.environ['U2NET_HOME'])/(model+'.onnx')).is_file():
        raise ValueError('本地抠图模型缺失，请管理员恢复模型文件。')
    with (Path(os.environ['U2NET_HOME'])/(model+'.onnx')).open('rb') as weights:
        if hashlib.file_digest(weights, 'md5').hexdigest() != '4fab47adc4ff364be1713e97b7e66334':
            raise ValueError('抠图模型校验不匹配，未下载或覆盖已有资源，请在安装程序中修复。')
    with Image.open(source) as original:
        if original.width*original.height>16000000:raise ValueError('图片超过1600万像素。')
        image=ImageOps.exif_transpose(original).convert('RGBA')
    session=new_session(model,providers=['CPUExecutionProvider'])
    mask=remove(image.convert('RGB'),session=session,only_mask=True).convert('L')
    if mask.size!=image.size:raise ValueError('抠图尺寸不一致。')
    alpha=ImageChops.multiply(mask,image.getchannel('A'))
    hist=alpha.histogram();pixels=image.width*image.height
    visible=sum(hist[128:])/pixels
    transparent=sum(hist[:16])/pixels
    if visible<0.0001 or transparent<0.0001:
        raise ValueError('未识别到可分离的主体与背景，请换一张主体更清楚的图片。')
    image.putalpha(alpha)
    image.save(output,format='PNG')
    print(json.dumps({'ok':True,'width':image.width,'height':image.height,'model':model,'transparent':True}))

if __name__=='__main__':
    try:main(*sys.argv[1:])
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)[:250]},ensure_ascii=True));sys.exit(1)

