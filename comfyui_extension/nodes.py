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
    real collision would require two saves within the same millisecond
    *and* exactly 10M ms apart, which is acceptable for a temp-output
    directory.
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
