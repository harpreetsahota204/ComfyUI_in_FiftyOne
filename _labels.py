"""Detection / segmentation label parsing and mask geometry helpers."""

import json
import re

import numpy as np


def _parse_copy_labels(copy_labels: str) -> list:
    """Parse the ``copy_labels`` wire format into a list of field names.

    Wire format (single string):
    - ``""`` → ``[]`` (copy nothing)
    - ``"a,b,c"`` → ``["a", "b", "c"]``
    """
    if not copy_labels:
        return []
    return [name.strip() for name in copy_labels.split(",") if name.strip()]


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
