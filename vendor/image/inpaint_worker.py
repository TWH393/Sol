"""CPU LaMa repair; model exits with the worker, never redraws unmasked pixels."""
import json
import os
import sys
from pathlib import Path

def main(source,output,mask_path):
    import numpy as np
    import cv2
    import torch
    from PIL import Image
    torch.set_num_threads(2);torch.set_num_interop_threads(1);cv2.setNumThreads(2)
    model_path=Path(os.environ.get('COMPANION_INPAINT_MODEL',str(Path(__file__).resolve().parents[2] / 'runtime/images/models/inpaint/big-lama.pt')))
    if not model_path.is_file():raise ValueError('本地背景修复模型缺失，请管理员恢复模型。')
    with Image.open(source) as original:image=original.convert('RGBA')
    if image.width*image.height>16000000:raise ValueError('图片超过1600万像素。')
    with Image.open(mask_path) as original:mask=original.convert('L')
    if mask.size!=image.size or not mask.getbbox():raise ValueError('擦除选区无效。')
    x0,y0,x1,y1=mask.getbbox();margin=128
    box=(max(0,x0-margin),max(0,y0-margin),min(image.width,x1+margin),min(image.height,y1+margin))
    crop=image.crop(box).convert('RGB');selected=mask.crop(box)
    old_size=crop.size;crop.thumbnail((1024,1024),Image.Resampling.LANCZOS)
    selected=selected.resize(crop.size,Image.Resampling.NEAREST)
    pixels=np.asarray(crop,dtype=np.float32)/255.0
    binary=(np.asarray(selected)>0).astype(np.float32)
    h,w=pixels.shape[:2];pad=((0,(-h)%8),(0,(-w)%8))
    pixels=np.pad(pixels,(*pad,(0,0)),mode='symmetric')
    binary=np.pad(binary,pad,mode='symmetric')
    model=torch.jit.load(str(model_path),map_location='cpu').eval()
    with torch.inference_mode():
        result=model(torch.from_numpy(pixels.transpose(2,0,1).copy())[None],torch.from_numpy(binary.copy())[None,None])
    result=result[0].permute(1,2,0).cpu().numpy()[:h,:w]
    if not np.isfinite(result).all():raise ValueError('背景修复模型返回无效像素。')
    repaired=Image.fromarray(np.clip(result*255,0,255).astype(np.uint8)).resize(old_size,Image.Resampling.LANCZOS)
    # Match the repaired gradients to the surrounding original pixels. Final
    # composition below still strictly restricts all changes to the given mask.
    blend_mask=np.array(mask.crop(box),dtype=np.uint8)
    blend_mask[blend_mask>0]=255
    blend_mask[:2,:]=0;blend_mask[-2:,:]=0;blend_mask[:,:2]=0;blend_mask[:,-2:]=0
    bx,by,bw,bh=cv2.boundingRect(blend_mask)
    if bw>2 and bh>2:
        repaired_bgr=cv2.cvtColor(np.array(repaired),cv2.COLOR_RGB2BGR)
        original_bgr=cv2.cvtColor(np.array(image.crop(box).convert('RGB')),cv2.COLOR_RGB2BGR)
        blended=cv2.seamlessClone(repaired_bgr,original_bgr,blend_mask,
            (bx+bw//2,by+bh//2),cv2.NORMAL_CLONE)
        repaired=Image.fromarray(cv2.cvtColor(blended,cv2.COLOR_BGR2RGB))
    # Compose RGB only, restoring original alpha verbatim.
    rgb=image.convert('RGB');rgb.paste(repaired,box[:2],mask.crop(box))
    rgb.putalpha(image.getchannel('A'));rgb.save(output,format='PNG')
    print(json.dumps({'ok':True,'model':'LaMa CPU','width':image.width,'height':image.height}))

if __name__=='__main__':
    try:main(*sys.argv[1:])
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)[:250]},ensure_ascii=True));sys.exit(1)
