# Sol 透明纯线条版 v1

当前用户要求：使用无边框的透明太阳星球，星环与四条光芒加粗，圆圈保持细线；不要光晕、渐变或颜色扩散。

输出：`sol-transparent-flat-lines-v1.png`，1536×1024，单色 #D47D58，透明背景；已查看结果，四角alpha=0。

处理：内置image_gen生成无光晕的白底线稿，随后本地提取线条并统一单色、将空白置透明。使用之前已讨论的本地精确处理方式，不使用会污染底色的生成式透明效果。

生成提示词要点：STRICT FLAT VECTOR LINE ART; thin circular outline, bold tilted orbital ring and four bold rounded rays; one terracotta ink; pure white extraction background; no glow, gradients, shadows, texture, blur, lighting or color bleed; no border or tile.

这份图标仍是设计候选，未替换软件图标。
