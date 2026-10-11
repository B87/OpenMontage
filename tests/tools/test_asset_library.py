"""asset_library: ingest, dedupe, search, reuse and savings tracking."""

import json

from PIL import Image

from tools.assets.asset_library import AssetLibrary


def _img(path, size=(1600, 900), color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def _run(**inputs):
    return AssetLibrary().execute(inputs)


def test_ingest_records_provenance_and_image_info(tmp_path):
    lib = tmp_path / "lib"
    src = _img(tmp_path / "a.png")
    r = _run(operation="ingest", library_dir=str(lib), source_path=str(src), type="image",
             description="Watercolor lighthouse on a rocky coast", subjects=["lighthouse", "coast"],
             prompt="lighthouse at dusk", model="gemini-3-pro-image", cost_usd=0.13, source_project="p1")
    assert r.success, r.error
    asset = r.data["asset"]
    assert asset["aspect_ratio"] == "16:9"
    assert asset["provenance"]["cost_usd"] == 0.13
    assert (lib / asset["file"]).exists()
    assert json.loads((lib / "index.jsonl").read_text().splitlines()[0])["id"] == asset["id"]


def test_same_bytes_are_merged_not_duplicated(tmp_path):
    lib = tmp_path / "lib"
    src = _img(tmp_path / "a.png")
    first = _run(operation="ingest", library_dir=str(lib), source_path=str(src), type="image",
                 description="x", tags=["one"])
    second = _run(operation="ingest", library_dir=str(lib), source_path=str(src), type="image",
                  description="x", tags=["two"])
    assert second.data["created"] is False
    assert second.data["asset"]["id"] == first.data["asset"]["id"]
    assert second.data["asset"]["tags"] == ["one", "two"]
    assert len((lib / "index.jsonl").read_text().splitlines()) == 1


def test_description_is_required(tmp_path):
    r = _run(operation="ingest", library_dir=str(tmp_path / "lib"), source_path=str(_img(tmp_path / "a.png")),
             type="image", description="  ")
    assert not r.success and "description is required" in r.error


def test_search_ranks_by_subjects_then_description_and_filters(tmp_path):
    lib = str(tmp_path / "lib")
    _run(operation="ingest", library_dir=lib, source_path=str(_img(tmp_path / "1.png", color="red")), type="image",
         description="Red bicycle leaning on a brick wall", subjects=["bicycle", "wall"])
    _run(operation="ingest", library_dir=lib, source_path=str(_img(tmp_path / "2.png", color="blue")), type="image",
         description="Lighthouse beam over a stormy sea", subjects=["lighthouse", "sea"])
    _run(operation="ingest", library_dir=lib, source_path=str(_img(tmp_path / "3.png", size=(900, 1600), color="green")),
         type="image", description="Vertical bicycle wheel close-up", subjects=["bicycle"])

    results = _run(operation="search", library_dir=lib, query="bicycle wall").data["results"]
    assert results[0]["description"].startswith("Red bicycle")
    vertical = _run(operation="search", library_dir=lib, query="bicycle", aspect_ratio="9:16").data["results"]
    assert [r["aspect_ratio"] for r in vertical] == ["9:16"]
    assert _run(operation="search", library_dir=lib, query="volcano eruption").data["results"] == []


def test_use_links_into_project_and_tracks_savings(tmp_path):
    lib = str(tmp_path / "lib")
    asset = _run(operation="ingest", library_dir=lib, source_path=str(_img(tmp_path / "a.png")), type="image",
                 description="Paper boat on a pond", cost_usd=0.25).data["asset"]
    dest_dir = tmp_path / "projects" / "p2" / "assets" / "images"
    used = _run(operation="use", library_dir=lib, asset_id=asset["id"], dest_path=str(dest_dir), project="p2")
    assert used.success, used.error
    assert (dest_dir / f"{asset['id']}.png").exists()
    stats = _run(operation="stats", library_dir=lib).data
    assert stats["reuses"] == 1
    assert stats["cost_avoided_by_reuse_usd"] == 0.25


def test_deprecated_assets_are_hidden_and_blocked(tmp_path):
    lib = str(tmp_path / "lib")
    asset = _run(operation="ingest", library_dir=lib, source_path=str(_img(tmp_path / "a.png")), type="image",
                 description="Old style lighthouse").data["asset"]
    _run(operation="update", library_dir=lib, asset_id=asset["id"], status="deprecated")
    assert _run(operation="search", library_dir=lib, query="lighthouse").data["results"] == []
    blocked = _run(operation="use", library_dir=lib, asset_id=asset["id"], dest_path=str(tmp_path / "x"))
    assert not blocked.success and "deprecated" in blocked.error


def test_ingest_manifest_skips_narration_and_uses_descriptions(tmp_path):
    project = tmp_path / "projects" / "demo--ep"
    _img(project / "assets" / "images" / "s1.png")
    (project / "assets" / "audio").mkdir(parents=True)
    (project / "assets" / "audio" / "n1.mp3").write_bytes(b"fake narration")
    (project / "artifacts").mkdir(parents=True)
    (project / "artifacts" / "asset_manifest.json").write_text(json.dumps({
        "version": "1.0",
        "assets": [
            {"id": "img_s1", "type": "image", "path": "assets/images/s1.png", "source_tool": "google_imagen",
             "scene_id": "s1", "prompt": "bicycle", "model": "gemini-3-pro-image", "cost_usd": 0.13},
            {"id": "nar_1", "type": "narration", "path": "assets/audio/n1.mp3", "source_tool": "elevenlabs_tts",
             "scene_id": "s1"},
            {"id": "img_missing", "type": "image", "path": "assets/images/nope.png", "source_tool": "x", "scene_id": "s2",
             "prompt": "p"},
        ],
    }))
    r = _run(operation="ingest_manifest", library_dir=str(tmp_path / "lib"), project_dir=str(project),
             descriptions={"img_s1": "Bicycle parked by a canal"})
    assert r.success, r.error
    assert len(r.data["added"]) == 1
    reasons = {s["id"]: s["reason"] for s in r.data["skipped"]}
    assert "script-specific" in reasons["nar_1"] and reasons["img_missing"] == "file missing"
    found = _run(operation="search", library_dir=str(tmp_path / "lib"), query="bicycle").data["results"][0]
    assert found["description"] == "Bicycle parked by a canal"
    assert found["provenance"]["source_project"] == "demo--ep"
    assert "demo--ep" in found["tags"]


def test_channel_defaults_library_location(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = _run(operation="stats", channel="demo")
    assert r.success and r.data["assets"] == 0
