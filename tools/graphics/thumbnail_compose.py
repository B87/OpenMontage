"""Thumbnail compositor: deterministic title text over a generated background.

Image models misspell on-image text, so thumbnails are made in two steps:
generate a text-free background, then set the words here with a real font.
Produces a YouTube-ready 1280x720 image (or any size) with heavy outlined
text, an optional accent line and an optional logo. No external services.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolStatus,
    ToolTier,
)

_POSITIONS = ("center", "left", "right")
_LOGO_CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")


class ThumbnailCompose(BaseTool):
    name = "thumbnail_compose"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "graphics"
    provider = "pillow"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["python:PIL"]
    install_instructions = "pip install Pillow"
    agent_skills = []

    capabilities = ["compose_thumbnail", "text_overlay"]
    best_for = [
        "video thumbnails with short bold titles over a generated background",
        "guaranteed-correct spelling of on-image text",
    ]
    not_good_for = ["generating the background artwork (use an image_generation tool first)"]

    input_schema = {
        "type": "object",
        "required": ["background_path", "lines", "output_path"],
        "properties": {
            "background_path": {"type": "string", "description": "Text-free background image; cover-cropped to size."},
            "lines": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
                "description": "Title lines, top to bottom. Keep to 2-3 words total for thumbnails.",
            },
            "output_path": {"type": "string", "description": ".jpg/.jpeg (recommended, <2MB) or .png"},
            "font_path": {"type": "string", "description": "TTF/OTF file. Falls back to Pillow's default font."},
            "width": {"type": "integer", "default": 1280},
            "height": {"type": "integer", "default": 720},
            "position": {"type": "string", "enum": list(_POSITIONS), "default": "center"},
            "uppercase": {"type": "boolean", "default": True},
            "text_color": {"type": "string", "default": "#111111"},
            "stroke_color": {"type": "string", "default": "#FFFFFF"},
            "stroke_ratio": {
                "type": "number",
                "default": 0.08,
                "description": "Outline width as a fraction of the font size.",
            },
            "accent_line": {"type": "integer", "description": "0-based index of a line drawn in accent_color."},
            "accent_color": {"type": "string", "default": "#F59E0B"},
            "max_text_width": {
                "type": "number",
                "default": 0.42,
                "description": "Max width of the text block as a fraction of the image width.",
            },
            "max_text_height": {"type": "number", "default": 0.8},
            "shadow": {"type": "boolean", "default": True, "description": "Soft drop shadow behind the text."},
            "logo_path": {"type": "string", "description": "Optional transparent PNG logo."},
            "logo_corner": {"type": "string", "enum": list(_LOGO_CORNERS), "default": "bottom_right"},
            "logo_size": {"type": "number", "default": 0.12, "description": "Logo height as a fraction of image height."},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=20)
    idempotency_key_fields = ["background_path", "lines", "font_path", "position", "accent_line"]
    side_effects = ["writes image to output_path"]
    user_visible_verification = ["Check the title is legible at small size and doesn't cover the key visual"]

    def get_status(self) -> ToolStatus:
        try:
            from PIL import Image  # noqa: F401
            return ToolStatus.AVAILABLE
        except ImportError:
            return ToolStatus.UNAVAILABLE

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
        except ImportError:
            return ToolResult(success=False, error="Pillow required. Run: pip install Pillow")

        start = time.time()
        bg_path = Path(inputs["background_path"])
        if not bg_path.exists():
            return ToolResult(success=False, error=f"Background not found: {bg_path}")
        lines = [ln for ln in inputs["lines"] if ln.strip()]
        if not lines:
            return ToolResult(success=False, error="At least one non-empty line is required")
        if inputs.get("uppercase", True):
            lines = [ln.upper() for ln in lines]

        width = int(inputs.get("width", 1280))
        height = int(inputs.get("height", 720))
        position = inputs.get("position", "center")
        if position not in _POSITIONS:
            return ToolResult(success=False, error=f"position must be one of {_POSITIONS}")
        font_path = inputs.get("font_path")
        if font_path and not Path(font_path).exists():
            return ToolResult(success=False, error=f"Font not found: {font_path}")

        def load_font(size: int):
            if font_path:
                return ImageFont.truetype(font_path, size)
            return ImageFont.load_default(size=size)

        canvas = ImageOps.fit(Image.open(bg_path).convert("RGB"), (width, height), Image.LANCZOS)

        # Largest font size where every line fits the width and the block fits the height.
        stroke_ratio = float(inputs.get("stroke_ratio", 0.08))
        max_w = width * float(inputs.get("max_text_width", 0.42))
        max_h = height * float(inputs.get("max_text_height", 0.8))

        def measure(size: int):
            font = load_font(size)
            stroke = max(1, round(size * stroke_ratio))
            boxes = [font.getbbox(ln, stroke_width=stroke) for ln in lines]
            gap = round(size * 0.06)
            block_w = max(b[2] - b[0] for b in boxes)
            block_h = sum(b[3] - b[1] for b in boxes) + gap * (len(lines) - 1)
            return font, stroke, boxes, gap, block_w, block_h

        lo, hi = 8, height
        while lo < hi:
            mid = (lo + hi + 1) // 2
            *_, bw, bh = measure(mid)
            if bw <= max_w and bh <= max_h:
                lo = mid
            else:
                hi = mid - 1
        font, stroke, boxes, gap, block_w, block_h = measure(lo)

        margin = round(width * 0.05)
        if position == "left":
            center_x = margin + block_w / 2
        elif position == "right":
            center_x = width - margin - block_w / 2
        else:
            center_x = width / 2
        y = (height - block_h) / 2

        text_color = inputs.get("text_color", "#111111")
        stroke_color = inputs.get("stroke_color", "#FFFFFF")
        accent_line = inputs.get("accent_line")
        accent_color = inputs.get("accent_color", "#F59E0B")

        placements = []
        for i, (ln, box) in enumerate(zip(lines, boxes)):
            x = center_x - (box[2] - box[0]) / 2 - box[0]
            placements.append((ln, x, y - box[1], accent_color if i == accent_line else text_color))
            y += (box[3] - box[1]) + gap

        if inputs.get("shadow", True):
            shadow = Image.new("L", (width, height), 0)
            sdraw = ImageDraw.Draw(shadow)
            offset = max(2, round(lo * 0.03))
            for ln, x, ty, _ in placements:
                sdraw.text((x + offset, ty + offset), ln, font=font, fill=150, stroke_width=stroke, stroke_fill=150)
            shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, lo * 0.04)))
            canvas = Image.composite(Image.new("RGB", (width, height), "black"), canvas, shadow)

        draw = ImageDraw.Draw(canvas)
        for ln, x, ty, color in placements:
            draw.text((x, ty), ln, font=font, fill=color, stroke_width=stroke, stroke_fill=stroke_color)

        logo_path = inputs.get("logo_path")
        if logo_path:
            if not Path(logo_path).exists():
                return ToolResult(success=False, error=f"Logo not found: {logo_path}")
            logo = Image.open(logo_path).convert("RGBA")
            lh = round(height * float(inputs.get("logo_size", 0.12)))
            logo = logo.resize((round(logo.width * lh / logo.height), lh), Image.LANCZOS)
            pad = round(height * 0.04)
            corner = inputs.get("logo_corner", "bottom_right")
            lx = pad if "left" in corner else width - logo.width - pad
            ly = pad if "top" in corner else height - logo.height - pad
            canvas.paste(logo, (lx, ly), logo)

        out = Path(inputs["output_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() in (".jpg", ".jpeg"):
            canvas.save(out, "JPEG", quality=92, optimize=True)
        else:
            canvas.save(out)

        return ToolResult(
            success=True,
            data={
                "output_path": str(out),
                "width": width,
                "height": height,
                "font_size": lo,
                "lines": lines,
                "file_size_bytes": out.stat().st_size,
            },
            artifacts=[str(out)],
            duration_seconds=round(time.time() - start, 2),
        )
