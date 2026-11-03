param([ValidateSet('light','dark')][string]$Variant = 'light')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing.Common
$taskCode = @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class SolLogoCutout {
    public static string Crop(string source, string destination) {
        using (var image = new Bitmap(source)) {
            int w = image.Width, h = image.Height, n = w*h;
            var rect = new Rectangle(0,0,w,h);
            var data = image.LockBits(rect, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
            byte[] pixels = new byte[data.Stride*h];
            int stride = data.Stride;
            Marshal.Copy(data.Scan0, pixels, 0, pixels.Length);
            image.UnlockBits(data);
            bool[] border = new bool[n], outside = new bool[n];
            for (int y=0; y<h; y++) for(int x=0; x<w; x++) {
                int p=y*stride+4*x;
                int b=pixels[p], g=pixels[p+1], r=pixels[p+2];
                // Both approved variants use terracotta strokes. The original
                // interior and exterior backgrounds have near-neutral chroma.
                border[y*w+x] = r>=100 && r-g>=18 && g-b>=8;
            }
            int[] queue = new int[n]; int head=0, tail=0;
            Action<int> visit = i => {
                if (!outside[i] && !border[i]) { outside[i]=true; queue[tail++]=i; }
            };
            for (int x=0; x<w; x++) {visit(x); visit((h-1)*w+x);}
            for (int y=0; y<h; y++) {visit(y*w); visit(y*w+w-1);}
            while (head<tail) {
                int i=queue[head++], x=i%w, y=i/w;
                if(x>0)visit(i-1); if(x<w-1)visit(i+1);
                if(y>0)visit(i-w); if(y<h-1)visit(i+w);
            }
            if(outside[(h/2)*w+w/2]) throw new Exception("Frame was not closed; no image written.");
            int left=w, top=h, right=-1, bottom=-1;
            for(int y=0;y<h;y++)for(int x=0;x<w;x++)if(!outside[y*w+x]) {
                left=Math.Min(left,x); right=Math.Max(right,x);
                top=Math.Min(top,y); bottom=Math.Max(bottom,y);
            }
            if(left==0 || top==0 || right==w-1 || bottom==h-1)
                throw new Exception("Could not isolate the original inset frame; no image written.");
            int cw=right-left+1, ch=bottom-top+1, kept=0, transparent=0;
            using(var output=new Bitmap(cw,ch,PixelFormat.Format32bppArgb)) {
                var outputData=output.LockBits(new Rectangle(0,0,cw,ch),ImageLockMode.WriteOnly,PixelFormat.Format32bppArgb);
                byte[] result=new byte[outputData.Stride*ch];
                for(int y=0;y<ch;y++)for(int x=0;x<cw;x++) {
                    int original=(y+top)*stride+4*(x+left), target=y*outputData.Stride+4*x;
                    if(outside[(y+top)*w+x+left]) {transparent++;continue;}
                    for(int c=0;c<4;c++)result[target+c]=pixels[original+c];
                    kept++;
                }
                Marshal.Copy(result,0,outputData.Scan0,result.Length);
                output.UnlockBits(outputData);
                output.Save(destination,ImageFormat.Png);
                if(output.GetPixel(0,0).A!=0 || output.GetPixel(cw-1,0).A!=0 ||
                   output.GetPixel(0,ch-1).A!=0 || output.GetPixel(cw-1,ch-1).A!=0 ||
                   output.GetPixel(cw/2,ch/2).A!=255)
                    throw new Exception("Unexpected alpha mask.");
            }
            return String.Format("{0}x{1} -> {2}x{3}; original pixels retained={4}; transparent outside pixels={5}; all four corners alpha=0; center alpha=255",w,h,cw,ch,kept,transparent);
        }
    }
}
'@
Add-Type -TypeDefinition $taskCode -ReferencedAssemblies @('System.Drawing.Common','System.Drawing.Primitives','System.Runtime','System.Runtime.InteropServices','System.Private.Windows.GdiPlus','System.Private.Windows.Core')
$taskOriginalRoot = 'C:/Users/User/.codex/generated_images/01a19522-6ffb-71d2-9df4-a3bab411213c'
if ($Variant -eq 'light') { $taskOriginal = Join-Path $taskOriginalRoot 'exec-64e9d3ce-bbf6-4804-8af9-68ec56b01f0e.png' }
else { $taskOriginal = Join-Path $taskOriginalRoot 'exec-f136e9a2-82b9-4b80-99cf-19aaab86f889.png' }
$taskDestination = Join-Path $PSScriptRoot ('sol-' + $Variant + '-cutout-v2.png')
[SolLogoCutout]::Crop($taskOriginal, $taskDestination)
Write-Output $taskDestination
