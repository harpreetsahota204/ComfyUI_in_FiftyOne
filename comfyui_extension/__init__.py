"""FiftyOne Bridge — ComfyUI custom node package.

Registers Save nodes for sending outputs back to FiftyOne, a Load Image
shortcut entry under ``FiftyOne/IO`` (a thin wrapper around ComfyUI's
built-in ``LoadImage``), and the ``fiftyone_bridge.js`` web extension
via ``WEB_DIRECTORY``.
"""

from .nodes import (
    FO_LoadImage,
    FO_SaveDepth,
    FO_SaveImage,
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
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FO_SaveImage": "Save Image to FiftyOne",
    "FO_SaveVideo": "Save Video to FiftyOne",
    "FO_SaveText": "Save Text to FiftyOne",
    "FO_SaveDepth": "Save Depth to FiftyOne",
}

# The Load shortcut is only registered if we successfully imported
# ComfyUI's built-in LoadImage to subclass.  Defensive against future
# ComfyUI restructuring — the rest of the plugin still works without it.
if _HAS_FO_LOAD_IMAGE:
    NODE_CLASS_MAPPINGS["FO_LoadImage"] = FO_LoadImage
    NODE_DISPLAY_NAME_MAPPINGS["FO_LoadImage"] = "Load Image from FiftyOne"
