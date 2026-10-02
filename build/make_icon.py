# -*- coding: utf-8 -*-
"""生成程序图标 app.ico(用 DSH 运行时自带的 Pillow)。

画一个「芯片」图案:深蓝圆角底 + 白色芯片本体 + 两侧引脚。
不依赖任何字体文件,避免不同机器上文字渲染不一致。
"""
import os
import sys

from PIL import Image, ImageDraw

BLUE = (21, 94, 168, 255)
BLUE_DARK = (14, 68, 124, 255)
WHITE = (255, 255, 255, 255)

S = 256


def rounded(draw, box, radius, fill):
    """Pillow 老版本没有 rounded_rectangle,做个降级。"""
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill)
    except AttributeError:
        draw.rectangle(box, fill=fill)


def build():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    rounded(d, [6, 6, S - 6, S - 6], 46, BLUE)          # 底板
    rounded(d, [16, 16, S - 16, S - 16], 38, BLUE_DARK)  # 内描边

    # 引脚:左右各 3 根
    for i in range(3):
        y = 96 + i * 32
        d.rectangle([54, y - 5, 82, y + 5], fill=WHITE)
        d.rectangle([S - 82, y - 5, S - 54, y + 5], fill=WHITE)

    # 芯片本体
    rounded(d, [82, 74, S - 82, S - 74], 14, WHITE)

    # 芯片上的小方块(象征元件位)
    d.rectangle([104, 104, 152, 152], fill=BLUE)
    d.rectangle([112, 112, 144, 144], fill=WHITE)

    return img


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "app.ico"
    img = build()
    # 一次写出多尺寸,Windows 在各处缩放才清晰
    img.save(out, format="ICO",
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("已生成 %s (%d B)" % (out, os.path.getsize(out)))


if __name__ == "__main__":
    main()
