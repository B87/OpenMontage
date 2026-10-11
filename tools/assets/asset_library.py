"""Reusable asset library: keep approved generated media and reuse it later.

Generating images, clips and music costs money every time. Once an asset has
been approved in a production, it goes into a library so future videos can
search for it and reuse it at zero cost.

Layout (``library_dir``, default ``channels/<channel>/library``)::

    index.jsonl            # one record per asset (description, tags, provenance, usage)
    media/<type>/<id>.<ext>

Design:
- **The index is the product.** Each record carries a human-quality
  description written by an agent that viewed the asset, plus tags, subjects
  and full provenance (prompt, model, provider, cost, source project/scene),
  so search works without embeddings and a reuse is auditable.
- **Content-addressed dedupe.** Ingesting the same bytes twice merges metadata
  instead of storing a second copy.
- **Hard links first, copies as fallback**, both into the library and back out
  into projects, so the library survives project deletion without doubling disk.
- **Usage tracking.** ``use`` records which project reused an asset; ``stats``
  reports the generation cost those reuses avoided.
- The index is rewritten atomically (temp file + ``os.replace``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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

ASSET_TYPES = ("image", "video", "music", "sfx", "audio", "render", "other")
STATUSES = ("approved", "candidate", "deprecated")
# asset_manifest types that are script-specific and rarely reusable
_SKIP_BY_DEFAULT = {"narration", "tts", "voiceover", "subtitle", "subtitles", "caption", "captions"}
_MANIFEST_TYPE_MAP = {
    "image": "image", "still": "image", "illustration": "image", "diagram": "image", "thumbnail": "image",
    "video": "video", "clip": "video", "broll": "video", "animation": "video",
    "music": "music", "sfx": "sfx", "sound_effect": "sfx", "audio": "audio",
    "render": "render", "composition": "render",
}
_WORD = re.compile(r"[a-z0-9]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _image_info(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
    except Exception:
        return {}
    return {"width": w, "height": h, "aspect_ratio": _ratio(w, h)}


def _ratio(w: int, h: int) -> str:
    r = w / h if h else 0
    for name, val in (("16:9", 16 / 9), ("9:16", 9 / 16), ("1:1", 1.0), ("4:3", 4 / 3), ("3:4", 3 / 4), ("21:9", 21 / 9)):
        if abs(r - val) < 0.03:
            return name
    return f"{w}:{h}"


class Library:
    """File-backed asset index. Pure storage + search; no judgement."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.index_path = self.root / "index.jsonl"
        self.records: list[dict[str, Any]] = []
        if self.index_path.exists():
            with open(self.index_path, encoding="utf-8") as f:
                self.records = [json.loads(line) for line in f if line.strip()]

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".index.", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in self.records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, self.index_path)

    def get(self, asset_id: str) -> dict[str, Any] | None:
        return next((r for r in self.records if r["id"] == asset_id), None)

    def media_path(self, record: dict[str, Any]) -> Path:
        return self.root / record["file"]

    def ingest(self, source: Path, *, type: str, description: str, **meta: Any) -> tuple[dict[str, Any], bool]:
        """Add a file. Returns (record, created). Same bytes → merged into the existing record."""
        if type not in ASSET_TYPES:
            raise ValueError(f"type must be one of {ASSET_TYPES}")
        if not description or not description.strip():
            raise ValueError("description is required: say what the asset shows, so it can be found later")
        digest = _sha256(source)
        existing = next((r for r in self.records if r["sha256"] == digest), None)
        if existing:
            existing["tags"] = sorted(set(existing.get("tags", [])) | set(meta.get("tags") or []))
            existing["subjects"] = sorted(set(existing.get("subjects", [])) | set(meta.get("subjects") or []))
            return existing, False

        asset_id = f"{type}-{digest[:12]}"
        rel = Path("media") / type / f"{asset_id}{source.suffix.lower()}"
        _link_or_copy(source, self.root / rel)
        record: dict[str, Any] = {
            "id": asset_id,
            "type": type,
            "file": str(rel),
            "sha256": digest,
            "description": description.strip(),
            "tags": sorted(set(meta.get("tags") or [])),
            "subjects": sorted(set(meta.get("subjects") or [])),
            "status": meta.get("status") or "approved",
            "provenance": {
                k: meta[k]
                for k in ("prompt", "model", "provider", "source_tool", "seed", "cost_usd", "source_project",
                          "scene_id", "license", "style_reference", "notes")
                if meta.get(k) is not None
            },
            "added_at": _now(),
            "usage": [],
        }
        if type == "image":
            record.update(_image_info(self.root / rel))
        for k in ("duration_seconds", "aspect_ratio", "width", "height"):
            if meta.get(k) is not None:
                record[k] = meta[k]
        self.records.append(record)
        return record, True

    def search(
        self,
        query: str = "",
        *,
        types: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        aspect_ratio: str | None = None,
        include_deprecated: bool = False,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        types = set(types or [])
        tags = set(tags or [])
        q = set(_tokens(query))
        scored = []
        for r in self.records:
            if r.get("status") == "deprecated" and not include_deprecated:
                continue
            if types and r["type"] not in types:
                continue
            if tags and not tags <= set(r.get("tags", [])):
                continue
            if aspect_ratio and r.get("aspect_ratio") != aspect_ratio:
                continue
            if q:
                # Weighted token overlap: subjects/tags matter most, then description, then prompt.
                fields = (
                    (3.0, " ".join(r.get("subjects", []) + r.get("tags", []))),
                    (2.0, r.get("description", "")),
                    (1.0, r.get("provenance", {}).get("prompt", "")),
                )
                score = sum(w * len(q & set(_tokens(text))) for w, text in fields) / len(q)
                if score == 0:
                    continue
            else:
                score = 0.0
            scored.append((score, len(r.get("usage", [])), r))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [dict(r, score=round(s, 3), path=str(self.media_path(r))) for s, _, r in scored[:limit]]

    def use(self, asset_id: str, dest: Path, project: str | None) -> dict[str, Any]:
        record = self.get(asset_id)
        if record is None:
            raise KeyError(f"Unknown asset id: {asset_id}")
        if record.get("status") == "deprecated":
            raise ValueError(f"{asset_id} is deprecated; pick another asset or un-deprecate it first")
        if dest.suffix == "":
            dest = dest / Path(record["file"]).name
        _link_or_copy(self.media_path(record), dest)
        record.setdefault("usage", []).append({"project": project, "path": str(dest), "used_at": _now()})
        return record

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        generated = saved = 0.0
        reuses = 0
        for r in self.records:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
            cost = float(r.get("provenance", {}).get("cost_usd") or 0)
            generated += cost
            saved += cost * len(r.get("usage", []))
            reuses += len(r.get("usage", []))
        return {
            "assets": len(self.records),
            "by_type": by_type,
            "reuses": reuses,
            "original_generation_cost_usd": round(generated, 2),
            "cost_avoided_by_reuse_usd": round(saved, 2),
        }


class AssetLibrary(BaseTool):
    name = "asset_library"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "asset_management"
    provider = "local"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = []
    install_instructions = "No setup needed."
    agent_skills = []

    capabilities = ["ingest_asset", "search_assets", "reuse_asset", "library_stats"]
    best_for = [
        "reusing approved images, clips, music, SFX and renders across videos to avoid paying twice",
        "searching past assets by description, subjects, tags and aspect ratio",
    ]
    not_good_for = ["stock footage retrieval (use corpus_builder / clip_search)"]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["search", "ingest", "ingest_manifest", "use", "update", "get", "stats"],
            },
            "channel": {"type": "string", "description": "Library defaults to channels/<channel>/library."},
            "library_dir": {"type": "string", "description": "Explicit library root (overrides channel)."},
            # search
            "query": {"type": "string"},
            "types": {"type": "array", "items": {"type": "string", "enum": list(ASSET_TYPES)}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "aspect_ratio": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
            # ingest
            "source_path": {"type": "string"},
            "type": {"type": "string", "enum": list(ASSET_TYPES)},
            "description": {"type": "string", "description": "What the asset shows/sounds like. Write it after viewing it."},
            "subjects": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string", "enum": list(STATUSES)},
            "prompt": {"type": "string"},
            "model": {"type": "string"},
            "provider": {"type": "string"},
            "cost_usd": {"type": "number"},
            "source_project": {"type": "string"},
            "scene_id": {"type": "string"},
            "license": {"type": "string"},
            "duration_seconds": {"type": "number"},
            # ingest_manifest
            "project_dir": {"type": "string", "description": "projects/<id>; reads artifacts/asset_manifest.json."},
            "asset_ids": {"type": "array", "items": {"type": "string"}, "description": "Only these manifest ids."},
            "descriptions": {
                "type": "object",
                "description": "Manifest asset id -> description (preferred over the manifest's own summary).",
            },
            "include_narration": {"type": "boolean", "default": False},
            # use / update / get
            "asset_id": {"type": "string"},
            "dest_path": {"type": "string", "description": "File path or directory inside the project."},
            "project": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=0)
    idempotency_key_fields = ["operation", "source_path", "asset_id", "query"]
    side_effects = ["writes media and index.jsonl in the library", "links/copies assets into projects"]
    user_visible_verification = ["Reused assets fit the new scene (art-director checks at first_images)"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    @staticmethod
    def _root(inputs: dict[str, Any]) -> Path:
        if inputs.get("library_dir"):
            return Path(inputs["library_dir"])
        if inputs.get("channel"):
            return Path("channels") / inputs["channel"] / "library"
        raise ValueError("channel or library_dir is required")

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        op = inputs["operation"]
        try:
            lib = Library(self._root(inputs))
            if op == "search":
                data = {"results": lib.search(
                    inputs.get("query", ""), types=inputs.get("types"), tags=inputs.get("tags"),
                    aspect_ratio=inputs.get("aspect_ratio"), limit=int(inputs.get("limit", 10)),
                )}
            elif op == "get":
                record = lib.get(inputs["asset_id"])
                if record is None:
                    return ToolResult(success=False, error=f"Unknown asset id: {inputs['asset_id']}")
                data = dict(record, path=str(lib.media_path(record)))
            elif op == "stats":
                data = lib.stats()
            elif op == "ingest":
                src = Path(inputs["source_path"])
                if not src.exists():
                    return ToolResult(success=False, error=f"File not found: {src}")
                meta = {k: v for k, v in inputs.items() if k not in ("operation", "source_path", "type", "description")}
                record, created = lib.ingest(src, type=inputs.get("type", ""), description=inputs.get("description", ""), **meta)
                lib.save()
                data = {"asset": record, "created": created}
            elif op == "ingest_manifest":
                data = self._ingest_manifest(lib, inputs)
                lib.save()
            elif op == "use":
                record = lib.use(inputs["asset_id"], Path(inputs["dest_path"]), inputs.get("project"))
                lib.save()
                data = {"asset": record, "dest_path": record["usage"][-1]["path"]}
            elif op == "update":
                record = lib.get(inputs["asset_id"])
                if record is None:
                    return ToolResult(success=False, error=f"Unknown asset id: {inputs['asset_id']}")
                for k in ("description", "status"):
                    if inputs.get(k):
                        record[k] = inputs[k]
                for k in ("tags", "subjects"):
                    if inputs.get(k) is not None:
                        record[k] = sorted(set(inputs[k]))
                lib.save()
                data = {"asset": record}
            else:
                return ToolResult(success=False, error=f"Unknown operation: {op}")
        except (KeyError, ValueError, FileNotFoundError) as exc:
            return ToolResult(success=False, error=str(exc))
        return ToolResult(success=True, data=data, duration_seconds=round(time.time() - start, 2))

    @staticmethod
    def _ingest_manifest(lib: Library, inputs: dict[str, Any]) -> dict[str, Any]:
        project_dir = Path(inputs["project_dir"])
        manifest_path = project_dir / "artifacts" / "asset_manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"No asset manifest at {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        only = set(inputs.get("asset_ids") or [])
        descriptions = inputs.get("descriptions") or {}
        project = inputs.get("source_project") or project_dir.name
        added, merged, skipped = [], [], []
        for a in manifest.get("assets", []):
            if only and a["id"] not in only:
                continue
            mtype = str(a.get("type", "")).lower()
            if mtype in _SKIP_BY_DEFAULT and not inputs.get("include_narration"):
                skipped.append({"id": a["id"], "reason": f"{mtype} is script-specific"})
                continue
            path = Path(a["path"])
            if not path.is_absolute() and not path.exists():
                path = project_dir / a["path"]
            if not path.exists():
                skipped.append({"id": a["id"], "reason": "file missing"})
                continue
            description = descriptions.get(a["id"]) or a.get("generation_summary") or a.get("prompt")
            if not description:
                skipped.append({"id": a["id"], "reason": "no description; pass one via descriptions"})
                continue
            record, created = lib.ingest(
                path,
                type=_MANIFEST_TYPE_MAP.get(mtype, "audio" if mtype.endswith("audio") else "other"),
                description=description,
                tags=[project] + list(inputs.get("tags") or []),
                subjects=inputs.get("subjects"),
                prompt=a.get("prompt"),
                model=a.get("model"),
                provider=a.get("provider"),
                source_tool=a.get("source_tool"),
                seed=a.get("seed"),
                cost_usd=a.get("cost_usd"),
                license=a.get("license"),
                source_project=project,
                scene_id=a.get("scene_id"),
                duration_seconds=a.get("duration_seconds"),
            )
            (added if created else merged).append(record["id"])
        return {"added": added, "merged": merged, "skipped": skipped, "library": str(lib.root)}
