"""Higgsfield image generation via the official `higgsfield` CLI.

One wrapper over Higgsfield's image lineup: GPT Image 2 (default high-fidelity /
text-in-image), the Nano Banana family (character/cartoon), Soul models (editorial,
cinematic, locations, identity-faithful with `--soul-id`), Recraft (vector/logos),
Flux, Seedream, and Z Image. Auto-routes through `image_selector` because it
declares `capability="image_generation"`.

Layer 3 knowledge: `.agents/skills/higgsfield-generate/` (model catalog, media
inputs) and `.agents/skills/higgsfield-soul-id/` for `--soul-id` workflows.
"""

from __future__ import annotations

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


# job_set_type -> traits. `media` = accepts --image references; `soul` = accepts --soul-id.
MODELS = {
    "gpt_image_2": {"name": "GPT Image 2", "media": True, "soul": False, "cost": 0.08},
    "nano_banana_2": {"name": "Nano Banana 2", "media": True, "soul": False, "cost": 0.04},
    "nano_banana_pro": {"name": "Nano Banana Pro", "media": True, "soul": False, "cost": 0.08},
    "text2image_soul_v2": {"name": "Higgsfield Soul 2.0", "media": True, "soul": True, "cost": 0.06},
    "soul_cinematic": {"name": "Soul Cinema", "media": True, "soul": True, "cost": 0.08},
    "soul_location": {"name": "Soul Location", "media": False, "soul": False, "cost": 0.06},
    "recraft_v4_1": {"name": "Recraft V4.1", "media": False, "soul": False, "cost": 0.05},
    "flux_2": {"name": "Flux 2.0", "media": True, "soul": False, "cost": 0.06},
    "seedream_v4_5": {"name": "Seedream 4.5", "media": True, "soul": False, "cost": 0.05},
    "z_image": {"name": "Z Image", "media": False, "soul": False, "cost": 0.02},
    "kling_o1_image": {"name": "Kling O1 Image", "media": True, "soul": False, "cost": 0.05},
}


class HiggsFieldImage(BaseTool):
    name = "higgsfield_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "higgsfield"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:higgsfield"]
    install_instructions = INSTALL_INSTRUCTIONS
    agent_skills = ["higgsfield-generate", "higgsfield-soul-id", "flux-best-practices"]

    capabilities = ["text_to_image", "image_to_image", "edit_image", "generate_illustration"]
    supports = {
        "text_to_image": True,
        "image_to_image": True,
        "multi_reference": True,
        "on_image_text": True,
        "vector_output": True,
        "soul_identity": True,
        "character_style": True,
    }
    best_for = [
        "high-fidelity general images, graphic design, UI, banners, on-image text (GPT Image 2)",
        "character / cartoon / stylized work (Nano Banana 2 / Pro)",
        "editorial, cinematic, location, and identity-faithful stills (Soul models + --soul-id)",
        "logos, icons, vector marks, controlled palettes (Recraft V4.1, model_type=vector)",
        "multi-reference image editing and style transfer",
    ]
    not_good_for = [
        "marketplace listing cards (use the higgsfield-marketplace-cards skill flow)",
        "branded product photoshoots (use the higgsfield-product-photoshoot skill flow)",
        "offline generation",
    ]
    fallback_tools = ["flux_image", "openai_image", "recraft_image", "google_imagen"]
    quality_score = 0.85

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {
                "type": "string",
                "enum": list(MODELS.keys()),
                "default": "gpt_image_2",
                "description": "CLI job_set_type. Default GPT Image 2 (high-fidelity general).",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"],
                "default": "1:1",
            },
            "resolution": {"type": "string", "description": "e.g. 1k/2k (GPT Image 2 etc.)."},
            "quality": {"type": "string", "description": "Soul-only UI tier: 1.5k or 2k."},
            "image": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Reference image paths or UUIDs (multi-reference editing).",
            },
            "soul_id": {"type": "string", "description": "Soul Character reference id from higgsfield-soul-id."},
            "model_type": {"type": "string", "description": "Recraft only: 'vector' or 'standard'."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model", "aspect_ratio", "soul_id"]
    side_effects = ["writes image file to output_path", "calls the Higgsfield CLI"]
    user_visible_verification = ["Inspect generated image for relevance, fidelity, and any on-image text"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if is_installed() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return MODELS.get(inputs.get("model", "gpt_image_2"), {}).get("cost", 0.06)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 45.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not is_installed():
            return ToolResult(success=False, error="Higgsfield CLI not found. " + INSTALL_INSTRUCTIONS)

        model = inputs.get("model", "gpt_image_2")
        traits = MODELS.get(model, {"media": True, "soul": False})

        media: list[tuple[str, str]] = []
        refs = inputs.get("image") or []
        if isinstance(refs, str):
            refs = [refs]
        if refs and not traits["media"]:
            return ToolResult(
                success=False,
                error=f"{traits.get('name', model)} is prompt-only and does not accept reference images.",
            )
        for ref in refs:
            media.append(("--image", ref))

        params: dict[str, Any] = {}
        if inputs.get("aspect_ratio"):
            params["aspect_ratio"] = inputs["aspect_ratio"]
        if inputs.get("resolution"):
            params["resolution"] = inputs["resolution"]
        if inputs.get("quality") and traits.get("soul"):
            params["quality"] = inputs["quality"]
        if inputs.get("model_type") and model == "recraft_v4_1":
            params["model_type"] = inputs["model_type"]

        extra_flags: list[str] = []
        if inputs.get("soul_id"):
            if not traits.get("soul"):
                return ToolResult(
                    success=False,
                    error=f"{traits.get('name', model)} does not accept a --soul-id. Use text2image_soul_v2 or soul_cinematic.",
                )
            extra_flags += ["--soul-id", inputs["soul_id"]]

        output_path = inputs.get("output_path", "higgsfield_image.png")
        start = time.time()
        result = run_generate(
            model,
            prompt=inputs["prompt"],
            media=media or None,
            params=params,
            extra_flags=extra_flags or None,
            output_path=output_path,
            output_ext=".png",
            wait_timeout="10m",
        )

        if not result.success:
            return ToolResult(success=False, error=result.error)

        out = Path(result.output_path or output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "higgsfield",
                "model": result.model or model,
                "prompt": inputs["prompt"],
                "aspect_ratio": inputs.get("aspect_ratio", "1:1"),
                "soul_id": inputs.get("soul_id"),
                "job_id": result.job_id,
                "result_url": result.result_url,
                "adjustments": result.adjustments,
                "output": str(out),
                "output_path": str(out),
                "format": out.suffix.lstrip(".") or "png",
            },
            artifacts=[str(out)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=result.model or model,
        )
