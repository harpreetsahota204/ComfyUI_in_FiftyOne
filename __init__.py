"""FiftyOne ComfyUI Plugin.

Embeds a full ComfyUI instance inside the FiftyOne sample modal via an
iframe, copies the current sample (and any group slices) into ComfyUI's
input directory, and saves generated outputs back to the dataset.

Architecture
------------
``ComfyUIPanel`` (foo.Panel)
    Hybrid panel rendered in the sample modal.  Lifecycle hooks
    (``on_load``, ``on_change_current_sample``) are intentionally
    lightweight: they only push the current sample's filepath / id to
    React via ``ctx.panel.set_state``.  Heavy work — server startup,
    extension install, sample injection — happens in panel methods
    invoked by React (``initialize``, ``start_server``, ``stop_server``,
    ``inject_slice``, ``trigger_reload``, etc.).

``SaveComfyOutput`` (foo.Operator)
    Unlisted operator invoked from React via ``useOperatorExecutor``.
    Supports seven output types (image, video, text, depth, detections,
    segmentation, 3d) and multiple destinations (group slice, new
    sample, string field, classification, heatmap, ``fo.Detections``
    field, ``fo.Segmentation`` field).  Fetches metadata from ComfyUI's
    ``/history`` endpoint and stores generation parameters on the
    saved sample.

``GetComfyTemplates`` (foo.Operator)
    Unlisted operator returning workflow templates filtered by sample
    media type.
"""

import base64
import copy
import io as _io
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback

import numpy as np
import requests

import bson
import fiftyone as fo
import fiftyone.operators as foo
import fiftyone.operators.types as types
from fiftyone import ViewField as F
from fiftyone.core.odm.database import get_db_conn

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
EXTENSION_DIR = os.path.join(PLUGIN_DIR, "comfyui_extension")
# Templates ship inside the bridge custom-node so a single symlink
# (custom_nodes/fiftyone_bridge -> EXTENSION_DIR) is enough to make them
# discoverable.  ComfyUI's native "Workflow Templates" tab scans
# example_workflows/ (and a couple of legacy aliases) by convention; we
# read these only from the FiftyOne panel's own dropdown via the
# get_comfy_templates operator + _load_manifest() pair.
TEMPLATES_DIR = os.path.join(EXTENSION_DIR, "workflows")
VENDOR_DIR = os.path.join(PLUGIN_DIR, "vendor")

# Bundled third-party custom-node packs that get symlinked into ComfyUI's
# custom_nodes/ at panel startup. Each tuple is (vendor_subdir, dst_name)
# where dst_name is the directory created under custom_nodes/.
_VENDOR_PACKS = (
    ("ComfyUI-Grounding", "ComfyUI-Grounding"),
    ("ComfyUI-SAM3", "ComfyUI-SAM3"),
)

STATE_DIR = os.path.join(os.path.expanduser("~"), ".fiftyone", "comfyui_plugin")
PID_FILE = os.path.join(STATE_DIR, ".comfyui.pid")

GROUP_FIELD = "group"
ORIGINAL_SLICE = "original"

DEFAULT_COMFYUI_PATH = os.path.expanduser("~/comfy/ComfyUI")
DEFAULT_COMFYUI_PORT = 8188

# ---------------------------------------------------------------------------
# Process persistence — survives module reimports
# ---------------------------------------------------------------------------

_PERSIST_KEY = "comfyui_plugin__persist"
if _PERSIST_KEY not in sys.modules:
    import types as _types

    _persist = _types.ModuleType(_PERSIST_KEY)
    _persist.comfyui_process = None
    sys.modules[_PERSIST_KEY] = _persist
else:
    _persist = sys.modules[_PERSIST_KEY]


# ---------------------------------------------------------------------------
# Server lifecycle helpers
# ---------------------------------------------------------------------------


def _get_config(ctx) -> dict:
    """Read plugin configuration from the execution store.

    ``comfyui_path`` is run through ``os.path.expanduser`` so that
    user-supplied values like ``~/comfy/ComfyUI`` resolve.  Idempotent
    on absolute paths.
    """
    store = ctx.store("comfyui_plugin_config")
    return {
        "comfyui_path": os.path.expanduser(
            store.get("comfyui_path") or DEFAULT_COMFYUI_PATH
        ),
        "comfyui_port": int(store.get("comfyui_port") or DEFAULT_COMFYUI_PORT),
        "comfyui_args": store.get("comfyui_args") or [],
    }


def _set_config(ctx, key: str, value):
    """Write a single config value to the execution store."""
    store = ctx.store("comfyui_plugin_config")
    store.set(key, value)


def _is_server_running(port: int, timeout: float = 2.0) -> bool:
    """Check if a ComfyUI server is responding on the given port."""
    try:
        resp = requests.get(
            f"http://127.0.0.1:{port}/system_stats",
            timeout=timeout,
        )
        return resp.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


