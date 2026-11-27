import io

import pytest
from PIL import Image

from crawler import images


def picture(width, height, fmt="PNG", frames=1) -> bytes:
    buf = io.BytesIO()
    first = Image.new("RGB", (width, height), "white")
    if frames > 1:
        first.save(buf, fmt, save_all=True, append_images=[Image.new("RGB", (width, height), "black")] * (frames - 1))
    else:
        first.save(buf, fmt)
    return buf.getvalue()


def test_long_image_is_narrowed_and_cut_from_the_top():
    out = images.slices(picture(2000, 7000))              # 1000 x 3500 -> 1500, 1500, 500
    sizes = [Image.open(io.BytesIO(b)).size for b in out]
    assert sizes == [(1000, 1500), (1000, 1500), (1000, 500)]
    assert all(Image.open(io.BytesIO(b)).format == "JPEG" for b in out)


def test_limit_and_animated_gif_and_non_image():
    assert len(images.slices(picture(800, 9000), limit=2)) == 2
    assert len(images.slices(picture(300, 200, "GIF", frames=3))) == 1     # 움직이는 GIF 는 첫 장
    with pytest.raises(ValueError):
        images.slices(b"<html>not an image</html>")
