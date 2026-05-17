"""生成 PWA 所需的 PNG 图标。

输出（全部到 前端/）：
  favicon-192.png            192x192 (purpose: any)
  favicon-192-maskable.png   192x192 (purpose: maskable，含 12% 安全区)
  favicon-512.png            512x512 (purpose: any)
  favicon-512-maskable.png   512x512 (purpose: maskable)
  apple-touch-icon-180.png   180x180

由于环境无 cairosvg，这里直接用 Pillow 画一个简洁的品牌图：
  - 紫色渐变背景（取自 favicon.svg 的 #863bff / #7e14ff）
  - 中央白色"T"字母（站点 Txxx 的首字母）
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 站点品牌色（来自 favicon.svg）
BRAND = (134, 59, 255)   # #863bff
DEEP = (126, 20, 255)    # #7e14ff
ACCENT = (71, 191, 255)  # #47bfff
WHITE = (255, 255, 255)

OUT = Path(__file__).resolve().parent.parent / "前端"


def make_gradient(size: int) -> Image.Image:
    """竖直方向品牌色渐变。"""
    img = Image.new("RGBA", (size, size), BRAND + (255,))
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        # 从 BRAND 渐变到 DEEP
        r = int(BRAND[0] * (1 - t) + DEEP[0] * t)
        g = int(BRAND[1] * (1 - t) + DEEP[1] * t)
        b = int(BRAND[2] * (1 - t) + DEEP[2] * t)
        for x in range(size):
            px[x, y] = (r, g, b, 255)
    return img


def find_font(size: int) -> ImageFont.FreeTypeFont:
    """挑一个能渲染英文字母的字体。"""
    candidates = [
        # Windows 常见
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc",
        # 通用
        "/usr/share/fonts/truetype/dejavu/DejaVu-Sans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw_logo(img: Image.Image, *, safe_pad: float = 0.0) -> None:
    """在图上画一个中央的"T"字母 + 一个装饰圆点。

    safe_pad: 0~1，maskable 图标使用，让主体内缩。
    """
    w, h = img.size
    inner = int(w * (1 - safe_pad))
    cx, cy = w // 2, h // 2

    draw = ImageDraw.Draw(img)

    # 装饰圆点（左上一颗，右下一颗，呼应 favicon.svg 里的星点感）
    dot_r = int(inner * 0.05)
    draw.ellipse(
        (cx - inner * 0.32 - dot_r, cy - inner * 0.32 - dot_r,
         cx - inner * 0.32 + dot_r, cy - inner * 0.32 + dot_r),
        fill=ACCENT + (200,),
    )
    draw.ellipse(
        (cx + inner * 0.30 - dot_r * 0.7, cy + inner * 0.30 - dot_r * 0.7,
         cx + inner * 0.30 + dot_r * 0.7, cy + inner * 0.30 + dot_r * 0.7),
        fill=(237, 230, 255, 220),
    )

    # 中央"T"字母
    font_size = int(inner * 0.55)
    font = find_font(font_size)
    text = "T"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = cx - tw // 2 - bbox[0]
    ty = cy - th // 2 - bbox[1]
    # 阴影
    draw.text((tx + 2, ty + 3), text, font=font, fill=(0, 0, 0, 90))
    # 主体
    draw.text((tx, ty), text, font=font, fill=WHITE)


def build_icon(size: int, *, maskable: bool = False) -> Image.Image:
    img = make_gradient(size)
    safe_pad = 0.20 if maskable else 0.0  # maskable 留 ~10% 安全区（每边）
    draw_logo(img, safe_pad=safe_pad)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    targets = [
        ("favicon-192.png", 192, False),
        ("favicon-192-maskable.png", 192, True),
        ("favicon-512.png", 512, False),
        ("favicon-512-maskable.png", 512, True),
        ("apple-touch-icon-180.png", 180, False),
    ]
    for name, size, maskable in targets:
        img = build_icon(size, maskable=maskable)
        path = OUT / name
        img.save(path, format="PNG", optimize=True)
        print(f"wrote {path} ({size}x{size}, maskable={maskable})")


if __name__ == "__main__":
    main()
