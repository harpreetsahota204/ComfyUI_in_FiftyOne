"""Shared constants and cross-reimport process state for the ComfyUI plugin.

This module has no plugin-internal imports so every other submodule can
depend on it without risking an import cycle.
"""

import os
import sys

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
EXTENSION_DIR = os.path.join(PLUGIN_DIR, "comfyui_extension")
# Templates ship inside the bridge custom-node so a single symlink
# (custom_nodes/fiftyone_bridge -> EXTENSION_DIR) is enough to make them
# discoverable.  ComfyUI's native "Workflow Templates" tab scans
# example_workflows/ (and a couple of legacy aliases) by convention; we
# read these only from the FiftyOne panel's own dropdown via the
# get_templates panel method + _load_manifest() pair.
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

CURRENT_SAMPLE_FILENAME = "fo_current_sample.png"
_SLICE_FILE_PREFIX = "fo_current_sample_"

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
