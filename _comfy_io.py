"""ComfyUI HTTP I/O: history metadata extraction, file fetch, path naming."""

import os

import requests


_PROMPT_INPUT_KEYS = {"text", "prompt", "string", "positive", "instruction"}


_NEGATIVE_PROMPT_HINTS = {"negative", "neg", "uncond"}


_MODEL_INPUT_KEYS = {
    "unet_name", "ckpt_name", "model_name", "model_path",
    "model_filename", "lora_name",
}


_SAMPLER_CLASS_HINTS = {"sampler", "ksampler"}


_MODEL_CLASS_HINTS = {"loader", "checkpoint", "unet", "model"}


_METADATA_STR_FIELDS = frozenset({
    "comfy_workflow_name", "comfy_prompt", "comfy_negative_prompt",
    "comfy_sampler", "comfy_scheduler", "comfy_model",
})


def _coerce_int(val):
    """Coerce a metadata input to ``int`` if scalar, else ``None``.

    ComfyUI's API workflow JSON represents *linked* inputs (a node input
    wired from another node's output) as ``[source_node_id, output_idx]``
    lists.  In subgraphed workflows the source id is a string like
    ``"129:119"``.  Only scalar values are real numbers we can store in
    a FiftyOne ``IntField`` — anything else (link references, dicts,
    strings, bools) is dropped, leaving the field ``None``.
    """
    if isinstance(val, bool):
        # bool is an int subclass — reject deliberately so True / False
        # don't end up as 1 / 0 step counts / seeds.
        return None
    if isinstance(val, (int, float)):
        return int(val)
    return None


def _coerce_float(val):
    """Coerce a metadata input to ``float`` if scalar, else ``None``.

    Mirrors :func:`_coerce_int` — drops link references and other
    non-scalar values.
    """
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _coerce_str(val):
    """Coerce a metadata input to a non-empty ``str``, else ``None``.

    Drops link references (lists), dicts, and empty / whitespace-only
    strings.  Used for ``sampler`` / ``scheduler`` extraction in
    sampler-class nodes, where the value can be linked from a
    ``PrimitiveString`` node.
    """
    if isinstance(val, str) and val.strip():
        return val
    return None


def _fetch_comfy_metadata(port: int, prompt_id: str) -> "dict | None":
    """Fetch generation metadata from ComfyUI's /history endpoint.

    Uses a generic scan: instead of hard-coding specific node class names,
    we inspect every node's inputs and match by input-key heuristics so
    that arbitrary workflows (Qwen, Flux, SDXL, custom, etc.) all get
    captured.
    """
    print(f"[comfyui-plugin] _fetch_comfy_metadata: prompt_id={prompt_id!r}")
    if not prompt_id:
        print("[comfyui-plugin]   → returning None (no prompt_id)")
        return None

    try:
        url = f"http://127.0.0.1:{port}/history/{prompt_id}"
        print(f"[comfyui-plugin]   fetching {url}")
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        full_json = resp.json()
        history = full_json.get(prompt_id, {})
        print(f"[comfyui-plugin]   history keys: {list(history.keys()) if isinstance(history, dict) else type(history)}")
    except (requests.RequestException, ValueError) as e:
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

    # The UI/graph form (litegraph nodes + links) needed to *reload* the
    # workflow lives under extra_data.extra_pnginfo.workflow — distinct
    # from the API/prompt form above, which only drives metadata
    # extraction.  ComfyUI populates it for UI-queued prompts (how this
    # plugin runs them); absent for purely API-queued ones.
    ui_workflow = None
    extra_data = history.get("extra_data", {})
    if isinstance(extra_data, dict):
        png_info = extra_data.get("extra_pnginfo", {})
        if isinstance(png_info, dict) and isinstance(png_info.get("workflow"), dict):
            ui_workflow = png_info["workflow"]
    print(f"[comfyui-plugin]   ui_workflow present: {ui_workflow is not None}")

    metadata = {
        "workflow_json": api_workflow,
        "workflow_ui_json": ui_workflow,
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
            # Each input is funneled through a type-aware coercer so that
            # linked references (``[node_id, output_idx]`` lists) and
            # other non-scalar values are dropped to ``None`` rather than
            # crashing the IntField / FloatField / StringField at save
            # time.  Leaving the metadata field ``None`` is the
            # documented behavior for "exotic" workflows.
            if metadata["seed"] is None and "seed" in inputs:
                metadata["seed"] = _coerce_int(inputs["seed"])
            if metadata["steps"] is None and "steps" in inputs:
                metadata["steps"] = _coerce_int(inputs["steps"])
            if metadata["cfg"] is None and "cfg" in inputs:
                metadata["cfg"] = _coerce_float(inputs["cfg"])
            if metadata["sampler"] is None:
                metadata["sampler"] = _coerce_str(
                    inputs.get("sampler_name") or inputs.get("sampler")
                )
            if metadata["scheduler"] is None and "scheduler" in inputs:
                metadata["scheduler"] = _coerce_str(inputs["scheduler"])
            if metadata["denoise"] is None and "denoise" in inputs:
                metadata["denoise"] = _coerce_float(inputs["denoise"])
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
