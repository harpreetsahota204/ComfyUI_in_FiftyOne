"""FiftyOne custom ComfyUI nodes.

All nodes appear under the ``FiftyOne/IO`` category in ComfyUI's Add Node menu.
"""

import os
import shutil
import subprocess
import time
import numpy as np
from PIL import Image

from server import PromptServer
import folder_paths


class _AnyType(str):
    """Wildcard type that matches any ComfyUI connection."""
    def __ne__(self, __value: object) -> bool:
        return False

ANY = _AnyType("*")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _unique_suffix():
    """Return a 7-digit suffix derived from the current millisecond clock.

    Used to keep filenames unique within ComfyUI's output dir without
    relying on a counter (the node has no persistent state across
    workflow runs).  The mod-10M wraparound keeps filenames short; a
    collision requires two saves whose millisecond timestamps either
    coincide or differ by an exact multiple of 10,000,000 ms (~167
    minutes).  Acceptable for a temp-output directory.
    """
    return f"{int(time.time() * 1000) % 10_000_000:07d}"


def _save_image_tensor(image_tensor, output_dir, prefix):
    """Save a ComfyUI IMAGE tensor [B,H,W,C] to the output directory."""
    i = 255.0 * image_tensor[0].cpu().numpy()
    img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
    filename = f"fo_{prefix}_{_unique_suffix()}.png"
    img.save(os.path.join(output_dir, filename))
    return filename


def _save_video_tensor(image_tensor, output_dir, prefix, fps=24.0):
    """Encode a ComfyUI IMAGE tensor [B,H,W,C] as an H.264 MP4.

    Each element along the batch dimension is treated as one video frame.
    Requires ``ffmpeg`` on ``$PATH``.
    """
    frames = (np.clip(image_tensor.cpu().numpy(), 0, 1) * 255).astype(np.uint8)
    num_frames, height, width, _ = frames.shape

    # Ensure dimensions are divisible by 2 (required by libx264 yuv420p)
    pad_h = height % 2
    pad_w = width % 2
    if pad_h or pad_w:
        frames = np.pad(
            frames,
            ((0, 0), (0, pad_h), (0, pad_w), (0, 0)),
            mode="edge",
        )
        _, height, width, _ = frames.shape

    filename = f"fo_{prefix}_{_unique_suffix()}.mp4"
    filepath = os.path.join(output_dir, filename)

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", "19",
        "-preset", "fast",
        "-movflags", "+faststart",
        filepath,
    ]

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for i in range(num_frames):
        proc.stdin.write(frames[i].tobytes())
    proc.stdin.close()
    _, stderr = proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg exited with code {proc.returncode}: {stderr.decode(errors='replace')[-500:]}"
        )

    return filename


def _copy_video_file(source_path, output_dir, prefix):
    """Copy an existing video file into the ComfyUI output directory."""
    ext = os.path.splitext(source_path)[1] or ".mp4"
    filename = f"fo_{prefix}_{_unique_suffix()}{ext}"
    shutil.copy2(source_path, os.path.join(output_dir, filename))
    return filename


def _resolve_video_input(video, output_dir, prefix, fps):
    """Accept any common ComfyUI video representation and produce an MP4.

    Handles:
    - ``torch.Tensor`` [B,H,W,C] — encode frames via ffmpeg
    - ``str`` filepath — copy the existing video file
    - ``dict`` with a ``"filename"`` or ``"value"`` key — extract path
    - ``list``/``tuple`` (e.g. VHS_FILENAMES) — walk until a file is found
    - Objects with a ``save_to(path)`` method (e.g. ``VideoFromComponents``)
    """
    import torch

    if isinstance(video, torch.Tensor) and video.ndim == 4:
        return _save_video_tensor(video, output_dir, prefix, fps)

    if isinstance(video, str) and os.path.isfile(video):
        return _copy_video_file(video, output_dir, prefix)

    if isinstance(video, dict):
        for key in ("filename", "value", "path", "file"):
            path = video.get(key, "")
            if isinstance(path, str) and os.path.isfile(path):
                return _copy_video_file(path, output_dir, prefix)

    if isinstance(video, (list, tuple)):
        for item in video:
            if isinstance(item, str) and os.path.isfile(item):
                return _copy_video_file(item, output_dir, prefix)
            if isinstance(item, (list, tuple)):
                for sub in item:
                    if isinstance(sub, str) and os.path.isfile(sub):
                        return _copy_video_file(sub, output_dir, prefix)
            if isinstance(item, dict):
                for key in ("filename", "value", "path", "file"):
                    path = item.get(key, "")
                    if isinstance(path, str) and os.path.isfile(path):
                        return _copy_video_file(path, output_dir, prefix)

    # VideoInput subclasses (e.g. VideoFromComponents, VideoFromFile) have
    # a save_to(path) method that encodes to MP4 directly.
    if hasattr(video, "save_to") and callable(video.save_to):
        filename = f"fo_{prefix}_{_unique_suffix()}.mp4"
        filepath = os.path.join(output_dir, filename)
        video.save_to(filepath)
        return filename

    raise ValueError(
        f"Cannot extract video from input type {type(video).__name__}. "
        f"Supported: IMAGE tensor (batch of frames), filepath string, "
        f"VHS_FILENAMES, dict with 'filename' key, or VideoInput objects."
    )


