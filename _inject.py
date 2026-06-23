"""Copy sample / group-slice images into ComfyUI's input directory."""

import os
import re

import fiftyone as fo

from ._constants import CURRENT_SAMPLE_FILENAME, _SLICE_FILE_PREFIX
from ._templates import _get_media_type


def _slice_filename(slice_name: str) -> str:
    """Return the filename used for a per-slice image in ComfyUI's input dir.

    Example: ``"close up"`` → ``"fo_current_sample_close_up.png"``.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", slice_name)
    return f"{_SLICE_FILE_PREFIX}{sanitized}.png"


def _origin_input_filename(filepath: str) -> str:
    """Input-dir filename for a generated sample's *source* image.

    A dedicated, deterministic name (``fo_source_<stem>.png``) so the
    originating workflow's LoadImage can show the real source image
    without overwriting ``fo_current_sample.png`` — which still tracks the
    open (generated) sample for templates / fresh workflows.
    """
    stem = os.path.splitext(os.path.basename(filepath))[0]
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", stem)
    return f"fo_source_{sanitized}.png"


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