def _read_pid() -> "int | None":
    """Read the PID from the PID file, or None if absent/stale."""
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_pid(pid: int):
    """Write a PID to the PID file."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(pid))


def _clear_pid():
    """Remove the PID file."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def _spawn_comfyui(comfyui_path: str, port: int, extra_args: list) -> subprocess.Popen:
    """Spawn a ComfyUI server subprocess."""
    main_py = os.path.join(comfyui_path, "main.py")
    if not os.path.isfile(main_py):
        raise FileNotFoundError(
            f"ComfyUI main.py not found at {main_py}. "
            f"Check your comfyui_path setting."
        )

    cmd = [
        sys.executable,
        main_py,
        "--listen", "127.0.0.1",
        "--port", str(port),
        "--enable-cors-header",
        *extra_args,
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=comfyui_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    _persist.comfyui_process = proc
    _write_pid(proc.pid)

    return proc


def _wait_for_server(port: int, timeout: float = 120.0) -> bool:
    """Poll until the ComfyUI server is responsive (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_server_running(port):
            return True
        time.sleep(1.0)
    return False


# ---------------------------------------------------------------------------
# Extension installation
# ---------------------------------------------------------------------------


def _symlink_pack(src: str, dst: str, label: str) -> bool:
    """Create-or-refresh a symlink ``dst → src``.

    Returns True if the symlink is in the desired state on exit (whether
    we created it now or it was already correct), False if we had to
    skip because ``dst`` exists as a real directory we don't own.

    Behavior:
    - If ``dst`` is missing: create the symlink.
    - If ``dst`` is already a symlink to ``src``: no-op.
    - If ``dst`` is a stale symlink: replace it.
    - If ``dst`` is a real directory: log warning and skip (user-owned).
    """
    src_abs = os.path.abspath(src)

    if os.path.lexists(dst):
        if os.path.islink(dst):
            current_target = os.path.realpath(dst)
            if current_target == os.path.realpath(src_abs):
                print(f"[comfyui-plugin] {label} symlink already current → {dst}")
                return True
            print(f"[comfyui-plugin] {label} symlink stale (→ {current_target}), replacing")
            os.remove(dst)
        else:
            print(
                f"[comfyui-plugin] WARNING: {dst} exists as a real directory "
                f"(not symlink); leaving user copy in place. To use the "
                f"bundled {label}, remove or rename that directory."
            )
            return False

    os.symlink(src_abs, dst)
    print(f"[comfyui-plugin] installed {label} symlink → {dst}  (target: {src_abs})")
    return True


def _install_extension(comfyui_path: str):
    """Symlink the FiftyOne bridge + bundled custom-node packs into ComfyUI.

    Three pieces are installed:

    1. ``comfyui_extension/``   → ``custom_nodes/fiftyone_bridge``
       (FiftyOne save nodes + JS bridge)
    2. ``vendor/ComfyUI-Grounding/`` → ``custom_nodes/ComfyUI-Grounding``
    3. ``vendor/ComfyUI-SAM3/``      → ``custom_nodes/ComfyUI-SAM3``

    All three use ``_symlink_pack`` which is idempotent and refuses to
    overwrite a real directory — if a user already has Grounding or SAM3
    installed manually, theirs wins and we log a warning.
    """
    custom_nodes_dir = os.path.join(comfyui_path, "custom_nodes")
    if not os.path.isdir(custom_nodes_dir):
        print(f"[comfyui-plugin] custom_nodes dir not found: {custom_nodes_dir}")
        return

    print(f"[comfyui-plugin] installing custom-node symlinks under {custom_nodes_dir}")

    bridge_dst = os.path.join(custom_nodes_dir, "fiftyone_bridge")
    _symlink_pack(EXTENSION_DIR, bridge_dst, "fiftyone_bridge")

    if not os.path.isdir(VENDOR_DIR):
        print(f"[comfyui-plugin] vendor/ not found at {VENDOR_DIR}; skipping vendor packs")
        return

    for subdir, dst_name in _VENDOR_PACKS:
        src = os.path.join(VENDOR_DIR, subdir)
        if not os.path.isdir(src):
            print(f"[comfyui-plugin] vendor pack missing: {src}; skipping")
            continue
        dst = os.path.join(custom_nodes_dir, dst_name)
        _symlink_pack(src, dst, f"vendor/{subdir}")

    print("[comfyui-plugin] custom-node install pass complete")


# ---------------------------------------------------------------------------
# Sample injection
# ---------------------------------------------------------------------------


CURRENT_SAMPLE_FILENAME = "fo_current_sample.png"
_SLICE_FILE_PREFIX = "fo_current_sample_"


def _slice_filename(slice_name: str) -> str:
    """Return the filename used for a per-slice image in ComfyUI's input dir.

    Example: ``"close up"`` → ``"fo_current_sample_close_up.png"``.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", slice_name)
    return f"{_SLICE_FILE_PREFIX}{sanitized}.png"


def _inject_sample(
    comfyui_path: str,
    filepath: str,
    target_filename: str = CURRENT_SAMPLE_FILENAME,
) -> str:
    """Copy a sample's image into ComfyUI's input directory as PNG.

    Only injects image media types.  Videos and other non-image files are
    skipped (returns ``""``), keeping the last valid image in place so
    ComfyUI's LoadImage node isn't broken by a missing file.

    The default ``target_filename`` is ``fo_current_sample.png`` (the
    "active" file that follows the current sample / active modal slice).
    Callers can pass a different name (e.g. ``fo_current_sample_<slice>.png``)
    to write per-slice copies.
    """
    media_type = _get_media_type(filepath)
    if media_type != "image":
        print(f"[comfyui-plugin] _inject_sample: skipping non-image ({media_type}) {filepath}")
        return ""

    from PIL import Image

    input_dir = os.path.join(comfyui_path, "input")
    os.makedirs(input_dir, exist_ok=True)

    dst = os.path.join(input_dir, target_filename)

    # lexists() is True for files, dirs, valid symlinks, AND broken
    # symlinks — exactly the union we need to safely overwrite.
    if os.path.lexists(dst):
        os.remove(dst)

    with Image.open(filepath) as img:
        img.save(dst, "PNG")
    return target_filename


def _inject_all_slices(
    comfyui_path: str,
    dataset: fo.Dataset,
    sample_id: str,
) -> list:
    """Copy every group slice's image into ComfyUI's input dir.

    Each slice's image is written as ``fo_current_sample_<slice>.png`` so
    it appears in any LoadImage node's image dropdown.  Multi-input
    workflows can then bind each LoadImage to the slice they want.

    Stale ``fo_current_sample_*.png`` files left over from a previous
    group (e.g. after the user navigates to a different sample) are
    swept from the input dir.

    Returns the list of filenames that exist after this call.  Returns
    ``[]`` for flat (non-grouped) datasets — nothing to write.
    """
    try:
        if not dataset.group_field:
            return []
        sample = dataset[sample_id]
        group_elem = sample[dataset.group_field]
        if not group_elem:
            return []
        group_id = group_elem.id
    except Exception as exc:
        print(f"[comfyui-plugin] _inject_all_slices: lookup error: {exc}")
        return []

    # One query for all slice samples in this group, vs. one-per-slice.
    try:
        group_samples = dataset.get_group(group_id)
    except Exception as exc:
        print(f"[comfyui-plugin] _inject_all_slices: get_group error: {exc}")
        return []

    input_dir = os.path.join(comfyui_path, "input")
    os.makedirs(input_dir, exist_ok=True)

    media_types = dataset.group_media_types or {}
    desired = set()

    for slice_name, slice_sample in group_samples.items():
        if media_types.get(slice_name) != "image" or slice_sample is None:
            continue
        try:
            target = _slice_filename(slice_name)
            if _inject_sample(comfyui_path, slice_sample.filepath, target_filename=target):
                desired.add(target)
        except Exception as exc:
            print(f"[comfyui-plugin] _inject_all_slices: slice '{slice_name}' error: {exc}")

    # Sweep stale per-slice files (different group, deleted slice, etc.)
    try:
        for entry in os.listdir(input_dir):
            if (
                entry.startswith(_SLICE_FILE_PREFIX)
                and entry.endswith(".png")
                and entry not in desired
            ):
                os.remove(os.path.join(input_dir, entry))
                print(f"[comfyui-plugin] _inject_all_slices: swept stale {entry}")
    except OSError as exc:
        print(f"[comfyui-plugin] _inject_all_slices: sweep error: {exc}")

    return sorted(desired)


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dataset / group helpers
# ---------------------------------------------------------------------------


def _ensure_grouped(dataset: fo.Dataset, sample_id: str) -> str:
    """Ensure the dataset is grouped and the sample belongs to a group.

    Returns the sample's group id.  If the dataset was flat, this
    performs the flat→grouped migration in place via a bulk MongoDB
    write.  React detects the migration *after the fact* by diffing
    ``dataset_is_grouped`` from ``get_group_slices`` calls before and
    after a save (it doesn't rely on a flag returned from here),
    because FiftyOne's ``useOperatorExecutor.execute`` is unreliable
    about propagating operator return values across versions.
    """
    sample = dataset[sample_id]
    gf = dataset.group_field

    if not gf:
        dataset.add_group_field(GROUP_FIELD, default=ORIGINAL_SLICE)
        dataset.add_group_slice(ORIGINAL_SLICE, "image")
        gf = dataset.group_field

        # Raw MongoDB for bulk group assignment — the ORM would require
        # loading, modifying, and saving every sample individually.
        db = get_db_conn()
        coll = db[dataset._sample_collection_name]

        target_group_id = None
        n = 0
        for doc in coll.find({gf: {"$exists": False}}):
            g = fo.Group()
            coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {gf: {
                    "_id": bson.ObjectId(g.id),
                    "_cls": "Group",
                    "name": ORIGINAL_SLICE,
                }}},
            )
            if str(doc["_id"]) == sample_id:
                target_group_id = g.id
            n += 1

        dataset.reload()
        print(f"[comfyui-plugin] converted {n} samples to grouped ('{ORIGINAL_SLICE}' slice)")

        if target_group_id is None:
            raise RuntimeError(f"Sample {sample_id} not found during group conversion")
        return target_group_id

    if sample[gf] is None:
        group = fo.Group()
        sample[gf] = group.element(ORIGINAL_SLICE)
        sample.save()
        return group.id

    return sample[gf].id


def _ensure_comfy_fields(dataset: fo.Dataset):
    """Declare ComfyUI metadata fields if not already present."""
    schema = dataset.get_field_schema()
    fields = {
        "comfy_workflow_name": fo.StringField,
        "comfy_prompt": fo.StringField,
        "comfy_negative_prompt": fo.StringField,
        "comfy_seed": fo.IntField,
        "comfy_steps": fo.IntField,
        "comfy_cfg": fo.FloatField,
        "comfy_sampler": fo.StringField,
        "comfy_scheduler": fo.StringField,
        "comfy_denoise": fo.FloatField,
        "comfy_model": fo.StringField,
        "comfy_node_title": fo.StringField,
        "comfy_prompt_id": fo.StringField,
    }
    for name, ftype in fields.items():
        if name not in schema:
            dataset.add_sample_field(name, ftype)


def _get_sample_label_fields(dataset: fo.Dataset, sample: fo.Sample) -> list:
    """Return label field names with non-None values on *sample*.

    Used to populate the "Copy labels" pickers on Save nodes and in the
    save dialog.  Filtering on:

    - ``EmbeddedDocumentField`` whose ``document_type`` is a
      ``fo.Label`` subclass — excludes generic embedded docs, vector
      embeddings, brain results, etc.
    - non-None value on the source sample — empty fields would be
      useless to copy.
    """
    out = []
    for name, field in dataset.get_field_schema().items():
        if not isinstance(field, fo.EmbeddedDocumentField):
            continue
        doc_type = getattr(field, "document_type", None)
        if doc_type is None:
            continue
        try:
            if not issubclass(doc_type, fo.Label):
                continue
        except TypeError:
            continue
        if sample.get_field(name) is not None:
            out.append(name)
    return out


def _parse_copy_labels(copy_labels: str) -> list:
    """Parse the ``copy_labels`` wire format into a list of field names.

    Wire format (single string):
    - ``""`` → ``[]`` (copy nothing)
    - ``"a,b,c"`` → ``["a", "b", "c"]``
    """
    if not copy_labels:
        return []
    return [name.strip() for name in copy_labels.split(",") if name.strip()]


# ---------------------------------------------------------------------------
# Detection / segmentation helpers
# ---------------------------------------------------------------------------