_SAVE_MODE_WIDGET = (["new_sample", "group_slice"], {
    "default": "new_sample",
    "tooltip": "How to save: as a group slice alongside the original, or as a standalone new sample",
})

_NAME_WIDGET = ("STRING", {
    "default": "comfy_output",
    "tooltip": "Slice name when saving as a group slice in FiftyOne",
})

# Wire format for ``labels``:
#   ""               → copy nothing (default)
#   "field_a,field_b" → copy those label fields from the source sample
# The bridge JS replaces this widget's onClick with a custom multi-select
# picker that emits the comma-separated form.
_LABELS_WIDGET = ("STRING", {
    "default": "",
    "tooltip": "Label fields to copy from the source sample onto the new sample. Click to pick.",
})


# ---------------------------------------------------------------------------
# FO_SaveImage
# ---------------------------------------------------------------------------

class FO_SaveImage:
    """Save an image output to FiftyOne."""

    CATEGORY = "FiftyOne/IO"
    FUNCTION = "execute"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = "Save an image to FiftyOne as a new sample or group slice."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"image": ("IMAGE",)},
            "optional": {
                "save_mode": _SAVE_MODE_WIDGET,
                "name": _NAME_WIDGET,
                "labels": _LABELS_WIDGET,
            },
        }

    def execute(self, image, save_mode="new_sample", name="comfy_output", labels="", **_):
        filename = _save_image_tensor(image, folder_paths.get_output_directory(), name)
        PromptServer.instance.send_sync("fiftyone.save_output", {
            "type": "image",
            "save_mode": save_mode,
            "name": name,
            "filename": filename,
            "subfolder": "",
            "copy_labels": labels,
        })
        return {}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")


# ---------------------------------------------------------------------------
# FO_SaveVideo
# ---------------------------------------------------------------------------

class FO_SaveVideo:
    """Save any video output to FiftyOne as an H.264 MP4.

    Accepts IMAGE tensors (batch of frames), file paths, VHS_FILENAMES,
    or any dict/tuple containing a path to a video file.  When given
    raw frames, encodes via ffmpeg; when given an existing file, copies it.
    """

    CATEGORY = "FiftyOne/IO"
    FUNCTION = "execute"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Save video to FiftyOne. Connects to any video output — IMAGE "
        "frame batches, VHS_FILENAMES, file paths, etc. Requires ffmpeg "
        "for frame encoding."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"video": (ANY, {})},
            "optional": {
                "fps": ("FLOAT", {
                    "default": 24.0,
                    "min": 1.0,
                    "max": 120.0,
                    "step": 0.5,
                    "tooltip": "Frames per second (used when encoding raw frames)",
                }),
                "save_mode": _SAVE_MODE_WIDGET,
                "name": _NAME_WIDGET,
                "labels": _LABELS_WIDGET,
            },
        }

    def execute(self, video, fps=24.0, save_mode="new_sample", name="comfy_output", labels="", **_):
        filename = _resolve_video_input(
            video, folder_paths.get_output_directory(), name, fps,
        )
        PromptServer.instance.send_sync("fiftyone.save_output", {
            "type": "video",
            "save_mode": save_mode,
            "name": name,
            "filename": filename,
            "subfolder": "",
            "copy_labels": labels,
        })
        return {}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")


# ---------------------------------------------------------------------------
# FO_SaveText
# ---------------------------------------------------------------------------

class FO_SaveText:
    """Save a text output to FiftyOne as a sample field."""

    CATEGORY = "FiftyOne/IO"
    FUNCTION = "execute"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = "Save text to FiftyOne as a string or classification field."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"text": ("STRING", {"forceInput": True})},
            "optional": {
                "name": ("STRING", {
                    "default": "comfy_text",
                    "tooltip": "Field name to store the text on the sample",
                }),
            },
        }

    def execute(self, text, name="comfy_text", **_):
        PromptServer.instance.send_sync("fiftyone.save_output", {
            "type": "text",
            "save_mode": "string_field",
            "name": name,
            "text": text,
        })
        return {}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")


# ---------------------------------------------------------------------------
# FO_SaveDepth
# ---------------------------------------------------------------------------

class FO_SaveDepth:
    """Save a depth map as a Heatmap field on the current FiftyOne sample."""

    CATEGORY = "FiftyOne/IO"
    FUNCTION = "execute"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Save a depth map to FiftyOne as a Heatmap field on the current sample. "
        "The 'name' widget sets the field name (e.g. 'depth')."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"depth": ("IMAGE",)},
            "optional": {
                "name": ("STRING", {
                    "default": "depth",
                    "tooltip": "Heatmap field name on the FiftyOne sample",
                }),
            },
        }

    def execute(self, depth, name="depth", **_):
        filename = _save_image_tensor(depth, folder_paths.get_output_directory(), name)
        PromptServer.instance.send_sync("fiftyone.save_output", {
            "type": "depth",
            "save_mode": "heatmap",
            "name": name,
            "filename": filename,
            "subfolder": "",
        })
        return {}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")


# ---------------------------------------------------------------------------
# Detection / segmentation save helpers
# ---------------------------------------------------------------------------
#
# The grounding/SAM3 packs emit detections in two different shapes:
#   - ComfyUI-Grounding: boxes as BBOX list (List[List[float]]), labels as
#     STRING (period-joined), scores as FLOAT (list).
#   - ComfyUI-SAM3:      boxes/scores as STRING-JSON, no labels output.
#
# We accept either via ANY-typed inputs and serialize them as JSON before
# shipping to the FiftyOne operator over PromptServer.send_sync (which is
# WebSocket → JSON, so no torch tensors / numpy can ride along).  Masks
# are saved to the output dir as a .npy file and the filename is passed
# instead.

