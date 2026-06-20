"""Higgsfield Marketing Studio (branded ads) via the official `higgsfield` CLI.

Generates branded ad video/image from a prompt plus brand context: products
(by id or URL), optional presenter avatars, and optional setup hook/setting blocks.
Distinct `capability="marketing_ad"` so it stays out of the generic video_selector —
this is RAG-over-brand-assets generation, not plain clip synthesis.

Layer 3 knowledge: `.agents/skills/higgsfield-generate/` (Marketing Studio section)
and `references/marketing-*.md`. This tool covers the common one-shot path; for
avatar/product/hook discovery and creation use the CLI commands those references
document (`higgsfield marketing-studio ...`).
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from tools._higgsfield_cli import INSTALL_INSTRUCTIONS, is_installed, run_generate
from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


VIDEO_MODES = {
    "ugc",
    "ugc_how_to",
    "ugc_unboxing",
    "product_showcase",
    "product_review",
    "tv_spot",
    "wild_card",
    "ugc_virtual_try_on",
    "virtual_try_on",
}
# Setup hook/setting blocks are only valid for these modes.
HOOK_SETTING_MODES = {"ugc", "ugc_how_to", "ugc_unboxing", "product_review", "ugc_virtual_try_on"}


class HiggsFieldMarketing(BaseTool):
    name = "higgsfield_marketing"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "marketing_ad"
    provider = "higgsfield"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:higgsfield"]
    install_instructions = INSTALL_INSTRUCTIONS
    agent_skills = ["higgsfield-generate", "higgsfield-product-photoshoot"]

    capabilities = ["branded_ad_video", "branded_ad_image", "ugc_ad", "product_demo"]
    supports = {
        "product_context": True,
        "avatar_presenter": True,
        "hook_setting_blocks": True,
        "url_to_ad": True,
        "generate_audio": True,
    }
    best_for = [
        "UGC ads, unboxings, product showcases, TV spots, and presenter-led brand video",
        "click-to-ad from a product URL (backend fetches and reuses the product entity)",
        "branded ad stills via marketing_studio_image",
    ]
    not_good_for = [
        "generic non-branded clips (use higgsfield_video)",
        "marketplace listing cards (use the higgsfield-marketplace-cards skill flow)",
        "offline generation",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "format": {
                "type": "string",
                "enum": ["video", "image"],
                "default": "video",
                "description": "video -> marketing_studio_video; image -> marketing_studio_image.",
            },
            "mode": {
                "type": "string",
                "default": "ugc",
                "description": "Video ad mode (ugc, ugc_unboxing, product_showcase, tv_spot, ...). Video only.",
            },
            "product_id": {"type": "string", "description": "Existing product id (from marketing-studio products list)."},
            "product_url": {"type": "string", "description": "Product URL for click-to-ad; backend fetches/dedupes it."},
            "avatar_id": {"type": "string", "description": "Preset avatar id (from marketing-studio avatars list)."},
            "avatar_type": {"type": "string", "enum": ["preset", "custom"], "default": "preset"},
            "hook_id": {"type": "string", "description": "Setup hook id. Valid only for hook/setting-capable modes. Video only."},
            "setting_id": {"type": "string", "description": "Setup setting id. Valid only for hook/setting-capable modes. Video only."},
            "duration": {"type": "integer", "default": 15, "description": "Seconds (video)."},
            "aspect_ratio": {
                "type": "string",
                "enum": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
                "default": "9:16",
            },
            "resolution": {"type": "string", "enum": ["480p", "720p", "2k"], "description": "480p/720p (video) or 2k (image)."},
            "generate_audio": {"type": "boolean", "default": False, "description": "Add generated audio (video only)."},
            "start_image": {"type": "string"},
            "end_image": {"type": "string"},
            "image": {"type": "string"},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "format", "mode", "product_id", "product_url", "aspect_ratio"]
    side_effects = ["writes ad asset to output_path", "calls the Higgsfield CLI"]
    user_visible_verification = ["Review the ad for brand fit, product accuracy, and hook strength"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if is_installed() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        if inputs.get("format", "video") == "image":
            return 0.10
        return round(0.40 * (int(inputs.get("duration", 15) or 15) / 15), 2)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 240.0 if inputs.get("format", "video") == "video" else 60.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not is_installed():
            return ToolResult(success=False, error="Higgsfield CLI not found. " + INSTALL_INSTRUCTIONS)

        fmt = inputs.get("format", "video")
        is_video = fmt == "video"
        model = "marketing_studio_video" if is_video else "marketing_studio_image"

        params: dict[str, Any] = {}
        if inputs.get("aspect_ratio"):
            params["aspect_ratio"] = inputs["aspect_ratio"]
        if inputs.get("resolution"):
            params["resolution"] = inputs["resolution"]

        media: list[tuple[str, str]] = []
        for key, flag in (("start_image", "--start-image"), ("end_image", "--end-image"), ("image", "--image")):
            if inputs.get(key):
                media.append((flag, inputs[key]))

        extra_flags: list[str] = []
        temp_files: list[str] = []

        # product_ids and avatars are JSON arrays passed via @file.
        if inputs.get("product_id"):
            pf = self._write_json_temp([inputs["product_id"]])
            temp_files.append(pf)
            extra_flags += ["--product_ids", f"@{pf}"]
        if inputs.get("product_url"):
            extra_flags += ["--url", inputs["product_url"]]
        if inputs.get("avatar_id"):
            af = self._write_json_temp([{"id": inputs["avatar_id"], "type": inputs.get("avatar_type", "preset")}])
            temp_files.append(af)
            extra_flags += ["--avatars", f"@{af}"]

        if is_video:
            mode = inputs.get("mode", "ugc")
            if mode not in VIDEO_MODES:
                self._cleanup(temp_files)
                return ToolResult(
                    success=False,
                    error=f"Unknown marketing mode '{mode}'. Valid: {', '.join(sorted(VIDEO_MODES))}.",
                )
            params["mode"] = mode
            if inputs.get("duration"):
                params["duration"] = int(inputs["duration"])
            if inputs.get("generate_audio"):
                params["generate-audio"] = True
            # Hook/setting blocks: only some modes accept them.
            if inputs.get("hook_id") or inputs.get("setting_id"):
                if mode not in HOOK_SETTING_MODES:
                    self._cleanup(temp_files)
                    return ToolResult(
                        success=False,
                        error=f"hook_id/setting_id are not valid for mode '{mode}'. Use one of: {', '.join(sorted(HOOK_SETTING_MODES))}.",
                    )
                if inputs.get("hook_id"):
                    extra_flags += ["--hook_id", inputs["hook_id"]]
                if inputs.get("setting_id"):
                    extra_flags += ["--setting_id", inputs["setting_id"]]

        output_path = inputs.get("output_path", "higgsfield_ad.mp4" if is_video else "higgsfield_ad.png")
        output_ext = ".mp4" if is_video else ".png"

        start = time.time()
        try:
            result = run_generate(
                model,
                prompt=inputs["prompt"],
                media=media or None,
                params=params,
                extra_flags=extra_flags or None,
                output_path=output_path,
                output_ext=output_ext,
                wait_timeout="30m",
            )
        finally:
            self._cleanup(temp_files)

        if not result.success:
            return ToolResult(success=False, error=result.error)

        out = Path(result.output_path or output_path)
        data: dict[str, Any] = {
            "provider": "higgsfield",
            "model": model,
            "format": fmt,
            "prompt": inputs["prompt"],
            "aspect_ratio": inputs.get("aspect_ratio"),
            "job_id": result.job_id,
            "result_url": result.result_url,
            "adjustments": result.adjustments,
            "output": str(out),
            "output_path": str(out),
        }
        if is_video:
            data["mode"] = inputs.get("mode", "ugc")
            from tools.video._shared import probe_output

            if out.exists():
                data.update(probe_output(out))

        return ToolResult(
            success=True,
            data=data,
            artifacts=[str(out)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )

    @staticmethod
    def _write_json_temp(payload: Any) -> str:
        fd, path = tempfile.mkstemp(suffix=".json", prefix="higgsfield_ms_")
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    @staticmethod
    def _cleanup(paths: list[str]) -> None:
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
