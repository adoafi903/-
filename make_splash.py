"""Draw the start-up picture shown while the Windows app unpacks."""
import os
from PIL import Image, ImageDraw, ImageFont

im = Image.new("RGB", (520, 200), (238, 242, 244))
d = ImageDraw.Draw(im)
d.rectangle([0, 0, 519, 199], outline=(12, 110, 134), width=3)
font = small = None
for name in ("YuGothB.ttc", "meiryob.ttc", "meiryo.ttc", "msgothic.ttc"):
    path = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", name)
    if os.path.exists(path):
        font, small = ImageFont.truetype(path, 30), ImageFont.truetype(path, 17)
        break
if font:
    d.text((36, 58), "消えた写真・動画の復元", font=font, fill=(20, 33, 43))
    d.text((38, 118), "起動しています。少しお待ちください…", font=small, fill=(85, 102, 114))
else:
    d.text((36, 80), "Fukugen - starting...", fill=(20, 33, 43))
im.save("splash.png")
print("splash.png written")