import json as _json


def _coerce_to_jsonable(value):
    """Best-effort JSON-friendly form for a polymorphic upstream value.

    - None / empty → ``None``
    - JSON-decodable string → parsed structure
    - list/tuple → recursively coerced
    - numbers / strings / booleans → as-is
    - tensors / numpy arrays → ``.tolist()``
    - everything else → ``repr()`` (debug aid only)
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return _json.loads(s)
        except (ValueError, TypeError):
            return s
    if hasattr(value, "tolist") and callable(value.tolist):
        try:
            return value.tolist()
        except Exception:
            pass
    if isinstance(value, (list, tuple)):
        return [_coerce_to_jsonable(v) for v in value]
    if isinstance(value, (int, float, bool)):
        return value
    return repr(value)


def _save_mask_tensor_npy(mask_tensor, output_dir, prefix):
    """Save a MASK tensor (1HW or NHW float in [0,1]) as a uint8 .npy file.

    Returns the filename (relative to output_dir).  We use .npy so the
    operator can losslessly reconstruct per-instance masks; PNG would
    quantize and a single PNG can't hold N separate masks.
    """
    arr = mask_tensor.detach().cpu().numpy()
    if arr.ndim == 2:
        arr = arr[None, ...]
    arr8 = (np.clip(arr, 0.0, 1.0) * 255).astype(np.uint8)
    filename = f"fo_masks_{prefix}_{_unique_suffix()}.npy"
    np.save(os.path.join(output_dir, filename), arr8)
    return filename, list(arr8.shape)


def _save_segmask_tensor_png(mask_tensor, output_dir, prefix, threshold=0.5):
    """Save a MASK tensor as an indexed PNG suitable for ``fo.Segmentation``.

    Output PNG values follow FiftyOne's convention:
    - ``0`` = background
    - ``1..N`` = class IDs (mapped via ``mask_targets``)

    Handles three input shapes:

    - ``[H, W]``                — single binary mask. Float values are
      thresholded; integer values are kept as-is (presumed already
      class-indexed).
    - ``[1, H, W]``             — single mask, same handling.
    - ``[N, H, W]`` with N > 1  — multi-instance: each instance becomes a
      unique class index ``1..N``. Pixels covered by multiple instances
      go to whichever has the highest probability. Pixels not covered
      by any instance stay ``0`` (background).

    The previous implementation argmax'd directly over the leading axis,
    which gave background pixels an instance class of 0 (bug — the first
    instance "swallowed" all background, and only the other instances
    looked like real masks).

    Returns ``(filename, shape, num_classes)`` where ``num_classes`` is
    the count of unique non-zero class IDs in the output. Callers can
    use it to auto-populate ``mask_targets`` if the user didn't.
    """
    arr = mask_tensor.detach().cpu().numpy()

    if arr.ndim == 3 and arr.shape[0] > 1:
        # Multi-instance — proper background handling
        thr = threshold if arr.max() <= 1.0 else (threshold * 255)
        any_positive = (arr > thr).any(axis=0)
        winner_idx = np.argmax(arr, axis=0).astype(np.uint8)
        out = np.where(any_positive, winner_idx + 1, 0).astype(np.uint8)
    else:
        if arr.ndim == 3:
            arr = arr[0]
        if arr.dtype != np.uint8:
            if arr.max() <= 1.0:
                # Binary float mask → 0/1 indexing
                out = (arr > threshold).astype(np.uint8)
            else:
                out = arr.astype(np.uint8)
        else:
            out = arr

    num_classes = int(np.unique(out[out > 0]).size)
    filename = f"fo_segmask_{prefix}_{_unique_suffix()}.png"
    Image.fromarray(out, mode="L").save(os.path.join(output_dir, filename))
    print(
        f"[_save_segmask_tensor_png] wrote {filename}: shape={out.shape}, "
        f"num_classes={num_classes}, max={int(out.max())}"
    )
    return filename, list(out.shape), num_classes


# ---------------------------------------------------------------------------
# FO_SaveDetections — ingest BBOX / STRING-JSON detections as fo.Detections
# ---------------------------------------------------------------------------

class FO_SaveDetections:
    """Save object detections (boxes + optional labels/scores/masks) to FiftyOne.

    Polymorphic — accepts both ComfyUI-Grounding-style outputs (``BBOX``
    list, ``STRING`` labels, ``FLOAT`` scores, ``MASK``) and
    ComfyUI-SAM3-style outputs (``STRING``-JSON boxes / scores, ``MASK``)
    on the same sockets.  Detections land on the *current* FiftyOne
    sample (active group slice).

    All inputs are **optional** by design — connect what you have:

    - With **boxes only**: each box becomes one ``fo.Detection`` (label
      defaults to ``"object"`` or comes from the ``labels`` widget pills).
    - With **boxes + labels**: per-detection labels populate
      ``fo.Detection.label``.
    - With **boxes + masks**: each mask is cropped to its bbox and
      attached to ``fo.Detection.mask``.
    - With **masks only** (e.g. SAM2 output): bboxes are derived from
      each mask via tight enclosure (``np.where``) — matches FiftyOne's
      convention for instance segmentation.

    Image dimensions for normalization come from the optional ``image``
    socket if connected; otherwise from the source sample's
    ``metadata.width / metadata.height`` server-side.
    """

    CATEGORY = "FiftyOne/IO"
    FUNCTION = "execute"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Save detections to FiftyOne as fo.Detections on the current sample. "
        "Polymorphic — connect any combination of boxes/labels/scores/masks; "
        "boxes can be BBOX lists or STRING JSON. With masks-only, bboxes "
        "are auto-derived from each mask. No image input required — "
        "dimensions come from the source sample's metadata."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "optional": {
                "boxes": (ANY, {
                    "forceInput": True,
                    "tooltip": "Bounding boxes — BBOX list or STRING JSON of [[x1,y1,x2,y2],...]",
                }),
                "pred_labels": (ANY, {
                    "forceInput": True,
                    "tooltip": "Optional per-detection labels — list of strings or period/comma-separated STRING",
                }),
                "scores": (ANY, {
                    "forceInput": True,
                    "tooltip": "Optional per-detection confidence scores — FLOAT list or STRING JSON",
                }),
                "masks": ("MASK", {
                    "forceInput": True,
                    "tooltip": "Optional per-instance binary masks ([N,H,W]) — cropped to bbox per detection",
                }),
                "image": ("IMAGE", {
                    "forceInput": True,
                    "tooltip": "Optional reference image — only used to source H/W for box normalization. If omitted, source sample's metadata is used.",
                }),
                "field": ("STRING", {
                    "default": "detections",
                    "tooltip": "Field name on the FiftyOne sample (creates fo.Detections)",
                }),
                "labels": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "Fallback class names — used only when no upstream "
                        "labels are provided. Click to enter as pills "
                        "(cycled round-robin across detections)."
                    ),
                }),
            },
        }

    def execute(
        self,
        boxes=None,
        pred_labels=None,
        scores=None,
        masks=None,
        image=None,
        field="detections",
        labels="",
        **_,
    ):
        # All inputs optional — allow masks-only / boxes-only / etc.
        h, w = 0, 0
        if image is not None and hasattr(image, "shape") and len(image.shape) >= 3:
            h, w = int(image.shape[1]), int(image.shape[2])

        print(
            f"[FO_SaveDetections] called: field={field!r}, labels_widget={labels!r}, "
            f"image.shape={getattr(image, 'shape', None)}, "
            f"boxes_type={type(boxes).__name__ if boxes is not None else None}, "
            f"pred_labels_type={type(pred_labels).__name__ if pred_labels is not None else None}, "
            f"scores_type={type(scores).__name__ if scores is not None else None}, "
            f"masks_type={type(masks).__name__ if masks is not None else None}, "
            f"hw=({h},{w})"
        )

        if boxes is None and masks is None:
            print(
                "[FO_SaveDetections] WARNING: nothing to save — neither 'boxes' nor 'masks' "
                "was connected.  Connect one (or both) of them to a detector / segmenter."
            )
            return {}

        # Skip the save dispatch entirely when the upstream produced
        # nothing usable.  Common case: SAM3InteractiveCollector's
        # ``ensureModelLoaded`` auto-queues the whole workflow on the
        # first Run-button click to warm the model cache; with no real
        # prompts yet, it returns ``torch.zeros(1, H, W)`` and we'd
        # otherwise pollute the FiftyOne side with a save attempt that
        # creates no field.  Exiting here keeps the auto-queue silent.
        boxes_have_content = (
            boxes is not None
            and (
                (isinstance(boxes, str) and boxes.strip() not in ("", "[]"))
                or (isinstance(boxes, (list, tuple)) and len(boxes) > 0)
            )
        )
        masks_have_content = False
        if masks is not None:
            try:
                _peek = masks.detach().cpu().numpy() if hasattr(masks, "detach") else np.asarray(masks)
                masks_have_content = bool((_peek > 0).any())
            except Exception as exc:
                print(f"[FO_SaveDetections] mask content peek failed: {exc}")
                # Best-effort: assume there is content; downstream will deal with it.
                masks_have_content = True

        if not boxes_have_content and not masks_have_content:
            print(
                "[FO_SaveDetections] SKIP — upstream produced no usable boxes or non-zero masks. "
                "(Common with SAM3InteractiveCollector's first 'Run' click — its auto-queue "
                "warms the model cache before any prompts have been entered. "
                "Add prompts via the canvas, then click 'Queue' to actually save.)"
            )
            return {}

        boxes_j = _coerce_to_jsonable(boxes)
        labels_j = _coerce_to_jsonable(pred_labels)
        scores_j = _coerce_to_jsonable(scores)

        # Sanity-check the coerced boxes shape.  We expect a list of
        # ``[x1, y1, x2, y2]`` 4-tuples — flat numbers, two levels deep.
        # If a user accidentally wires the MASK output (``[N, H, W]``)
        # into the boxes socket, ``.tolist()`` produces 3-deep nested
        # lists that JSON-encode to megabytes of zeros and crash the
        # operator on ``float(list)``.  Discard here so the operator's
        # mask-derived fallback handles it cleanly.
        if isinstance(boxes_j, list) and boxes_j:
            first = boxes_j[0]
            looks_like_xyxy = (
                isinstance(first, (list, tuple))
                and len(first) >= 4
                and all(isinstance(v, (int, float)) for v in first[:4])
            )
            if not looks_like_xyxy:
                print(
                    f"[FO_SaveDetections] WARNING — boxes input has unexpected shape "
                    f"(first element type={type(first).__name__}, "
                    f"len={len(first) if hasattr(first, '__len__') else '?'}); "
                    "discarding boxes and falling back to mask-derived bboxes. "
                    "Did you accidentally wire the MASK output to the boxes socket?"
                )
                boxes_j = None

        masks_filename = ""
        if masks is not None:
            try:
                masks_filename, masks_shape = _save_mask_tensor_npy(
                    masks, folder_paths.get_output_directory(), field,
                )
                print(f"[FO_SaveDetections] saved masks → {masks_filename} shape={masks_shape}")
            except Exception as exc:
                print(f"[FO_SaveDetections] WARNING failed to save masks: {exc}")

        payload = {
            "type": "detections",
            "save_mode": "field",
            "field": field,
            # 0 / 0 means "infer from sample" — operator side.
            "image_height": h,
            "image_width": w,
            "boxes_json": _json.dumps(boxes_j) if boxes_j is not None else "",
            "pred_labels_json": _json.dumps(labels_j) if labels_j is not None else "",
            "scores_json": _json.dumps(scores_j) if scores_j is not None else "",
            "masks_filename": masks_filename,
            "fallback_labels": labels,
        }
        print(
            f"[FO_SaveDetections] dispatching: field={field!r}, "
            f"boxes_json_len={len(payload['boxes_json'])}, "
            f"labels_json_len={len(payload['pred_labels_json'])}, "
            f"scores_json_len={len(payload['scores_json'])}, "
            f"masks_filename={masks_filename!r}, "
            f"fallback_labels={labels!r}, hw=({h},{w})"
        )
        PromptServer.instance.send_sync("fiftyone.save_output", payload)
        return {}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")


# ---------------------------------------------------------------------------
# FO_SaveSegmentation — ingest MASK as fo.Segmentation
# ---------------------------------------------------------------------------

class FO_SaveSegmentation:
    """Save a (semantic) segmentation mask to FiftyOne as ``fo.Segmentation``.

    Saves the mask as an indexed PNG next to the source sample's
    filepath and stores the path on the sample via ``mask_path`` (no
    in-memory conversion required at view time).
    """

    CATEGORY = "FiftyOne/IO"
    FUNCTION = "execute"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Save a segmentation mask to FiftyOne as fo.Segmentation on the "
        "current sample.  Mask is stored on disk via mask_path."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mask": ("MASK",),
            },
            "optional": {
                "field": ("STRING", {
                    "default": "segmentation",
                    "tooltip": "Field name on the FiftyOne sample (creates fo.Segmentation)",
                }),
                "mask_targets": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "Optional class-index → label mapping. JSON object "
                        "or 'key=value,key=value' (e.g. '0=bg,1=fg')."
                    ),
                }),
            },
        }

    def execute(self, mask, field="segmentation", mask_targets="", **_):
        print(
            f"[FO_SaveSegmentation] called: field={field!r}, "
            f"mask_targets={mask_targets!r}, mask.shape={getattr(mask, 'shape', None)}"
        )

        # Guard against SAM3InteractiveCollector's auto-queue
        # (warm-up run with no real prompts → returns torch.zeros(1, H, W)).
        # Without this guard the user gets a phantom save with an empty
        # segmentation field.  See FO_SaveDetections for the same pattern.
        try:
            _peek = mask.detach().cpu().numpy() if hasattr(mask, "detach") else np.asarray(mask)
            if not (_peek > 0).any():
                print(
                    "[FO_SaveSegmentation] SKIP — upstream produced an all-zero mask. "
                    "(Common with SAM3InteractiveCollector's first 'Run' click — its "
                    "auto-queue warms the model cache before prompts are entered. "
                    "Add prompts and click 'Queue' to actually save.)"
                )
                return {}
        except Exception as exc:
            print(f"[FO_SaveSegmentation] mask content peek failed: {exc}")
            # Best-effort: continue and let the save attempt happen.

        try:
            filename, mshape, num_classes = _save_segmask_tensor_png(
                mask, folder_paths.get_output_directory(), field,
            )
            print(
                f"[FO_SaveSegmentation] saved mask → {filename} "
                f"shape={mshape} num_classes={num_classes}"
            )
        except Exception as exc:
            print(f"[FO_SaveSegmentation] ERROR failed to save mask: {exc}")
            raise

        # Auto-populate mask_targets if the user left it empty AND we
        # produced a multi-instance map (N>1 distinct class IDs).
        if not mask_targets and num_classes > 1:
            auto_targets = {i + 1: f"instance_{i + 1}" for i in range(num_classes)}
            mask_targets = _json.dumps(auto_targets)
            print(
                f"[FO_SaveSegmentation] auto-populated mask_targets for "
                f"{num_classes} instance(s): {mask_targets}"
            )

        payload = {
            "type": "segmentation",
            "save_mode": "field",
            "field": field,
            "filename": filename,
            "subfolder": "",
            "mask_targets": mask_targets,
        }
        print(f"[FO_SaveSegmentation] dispatching: {payload}")
        PromptServer.instance.send_sync("fiftyone.save_output", payload)
        return {}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")


# ---------------------------------------------------------------------------
# 3D save helpers
# ---------------------------------------------------------------------------
#
# We delegate the actual mesh serialization to ComfyUI's built-in
# ``save_glb`` helper (``comfy_extras/nodes_hunyuan3d.py:save_glb``) —
# the same one its native ``SaveGLB`` ("Save 3D Model") node uses.
# Doing so lets us inherit ComfyUI's batch / dtype / format handling
# for free without re-implementing them or pulling in ``trimesh``.
#
# Three input shapes are supported on the ``model`` socket:
#   - ``str`` filepath ending in a 3D extension → preserve verbatim
#   - object exposing ``save_to(path)`` (File3D / VHS wrappers) → call it
#   - MESH tensor (``.vertices`` + ``.faces`` attributes) → delegate to
#     ``save_glb``

# Extensions FiftyOne natively recognizes for media_type="3d".
_VALID_3D_EXTS = (".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".pcd", ".fo3d")


def _copy_3d_file(source_path, output_dir, prefix):
    """Copy a 3D asset file into the ComfyUI output dir, preserving extension."""
    src_ext = os.path.splitext(source_path)[1].lower() or ".glb"
    filename = f"fo_{prefix}_{_unique_suffix()}{src_ext}"
    shutil.copy2(source_path, os.path.join(output_dir, filename))
    return filename


def _save_glb_via_comfy(verts, faces, output_path):
    """Write a GLB by delegating to ComfyUI's ``save_glb`` helper.

    ``save_glb`` lives in ``comfy_extras/nodes_hunyuan3d.py`` and is the
    serializer the built-in 'Save 3D Model' (SaveGLB) node uses.  It
    expects unbatched ``(N, 3)`` torch tensors of vertices and faces;
    we coerce non-tensor inputs upstream of this helper.

    Imported lazily so loading FO_Save3D doesn't fail in a ComfyUI build
    that's missing the helper (older or more minimal forks).
    """
    try:
        from comfy_extras.nodes_hunyuan3d import save_glb
    except ImportError as exc:
        raise RuntimeError(
            "Cannot import ComfyUI's `save_glb` helper "
            "(comfy_extras.nodes_hunyuan3d.save_glb).  FO_Save3D needs a "
            "ComfyUI build that ships the native 'Save 3D Model' node — "
            "this is the same node you would otherwise use to write GLBs."
        ) from exc

    # save_glb expects torch tensors with .cpu().numpy() callable.
    # Coerce numpy / list / non-tensor inputs.
    import torch
    if not torch.is_tensor(verts):
        verts = torch.from_numpy(np.asarray(verts))
    if not torch.is_tensor(faces):
        faces = torch.from_numpy(np.asarray(faces))

    save_glb(verts, faces, output_path)
    return output_path


def _save_pointcloud_ply(verts, output_path):
    """Write a vertex-only point cloud as a binary PLY file.

    Used when a MESH-like input has ``.vertices`` but no ``.faces`` — the
    classic shape coming out of Gaussian-splat / point-cloud workflows
    (DreamGaussian, Trellis, custom packs).  ``save_glb`` requires faces,
    so we side-step it here and write PLY directly via numpy.

    PLY 1.0 binary format with little-endian floats.  Optional per-vertex
    RGB colors are picked up from a 4-tuple input or skipped if absent.
    """
    verts_np = np.asarray(
        verts.detach().cpu().numpy() if hasattr(verts, "detach") else verts,
        dtype=np.float32,
    )
    if verts_np.ndim != 2 or verts_np.shape[1] != 3:
        raise ValueError(
            f"Expected vertices of shape [N, 3] for point cloud, "
            f"got {verts_np.shape}"
        )
    n = verts_np.shape[0]

    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "end_header\n"
    ).encode("ascii")

    with open(output_path, "wb") as f:
        f.write(header)
        f.write(verts_np.astype(np.float32, copy=False).tobytes())

    print(f"[_save_pointcloud_ply] wrote {output_path} verts={n}")
    return output_path


def _serialize_meshlike(verts, faces, output_dir, prefix):
    """Write a mesh-or-point-cloud given vertices and optional faces.

    Picks between ``save_glb`` (full mesh) and ``_save_pointcloud_ply``
    (vertices only) based on whether faces are present.  Used by both
    the MESH-tensor and MESH-dict branches of ``_resolve_3d_input``.
    """
    v_shape = getattr(verts, "shape", None)
    f_shape = getattr(faces, "shape", None) if faces is not None else None

    # Strip leading singleton batch dims to match save_glb's expectation
    # of (N, 3) / (M, 3).  ComfyUI's MESH carries [B, N, 3] tensors; this
    # also handles dicts whose authors mirrored that convention.
    if v_shape is not None and len(v_shape) == 3:
        if v_shape[0] > 1:
            print(
                f"[_serialize_meshlike]   verts batch={v_shape[0]} > 1, "
                f"saving first only"
            )
        verts = verts[0]
    if (
        faces is not None
        and f_shape is not None
        and len(f_shape) == 3
    ):
        faces = faces[0]

    has_real_faces = (
        faces is not None
        and getattr(faces, "shape", None) is not None
        and len(faces.shape) == 2
        and faces.shape[0] > 0
    )

    if has_real_faces:
        filename = f"fo_{prefix}_{_unique_suffix()}.glb"
        target_path = os.path.join(output_dir, filename)
        _save_glb_via_comfy(verts, faces, target_path)
        print(f"[_serialize_meshlike]   wrote {target_path} via ComfyUI save_glb")
    else:
        filename = f"fo_{prefix}_{_unique_suffix()}.ply"
        target_path = os.path.join(output_dir, filename)
        _save_pointcloud_ply(verts, target_path)
        print(f"[_serialize_meshlike]   wrote {target_path} as point-cloud PLY")
    return filename


def _resolve_3d_input(model, output_dir, prefix):
    """Accept any common ComfyUI 3D-asset representation, return a filename.

    Five input patterns, checked in order:

    1. ``str`` filepath ending in a valid 3D extension → copy verbatim.
    2. Object exposing ``save_to(path)`` (ComfyUI ``File3D``, VHS-style
       wrappers).  File3D objects know their own format via ``.format``.
    3. Object exposing ``export(path)`` (trimesh.Trimesh, open3d meshes,
       custom pack types).  Lets the object self-serialize to whatever
       format its ``export`` infers from the extension we pick.
    4. **MESH tensor** (``.vertices`` + ``.faces`` attributes — ComfyUI
       core's ``MESH`` class) → ``save_glb`` for full meshes, PLY for
       vertex-only.
    5. **MESH dict** (``{"vertices": ..., "faces": ...}`` — used by
       third-party packs like ComfyUI-3D-Pack) → same routing as (4).

    Lists / tuples are walked for the first usable item.
    """
    type_name = type(model).__name__
    is_str = isinstance(model, str)
    is_dict = isinstance(model, dict)
    is_seq = isinstance(model, (list, tuple))
    has_save_to = hasattr(model, "save_to") and callable(getattr(model, "save_to", None))
    has_export = hasattr(model, "export") and callable(getattr(model, "export", None))
    has_vertices_attr = hasattr(model, "vertices")

    print(
        f"[_resolve_3d_input] type={type_name}, "
        f"is_str={is_str}, is_dict={is_dict}, is_seq={is_seq}, "
        f"has_save_to={has_save_to}, has_export={has_export}, "
        f"has_vertices_attr={has_vertices_attr}"
    )

    # 1. Filepath str — passthrough, preserves source extension.
    if is_str and model:
        if not os.path.isfile(model):
            raise ValueError(f"3D filepath does not exist: {model}")
        ext = os.path.splitext(model)[1].lower()
        if ext not in _VALID_3D_EXTS:
            raise ValueError(
                f"Unsupported 3D file extension {ext!r}. "
                f"Supported: {', '.join(_VALID_3D_EXTS)}"
            )
        print(f"[_resolve_3d_input]   branch: filepath passthrough ({ext})")
        return _copy_3d_file(model, output_dir, prefix)

    # 2. File3D / VHS wrapper objects with `save_to(path)` — they handle
    # their own serialization.  File3D's `.format` attribute carries the
    # extension; fall back to GLB if absent.
    if has_save_to:
        ext = (getattr(model, "format", None) or "glb").lstrip(".").lower() or "glb"
        filename = f"fo_{prefix}_{_unique_suffix()}.{ext}"
        target_path = os.path.join(output_dir, filename)
        print(f"[_resolve_3d_input]   branch: model.save_to({target_path}) (.{ext})")
        model.save_to(target_path)
        return filename

    # 3. trimesh.Trimesh / open3d / custom mesh classes that expose
    # ``export(path)`` — their export() infers the format from the
    # extension we choose.  We pick GLB (FiftyOne-recommended) and let
    # the object handle the conversion.  Skip for File3D, which we
    # already handled via save_to.
    if has_export:
        filename = f"fo_{prefix}_{_unique_suffix()}.glb"
        target_path = os.path.join(output_dir, filename)
        print(f"[_resolve_3d_input]   branch: model.export({target_path})")
        try:
            model.export(target_path)
            return filename
        except Exception as exc:
            # Some objects (e.g. open3d) have export() with a different
            # signature.  Fall through to the attribute / dict branches
            # below, which will pull the raw vertex/face arrays.
            print(
                f"[_resolve_3d_input]   model.export() failed: {exc!s:.120} "
                f"— falling through to .vertices/.faces"
            )

    # 4. MESH class instance — ComfyUI core's MESH (.vertices + .faces).
    # Open3D's TriangleMesh exposes .vertices + .triangles; we accept
    # that shape too.
    if has_vertices_attr:
        verts = getattr(model, "vertices", None)
        faces = getattr(model, "faces", None)
        if faces is None:
            faces = getattr(model, "triangles", None)
        v_shape = getattr(verts, "shape", None)
        f_shape = getattr(faces, "shape", None)
        print(
            f"[_resolve_3d_input]   branch: MESH ({type_name}), "
            f"vertices.shape={v_shape}, faces.shape={f_shape}"
        )
        if verts is None:
            raise ValueError(
                f"MESH input ({type_name}) has missing vertices "
                f"(verts={v_shape}, faces={f_shape})"
            )
        return _serialize_meshlike(verts, faces, output_dir, prefix)

    # 5. MESH dict — ComfyUI-3D-Pack and similar packs use a dict
    # ``{"vertices": ..., "faces": ...}`` shape rather than a class.
    # Same routing as the MESH-class branch above.
    if is_dict and ("vertices" in model or "verts" in model):
        verts = model.get("vertices", model.get("verts"))
        faces = model.get("faces", model.get("triangles"))
        v_shape = getattr(verts, "shape", None)
        f_shape = getattr(faces, "shape", None)
        print(
            f"[_resolve_3d_input]   branch: MESH dict, "
            f"vertices.shape={v_shape}, faces.shape={f_shape}, "
            f"keys={list(model.keys())[:8]}"
        )
        return _serialize_meshlike(verts, faces, output_dir, prefix)

    # 6. list / tuple — walk for first usable item.
    if is_seq:
        print(f"[_resolve_3d_input]   branch: walking list/tuple of len={len(model)}")
        for i, item in enumerate(model):
            try:
                return _resolve_3d_input(item, output_dir, prefix)
            except (ValueError, TypeError) as exc:
                print(f"[_resolve_3d_input]     list[{i}] skipped: {exc!s:.120}")
                continue

    # Final raise with diagnostic info.
    attrs = []
    if is_dict:
        attrs = list(model.keys())[:30]
    elif not (is_str or is_seq):
        attrs = [a for a in dir(model) if not a.startswith("_")][:30]
    raise ValueError(
        f"Cannot extract 3D model from input type {type_name}. "
        f"Supported: filepath string ({', '.join(_VALID_3D_EXTS)}), "
        f"objects with save_to() / export(), MESH class (.vertices/.faces), "
        f"or MESH dict (vertices/faces).  "
        f"Attributes/keys seen: {attrs}"
    )


# ---------------------------------------------------------------------------
# FO_Save3D
# ---------------------------------------------------------------------------

class FO_Save3D:
    """Save a 3D model output to FiftyOne with ``media_type="3d"``.

    Polymorphic on the ``model`` socket — accepts:

    - **Filepath strings** (``.glb/.gltf/.obj/.ply/.stl/.fbx/.pcd/.fo3d``):
      preserved verbatim.
    - **File3D objects** (ComfyUI V3 type system, e.g. from a 3D loader)
      via their ``save_to(path)`` method, which preserves their format.
    - **MESH tensors** (``.vertices`` + ``.faces`` attributes): delegated
      to ComfyUI's built-in ``save_glb`` helper — the same one its
      native 'Save 3D Model' (SaveGLB) node uses.  Output is GLB.

    Saves the file alongside the source sample on disk and creates either
    a brand-new sample (``new_sample``) or a group slice (``group_slice``)
    in FiftyOne, mirroring FO_SaveImage / FO_SaveVideo.
    """

    CATEGORY = "FiftyOne/IO"
    FUNCTION = "execute"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Save a 3D model to FiftyOne. Connects to any 3D output — filepath "
        "strings, File3D objects, or MESH tensors. Delegates MESH "
        "serialization to ComfyUI's built-in save_glb helper for "
        "broad compatibility."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"model": (ANY, {})},
            "optional": {
                "save_mode": _SAVE_MODE_WIDGET,
                "name": _NAME_WIDGET,
                "labels": _LABELS_WIDGET,
            },
        }

    def execute(
        self,
        model,
        save_mode="new_sample",
        name="comfy_output",
        labels="",
        **_,
    ):
        print(
            f"[FO_Save3D] called: model_type={type(model).__name__}, "
            f"save_mode={save_mode!r}, name={name!r}, labels={labels!r}"
        )
        if model is None:
            print("[FO_Save3D] SKIP — no model input connected")
            return {}

        try:
            filename = _resolve_3d_input(
                model, folder_paths.get_output_directory(), name,
            )
        except Exception as exc:
            print(f"[FO_Save3D] ERROR resolving input: {exc}")
            raise

        print(
            f"[FO_Save3D] dispatching to FiftyOne: filename={filename!r}, "
            f"save_mode={save_mode!r}"
        )
        PromptServer.instance.send_sync("fiftyone.save_output", {
            "type": "3d",
            "save_mode": save_mode,
            "name": name,
            "filename": filename,
            "subfolder": "",
            "copy_labels": labels,
        })
        return {}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")


# ---------------------------------------------------------------------------
# FO_LoadImage
# ---------------------------------------------------------------------------
#
# Thin wrapper around ComfyUI's built-in ``LoadImage`` node, registered
# under the ``FiftyOne/IO`` category for discoverability alongside the
# Save nodes.  Behavior is identical to the built-in: the ``image``
# widget is a dropdown of files in ComfyUI's input dir.
#
# When a FiftyOne sample is open the plugin writes:
#   - ``fo_current_sample.png`` — follows the active modal slice
#   - ``fo_current_sample_<slice_name>.png`` — one per group slice
# so users can pick exactly which slice goes into each LoadImage in
# multi-input workflows.
#
# We import lazily and degrade gracefully: if ComfyUI's ``LoadImage``
# can't be imported (e.g. its module path changes in a future version),
# we skip the shortcut without breaking the plugin.

try:
    from nodes import LoadImage as _BuiltinLoadImage

    class FO_LoadImage(_BuiltinLoadImage):
        CATEGORY = "FiftyOne/IO"
        DESCRIPTION = (
            "Load an image from ComfyUI's input directory.  Same as the "
            "built-in LoadImage but listed under FiftyOne/IO for "
            "convenience.  When a FiftyOne sample is open, the current "
            "sample (and any group slices) appear in the dropdown as "
            "``fo_current_sample[_slicename].png``."
        )

    _HAS_FO_LOAD_IMAGE = True
except Exception as _e:  # pragma: no cover — defensive against ComfyUI changes
    print(f"[comfyui-plugin] FO_LoadImage unavailable: {_e}")
    FO_LoadImage = None  # type: ignore[assignment]
    _HAS_FO_LOAD_IMAGE = False
