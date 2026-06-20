"""Higgsfield image-to-3D (Multi-Image to 3D / Meshy) via the official `higgsfield` CLI.

Turns 1-4 object/product reference images into a 3D mesh asset (GLB-style). New
`capability="model_3d"` family — no selector yet, so the agent invokes this tool
directly via the registry.

Layer 3 knowledge: `.agents/skills/higgsfield-generate/` (3D section, media inputs).
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


class HiggsField3D(BaseTool):
    name = "higgsfield_3d"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "model_3d"
    provider = "higgsfield"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["cmd:higgsfield"]
    install_instructions = INSTALL_INSTRUCTIONS
    agent_skills = ["higgsfield-generate"]

    capabilities = ["image_to_3d", "mesh_generation"]
    supports = {"multi_image": True, "texturing": True, "glb_output": True}
    best_for = [
        "creating a real 3D mesh/GLB from 1-4 product or object reference photos",
        "turning a product shot into a rotatable/printable asset",
    ]
    not_good_for = [
        "pictures that merely look like a 3D render (use higgsfield_image instead)",
        "rigged/animated characters (use the character-animation pipeline)",
        "offline generation",
    ]

    input_schema = {
        "type": "object",
        "required": ["image"],
        "properties": {
            "image": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-4 reference image paths or UUIDs (front/side/back/detail).",
            },
            "should_texture": {"type": "boolean", "default": True, "description": "Generate textures for the mesh."},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=200, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["image", "should_texture"]
    side_effects = ["writes a 3D asset file to output_path", "calls the Higgsfield CLI"]
    user_visible_verification = ["Open the GLB in a 3D viewer; check geometry and texture fidelity"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if is_installed() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.25

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 180.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if not is_installed():
            return ToolResult(success=False, error="Higgsfield CLI not found. " + INSTALL_INSTRUCTIONS)

        refs = inputs.get("image") or []
        if isinstance(refs, str):
            refs = [refs]
        if not refs:
            return ToolResult(success=False, error="multi_image_to_3d requires at least one reference image.")
        if len(refs) > 4:
            return ToolResult(success=False, error="multi_image_to_3d accepts at most 4 reference images.")

        media = [("--image", ref) for ref in refs]
        params: dict[str, Any] = {"should_texture": bool(inputs.get("should_texture", True))}

        output_path = inputs.get("output_path", "higgsfield_model.glb")
        start = time.time()
        result = run_generate(
            "multi_image_to_3d",
            media=media,
            params=params,
            output_path=output_path,
            output_ext=".glb",
            wait_timeout="30m",
        )

        if not result.success:
            return ToolResult(success=False, error=result.error)

        out = Path(result.output_path or output_path)
        return ToolResult(
            success=True,
            data={
                "provider": "higgsfield",
                "model": "multi_image_to_3d",
                "image_count": len(refs),
                "textured": bool(inputs.get("should_texture", True)),
                "job_id": result.job_id,
                "result_url": result.result_url,
                "output": str(out),
                "output_path": str(out),
                "format": out.suffix.lstrip(".") or "glb",
            },
            artifacts=[str(out)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model="multi_image_to_3d",
        )
