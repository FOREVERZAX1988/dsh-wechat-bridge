# -*- coding: utf-8 -*-
"""程序化画卡通鱼头像。

不联网、不找素材，直接用 PIL 画——可控、可批量、没有版权问题。
产物默认放 ~/.dsh/wechat-avatar/，可以直接发到微信里让人挑。

用法：
    python make_avatar.py            # 生成 3 张
    python make_avatar.py 8          # 生成 8 张（随机配色）
"""
import os
import random
import sys

from PIL import Image, ImageDraw

S = 512           # 微信头像建议 ≥196px，512 够清晰
OUT = os.path.join(os.path.expanduser("~"), ".dsh", "wechat-avatar")

# 配色：粉 / 蓝 / 黄 / 薄荷 / 紫
配色 = [
    ((255, 232, 240), (255, 138, 160), (255, 175, 190)),
    ((226, 244, 255), (110, 190, 255), (255, 190, 200)),
    ((255, 246, 220), (255, 196, 90), (255, 170, 160)),
    ((224, 250, 240), (90, 205, 170), (255, 185, 190)),
    ((240, 232, 255), (160, 140, 245), (255, 180, 200)),
]


def 圆(d, cx, cy, r, fill):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


def 画一张(底色, 鱼色, 腮红):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    圆(d, S / 2, S / 2, S / 2 - 6, 底色)                 # 背景圆
    d.ellipse([110, 190, 360, 380], fill=鱼色)             # 身体
    d.polygon([(348, 285), (452, 235), (452, 335)], fill=鱼色)   # 尾鳍
    d.polygon([(210, 196), (250, 130), (290, 196)], fill=鱼色)   # 背鳍
    圆(d, 200, 262, 26, (255, 255, 255))                  # 眼白
    圆(d, 200, 262, 14, (40, 40, 50))                     # 瞳孔
    圆(d, 206, 256, 5, (255, 255, 255))                   # 眼神光
    d.ellipse([150, 292, 205, 322], fill=腮红)             # 腮红
    d.arc([215, 285, 265, 320], start=10, end=170, fill=(90, 60, 60), width=6)  # 笑嘴
    d.ellipse([150, 210, 250, 250], fill=(255, 255, 255, 60))                   # 高光
    return img.convert("RGB")


def main():
    张数 = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    os.makedirs(OUT, exist_ok=True)
    出 = []
    for i in range(张数):
        底, 鱼, 腮 = 配色[i % len(配色)] if i < len(配色) else random.choice(配色)
        p = os.path.join(OUT, "avatar_%d.png" % (i + 1))
        画一张(底, 鱼, 腮).save(p, "PNG")
        出.append(p)
    for p in 出:
        print(p)


if __name__ == "__main__":
    main()