def _parse_jsonish_list(raw):
    """Parse polymorphic ``boxes`` / ``scores`` payloads to a Python list.

    Accepts:
    - ``""`` / ``None``                         → ``[]``
    - JSON string (``"[[1,2,3,4],…]"``)         → parsed
    - already-a-list                            → as-is
    Anything else returns ``[]`` and logs.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            return decoded if isinstance(decoded, list) else []
        except (ValueError, TypeError) as exc:
            print(f"[comfyui-plugin] _parse_jsonish_list: parse failed: {exc}")
            return []
    print(f"[comfyui-plugin] _parse_jsonish_list: unexpected type {type(raw).__name__}")
    return []


def _resolve_detection_labels(pred_labels_json, fallback_labels: str, n_boxes: int) -> list:
    """Resolve the per-detection class label list.

    Priority (consistent with the plan's "pills are fallback" rule):
    1. Upstream ``pred_labels_json`` if non-empty:
       - list of strings → use directly (padded with last label if too short)
       - period- / comma-separated single string → split
    2. Fallback pill list ``fallback_labels`` (round-robin cycle).
    3. Literal ``"object"`` for every detection.
    """
    upstream = _parse_jsonish_list(pred_labels_json)
    if upstream and any(isinstance(x, str) and x.strip() for x in upstream):
        if len(upstream) == 1 and isinstance(upstream[0], str):
            tokens = [t.strip() for t in re.split(r"[.,]", upstream[0]) if t.strip()]
            if len(tokens) > 1:
                upstream = tokens
        out = [str(upstream[i]) if i < len(upstream) else str(upstream[-1])
               for i in range(n_boxes)]
        print(f"[comfyui-plugin]   labels source=upstream, count={len(upstream)} → {out[:5]}…")
        return out

    pills = [p.strip() for p in (fallback_labels or "").split(",") if p.strip()]
    if pills:
        out = [pills[i % len(pills)] for i in range(n_boxes)]
        print(f"[comfyui-plugin]   labels source=pills({pills}), cycled → {out[:5]}…")
        return out

    print("[comfyui-plugin]   labels source=default 'object'")
    return ["object"] * n_boxes


def _bboxes_from_masks(masks_arr) -> list:
    """Compute tight pixel-space xyxy bboxes for each instance mask.

    Used when the upstream pipeline only emits ``MASK`` (e.g. SAM2 /
    SAM3 segmentation) and we need to construct ``fo.Detection`` objects
    — every detection in FiftyOne carries a ``bounding_box``.

    ``masks_arr`` is ``[N, H, W]`` uint8 (``0`` / ``255`` after our
    round-trip through ``_save_mask_tensor_npy``).  Returns
    ``[[x1, y1, x2, y2], ...]`` in pixel coordinates.  Empty / all-zero
    masks become ``[0, 0, 0, 0]`` (skipped by the caller).

    Logs per-mask non-zero pixel count so we can tell at a glance
    whether the upstream model emitted real masks or just empties.
    """
    if masks_arr is None or masks_arr.ndim != 3:
        print(f"[comfyui-plugin] _bboxes_from_masks: bad shape {None if masks_arr is None else masks_arr.shape}")
        return []
    out = []
    for i in range(masks_arr.shape[0]):
        ys, xs = np.where(masks_arr[i] > 127)
        if xs.size == 0:
            print(f"[comfyui-plugin]   mask[{i}] is all-zero → bbox=[0,0,0,0] (will be skipped)")
            out.append([0.0, 0.0, 0.0, 0.0])
            continue
        bbox = [
            float(xs.min()),
            float(ys.min()),
            float(xs.max() + 1),
            float(ys.max() + 1),
        ]
        print(f"[comfyui-plugin]   mask[{i}]: {xs.size} non-zero px → bbox={bbox}")
        out.append(bbox)
    return out


def _crop_mask_to_bbox(masks_arr, idx: int, x1, y1, x2, y2):
    """Return the per-instance mask cropped to its bbox as ``np.uint8`` 2D.

    ``masks_arr`` is the full per-instance stack ``[N, H, W]`` (uint8,
    ``0/255`` after our round-trip).  Output is ``(h_box, w_box)`` with
    values in ``{0, 255}``, suitable for ``fo.Detection.mask``.
    Returns ``None`` if the index is out of range or the bbox is empty.
    """
    if masks_arr.ndim != 3 or idx >= masks_arr.shape[0]:
        return None
    H, W = masks_arr.shape[1], masks_arr.shape[2]
    xa = max(0, int(round(x1)))
    ya = max(0, int(round(y1)))
    xb = min(W, int(round(x2)))
    yb = min(H, int(round(y2)))
    if xb <= xa or yb <= ya:
        return None
    return np.ascontiguousarray(masks_arr[idx, ya:yb, xa:xb])


def _parse_mask_targets(raw: str) -> dict:
    """Parse a mask-targets string into a ``{str: str}`` mapping.

    Accepts JSON object (``'{"0":"bg","1":"fg"}'``) or
    ``key=value,key=value`` form.  Empty/None/unparseable → ``{}``.

    Keys are kept as strings — MongoDB requires string document keys
    (writing a dict with int keys raises ``bson.errors.InvalidDocument``),
    and FiftyOne accepts string-typed pixel indices for
    ``fo.Segmentation.mask_targets`` (it converts back to int internally
    when rendering).  Each key must still represent a valid integer
    pixel value; non-integer keys are dropped.
    """
    if not raw:
        return {}
    s = raw.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                k_str = str(k).strip()
                # Validate that the key is integer-typed (pixel value)
                # but keep it as a string in the returned dict.
                try:
                    int(k_str)
                except ValueError:
                    continue
                out[k_str] = str(v)
            return out
    except (ValueError, TypeError):
        pass
    out = {}
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            k_str = k.strip()
            try:
                int(k_str)
            except ValueError:
                continue
            out[k_str] = v.strip()
    return out


def _resolve_active_slice_sample(ctx, slice_override: str = "") -> tuple:
    """Return ``(sample_id, filepath)`` for the slice the user is viewing.

    FiftyOne's ``ctx.current_sample`` always points to the group's
    default ("original") slice sample, regardless of which slice tab
    the user has selected.  This helper looks up the actual visible
    slice's sample.

    Active-slice resolution order:

    1. ``slice_override`` (if non-empty) — the most reliable source,
       since the React panel can pass the slice name directly from
       Recoil's ``modalGroupSlice`` atom.  Used by the save operator,
       where ``ctx.group_slice`` is not consistently populated.
    2. ``ctx.group_slice`` — works in lifecycle hooks and panel
       methods, may be ``None`` in operator context.
    3. None → keep ``ctx.current_sample`` (default-slice case or flat
       dataset).

    Returns ``("", "")`` if no sample is loaded at all.
    """
    if not ctx.current_sample:
        return "", ""
    dataset = ctx.dataset
    try:
        sample = dataset[ctx.current_sample]
    except Exception as exc:
        print(f"[comfyui-plugin] _resolve_active_slice_sample: lookup error: {exc}")
        return "", ""

    gf = dataset.group_field
    sample_id = ctx.current_sample
    filepath = sample.filepath

    active_slice = slice_override or ctx.group_slice or ""

    if gf and active_slice:
        group_elem = sample[gf]
        if group_elem and group_elem.name != active_slice:
            try:
                slice_sample = (
                    dataset
                    .select_group_slices(active_slice)
                    .match(F(f"{gf}._id") == bson.ObjectId(group_elem.id))
                    .first()
                )
                if slice_sample is not None:
                    sample_id = slice_sample.id
                    filepath = slice_sample.filepath
            except Exception as exc:
                print(f"[comfyui-plugin] _resolve_active_slice_sample: slice lookup error: {exc}")

    return sample_id, filepath


def _ensure_compatible_slice(dataset: fo.Dataset, media_type: str) -> str:
    """Return a group-slice name compatible with ``media_type``.

    Tries the dataset's default slice first, then any existing slice with
    a matching media type, and finally creates a new slice named
    ``ORIGINAL_SLICE`` (for image) or ``media_type`` (for video, etc.).

    Used when saving as a "new sample" into a grouped dataset: every
    sample must have a group field, and that group's slice must match
    the new sample's media type.
    """
    media_types = dataset.group_media_types or {}

    default = dataset.default_group_slice
    if default and media_types.get(default) == media_type:
        return default

    for name, mt in media_types.items():
        if mt == media_type:
            return name

    name = ORIGINAL_SLICE if media_type == "image" else media_type
    if name not in dataset.group_slices:
        dataset.add_group_slice(name, media_type)
    return name


# ---------------------------------------------------------------------------
# ComfyUI metadata extraction
# ---------------------------------------------------------------------------


_PROMPT_INPUT_KEYS = {"text", "prompt", "string", "positive", "instruction"}
_NEGATIVE_PROMPT_HINTS = {"negative", "neg", "uncond"}
_MODEL_INPUT_KEYS = {
    "unet_name", "ckpt_name", "model_name", "model_path",
    "model_filename", "lora_name",
}
_SAMPLER_CLASS_HINTS = {"sampler", "ksampler"}
_MODEL_CLASS_HINTS = {"loader", "checkpoint", "unet", "model"}

# Metadata fields that must be coerced to str before assignment.  Some
# ComfyUI nodes return list/tuple values (e.g. multi-LoRA loaders return a
# list of model names), which would fail FiftyOne's StringField validation.
_METADATA_STR_FIELDS = frozenset({
    "comfy_workflow_name", "comfy_prompt", "comfy_negative_prompt",
    "comfy_sampler", "comfy_scheduler", "comfy_model",
})


def _fetch_comfy_metadata(port: int, prompt_id: str) -> "dict | None":
    """Fetch generation metadata from ComfyUI's /history endpoint.

    Uses a generic scan: instead of hard-coding specific node class names,
    we inspect every node's inputs and match by input-key heuristics so
    that arbitrary workflows (Qwen, Flux, SDXL, custom, etc.) all get
    captured.
    """
    print(f"[comfyui-plugin] _fetch_comfy_metadata: prompt_id={prompt_id!r}")
    if not prompt_id:
        print(f"[comfyui-plugin]   → returning None (no prompt_id)")
        return None

    try:
        url = f"http://127.0.0.1:{port}/history/{prompt_id}"
        print(f"[comfyui-plugin]   fetching {url}")
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        full_json = resp.json()
        history = full_json.get(prompt_id, {})
        print(f"[comfyui-plugin]   history keys: {list(history.keys()) if isinstance(history, dict) else type(history)}")
    except Exception as e:
        print(f"[comfyui-plugin]   could not fetch history: {e}")
        return None

    prompt_data = history.get("prompt", [])
    api_workflow = None
    if isinstance(prompt_data, (list, tuple)):
        for item in prompt_data:
            if isinstance(item, dict) and len(item) > 0:
                api_workflow = item
                break
    if api_workflow is None:
        api_workflow = {}
    print(f"[comfyui-plugin]   api_workflow: {len(api_workflow)} nodes")

    metadata = {
        "workflow_json": api_workflow,
        "prompt": "",
        "negative_prompt": "",
        "seed": None,
        "steps": None,
        "cfg": None,
        "sampler": None,
        "scheduler": None,
        "denoise": None,
        "model": "",
    }

    class_types_seen = []
    for node_id, node_data in api_workflow.items():
        if not isinstance(node_data, dict):
            continue
        class_type = node_data.get("class_type", "")
        inputs = node_data.get("inputs", {})
        class_types_seen.append(class_type)
        ct_lower = class_type.lower()

        for key, val in inputs.items():
            if not isinstance(val, str) or not val.strip():
                continue
            key_lower = key.lower()
            if key_lower in _PROMPT_INPUT_KEYS:
                is_negative = any(h in ct_lower for h in _NEGATIVE_PROMPT_HINTS)
                if is_negative and not metadata["negative_prompt"]:
                    metadata["negative_prompt"] = val
                    print(f"[comfyui-plugin]   neg prompt from {class_type} node {node_id} key={key}: {val[:80]!r}")
                elif not is_negative and not metadata["prompt"]:
                    metadata["prompt"] = val
                    print(f"[comfyui-plugin]   prompt from {class_type} node {node_id} key={key}: {val[:80]!r}")

            if key_lower in _MODEL_INPUT_KEYS and not metadata["model"]:
                metadata["model"] = val
                print(f"[comfyui-plugin]   model from {class_type} node {node_id} key={key}: {val!r}")

        if any(h in ct_lower for h in _SAMPLER_CLASS_HINTS):
            if metadata["seed"] is None and "seed" in inputs:
                metadata["seed"] = inputs["seed"]
            if metadata["steps"] is None and "steps" in inputs:
                metadata["steps"] = inputs["steps"]
            if metadata["cfg"] is None and "cfg" in inputs:
                metadata["cfg"] = inputs["cfg"]
            if metadata["sampler"] is None:
                metadata["sampler"] = inputs.get("sampler_name") or inputs.get("sampler")
            if metadata["scheduler"] is None and "scheduler" in inputs:
                metadata["scheduler"] = inputs["scheduler"]
            if metadata["denoise"] is None and "denoise" in inputs:
                metadata["denoise"] = inputs["denoise"]
            print(f"[comfyui-plugin]   sampler info from {class_type} node {node_id}: seed={metadata['seed']} steps={metadata['steps']} cfg={metadata['cfg']}")

        if not metadata["model"] and any(h in ct_lower for h in _MODEL_CLASS_HINTS):
            for key, val in inputs.items():
                if isinstance(val, str) and ("." in val or "/" in val):
                    metadata["model"] = val
                    print(f"[comfyui-plugin]   model (heuristic) from {class_type} node {node_id} key={key}: {val!r}")
                    break

    print(f"[comfyui-plugin]   all class_types: {class_types_seen}")
    _p = repr(metadata["prompt"][:60]) if metadata["prompt"] else "''"
    print(f"[comfyui-plugin]   final: prompt={_p} seed={metadata['seed']} steps={metadata['steps']} model={metadata['model']!r}")
    return metadata


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


class ComfyUIPanel(foo.Panel):
    """Hybrid panel embedding a full ComfyUI instance inside the modal."""

    @property
    def config(self):
        return foo.PanelConfig(
            name="comfyui_panel",
            label="ComfyUI",
            icon="brush",
            surfaces="modal",
            help_markdown=(
                "Run any [ComfyUI](https://github.com/comfyanonymous/ComfyUI) "
                "workflow against the current sample. Save generated outputs "
                "as group slices to build an evolution timeline."
            ),
        )

    # ── Lifecycle hooks ──────────────────────────────────────────────────
    # Keep these lightweight — no blocking I/O, no HTTP requests.
    # Heavy work (server startup, extension install) is triggered by
    # React calling the ``initialize`` panel method after mount.

    def on_load(self, ctx):
        self._sync_sample(ctx)

    def on_change_current_sample(self, ctx):
        self._sync_sample(ctx)

    def on_change_group_slice(self, ctx):
        # FiftyOne never updates ``ctx.current_sample`` when the user
        # switches slice tabs — it stays pinned to the group's default
        # ("original") slice sample.  We therefore must re-sync here so
        # that ``current_sample_id`` / ``current_filepath`` reflect the
        # *visible* slice, not the original.  Without this hook, every
        # save would land on the original slice regardless of which tab
        # the user is on.
        self._sync_sample(ctx)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _sync_sample(self, ctx):
        """Push the active slice's filepath and ID to React.

        Delegates slice-aware resolution to ``_resolve_active_slice_sample``
        — same logic the save operator uses, so panel state and save
        targets stay in lock-step.
        """
        try:
            sample_id, filepath = _resolve_active_slice_sample(ctx)
            if not sample_id:
                print("[comfyui-plugin] _sync_sample: no current sample — skipping (will retry on next lifecycle event)")
                return
            print(f"[comfyui-plugin] _sync_sample: sample_id={sample_id} filepath={filepath} active_slice={ctx.group_slice!r}")
            ctx.panel.set_state("current_filepath", filepath)
            ctx.panel.set_state("current_sample_id", sample_id)
        except Exception as exc:
            print(f"[comfyui-plugin] _sync_sample error: {exc}")

    def _safe_get_config(self, ctx) -> dict:
        """Read config with fallback defaults if store fails."""
        try:
            return _get_config(ctx)
        except Exception as exc:
            print(f"[comfyui-plugin] config error: {exc}")
            return {
                "comfyui_path": DEFAULT_COMFYUI_PATH,
                "comfyui_port": DEFAULT_COMFYUI_PORT,
                "comfyui_args": [],
            }

    # ── Panel methods (called from React via usePanelEvent) ──────────────

    def initialize(self, ctx):
        """Called by React after mount.

        Receives the current filepath from React (since ``get_state``
        does not work inside ``usePanelEvent`` calls), checks if ComfyUI
        is reachable, installs the bridge extension, injects the sample,
        and returns everything React needs.
        """
        filepath = ctx.params.get("filepath", "")
        config = self._safe_get_config(ctx)
        port = config["comfyui_port"]
        comfyui_path = config["comfyui_path"]

        # Install bridge extension
        try:
            if os.path.isdir(comfyui_path):
                _install_extension(comfyui_path)
        except Exception as exc:
            print(f"[comfyui-plugin] extension install error: {exc}")

        running = _is_server_running(port)

        sample_filename = ""
        if running and filepath and os.path.isdir(comfyui_path):
            try:
                sample_filename = _inject_sample(comfyui_path, filepath)
            except Exception as exc:
                print(f"[comfyui-plugin] inject error: {exc}")

            # Also write per-slice files so the LoadImage dropdown lists
            # every group slice for the current sample.  Cheap no-op for
            # flat datasets.
            try:
                if ctx.current_sample:
                    _inject_all_slices(comfyui_path, ctx.dataset, ctx.current_sample)
            except Exception as exc:
                print(f"[comfyui-plugin] inject_all_slices error: {exc}")

        return {
            "server_status": "ready" if running else "not_running",
            "server_port": port,
            "server_error": "",
            "iframe_url": f"http://localhost:{port}" if running else "",
            "comfyui_path": comfyui_path,
            "sample_filename": sample_filename,
        }

    def start_server(self, ctx):
        """Manually start or detect the ComfyUI server."""
        config = self._safe_get_config(ctx)
        port = config["comfyui_port"]
        comfyui_path = config["comfyui_path"]

        if not _is_server_running(port):
            if os.path.isdir(comfyui_path):
                try:
                    _install_extension(comfyui_path)
                    _spawn_comfyui(comfyui_path, port, config.get("comfyui_args", []))
                    _wait_for_server(port, timeout=60.0)
                except Exception as exc:
                    # The spawn may have launched a subprocess but the
                    # health check timed out (or another error).  Reset
                    # bookkeeping so the next attempt isn't fooled by a
                    # stale PID file / cached Popen handle.
                    _persist.comfyui_process = None
                    _clear_pid()
                    return {
                        "server_status": "error",
                        "server_error": str(exc),
                        "iframe_url": "",
                    }
            else:
                return {
                    "server_status": "not_found",
                    "server_error": f"ComfyUI not found at {comfyui_path}",
                    "iframe_url": "",
                }

        running = _is_server_running(port)
        return {
            "server_status": "ready" if running else "timeout",
            "server_port": port,
            "server_error": "" if running else "Server did not start",
            "iframe_url": f"http://localhost:{port}" if running else "",
            "comfyui_path": comfyui_path,
        }

    def stop_server(self, ctx):
        """Stop the ComfyUI server."""
        if _persist.comfyui_process is not None:
            try:
                _persist.comfyui_process.terminate()
                _persist.comfyui_process.wait(timeout=10)
            except Exception:
                _persist.comfyui_process.kill()
            _persist.comfyui_process = None

        pid = _read_pid()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            _clear_pid()

        return {
            "server_status": "stopped",
            "iframe_url": "",
            "server_error": "",
        }

    def load_template(self, ctx):
        """Load a workflow template with the current sample injected."""
        template_id = ctx.params.get("template_id", "")
        if not template_id:
            return {"error": "No template_id provided"}

        # Constrain to the same slug shape ``save_template`` produces so
        # ``..`` / ``/`` / absolute paths can't escape ``TEMPLATES_DIR``.
        if not re.fullmatch(r"[a-z0-9_]+", template_id):
            return {"error": f"Invalid template_id: {template_id!r}"}

        template_path = os.path.join(TEMPLATES_DIR, f"{template_id}.json")
        if not os.path.isfile(template_path):
            return {"error": f"Template not found: {template_id}"}

        with open(template_path) as f:
            workflow = json.load(f)

        sample_filename = ctx.params.get("sample_filename", "")
        if not sample_filename:
            filepath = ctx.params.get("filepath", "")
            if filepath:
                config = self._safe_get_config(ctx)
                comfyui_path = config["comfyui_path"]
                if os.path.isdir(comfyui_path):
                    try:
                        sample_filename = _inject_sample(comfyui_path, filepath)
                    except Exception:
                        pass

        if sample_filename:
            workflow = _patch_load_image_nodes(workflow, sample_filename)

        return {"workflow": workflow}

    def save_template(self, ctx):
        """Save a workflow as a reusable template."""
        template_name = ctx.params.get("name", "").strip()
        workflow = ctx.params.get("workflow")

        if not template_name or not workflow:
            return {"error": "Template name and workflow data are required"}

        slug = re.sub(r"[^a-z0-9]+", "_", template_name.lower()).strip("_")
        if not slug:
            return {"error": "Invalid template name"}

        template_path = os.path.join(TEMPLATES_DIR, f"{slug}.json")
        with open(template_path, "w") as f:
            json.dump(workflow, f, indent=2)

        manifest_path = os.path.join(TEMPLATES_DIR, "_manifest.json")
        try:
            manifest = _load_manifest()
        except Exception:
            manifest = {"templates": []}

        existing_ids = {t["id"] for t in manifest.get("templates", [])}
        if slug not in existing_ids:
            manifest.setdefault("templates", []).append({
                "id": slug,
                "name": template_name,
                "description": f"User-saved template: {template_name}",
                "file": f"{slug}.json",
                "input_types": ["image"],
                "output_type": "image",
                "category": "user",
            })
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

        print(f"[comfyui-plugin] saved template: {slug}")
        return {"status": "ok", "template_id": slug}

    def update_config(self, ctx):
        """Update plugin configuration."""
        for key in ("comfyui_path", "comfyui_port", "comfyui_args"):
            val = ctx.params.get(key)
            if val is not None:
                _set_config(ctx, key, val)
        return {"status": "ok"}

    def get_group_slices(self, ctx):
        """Return the dataset's group slices, heatmap fields, and label fields.

        ``slices`` and ``heatmap_fields`` are dataset-level — derived from
        the schema.  ``label_fields`` is **sample-level** — only label
        fields with a non-None value on ``ctx.current_sample`` are
        returned, matching the qwen plugin's "Copy labels" behaviour.
        ``dataset_is_grouped`` is a simple boolean React diffs across
        save calls to detect the flat→grouped migration without
        depending on operator return-value plumbing.

        React calls this after every save and on slice switch so the
        list stays in sync with whichever sample is currently shown.

        Side effect: also refreshes the per-slice
        ``fo_current_sample_<slice>.png`` files in ComfyUI's input dir
        so any newly-created slice immediately becomes available in
        LoadImage dropdowns.
        """
        result = {
            "slices": [],
            "heatmap_fields": [],
            "label_fields": [],
            "dataset_is_grouped": False,
        }
        try:
            dataset = ctx.dataset
            result["dataset_is_grouped"] = bool(dataset.group_field)
            if dataset.group_field:
                media_types = dataset.group_media_types or {}
                for name in dataset.group_slices:
                    result["slices"].append({
                        "name": name,
                        "media_type": media_types.get(name, "image"),
                    })

            schema = dataset.get_field_schema()
            for field_name, field in schema.items():
                if isinstance(field, fo.EmbeddedDocumentField):
                    doc_type = getattr(field, "document_type", None)
                    if doc_type is not None and issubclass(doc_type, fo.Heatmap):
                        result["heatmap_fields"].append(field_name)

            if ctx.current_sample:
                try:
                    sample = dataset[ctx.current_sample]
                    result["label_fields"] = _get_sample_label_fields(dataset, sample)
                except Exception as exc:
                    print(f"[comfyui-plugin] get_group_slices: label fields error: {exc}")
        except Exception as exc:
            print(f"[comfyui-plugin] get_group_slices error: {exc}")

        # Refresh per-slice files so they appear in the LoadImage dropdown.
        try:
            if ctx.current_sample:
                config = self._safe_get_config(ctx)
                comfyui_path = config["comfyui_path"]
                if os.path.isdir(comfyui_path):
                    _inject_all_slices(comfyui_path, ctx.dataset, ctx.current_sample)
        except Exception as exc:
            print(f"[comfyui-plugin] get_group_slices: slice file refresh error: {exc}")

        return result

    def inject_slice(self, ctx):
        """Inject a specific group slice's image into ComfyUI's input.

        Called from React when the active modal slice changes (via the
        Recoil ``modalGroupSlice`` atom).  Three things happen:

        1. The selected slice is copied to ``fo_current_sample.png`` so
           any LoadImage node already pointing at that filename refreshes
           its preview to the new slice.
        2. ``_inject_all_slices`` runs to refresh the per-slice
           ``fo_current_sample_<slice>.png`` files in ComfyUI's input
           dir, so the LoadImage dropdown stays accurate even if a save
           created a new slice since the last initialize.
        3. Panel state is updated with the slice's sample_id / filepath
           so React's ``data.current_sample_id`` / ``data.current_filepath``
           reflect the active slice.  This is belt-and-suspenders: the
           ``on_change_group_slice`` lifecycle hook *should* do the same
           thing, but it doesn't fire reliably in modal composite views,
           so we do it from the Recoil-driven path too.

        Returns the new sample_filename + filepath so React can post a
        SAMPLE_CHANGED message to the bridge.
        """
        slice_name = ctx.params.get("slice_name", "")
        if not slice_name:
            return {"error": "No slice_name provided", "sample_filename": ""}

        config = self._safe_get_config(ctx)
        comfyui_path = config["comfyui_path"]

        try:
            dataset = ctx.dataset
            if not ctx.current_sample:
                return {"error": "No current sample", "sample_filename": ""}

            sample = dataset[ctx.current_sample]
            gf = dataset.group_field

            if not gf:
                return {"error": "Dataset is not grouped", "sample_filename": ""}

            group_elem = sample[gf]
            if not group_elem:
                return {"error": "Sample has no group element", "sample_filename": ""}

            if group_elem.name == slice_name:
                filepath = sample.filepath
                sample_id = sample.id
            else:
                slice_sample = (
                    dataset
                    .select_group_slices(slice_name)
                    .match(F(f"{gf}._id") == bson.ObjectId(group_elem.id))
                    .first()
                )
                if slice_sample is None:
                    return {"error": f"No sample in slice '{slice_name}' for this group", "sample_filename": ""}
                filepath = slice_sample.filepath
                sample_id = slice_sample.id

            print(f"[comfyui-plugin] inject_slice: slice={slice_name} sample_id={sample_id} filepath={filepath}")
            sample_filename = _inject_sample(comfyui_path, filepath)
            if not sample_filename:
                return {"error": f"Cannot inject non-image file: {filepath}", "sample_filename": ""}

            # Push the resolved sample_id / filepath into panel state so
            # React's ``data.current_sample_id`` reflects the active
            # slice — this matters because the save operator falls back
            # to React's params if ctx-resolution returns empty (rare
            # but possible).  Most importantly, this updates state even
            # when ``on_change_group_slice`` doesn't fire.
            try:
                ctx.panel.set_state("current_filepath", filepath)
                ctx.panel.set_state("current_sample_id", sample_id)
            except Exception as exc:
                print(f"[comfyui-plugin] inject_slice: set_state error: {exc}")

            # Refresh per-slice files too — a save may have created new
            # slices since the last initialize, and we want them in the
            # LoadImage dropdown without forcing a panel reload.
            try:
                _inject_all_slices(comfyui_path, dataset, ctx.current_sample)
            except Exception as exc:
                print(f"[comfyui-plugin] inject_slice: per-slice refresh error: {exc}")

            return {"sample_filename": sample_filename, "filepath": filepath, "sample_id": sample_id}

        except Exception as exc:
            print(f"[comfyui-plugin] inject_slice error: {exc}")
            return {"error": str(exc), "sample_filename": ""}

    def trigger_reload(self, ctx):
        """Trigger a dataset + samples reload in the FiftyOne App.

        Called from React after a save completes.  Panel methods
        reliably propagate ``ctx.ops`` to the frontend, unlike
        operators invoked via ``useOperatorExecutor``.

        Note: we deliberately do NOT call ``ctx.ops.notify`` here.
        FiftyOne's toast renders with a higher z-index than our save
        dialog, which would block interaction with subsequent dialogs.
        Visual confirmation already comes from the dialog dismissing
        and the dataset reload showing the new sample/slice.
        """
        print("[comfyui-plugin] trigger_reload: reloading dataset + samples")
        ctx.ops.reload_dataset()
        return {"status": "ok"}

    def render(self, ctx):
        return types.Property(
            types.Object(),
            view=types.View(
                component="ComfyUIPanel",
                composite_view=True,
                initialize=self.initialize,
                start_server=self.start_server,
                stop_server=self.stop_server,
                load_template=self.load_template,
                save_template=self.save_template,
                update_config=self.update_config,
                get_group_slices=self.get_group_slices,
                inject_slice=self.inject_slice,
                trigger_reload=self.trigger_reload,
            ),
        )


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


def _auto_increment_path(base_path: str) -> str:
    """Return *base_path* if it doesn't exist, else append _2, _3, etc."""
    if not os.path.exists(base_path):
        return base_path
    stem, ext = os.path.splitext(base_path)
    idx = 2
    while os.path.exists(f"{stem}_{idx}{ext}"):
        idx += 1
    return f"{stem}_{idx}{ext}"


def _fetch_file_from_comfyui(port: int, filename: str, subfolder: str = "") -> bytes:
    """Download a file (image, video, etc.) from ComfyUI's /view endpoint."""
    resp = requests.get(
        f"http://127.0.0.1:{port}/view",
        params={"filename": filename, "subfolder": subfolder, "type": "output"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


class SaveComfyOutput(foo.Operator):
    """Save output from ComfyUI to the FiftyOne dataset.

    Supports seven output types (image, video, text, depth, detections,
    segmentation, 3d) and multiple destinations (group slice, new
    sample, string field, classification, heatmap, ``fo.Detections``
    field, ``fo.Segmentation`` field).  Fetches metadata from ComfyUI's
    ``/history`` endpoint and stores generation parameters.
    """

    @property
    def config(self):
        return foo.OperatorConfig(
            name="save_comfy_output",
            label="Save ComfyUI Output",
            unlisted=True,
        )

    def execute(self, ctx):
        try:
            # Authoritative source for the save target: resolve at save
            # time using React's ``active_slice`` param (from Recoil's
            # modalGroupSlice atom) + ``ctx.current_sample``.  React's
            # ``data.current_sample_id`` can lag behind slice changes
            # and ``ctx.group_slice`` is unreliable in operator context
            # — so we trust the explicit param above all.
            active_slice_param = ctx.params.get("active_slice") or ""
            resolved_sample_id, resolved_filepath = _resolve_active_slice_sample(
                ctx, slice_override=active_slice_param,
            )
            param_sample_id = ctx.params.get("sample_id") or ""
            param_original_filepath = ctx.params.get("original_filepath") or ""

            print(
                f"[comfyui-plugin] save: target resolution — "
                f"inputs(active_slice={active_slice_param!r}, "
                f"ctx.group_slice={ctx.group_slice!r}, "
                f"ctx.current_sample={ctx.current_sample!r}); "
                f"resolved=({resolved_sample_id!r}, {resolved_filepath!r}); "
                f"React_params=({param_sample_id!r}, {param_original_filepath!r})"
            )

            sample_id = resolved_sample_id or param_sample_id
            original_filepath = resolved_filepath or param_original_filepath

            if (
                resolved_sample_id
                and param_sample_id
                and resolved_sample_id != param_sample_id
            ):
                print(
                    f"[comfyui-plugin] save: ctx-resolved sample_id={resolved_sample_id!r} "
                    f"differs from React's param_sample_id={param_sample_id!r} — using resolved"
                )

            if not sample_id or not original_filepath:
                print(
                    f"[comfyui-plugin] save aborted — no sample loaded "
                    f"(sample_id={sample_id!r}, original_filepath={original_filepath!r})"
                )
                return {"success": False, "error": "No sample loaded — open a sample in the modal first."}

            port = ctx.params.get("port", DEFAULT_COMFYUI_PORT)
            output_type = ctx.params.get("output_type", "image")
            save_as = ctx.params.get("save_as", "group_slice")
            field_name = ctx.params.get("field_name", "comfy_output")
            prompt_id = ctx.params.get("prompt_id")
            node_title = ctx.params.get("node_title", "")

            image_data_b64 = ctx.params.get("image_data", "")
            comfyui_filename = ctx.params.get("comfyui_filename", "")
            comfyui_subfolder = ctx.params.get("comfyui_subfolder", "")
            text_value = ctx.params.get("text_value", "")
            workflow_name = ctx.params.get("workflow_name", "")
            copy_labels = ctx.params.get("copy_labels", "")

            print(f"[comfyui-plugin] === SAVE START ===")
            print(f"[comfyui-plugin]   prompt_id={prompt_id!r}  node_title={node_title!r}  workflow_name={workflow_name!r}")
            print(f"[comfyui-plugin]   output_type={output_type!r}  save_as={save_as!r}  field_name={field_name!r}")
            print(f"[comfyui-plugin]   comfyui_filename={comfyui_filename!r}  has_image_data={bool(image_data_b64)}")
            print(f"[comfyui-plugin]   copy_labels={copy_labels!r}")

            dataset = ctx.dataset
            original_dir = os.path.dirname(original_filepath)
            original_stem = os.path.splitext(os.path.basename(original_filepath))[0]

            metadata = _fetch_comfy_metadata(port, prompt_id)
            if metadata is not None:
                metadata["workflow_name"] = workflow_name
            print(f"[comfyui-plugin]   metadata returned: {metadata is not None}")

            if output_type in ("image", "depth", "video", "3d"):
                self._save_media(
                    dataset, sample_id, original_dir, original_stem,
                    port, output_type, save_as, field_name, node_title,
                    prompt_id, metadata, image_data_b64,
                    comfyui_filename, comfyui_subfolder, copy_labels,
                )
            elif output_type == "text":
                self._save_text(
                    dataset, sample_id, save_as, field_name, text_value,
                )
            elif output_type == "detections":
                self._save_detections(
                    dataset, sample_id, port, field_name, ctx.params,
                )
            elif output_type == "segmentation":
                self._save_segmentation(
                    dataset, sample_id, original_dir, original_stem,
                    port, field_name, ctx.params,
                )
            else:
                print(f"[comfyui-plugin] unsupported output_type: {output_type}")
                return {"success": False, "error": f"Unsupported type: {output_type}"}

            return {"success": True}

        except Exception as e:
            print(f"[comfyui-plugin] save error: {e}")
            print(traceback.format_exc())
            raise

    def _save_media(self, dataset, sample_id, original_dir, original_stem,
                    port, output_type, save_as, field_name, node_title,
                    prompt_id, metadata, image_data_b64, comfyui_filename,
                    comfyui_subfolder, copy_labels=""):
        """Save an image, video, or depth output as a file + dataset entry."""

        if comfyui_filename:
            image_bytes = _fetch_file_from_comfyui(port, comfyui_filename, comfyui_subfolder)
        elif image_data_b64:
            image_bytes = base64.b64decode(image_data_b64)
        else:
            raise ValueError("No image data or ComfyUI filename provided")

        if output_type == "depth" and save_as == "heatmap":
            map_path = _auto_increment_path(
                os.path.join(original_dir, f"{original_stem}_{int(time.time())}.png")
            )
            with open(map_path, "wb") as f:
                f.write(image_bytes)

            sample = dataset[sample_id]
            schema = dataset.get_field_schema()
            if field_name not in schema:
                dataset.add_sample_field(
                    field_name, fo.EmbeddedDocumentField,
                    embedded_doc_type=fo.Heatmap,
                )
            sample[field_name] = fo.Heatmap(map_path=map_path)
            sample.save()
            print(f"[comfyui-plugin] saved depth heatmap field '{field_name}' → {map_path}")
            return

        # On-disk filename uses a unix timestamp so it is stable across
        # save_as modes (new_sample / group_slice), unique per save, and
        # never depends on user-entered slice or field names.  The user's
        # field_name still controls the slice/field in the dataset.
        #
        # 3D inputs preserve the upstream extension (.glb / .ply / .obj /
        # .stl / .fbx / .pcd / .fo3d) so FiftyOne can route them through
        # the right loader.  Falls back to .glb if the dispatch lacked a
        # filename — shouldn't happen in practice, but keeps the path
        # well-formed.
        if output_type == "video":
            ext = ".mp4"
        elif output_type == "3d":
            ext = os.path.splitext(comfyui_filename)[1].lower() or ".glb"
        else:
            ext = ".png"
        output_path = _auto_increment_path(
            os.path.join(original_dir, f"{original_stem}_{int(time.time())}{ext}")
        )
        with open(output_path, "wb") as f:
            f.write(image_bytes)

        # Per the FiftyOne 3D docs, `.glb / .ply / .obj / .stl / .fbx /
        # .pcd / .fo3d` files all use ``media_type="3d"`` (PCDs included
        # — see the direct-asset example in the user guide).  We pass it
        # explicitly so we don't rely on FiftyOne's extension inference,
        # which has shifted between versions.
        sample_media_type = "3d" if output_type == "3d" else None

        if save_as == "new_sample":
            _ensure_comfy_fields(dataset)
            sample_kwargs = {"filepath": output_path, "tags": ["comfy_output"]}
            if sample_media_type:
                sample_kwargs["media_type"] = sample_media_type

            # If the dataset is grouped, every sample MUST be in a group.
            # The new sample gets its own brand-new group on a slice that
            # matches its media type — it appears as a standalone entry
            # in the grid view, with ``source_sample_id`` linking back to
            # the original sample.
            gf = dataset.group_field
            if gf:
                if output_type == "video":
                    target_media = "video"
                elif output_type == "3d":
                    target_media = "3d"
                else:
                    target_media = "image"
                slice_name = _ensure_compatible_slice(dataset, target_media)
                sample_kwargs[gf] = fo.Group().element(slice_name)
                print(f"[comfyui-plugin]   grouped dataset: new sample → new group, slice='{slice_name}'")

            new_sample = fo.Sample(**sample_kwargs)
            new_sample["comfy_node_title"] = node_title
            new_sample["comfy_prompt_id"] = prompt_id or ""
            new_sample["source_sample_id"] = sample_id
            if metadata:
                self._apply_metadata(new_sample, metadata, dataset)
            self._copy_labels(dataset, sample_id, new_sample, copy_labels)
            dataset.add_sample(new_sample)
            print(f"[comfyui-plugin] saved new sample from {sample_id}")
        else:
            slice_name = field_name
            group_id = _ensure_grouped(dataset, sample_id)
            _ensure_comfy_fields(dataset)

            gf = dataset.group_field
            sample_kwargs = {
                "filepath": output_path,
                gf: fo.Group(id=group_id).element(slice_name),
                "parent_sample_id": sample_id,
                "comfy_node_title": node_title,
                "comfy_prompt_id": prompt_id or "",
                "tags": ["comfy_output"],
            }
            if sample_media_type:
                sample_kwargs["media_type"] = sample_media_type
            new_sample = fo.Sample(**sample_kwargs)

            if metadata:
                self._apply_metadata(new_sample, metadata, dataset)
            self._copy_labels(dataset, sample_id, new_sample, copy_labels)

            # ``add_sample`` auto-registers the slice in
            # ``dataset.group_media_types`` via ``_expand_group_schema``,
            # using the new sample's inferred media type — no need for an
            # explicit ``add_group_slice`` call here.
            dataset.add_sample(new_sample)

            prompt_id_val = (
                new_sample.get_field("comfy_prompt_id")
                if new_sample.has_field("comfy_prompt_id")
                else "?"
            )
            print(
                f"[comfyui-plugin] saved slice '{slice_name}' for sample {sample_id}"
                f" (new sample id={new_sample.id}, comfy_prompt_id={prompt_id_val})"
            )

    @staticmethod
    def _copy_labels(dataset, source_sample_id, target_sample, copy_labels):
        """Deep-copy selected label fields from the source onto a new sample.

        Schema fields already exist (we copy from an existing labelled
        sample) so no ``add_sample_field`` is required.  Silently skips
        fields that aren't on the source — matches the qwen plugin.
        """
        names = _parse_copy_labels(copy_labels)
        if not names:
            return
        try:
            source_sample = dataset[source_sample_id]
        except Exception as exc:
            print(f"[comfyui-plugin] _copy_labels: source lookup failed: {exc}")
            return
        for name in names:
            val = source_sample.get_field(name)
            if val is not None:
                target_sample[name] = copy.deepcopy(val)
                print(f"[comfyui-plugin]   copied label '{name}' from {source_sample_id}")

    @staticmethod
    def _save_detections(dataset, sample_id, port, field_name, params):
        """Save detections produced by FO_SaveDetections as fo.Detections.

        Polymorphic — accepts both BBOX-style (list-of-list-of-floats) and
        SAM3-style (JSON string of the same shape) box payloads.  Labels
        and scores follow the same flexibility rules.

        Sources of truth (in order of preference):

        - **Image dimensions**: ``image_height``/``image_width`` from the
          payload (set by the node when it has the ``image`` socket
          connected); else read from ``sample.metadata`` server-side.
        - **Boxes**: ``boxes_json`` if provided; else derived from each
          mask's tight enclosure (``np.where``) if only masks are present.
          Matches FiftyOne's ``fo.Detection`` convention — every
          detection has a bbox; masks ride along cropped to that bbox.
        - **Labels**: upstream ``pred_labels_json`` if provided; else
          the user's pill widget (cycled round-robin); else ``"object"``.
        - **Scores**: upstream ``scores_json`` if provided; else None.

        Pixel-space xyxy boxes are converted to FiftyOne's normalized
        rxywh.  Per-detection masks are reconstructed from the ``.npy``
        file the node wrote into ComfyUI's output dir.
        """
        boxes_json = params.get("boxes_json", "")
        pred_labels_json = params.get("pred_labels_json", "")
        scores_json = params.get("scores_json", "")
        masks_filename = params.get("masks_filename", "")
        fallback_labels = params.get("fallback_labels", "")
        image_height = int(params.get("image_height", 0) or 0)
        image_width = int(params.get("image_width", 0) or 0)

        print(
            f"[comfyui-plugin] _save_detections: field={field_name!r}, "
            f"image_payload=({image_height}x{image_width}), "
            f"boxes_json_len={len(boxes_json or '')}, "
            f"labels_json_len={len(pred_labels_json or '')}, "
            f"scores_json_len={len(scores_json or '')}, "
            f"masks_filename={masks_filename!r}, "
            f"fallback_labels={fallback_labels!r}"
        )

        # Load masks first so we can derive bboxes from them if needed.
        masks_arr = None
        if masks_filename:
            try:
                npy_bytes = _fetch_file_from_comfyui(port, masks_filename, "")
                masks_arr = np.load(_io.BytesIO(npy_bytes))
                print(
                    f"[comfyui-plugin]   loaded masks: shape={masks_arr.shape}, "
                    f"dtype={masks_arr.dtype}"
                )
            except Exception as exc:
                print(f"[comfyui-plugin]   mask load failed (continuing without masks): {exc}")
                masks_arr = None

        # The node-side already drops boxes that aren't ``[[x1,y1,x2,y2],
        # ...]`` shaped (see FO_SaveDetections.execute), so by the time
        # we get here ``boxes_json`` is either valid bbox JSON or empty.
        boxes = _parse_jsonish_list(boxes_json)

        # Boxes-from-masks fallback (instance-segmentation-style workflows
        # where the user only connected MASK output, e.g. SAM2 / SAM3
        # mask-only flows).
        if not boxes and masks_arr is not None:
            boxes = _bboxes_from_masks(masks_arr)
            print(f"[comfyui-plugin]   derived {len(boxes)} bbox(es) from masks")

        if not boxes:
            print(
                "[comfyui-plugin]   nothing to save — no boxes from upstream "
                "and no masks to derive them from"
            )
            return

        # Image dims: payload first (post-processing-aware), then sample
        # metadata.  Both being zero falls through to identity-norm
        # which the user can correct by hooking up `image`.
        if image_height <= 0 or image_width <= 0:
            try:
                sample = dataset[sample_id]
                meta = sample.metadata or sample.compute_metadata()
                image_height = int(getattr(meta, "height", 0) or 0)
                image_width = int(getattr(meta, "width", 0) or 0)
                print(
                    f"[comfyui-plugin]   inferred image size from sample.metadata "
                    f"→ {image_height}x{image_width}"
                )
            except Exception as exc:
                print(f"[comfyui-plugin]   sample metadata lookup failed: {exc}")

        if (image_height <= 0 or image_width <= 0) and masks_arr is not None:
            image_height = image_height or int(masks_arr.shape[-2])
            image_width = image_width or int(masks_arr.shape[-1])
            print(
                f"[comfyui-plugin]   inferred image size from masks "
                f"→ {image_height}x{image_width}"
            )

        labels = _resolve_detection_labels(
            pred_labels_json, fallback_labels, len(boxes),
        )
        scores = _parse_jsonish_list(scores_json) or [None] * len(boxes)

        detections = []
        skipped_degenerate = 0
        skipped_malformed = 0
        for i, box in enumerate(boxes):
            if not isinstance(box, (list, tuple)) or len(box) < 4:
                print(f"[comfyui-plugin]   skipping malformed box[{i}]={box!r}")
                skipped_malformed += 1
                continue
            x1, y1, x2, y2 = (float(v) for v in box[:4])
            # Skip degenerate (zero-area) bboxes — they render invisibly
            # in the FiftyOne app and lead to silent "field exists but
            # empty" UX. Common when an upstream mask was all-zero.
            if (x2 - x1) <= 0 or (y2 - y1) <= 0:
                print(f"[comfyui-plugin]   skipping degenerate box[{i}]={box!r} (zero area)")
                skipped_degenerate += 1
                continue
            if image_width <= 0 or image_height <= 0:
                rx, ry, rw, rh = x1, y1, x2 - x1, y2 - y1
            else:
                rx = x1 / image_width
                ry = y1 / image_height
                rw = (x2 - x1) / image_width
                rh = (y2 - y1) / image_height

            label = labels[i] if i < len(labels) else "object"
            confidence = scores[i] if i < len(scores) else None
            kwargs = {
                "label": str(label),
                "bounding_box": [rx, ry, rw, rh],
            }
            if confidence is not None:
                try:
                    kwargs["confidence"] = float(confidence)
                except (TypeError, ValueError):
                    pass

            if masks_arr is not None:
                try:
                    crop = _crop_mask_to_bbox(masks_arr, i, x1, y1, x2, y2)
                    if crop is not None:
                        kwargs["mask"] = crop
                except Exception as exc:
                    print(f"[comfyui-plugin]   mask crop[{i}] failed: {exc}")

            detections.append(fo.Detection(**kwargs))

        if skipped_degenerate or skipped_malformed:
            print(
                f"[comfyui-plugin]   skipped {skipped_degenerate} degenerate + "
                f"{skipped_malformed} malformed box(es) "
                f"out of {len(boxes)} input(s)"
            )
        if not detections:
            print(
                f"[comfyui-plugin] nothing usable to save → field {field_name!r} not "
                f"created (check that upstream model produced non-empty masks)"
            )
            return

        sample = dataset[sample_id]
        schema = dataset.get_field_schema()
        if field_name not in schema:
            dataset.add_sample_field(
                field_name, fo.EmbeddedDocumentField,
                embedded_doc_type=fo.Detections,
            )
        sample[field_name] = fo.Detections(detections=detections)
        sample.save()
        print(
            f"[comfyui-plugin] saved {len(detections)} detection(s) → "
            f"{field_name!r} on sample {sample_id}"
        )

    @staticmethod
    def _save_segmentation(dataset, sample_id, original_dir, original_stem,
                           port, field_name, params):
        """Save a segmentation mask as ``fo.Segmentation`` on the sample.

        The mask PNG is fetched from ComfyUI's output dir, copied next
        to the source sample's filepath (so it travels with the dataset),
        and stored on the sample via ``mask_path``.
        """
        comfyui_filename = params.get("comfyui_filename", "")
        mask_targets_str = params.get("mask_targets", "")

        print(
            f"[comfyui-plugin] _save_segmentation: field={field_name!r}, "
            f"comfyui_filename={comfyui_filename!r}, "
            f"mask_targets={mask_targets_str!r}"
        )

        if not comfyui_filename:
            print("[comfyui-plugin]   no mask filename provided — abort")
            return

        png_bytes = _fetch_file_from_comfyui(port, comfyui_filename, "")
        mask_path = _auto_increment_path(
            os.path.join(original_dir, f"{original_stem}_seg_{int(time.time())}.png")
        )
        with open(mask_path, "wb") as f:
            f.write(png_bytes)
        print(f"[comfyui-plugin]   wrote mask → {mask_path}")

        seg_kwargs = {"mask_path": mask_path}
        targets = _parse_mask_targets(mask_targets_str)
        if targets:
            seg_kwargs["mask_targets"] = targets
            print(f"[comfyui-plugin]   mask_targets parsed: {targets}")

        sample = dataset[sample_id]
        schema = dataset.get_field_schema()
        if field_name not in schema:
            dataset.add_sample_field(
                field_name, fo.EmbeddedDocumentField,
                embedded_doc_type=fo.Segmentation,
            )
        sample[field_name] = fo.Segmentation(**seg_kwargs)
        sample.save()
        print(
            f"[comfyui-plugin] saved segmentation → "
            f"{field_name!r} on sample {sample_id}"
        )

    @staticmethod
    def _apply_metadata(sample, metadata: dict, dataset: fo.Dataset):
        """Copy ComfyUI generation metadata onto a sample."""
        print(f"[comfyui-plugin] _apply_metadata: applying to sample {sample.filepath}")

        fields = {
            "comfy_workflow_name": metadata.get("workflow_name", ""),
            "comfy_prompt": metadata.get("prompt", ""),
            "comfy_negative_prompt": metadata.get("negative_prompt", ""),
            "comfy_seed": metadata.get("seed"),
            "comfy_steps": metadata.get("steps"),
            "comfy_cfg": metadata.get("cfg"),
            "comfy_sampler": metadata.get("sampler"),
            "comfy_scheduler": metadata.get("scheduler"),
            "comfy_denoise": metadata.get("denoise"),
            "comfy_model": metadata.get("model", ""),
        }
        for k, v in fields.items():
            if k in _METADATA_STR_FIELDS and v is not None and not isinstance(v, str):
                v = ", ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
            sample[k] = v
            if v is not None and v != "":
                print(f"[comfyui-plugin]   {k} = {v!r}")

        wf_json = metadata.get("workflow_json")
        if wf_json:
            if "comfy_workflow_json" not in dataset.get_field_schema():
                dataset.add_sample_field("comfy_workflow_json", fo.StringField)
            sample["comfy_workflow_json"] = (
                json.dumps(wf_json) if isinstance(wf_json, dict) else str(wf_json)
            )

    @staticmethod
    def _save_text(dataset, sample_id, save_as, field_name, text_value):
        """Save a text output as a sample field."""
        sample = dataset[sample_id]
        schema = dataset.get_field_schema()

        if save_as == "classification":
            if field_name not in schema:
                dataset.add_sample_field(field_name, fo.EmbeddedDocumentField, embedded_doc_type=fo.Classification)
            sample[field_name] = fo.Classification(label=text_value)
        else:
            if field_name not in schema:
                dataset.add_sample_field(field_name, fo.StringField)
            sample[field_name] = text_value

        sample.save()
        print(f"[comfyui-plugin] saved text field '{field_name}' on sample {sample_id}")


class GetComfyTemplates(foo.Operator):
    """Return available workflow templates filtered by media type."""

    @property
    def config(self):
        return foo.OperatorConfig(
            name="get_comfy_templates",
            label="Get ComfyUI Templates",
            unlisted=True,
        )

    def execute(self, ctx):
        filepath = ctx.params.get("filepath", "")
        media_type = _get_media_type(filepath) if filepath else "image"
        print(
            f"[comfyui-plugin] GetComfyTemplates: filepath={filepath!r}, "
            f"media_type={media_type!r}"
        )

        try:
            manifest = _load_manifest()
        except Exception as e:
            print(f"[comfyui-plugin] GetComfyTemplates: manifest error: {e}")
            traceback.print_exc()
            return {"templates": [], "default": None}

        all_templates = manifest.get("templates", [])
        compatible = [
            t for t in all_templates
            if media_type in t.get("input_types", [])
        ]
        compat_ids = [t["id"] for t in compatible]
        print(
            f"[comfyui-plugin] GetComfyTemplates: returning {len(compatible)}/{len(all_templates)} "
            f"compatible template(s) for media_type={media_type!r}: {compat_ids}"
        )

        return {
            "templates": compatible,
            "default": compatible[0]["id"] if compatible else None,
        }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(p):
    p.register(ComfyUIPanel)
    p.register(SaveComfyOutput)
    p.register(GetComfyTemplates)
