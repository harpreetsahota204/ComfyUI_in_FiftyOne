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

_HIDDEN = {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"}


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
            "hidden": _HIDDEN,
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
            "hidden": _HIDDEN,
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
            "hidden": _HIDDEN,
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
            "hidden": _HIDDEN,
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
