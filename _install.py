"""Symlink the bridge extension and bundled vendor packs into ComfyUI."""

import os

from ._constants import EXTENSION_DIR, VENDOR_DIR, _VENDOR_PACKS


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
