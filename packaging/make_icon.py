"""生成应用图标 wb.ico（品牌紫渐变 + 对勾），用于 exe。"""
from PIL import Image, ImageDraw

S = 1024          # 先大图，再降采样抗锯齿
SS = 4            # 超采样倍数
W = S * SS

img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

# 渐变背景
grad = Image.new("RGB", (W, W))
gd = ImageDraw.Draw(grad)
c1, c2 = (91, 91, 240), (122, 90, 248)   # #5B5BF0 -> #7A5AF8
for y in range(W):
    for_x = y / (W - 1)
    # 只用对角线近似：按 x+y 混合，逐行近似足够
    t = for_x
    r = int(c1[0] + (c2[0] - c1[0]) * t)
    g = int(c1[1] + (c2[1] - c1[1]) * t)
    b = int(c1[2] + (c2[2] - c1[2]) * t)
    gd.line([(0, y), (W, y)], fill=(r, g, b))

mask = Image.new("L", (W, W), 0)
md = ImageDraw.Draw(mask)
r_pad = int(W * 0.0)
radius = int(W * 0.225)
md.rounded_rectangle([r_pad, r_pad, W - r_pad, W - r_pad], radius=radius, fill=255)

img.paste(grad, (0, 0), mask)

# 白色对勾
d = ImageDraw.Draw(img)
pts = [(int(W * 0.285), int(W * 0.525)),
       (int(W * 0.445), int(W * 0.685)),
       (int(W * 0.735), int(W * 0.345))]
d.line(pts, fill=(255, 255, 255, 255), width=int(W * 0.105), joint="curve")
# 端点圆角
for p in (pts[0], pts[2]):
    rr = int(W * 0.0525)
    d.ellipse([p[0] - rr, p[1] - rr, p[0] + rr, p[1] + rr], fill=(255, 255, 255, 255))

final = img.resize((S, S), Image.LANCZOS)
final.save("wb.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
final.resize((256, 256), Image.LANCZOS).save("_icon_preview.png")
print("wb.ico 生成完成")
