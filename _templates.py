"""Workflow-template manifest loading, media typing, LoadImage patching."""

import json
import os

from ._constants import TEMPLATES_DIR


def _load_manifest() -> dict:
    """Load the template manifest."""
    manifest_path = os.path.join(TEMPLATES_DIR, "_manifest.json")
    with open(manifest_path) as f:
        manifest = json.load(f)
    return manifest


def _get_media_type(filepath: str) -> str:
    """Infer the media type from a file extension."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".mp4", ".avi", ".mov", ".mkv", ".webm"):
        return "video"
    if ext in (".obj", ".ply", ".glb", ".gltf", ".stl"):
        return "point_cloud"
    return "image"


def _patch_load_image_nodes(workflow: dict, sample_filename: str) -> dict:
    """Patch or inject a LoadImage node so the current sample is referenced.

    Handles two template formats:
    1. **Traditional** — top-level ``LoadImage`` nodes get their filename
       replaced.
    2. **Component/blueprint** — single component node with an ``IMAGE``
       input slot.  A new ``LoadImage`` node is prepended and wired to
       the component's input.
    """
    # --- Traditional format: patch existing LoadImage nodes ---------------
    patched = False
    for node in workflow.get("nodes", []):
        if node.get("type") == "LoadImage":
            wv = node.get("widgets_values")
            if wv and len(wv) > 0:
                wv[0] = sample_filename
                patched = True

    if patched:
        return workflow

    # --- Component format: add a LoadImage node and link it ---------------
    nodes = workflow.get("nodes", [])
    if not nodes:
        return workflow

    # Find the first component node that has an IMAGE input
    target_node = None
    target_slot = None
    for node in nodes:
        for idx, inp in enumerate(node.get("inputs", [])):
            if inp.get("type") == "IMAGE" and inp.get("link") is None:
                target_node = node
                target_slot = idx
                break
        if target_node is not None:
            break

    if target_node is None:
        return workflow

    new_node_id = workflow.get("last_node_id", 100) + 1
    new_link_id = workflow.get("last_link_id", 100) + 1

    target_pos = target_node.get("pos", [400, 300])
    if isinstance(target_pos, dict):
        tx, ty = target_pos.get("0", 400), target_pos.get("1", 300)
    else:
        tx, ty = target_pos[0], target_pos[1]

    load_image_node = {
        "id": new_node_id,
        "type": "LoadImage",
        "pos": [tx - 400, ty],
        "size": [315, 314],
        "flags": {},
        "order": 0,
        "mode": 0,
        "inputs": [],
        "outputs": [
            {"name": "IMAGE", "type": "IMAGE", "links": [new_link_id], "slot_index": 0},
            {"name": "MASK", "type": "MASK", "links": [], "slot_index": 1},
        ],
        "properties": {"Node name for S&R": "LoadImage"},
        "widgets_values": [sample_filename, "image"],
    }

    # link: [link_id, source_node, source_slot, target_node, target_slot, type]
    new_link = [new_link_id, new_node_id, 0, target_node["id"], target_slot, "IMAGE"]

    target_node["inputs"][target_slot]["link"] = new_link_id

    nodes.append(load_image_node)
    workflow.setdefault("links", []).append(new_link)
    workflow["last_node_id"] = new_node_id
    workflow["last_link_id"] = new_link_id

    return workflow
