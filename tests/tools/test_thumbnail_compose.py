"""thumbnail_compose sets title text deterministically over a background image."""

from PIL import Image

from tools.graphics.thumbnail_compose import ThumbnailCompose


def _background(tmp_path, size=(1600, 900), color="#FFFFFF"):
    path = tmp_path / "bg.png"
    Image.new("RGB", size, color).save(path)
    return path


def test_composes_youtube_sized_jpeg(tmp_path):
    out = tmp_path / "thumb.jpg"
    result = ThumbnailCompose().execute(
        {"background_path": str(_background(tmp_path)), "lines": ["big", "idea"], "output_path": str(out)}
    )
    assert result.success, result.error
    assert result.data["lines"] == ["BIG", "IDEA"]
    img = Image.open(out)
    assert img.size == (1280, 720)
    assert img.format == "JPEG"
    assert out.stat().st_size < 2 * 1024 * 1024


def test_text_is_drawn_in_text_color(tmp_path):
    out = tmp_path / "thumb.png"
    ThumbnailCompose().execute(
        {
            "background_path": str(_background(tmp_path)),
            "lines": ["IDEA"],
            "text_color": "#000000",
            "shadow": False,
            "output_path": str(out),
        }
    )
    colors = {c for _, c in Image.open(out).convert("RGB").getcolors(maxcolors=1_000_000)}
    assert (0, 0, 0) in colors


def test_longer_lines_get_smaller_font(tmp_path):
    bg = str(_background(tmp_path))
    tool = ThumbnailCompose()
    short = tool.execute({"background_path": bg, "lines": ["IDEA"], "output_path": str(tmp_path / "a.png")})
    long = tool.execute(
        {"background_path": bg, "lines": ["A MUCH LONGER TITLE LINE"], "output_path": str(tmp_path / "b.png")}
    )
    assert long.data["font_size"] < short.data["font_size"]


def test_missing_background_fails_cleanly(tmp_path):
    result = ThumbnailCompose().execute(
        {"background_path": str(tmp_path / "nope.png"), "lines": ["X"], "output_path": str(tmp_path / "o.jpg")}
    )
    assert not result.success
    assert "Background not found" in result.error


def test_logo_is_pasted_in_corner(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 255)).save(logo)
    out = tmp_path / "thumb.png"
    result = ThumbnailCompose().execute(
        {
            "background_path": str(_background(tmp_path)),
            "lines": ["IDEA"],
            "logo_path": str(logo),
            "logo_corner": "bottom_right",
            "output_path": str(out),
        }
    )
    assert result.success, result.error
    img = Image.open(out).convert("RGB")
    assert img.getpixel((1280 - 40, 720 - 40)) == (255, 0, 0)
