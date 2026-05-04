"""FiftyOne Bridge — ComfyUI custom node package.

Registers:
  - Save nodes that ship outputs back to FiftyOne (image, video, text,
    depth, detections, segmentation).
  - A Load Image shortcut under ``FiftyOne/IO`` (thin wrapper around
    ComfyUI's built-in ``LoadImage``).
  - ``fiftyone_bridge.js`` web extension via ``WEB_DIRECTORY``.
"""

from .nodes import (
    FO_LoadImage,
    FO_Save3D,
    FO_SaveDepth,
    FO_SaveDetections,
    FO_SaveImage,
    FO_SaveSegmentation,
    FO_SaveText,
    FO_SaveVideo,
    _HAS_FO_LOAD_IMAGE,
)

WEB_DIRECTORY = "./js"

NODE_CLASS_MAPPINGS = {
    "FO_SaveImage": FO_SaveImage,
    "FO_SaveVideo": FO_SaveVideo,
    "FO_SaveText": FO_SaveText,
    "FO_SaveDepth": FO_SaveDepth,
    "FO_SaveDetections": FO_SaveDetections,
    "FO_SaveSegmentation": FO_SaveSegmentation,
    "FO_Save3D": FO_Save3D,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FO_SaveImage": "Save Image to FiftyOne",
    "FO_SaveVideo": "Save Video to FiftyOne",
    "FO_SaveText": "Save Text to FiftyOne",
    "FO_SaveDepth": "Save Depth to FiftyOne",
    "FO_SaveDetections": "Save Detections to FiftyOne",
    "FO_SaveSegmentation": "Save Segmentation to FiftyOne",
    "FO_Save3D": "Save 3D to FiftyOne",
}

# The Load shortcut is only registered if we successfully imported
# ComfyUI's built-in LoadImage to subclass.  Defensive against future
# ComfyUI restructuring — the rest of the plugin still works without it.
if _HAS_FO_LOAD_IMAGE:
    NODE_CLASS_MAPPINGS["FO_LoadImage"] = FO_LoadImage
    NODE_DISPLAY_NAME_MAPPINGS["FO_LoadImage"] = "Load Image from FiftyOne"

print(
    f"[fiftyone_bridge] registered {len(NODE_CLASS_MAPPINGS)} node(s): "
    f"{sorted(NODE_CLASS_MAPPINGS)}"
)
