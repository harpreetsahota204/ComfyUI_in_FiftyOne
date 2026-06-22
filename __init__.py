"""FiftyOne ComfyUI Plugin.

Embeds a full ComfyUI instance inside the FiftyOne sample modal via an
iframe, copies the current sample (and any group slices) into ComfyUI's
input directory, and saves generated outputs back to the dataset.

Architecture
------------
``ComfyUIPanel`` (foo.Panel)
    Hybrid panel rendered in the sample modal.  Lifecycle hooks
    (``on_load``, ``on_change_current_sample``) are intentionally
    lightweight: they only push the current sample's filepath / id to
    React via ``ctx.panel.set_state``.  Heavy work — server startup,
    extension install, sample injection — happens in panel methods
    invoked by React (``initialize``, ``start_server``, ``stop_server``,
    ``inject_slice``, ``trigger_reload``, etc.).

``SaveComfyOutput`` (foo.Operator)
    Unlisted operator invoked from React via ``useOperatorExecutor``.
    Supports seven output types (image, video, text, depth, detections,
    segmentation, 3d) and multiple destinations (group slice, new
    sample, string field, classification, heatmap, ``fo.Detections``
    field, ``fo.Segmentation`` field).  Fetches metadata from ComfyUI's
    ``/history`` endpoint and stores generation parameters on the
    saved sample.

Workflow templates are served by the ``get_templates`` panel method on
``ComfyUIPanel`` (not a separate operator).

Helpers are split into focused ``_*`` submodules (see ARCHITECTURE.md §3):
``_constants``, ``_server``, ``_install``, ``_inject``, ``_templates``,
``_dataset``, ``_labels``, ``_comfy_io``.
"""

import base64
import copy
import io as _io
import json
import os
import re
import signal
import subprocess
import time
import traceback

import numpy as np

import fiftyone as fo
import fiftyone.operators as foo
import fiftyone.operators.types as types

# ---------------------------------------------------------------------------
# Plugin internals (split into focused submodules; see ARCHITECTURE.md §3).
# Constants and the cross-reimport process handle live in ._constants.
# ---------------------------------------------------------------------------

from ._constants import (
    DEFAULT_COMFYUI_PATH,
    DEFAULT_COMFYUI_PORT,
    TEMPLATES_DIR,
    _persist,
)
from ._server import (
    _clear_pid,
    _get_config,
    _is_server_running,
    _read_pid,
    _set_config,
    _spawn_comfyui,
    _wait_for_server,
)
from ._install import (
    _install_extension,
)
from ._inject import (
    _inject_all_slices,
    _inject_sample,
)
from ._templates import (
    _get_media_type,
    _load_manifest,
    _patch_load_image_nodes,
)
from ._dataset import (
    _ensure_comfy_fields,
    _ensure_compatible_slice,
    _ensure_grouped,
    _get_sample_label_fields,
    _resolve_active_slice_sample,
    _sample_in_slice,
)
from ._labels import (
    _bboxes_from_masks,
    _crop_mask_to_bbox,
    _parse_copy_labels,
    _parse_jsonish_list,
    _parse_mask_targets,
    _resolve_detection_labels,
)
from ._comfy_io import (
    _METADATA_STR_FIELDS,
    _auto_increment_path,
    _fetch_comfy_metadata,
    _fetch_file_from_comfyui,
)


class ComfyUIPanel(foo.Panel):
    """Hybrid panel embedding a full ComfyUI instance inside the modal."""

    @property
    def config(self):
        return foo.PanelConfig(
            name="comfyui_panel",
            label="ComfyUI",
            icon="brush",
            surfaces="modal",
            help_markdown=(
                "Run any [ComfyUI](https://github.com/comfyanonymous/ComfyUI) "
                "workflow against the current sample. Save generated outputs "
                "as group slices to build an evolution timeline."
            ),
        )

    # ── Lifecycle hooks ──────────────────────────────────────────────────
    # Keep these lightweight — no blocking I/O, no HTTP requests.
    # Heavy work (server startup, extension install) is triggered by
    # React calling the ``initialize`` panel method after mount.

    def on_load(self, ctx):
        self._sync_sample(ctx)

    def on_change_current_sample(self, ctx):
        self._sync_sample(ctx)

    def on_change_group_slice(self, ctx):
        # FiftyOne never updates ``ctx.current_sample`` when the user
        # switches slice tabs — it stays pinned to the group's default
        # ("original") slice sample.  We therefore must re-sync here so
        # that ``current_sample_id`` / ``current_filepath`` reflect the
        # *visible* slice, not the original.  Without this hook, every
        # save would land on the original slice regardless of which tab
        # the user is on.
        self._sync_sample(ctx)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _sync_sample(self, ctx):
        """Push the active slice's filepath and ID to React.

        Delegates slice-aware resolution to ``_resolve_active_slice_sample``
        — same logic the save operator uses, so panel state and save
        targets stay in lock-step.
        """
        try:
            sample_id, filepath = _resolve_active_slice_sample(ctx)
            if not sample_id:
                print("[comfyui-plugin] _sync_sample: no current sample — skipping (will retry on next lifecycle event)")
                return
            print(f"[comfyui-plugin] _sync_sample: sample_id={sample_id} filepath={filepath} active_slice={ctx.group_slice!r}")
            ctx.panel.set_state("current_filepath", filepath)
            ctx.panel.set_state("current_sample_id", sample_id)
        except Exception as exc:
            print(f"[comfyui-plugin] _sync_sample error: {exc}")

    def _safe_get_config(self, ctx) -> dict:
        """Read config with fallback defaults if store fails."""
        try:
            return _get_config(ctx)
        except Exception as exc:
            print(f"[comfyui-plugin] config error: {exc}")
            return {
                "comfyui_path": DEFAULT_COMFYUI_PATH,
                "comfyui_port": DEFAULT_COMFYUI_PORT,
                "comfyui_args": [],
            }

    # ── Panel methods (called from React via usePanelEvent) ──────────────

    def initialize(self, ctx):
        """Called by React after mount.

        Receives the current filepath from React (since ``get_state``
        does not work inside ``usePanelEvent`` calls), checks if ComfyUI
        is reachable, installs the bridge extension, injects the sample,
        and returns everything React needs.
        """
        filepath = ctx.params.get("filepath", "")
        config = self._safe_get_config(ctx)
        port = config["comfyui_port"]
        comfyui_path = config["comfyui_path"]

        # Install bridge extension
        try:
            if os.path.isdir(comfyui_path):
                _install_extension(comfyui_path)
        except Exception as exc:
            print(f"[comfyui-plugin] extension install error: {exc}")

        running = _is_server_running(port)
        iframe_url = f"http://localhost:{port}" if running else ""

        sample_filename = ""
        if running and filepath and os.path.isdir(comfyui_path):
            try:
                sample_filename = _inject_sample(comfyui_path, filepath)
            except Exception as exc:
                print(f"[comfyui-plugin] inject error: {exc}")

            # Also write per-slice files so the LoadImage dropdown lists
            # every group slice for the current sample.  Cheap no-op for
            # flat datasets.
            try:
                if ctx.current_sample:
                    _inject_all_slices(comfyui_path, ctx.dataset, ctx.current_sample)
            except Exception as exc:
                print(f"[comfyui-plugin] inject_all_slices error: {exc}")

        return {
            "server_status": "ready" if running else "not_running",
            "server_port": port,
            "server_error": "",
            "iframe_url": iframe_url,
            "comfyui_path": comfyui_path,
            "sample_filename": sample_filename,
        }

    def start_server(self, ctx):
        """Manually start or detect the ComfyUI server."""
        config = self._safe_get_config(ctx)
        port = config["comfyui_port"]
        comfyui_path = config["comfyui_path"]

        if not _is_server_running(port):
            if os.path.isdir(comfyui_path):
                try:
                    _install_extension(comfyui_path)
                    _spawn_comfyui(comfyui_path, port, config.get("comfyui_args", []))
                    _wait_for_server(port, timeout=60.0)
                except Exception as exc:
                    # The spawn may have launched a subprocess but the
                    # health check timed out (or another error).  Reset
                    # bookkeeping so the next attempt isn't fooled by a
                    # stale PID file / cached Popen handle.
                    _persist.comfyui_process = None
                    _clear_pid()
                    return {
                        "server_status": "error",
                        "server_error": str(exc),
                        "iframe_url": "",
                    }
            else:
                return {
                    "server_status": "not_found",
                    "server_error": f"ComfyUI not found at {comfyui_path}",
                    "iframe_url": "",
                }

        running = _is_server_running(port)
        return {
            "server_status": "ready" if running else "timeout",
            "server_port": port,
            "server_error": "" if running else "Server did not start",
            "iframe_url": f"http://localhost:{port}" if running else "",
            "comfyui_path": comfyui_path,
        }

    def stop_server(self, ctx):
        """Stop the ComfyUI server."""
        if _persist.comfyui_process is not None:
            try:
                _persist.comfyui_process.terminate()
                _persist.comfyui_process.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                _persist.comfyui_process.kill()
            _persist.comfyui_process = None

        pid = _read_pid()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            _clear_pid()

        return {
            "server_status": "stopped",
            "iframe_url": "",
            "server_error": "",
        }

    def load_template(self, ctx):
        """Load a workflow template with the current sample injected."""
        template_id = ctx.params.get("template_id", "")
        if not template_id:
            return {"error": "No template_id provided"}

        # Constrain to the same slug shape ``save_template`` produces so
        # ``..`` / ``/`` / absolute paths can't escape ``TEMPLATES_DIR``.
        if not re.fullmatch(r"[a-z0-9_]+", template_id):
            return {"error": f"Invalid template_id: {template_id!r}"}

        template_path = os.path.join(TEMPLATES_DIR, f"{template_id}.json")
        if not os.path.isfile(template_path):
            return {"error": f"Template not found: {template_id}"}

        with open(template_path) as f:
            workflow = json.load(f)

        sample_filename = ctx.params.get("sample_filename", "")
        if not sample_filename:
            filepath = ctx.params.get("filepath", "")
            if filepath:
                config = self._safe_get_config(ctx)
                comfyui_path = config["comfyui_path"]
                if os.path.isdir(comfyui_path):
                    try:
                        sample_filename = _inject_sample(comfyui_path, filepath)
                    except Exception:
                        pass

        if sample_filename:
            workflow = _patch_load_image_nodes(workflow, sample_filename)

        return {"workflow": workflow}

    def save_template(self, ctx):
        """Save a workflow as a reusable template."""
        template_name = ctx.params.get("name", "").strip()
        workflow = ctx.params.get("workflow")

        if not template_name or not workflow:
            return {"error": "Template name and workflow data are required"}

        slug = re.sub(r"[^a-z0-9]+", "_", template_name.lower()).strip("_")
        if not slug:
            return {"error": "Invalid template name"}

        template_path = os.path.join(TEMPLATES_DIR, f"{slug}.json")
        with open(template_path, "w") as f:
            json.dump(workflow, f, indent=2)

        manifest_path = os.path.join(TEMPLATES_DIR, "_manifest.json")
        try:
            manifest = _load_manifest()
        except (OSError, json.JSONDecodeError):
            manifest = {"templates": []}

        existing_ids = {t["id"] for t in manifest.get("templates", [])}
        if slug not in existing_ids:
            manifest.setdefault("templates", []).append({
                "id": slug,
                "name": template_name,
                "description": f"User-saved template: {template_name}",
                "file": f"{slug}.json",
                "input_types": ["image"],
                "output_type": "image",
                "category": "user",
            })
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

        print(f"[comfyui-plugin] saved template: {slug}")
        return {"status": "ok", "template_id": slug}

    def get_templates(self, ctx):
        """Return workflow templates compatible with the sample's media type.

        Panel-method twin of the former ``get_comfy_templates`` operator —
        folded in so template fetch uses the same transport as
        ``load_template`` / ``save_template`` (one mechanism, not two).
        """
        filepath = ctx.params.get("filepath", "")
        media_type = _get_media_type(filepath) if filepath else "image"
        print(
            f"[comfyui-plugin] get_templates: filepath={filepath!r}, "
            f"media_type={media_type!r}"
        )

        try:
            manifest = _load_manifest()
        except (OSError, json.JSONDecodeError) as e:
            print(f"[comfyui-plugin] get_templates: manifest error: {e}")
            traceback.print_exc()
            return {"templates": [], "default": None}

        all_templates = manifest.get("templates", [])
        compatible = [
            t for t in all_templates
            if media_type in t.get("input_types", [])
        ]
        compat_ids = [t["id"] for t in compatible]
        print(
            f"[comfyui-plugin] get_templates: returning {len(compatible)}/{len(all_templates)} "
            f"compatible template(s) for media_type={media_type!r}: {compat_ids}"
        )

        return {
            "templates": compatible,
            "default": compatible[0]["id"] if compatible else None,
        }

    def update_config(self, ctx):
        """Update plugin configuration."""
        for key in ("comfyui_path", "comfyui_port", "comfyui_args"):
            val = ctx.params.get(key)
            if val is not None:
                _set_config(ctx, key, val)
        return {"status": "ok"}

    def get_group_slices(self, ctx):
        """Return the dataset's group slices, heatmap fields, and label fields.

        ``slices`` and ``heatmap_fields`` are dataset-level — derived from
        the schema.  ``label_fields`` is **sample-level** — only label
        fields with a non-None value on ``ctx.current_sample`` are
        returned, matching the qwen plugin's "Copy labels" behaviour.
        ``dataset_is_grouped`` is a simple boolean React diffs across
        save calls to detect the flat→grouped migration without
        depending on operator return-value plumbing.

        React calls this after every save and on slice switch so the
        list stays in sync with whichever sample is currently shown.

        Side effect: also refreshes the per-slice
        ``fo_current_sample_<slice>.png`` files in ComfyUI's input dir
        so any newly-created slice immediately becomes available in
        LoadImage dropdowns.
        """
        result = {
            "slices": [],
            "heatmap_fields": [],
            "label_fields": [],
            "dataset_is_grouped": False,
        }
        try:
            dataset = ctx.dataset
            result["dataset_is_grouped"] = bool(dataset.group_field)
            if dataset.group_field:
                media_types = dataset.group_media_types or {}
                for name in dataset.group_slices:
                    result["slices"].append({
                        "name": name,
                        "media_type": media_types.get(name, "image"),
                    })

            schema = dataset.get_field_schema()
            for field_name, field in schema.items():
                if isinstance(field, fo.EmbeddedDocumentField):
                    doc_type = getattr(field, "document_type", None)
                    if doc_type is not None and issubclass(doc_type, fo.Heatmap):
                        result["heatmap_fields"].append(field_name)

            if ctx.current_sample:
                try:
                    sample = dataset[ctx.current_sample]
                    result["label_fields"] = _get_sample_label_fields(dataset, sample)
                except Exception as exc:
                    print(f"[comfyui-plugin] get_group_slices: label fields error: {exc}")
        except Exception as exc:
            print(f"[comfyui-plugin] get_group_slices error: {exc}")

        # Refresh per-slice files so they appear in the LoadImage dropdown.
        try:
            if ctx.current_sample:
                config = self._safe_get_config(ctx)
                comfyui_path = config["comfyui_path"]
                if os.path.isdir(comfyui_path):
                    _inject_all_slices(comfyui_path, ctx.dataset, ctx.current_sample)
        except Exception as exc:
            print(f"[comfyui-plugin] get_group_slices: slice file refresh error: {exc}")

        return result

    def inject_slice(self, ctx):
        """Inject a specific group slice's image into ComfyUI's input.

        Called from React when the active modal slice changes (via the
        Recoil ``modalGroupSlice`` atom).  Three things happen:

        1. The selected slice is copied to ``fo_current_sample.png`` so
           any LoadImage node already pointing at that filename refreshes
           its preview to the new slice.
        2. ``_inject_all_slices`` runs to refresh the per-slice
           ``fo_current_sample_<slice>.png`` files in ComfyUI's input
           dir, so the LoadImage dropdown stays accurate even if a save
           created a new slice since the last initialize.
        3. Panel state is updated with the slice's sample_id / filepath
           so React's ``data.current_sample_id`` / ``data.current_filepath``
           reflect the active slice.  This is belt-and-suspenders: the
           ``on_change_group_slice`` lifecycle hook *should* do the same
           thing, but it doesn't fire reliably in modal composite views,
           so we do it from the Recoil-driven path too.

        Returns the new sample_filename + filepath so React can post a
        SAMPLE_CHANGED message to the bridge.
        """
        slice_name = ctx.params.get("slice_name", "")
        if not slice_name:
            return {"error": "No slice_name provided", "sample_filename": ""}

        config = self._safe_get_config(ctx)
        comfyui_path = config["comfyui_path"]

        try:
            dataset = ctx.dataset
            if not ctx.current_sample:
                return {"error": "No current sample", "sample_filename": ""}

            sample = dataset[ctx.current_sample]
            gf = dataset.group_field

            if not gf:
                return {"error": "Dataset is not grouped", "sample_filename": ""}

            group_elem = sample[gf]
            if not group_elem:
                return {"error": "Sample has no group element", "sample_filename": ""}

            if group_elem.name == slice_name:
                filepath = sample.filepath
                sample_id = sample.id
            else:
                slice_sample = _sample_in_slice(
                    dataset, gf, group_elem.id, slice_name
                )
                if slice_sample is None:
                    return {"error": f"No sample in slice '{slice_name}' for this group", "sample_filename": ""}
                filepath = slice_sample.filepath
                sample_id = slice_sample.id

            print(f"[comfyui-plugin] inject_slice: slice={slice_name} sample_id={sample_id} filepath={filepath}")
            sample_filename = _inject_sample(comfyui_path, filepath)
            if not sample_filename:
                return {"error": f"Cannot inject non-image file: {filepath}", "sample_filename": ""}

            # Push the resolved sample_id / filepath into panel state
            # (see step 3 in the docstring for why this path mirrors the
            # lifecycle hook).
            try:
                ctx.panel.set_state("current_filepath", filepath)
                ctx.panel.set_state("current_sample_id", sample_id)
            except Exception as exc:
                print(f"[comfyui-plugin] inject_slice: set_state error: {exc}")

            # Refresh per-slice files too — a save may have created new
            # slices since the last initialize, and we want them in the
            # LoadImage dropdown without forcing a panel reload.
            try:
                _inject_all_slices(comfyui_path, dataset, ctx.current_sample)
            except Exception as exc:
                print(f"[comfyui-plugin] inject_slice: per-slice refresh error: {exc}")

            return {"sample_filename": sample_filename, "filepath": filepath, "sample_id": sample_id}

        except Exception as exc:
            print(f"[comfyui-plugin] inject_slice error: {exc}")
            return {"error": str(exc), "sample_filename": ""}

    def trigger_reload(self, ctx):
        """Trigger a dataset + samples reload in the FiftyOne App.

        Called from React after a save completes.  Panel methods
        reliably propagate ``ctx.ops`` to the frontend, unlike
        operators invoked via ``useOperatorExecutor``.

        Note: we deliberately do NOT call ``ctx.ops.notify`` here.
        FiftyOne's toast renders with a higher z-index than our save
        dialog, which would block interaction with subsequent dialogs.
        Visual confirmation already comes from the dialog dismissing
        and the dataset reload showing the new sample/slice.
        """
        print("[comfyui-plugin] trigger_reload: reloading dataset + samples")
        ctx.ops.reload_dataset()
        return {"status": "ok"}

    def render(self, ctx):
        return types.Property(
            types.Object(),
            view=types.View(
                component="ComfyUIPanel",
                composite_view=True,
                initialize=self.initialize,
                start_server=self.start_server,
                stop_server=self.stop_server,
                load_template=self.load_template,
                save_template=self.save_template,
                get_templates=self.get_templates,
                update_config=self.update_config,
                get_group_slices=self.get_group_slices,
                inject_slice=self.inject_slice,
                trigger_reload=self.trigger_reload,
            ),
        )


class SaveComfyOutput(foo.Operator):
    """Save output from ComfyUI to the FiftyOne dataset.

    Supports seven output types (image, video, text, depth, detections,
    segmentation, 3d) and multiple destinations (group slice, new
    sample, string field, classification, heatmap, ``fo.Detections``
    field, ``fo.Segmentation`` field).  Fetches metadata from ComfyUI's
    ``/history`` endpoint and stores generation parameters.
    """

    @property
    def config(self):
        return foo.OperatorConfig(
            name="save_comfy_output",
            label="Save ComfyUI Output",
            unlisted=True,
        )

    def execute(self, ctx):
        try:
            # Authoritative source for the save target: resolve at save
            # time using React's ``active_slice`` param (from Recoil's
            # modalGroupSlice atom) + ``ctx.current_sample``.  React's
            # ``data.current_sample_id`` can lag behind slice changes
            # and ``ctx.group_slice`` is unreliable in operator context
            # — so we trust the explicit param above all.
            active_slice_param = ctx.params.get("active_slice") or ""
            resolved_sample_id, resolved_filepath = _resolve_active_slice_sample(
                ctx, slice_override=active_slice_param,
            )
            param_sample_id = ctx.params.get("sample_id") or ""
            param_original_filepath = ctx.params.get("original_filepath") or ""

            print(
                f"[comfyui-plugin] save: target resolution — "
                f"inputs(active_slice={active_slice_param!r}, "
                f"ctx.group_slice={ctx.group_slice!r}, "
                f"ctx.current_sample={ctx.current_sample!r}); "
                f"resolved=({resolved_sample_id!r}, {resolved_filepath!r}); "
                f"React_params=({param_sample_id!r}, {param_original_filepath!r})"
            )

            sample_id = resolved_sample_id or param_sample_id
            original_filepath = resolved_filepath or param_original_filepath

            if (
                resolved_sample_id
                and param_sample_id
                and resolved_sample_id != param_sample_id
            ):
                print(
                    f"[comfyui-plugin] save: ctx-resolved sample_id={resolved_sample_id!r} "
                    f"differs from React's param_sample_id={param_sample_id!r} — using resolved"
                )

            if not sample_id or not original_filepath:
                print(
                    f"[comfyui-plugin] save aborted — no sample loaded "
                    f"(sample_id={sample_id!r}, original_filepath={original_filepath!r})"
                )
                return {"success": False, "error": "No sample loaded — open a sample in the modal first."}

            port = ctx.params.get("port", DEFAULT_COMFYUI_PORT)
            output_type = ctx.params.get("output_type", "image")
            save_as = ctx.params.get("save_as", "group_slice")
            field_name = ctx.params.get("field_name", "comfy_output")
            prompt_id = ctx.params.get("prompt_id")
            node_title = ctx.params.get("node_title", "")

            image_data_b64 = ctx.params.get("image_data", "")
            comfyui_filename = ctx.params.get("comfyui_filename", "")
            comfyui_subfolder = ctx.params.get("comfyui_subfolder", "")
            text_value = ctx.params.get("text_value", "")
            workflow_name = ctx.params.get("workflow_name", "")
            copy_labels = ctx.params.get("copy_labels", "")

            print("[comfyui-plugin] === SAVE START ===")
            print(f"[comfyui-plugin]   prompt_id={prompt_id!r}  node_title={node_title!r}  workflow_name={workflow_name!r}")
            print(f"[comfyui-plugin]   output_type={output_type!r}  save_as={save_as!r}  field_name={field_name!r}")
            print(f"[comfyui-plugin]   comfyui_filename={comfyui_filename!r}  has_image_data={bool(image_data_b64)}")
            print(f"[comfyui-plugin]   copy_labels={copy_labels!r}")

            dataset = ctx.dataset
            original_dir = os.path.dirname(original_filepath)
            original_stem = os.path.splitext(os.path.basename(original_filepath))[0]

            metadata = _fetch_comfy_metadata(port, prompt_id)
            if metadata is not None:
                metadata["workflow_name"] = workflow_name
            print(f"[comfyui-plugin]   metadata returned: {metadata is not None}")

            if output_type in ("image", "depth", "video", "3d"):
                self._save_media(
                    dataset, sample_id, original_dir, original_stem,
                    port, output_type, save_as, field_name, node_title,
                    prompt_id, metadata, image_data_b64,
                    comfyui_filename, comfyui_subfolder, copy_labels,
                )
            elif output_type == "text":
                self._save_text(
                    dataset, sample_id, save_as, field_name, text_value,
                )
            elif output_type == "detections":
                self._save_detections(
                    dataset, sample_id, port, field_name, ctx.params,
                )
            elif output_type == "segmentation":
                self._save_segmentation(
                    dataset, sample_id, original_dir, original_stem,
                    port, field_name, ctx.params,
                )
            else:
                print(f"[comfyui-plugin] unsupported output_type: {output_type}")
                return {"success": False, "error": f"Unsupported type: {output_type}"}

            return {"success": True}

        except Exception as e:
            print(f"[comfyui-plugin] save error: {e}")
            print(traceback.format_exc())
            raise

    def _save_media(self, dataset, sample_id, original_dir, original_stem,
                    port, output_type, save_as, field_name, node_title,
                    prompt_id, metadata, image_data_b64, comfyui_filename,
                    comfyui_subfolder, copy_labels=""):
        """Save an image, video, or depth output as a file + dataset entry."""

        if comfyui_filename:
            image_bytes = _fetch_file_from_comfyui(port, comfyui_filename, comfyui_subfolder)
        elif image_data_b64:
            image_bytes = base64.b64decode(image_data_b64)
        else:
            raise ValueError("No image data or ComfyUI filename provided")

        if output_type == "depth" and save_as == "heatmap":
            map_path = _auto_increment_path(
                os.path.join(original_dir, f"{original_stem}_{int(time.time())}.png")
            )
            with open(map_path, "wb") as f:
                f.write(image_bytes)

            sample = dataset[sample_id]
            schema = dataset.get_field_schema()
            if field_name not in schema:
                dataset.add_sample_field(
                    field_name, fo.EmbeddedDocumentField,
                    embedded_doc_type=fo.Heatmap,
                )
            sample[field_name] = fo.Heatmap(map_path=map_path)
            sample.save()
            print(f"[comfyui-plugin] saved depth heatmap field '{field_name}' → {map_path}")
            return

        # On-disk filename uses a unix timestamp so it is stable across
        # save_as modes (new_sample / group_slice), unique per save, and
        # never depends on user-entered slice or field names.  The user's
        # field_name still controls the slice/field in the dataset.
        #
        # 3D inputs preserve the upstream extension (.glb / .ply / .obj /
        # .stl / .fbx / .pcd / .fo3d) so FiftyOne can route them through
        # the right loader.  Falls back to .glb if the dispatch lacked a
        # filename — shouldn't happen in practice, but keeps the path
        # well-formed.
        if output_type == "video":
            ext = ".mp4"
        elif output_type == "3d":
            ext = os.path.splitext(comfyui_filename)[1].lower() or ".glb"
        else:
            ext = ".png"
        output_path = _auto_increment_path(
            os.path.join(original_dir, f"{original_stem}_{int(time.time())}{ext}")
        )
        with open(output_path, "wb") as f:
            f.write(image_bytes)

        # Per the FiftyOne 3D docs, `.glb / .ply / .obj / .stl / .fbx /
        # .pcd / .fo3d` files all use ``media_type="3d"`` (PCDs included
        # — see the direct-asset example in the user guide).  We pass it
        # explicitly so we don't rely on FiftyOne's extension inference,
        # which has shifted between versions.
        sample_media_type = "3d" if output_type == "3d" else None

        if save_as == "new_sample":
            _ensure_comfy_fields(dataset)
            sample_kwargs = {"filepath": output_path, "tags": ["comfy_output"]}
            if sample_media_type:
                sample_kwargs["media_type"] = sample_media_type

            # If the dataset is grouped, every sample MUST be in a group.
            # The new sample gets its own brand-new group on a slice that
            # matches its media type — it appears as a standalone entry
            # in the grid view, with ``source_sample_id`` linking back to
            # the original sample.
            gf = dataset.group_field
            if gf:
                if output_type == "video":
                    target_media = "video"
                elif output_type == "3d":
                    target_media = "3d"
                else:
                    target_media = "image"
                slice_name = _ensure_compatible_slice(dataset, target_media)
                sample_kwargs[gf] = fo.Group().element(slice_name)
                print(f"[comfyui-plugin]   grouped dataset: new sample → new group, slice='{slice_name}'")

            new_sample = fo.Sample(**sample_kwargs)
            new_sample["comfy_node_title"] = node_title
            new_sample["comfy_prompt_id"] = prompt_id or ""
            new_sample["source_sample_id"] = sample_id
            if metadata:
                self._apply_metadata(new_sample, metadata, dataset)
            self._copy_labels(dataset, sample_id, new_sample, copy_labels)
            dataset.add_sample(new_sample)
            print(f"[comfyui-plugin] saved new sample from {sample_id}")
        else:
            slice_name = field_name
            group_id = _ensure_grouped(dataset, sample_id)
            _ensure_comfy_fields(dataset)

            gf = dataset.group_field
            sample_kwargs = {
                "filepath": output_path,
                gf: fo.Group(id=group_id).element(slice_name),
                "parent_sample_id": sample_id,
                "comfy_node_title": node_title,
                "comfy_prompt_id": prompt_id or "",
                "tags": ["comfy_output"],
            }
            if sample_media_type:
                sample_kwargs["media_type"] = sample_media_type
            new_sample = fo.Sample(**sample_kwargs)

            if metadata:
                self._apply_metadata(new_sample, metadata, dataset)
            self._copy_labels(dataset, sample_id, new_sample, copy_labels)

            # ``add_sample`` auto-registers the slice in
            # ``dataset.group_media_types`` via ``_expand_group_schema``,
            # using the new sample's inferred media type — no need for an
            # explicit ``add_group_slice`` call here.
            dataset.add_sample(new_sample)

            prompt_id_val = (
                new_sample.get_field("comfy_prompt_id")
                if new_sample.has_field("comfy_prompt_id")
                else "?"
            )
            print(
                f"[comfyui-plugin] saved slice '{slice_name}' for sample {sample_id}"
                f" (new sample id={new_sample.id}, comfy_prompt_id={prompt_id_val})"
            )

    @staticmethod
    def _copy_labels(dataset, source_sample_id, target_sample, copy_labels):
        """Deep-copy selected label fields from the source onto a new sample.

        Schema fields already exist (we copy from an existing labelled
        sample) so no ``add_sample_field`` is required.  Silently skips
        fields that aren't on the source — matches the qwen plugin.
        """
        names = _parse_copy_labels(copy_labels)
        if not names:
            return
        try:
            source_sample = dataset[source_sample_id]
        except Exception as exc:
            print(f"[comfyui-plugin] _copy_labels: source lookup failed: {exc}")
            return
        for name in names:
            val = source_sample.get_field(name)
            if val is not None:
                target_sample[name] = copy.deepcopy(val)
                print(f"[comfyui-plugin]   copied label '{name}' from {source_sample_id}")

    @staticmethod
    def _load_masks_npy(port, masks_filename):
        """Fetch and load the per-instance mask ``.npy`` from ComfyUI's output.

        Returns the numpy array, or ``None`` if no filename was given or the
        fetch/parse failed (detections then fall back to box-only).
        """
        if not masks_filename:
            return None
        try:
            npy_bytes = _fetch_file_from_comfyui(port, masks_filename, "")
            masks_arr = np.load(_io.BytesIO(npy_bytes))
            print(
                f"[comfyui-plugin]   loaded masks: shape={masks_arr.shape}, "
                f"dtype={masks_arr.dtype}"
            )
            return masks_arr
        except Exception as exc:
            print(f"[comfyui-plugin]   mask load failed (continuing without masks): {exc}")
            return None

    @staticmethod
    def _resolve_image_dims(dataset, sample_id, image_height, image_width, masks_arr):
        """Resolve image dimensions: payload → sample metadata → mask shape.

        Payload dims (set when the ``image`` socket is connected) win because
        they reflect any post-processing.  Both staying zero falls through to
        identity normalization in ``_build_detections``.
        """
        if image_height <= 0 or image_width <= 0:
            try:
                sample = dataset[sample_id]
                meta = sample.metadata or sample.compute_metadata()
                image_height = int(getattr(meta, "height", 0) or 0)
                image_width = int(getattr(meta, "width", 0) or 0)
                print(
                    f"[comfyui-plugin]   inferred image size from sample.metadata "
                    f"→ {image_height}x{image_width}"
                )
            except Exception as exc:
                print(f"[comfyui-plugin]   sample metadata lookup failed: {exc}")

        if (image_height <= 0 or image_width <= 0) and masks_arr is not None:
            image_height = image_height or int(masks_arr.shape[-2])
            image_width = image_width or int(masks_arr.shape[-1])
            print(
                f"[comfyui-plugin]   inferred image size from masks "
                f"→ {image_height}x{image_width}"
            )

        return image_height, image_width

    @staticmethod
    def _build_detections(boxes, labels, scores, masks_arr, image_height, image_width):
        """Assemble ``fo.Detection`` objects from boxes/labels/scores/masks.

        Pixel-space xyxy boxes are converted to FiftyOne's normalized rxywh
        (identity if dims are unknown).  Malformed and zero-area boxes are
        skipped; per-instance masks (if present) are cropped to each bbox.
        """
        detections = []
        skipped_degenerate = 0
        skipped_malformed = 0
        for i, box in enumerate(boxes):
            if not isinstance(box, (list, tuple)) or len(box) < 4:
                print(f"[comfyui-plugin]   skipping malformed box[{i}]={box!r}")
                skipped_malformed += 1
                continue
            x1, y1, x2, y2 = (float(v) for v in box[:4])
            # Skip degenerate (zero-area) bboxes — they render invisibly
            # in the FiftyOne app and lead to silent "field exists but
            # empty" UX. Common when an upstream mask was all-zero.
            if (x2 - x1) <= 0 or (y2 - y1) <= 0:
                print(f"[comfyui-plugin]   skipping degenerate box[{i}]={box!r} (zero area)")
                skipped_degenerate += 1
                continue
            if image_width <= 0 or image_height <= 0:
                rx, ry, rw, rh = x1, y1, x2 - x1, y2 - y1
            else:
                rx = x1 / image_width
                ry = y1 / image_height
                rw = (x2 - x1) / image_width
                rh = (y2 - y1) / image_height

            label = labels[i] if i < len(labels) else "object"
            confidence = scores[i] if i < len(scores) else None
            kwargs = {
                "label": str(label),
                "bounding_box": [rx, ry, rw, rh],
            }
            if confidence is not None:
                try:
                    kwargs["confidence"] = float(confidence)
                except (TypeError, ValueError):
                    pass

            if masks_arr is not None:
                try:
                    crop = _crop_mask_to_bbox(masks_arr, i, x1, y1, x2, y2)
                    if crop is not None:
                        kwargs["mask"] = crop
                except Exception as exc:
                    print(f"[comfyui-plugin]   mask crop[{i}] failed: {exc}")

            detections.append(fo.Detection(**kwargs))

        if skipped_degenerate or skipped_malformed:
            print(
                f"[comfyui-plugin]   skipped {skipped_degenerate} degenerate + "
                f"{skipped_malformed} malformed box(es) "
                f"out of {len(boxes)} input(s)"
            )
        return detections

    @staticmethod
    def _save_detections(dataset, sample_id, port, field_name, params):
        """Save detections produced by FO_SaveDetections as fo.Detections.

        Polymorphic — accepts both BBOX-style (list-of-list-of-floats) and
        SAM3-style (JSON string of the same shape) box payloads.  Labels
        and scores follow the same flexibility rules.

        Sources of truth (in order of preference):

        - **Image dimensions**: ``image_height``/``image_width`` from the
          payload (set by the node when it has the ``image`` socket
          connected); else read from ``sample.metadata`` server-side.
        - **Boxes**: ``boxes_json`` if provided; else derived from each
          mask's tight enclosure (``np.where``) if only masks are present.
          Matches FiftyOne's ``fo.Detection`` convention — every
          detection has a bbox; masks ride along cropped to that bbox.
        - **Labels**: upstream ``pred_labels_json`` if provided; else
          the user's pill widget (cycled round-robin); else ``"object"``.
        - **Scores**: upstream ``scores_json`` if provided; else None.

        Pixel-space xyxy boxes are converted to FiftyOne's normalized
        rxywh.  Per-detection masks are reconstructed from the ``.npy``
        file the node wrote into ComfyUI's output dir.
        """
        boxes_json = params.get("boxes_json", "")
        pred_labels_json = params.get("pred_labels_json", "")
        scores_json = params.get("scores_json", "")
        masks_filename = params.get("masks_filename", "")
        fallback_labels = params.get("fallback_labels", "")
        image_height = int(params.get("image_height", 0) or 0)
        image_width = int(params.get("image_width", 0) or 0)

        print(
            f"[comfyui-plugin] _save_detections: field={field_name!r}, "
            f"image_payload=({image_height}x{image_width}), "
            f"boxes_json_len={len(boxes_json or '')}, "
            f"labels_json_len={len(pred_labels_json or '')}, "
            f"scores_json_len={len(scores_json or '')}, "
            f"masks_filename={masks_filename!r}, "
            f"fallback_labels={fallback_labels!r}"
        )

        # Load masks first so we can derive bboxes from them if needed.
        masks_arr = SaveComfyOutput._load_masks_npy(port, masks_filename)

        # The node-side already drops boxes that aren't ``[[x1,y1,x2,y2],
        # ...]`` shaped (see FO_SaveDetections.execute), so by the time
        # we get here ``boxes_json`` is either valid bbox JSON or empty.
        boxes = _parse_jsonish_list(boxes_json)

        # Boxes-from-masks fallback (instance-segmentation-style workflows
        # where the user only connected MASK output, e.g. SAM2 / SAM3
        # mask-only flows).
        if not boxes and masks_arr is not None:
            boxes = _bboxes_from_masks(masks_arr)
            print(f"[comfyui-plugin]   derived {len(boxes)} bbox(es) from masks")

        if not boxes:
            print(
                "[comfyui-plugin]   nothing to save — no boxes from upstream "
                "and no masks to derive them from"
            )
            return

        image_height, image_width = SaveComfyOutput._resolve_image_dims(
            dataset, sample_id, image_height, image_width, masks_arr,
        )

        labels = _resolve_detection_labels(
            pred_labels_json, fallback_labels, len(boxes),
        )
        scores = _parse_jsonish_list(scores_json) or [None] * len(boxes)

        detections = SaveComfyOutput._build_detections(
            boxes, labels, scores, masks_arr, image_height, image_width,
        )

        if not detections:
            print(
                f"[comfyui-plugin] nothing usable to save → field {field_name!r} not "
                f"created (check that upstream model produced non-empty masks)"
            )
            return

        sample = dataset[sample_id]
        schema = dataset.get_field_schema()
        if field_name not in schema:
            dataset.add_sample_field(
                field_name, fo.EmbeddedDocumentField,
                embedded_doc_type=fo.Detections,
            )
        sample[field_name] = fo.Detections(detections=detections)
        sample.save()
        print(
            f"[comfyui-plugin] saved {len(detections)} detection(s) → "
            f"{field_name!r} on sample {sample_id}"
        )

    @staticmethod
    def _save_segmentation(dataset, sample_id, original_dir, original_stem,
                           port, field_name, params):
        """Save a segmentation mask as ``fo.Segmentation`` on the sample.

        The mask PNG is fetched from ComfyUI's output dir, copied next
        to the source sample's filepath (so it travels with the dataset),
        and stored on the sample via ``mask_path``.
        """
        comfyui_filename = params.get("comfyui_filename", "")
        mask_targets_str = params.get("mask_targets", "")

        print(
            f"[comfyui-plugin] _save_segmentation: field={field_name!r}, "
            f"comfyui_filename={comfyui_filename!r}, "
            f"mask_targets={mask_targets_str!r}"
        )

        if not comfyui_filename:
            print("[comfyui-plugin]   no mask filename provided — abort")
            return

        png_bytes = _fetch_file_from_comfyui(port, comfyui_filename, "")
        mask_path = _auto_increment_path(
            os.path.join(original_dir, f"{original_stem}_seg_{int(time.time())}.png")
        )
        with open(mask_path, "wb") as f:
            f.write(png_bytes)
        print(f"[comfyui-plugin]   wrote mask → {mask_path}")

        seg_kwargs = {"mask_path": mask_path}
        targets = _parse_mask_targets(mask_targets_str)
        if targets:
            seg_kwargs["mask_targets"] = targets
            print(f"[comfyui-plugin]   mask_targets parsed: {targets}")

        sample = dataset[sample_id]
        schema = dataset.get_field_schema()
        if field_name not in schema:
            dataset.add_sample_field(
                field_name, fo.EmbeddedDocumentField,
                embedded_doc_type=fo.Segmentation,
            )
        sample[field_name] = fo.Segmentation(**seg_kwargs)
        sample.save()
        print(
            f"[comfyui-plugin] saved segmentation → "
            f"{field_name!r} on sample {sample_id}"
        )

    @staticmethod
    def _apply_metadata(sample, metadata: dict, dataset: fo.Dataset):
        """Copy ComfyUI generation metadata onto a sample."""
        print(f"[comfyui-plugin] _apply_metadata: applying to sample {sample.filepath}")

        fields = {
            "comfy_workflow_name": metadata.get("workflow_name", ""),
            "comfy_prompt": metadata.get("prompt", ""),
            "comfy_negative_prompt": metadata.get("negative_prompt", ""),
            "comfy_seed": metadata.get("seed"),
            "comfy_steps": metadata.get("steps"),
            "comfy_cfg": metadata.get("cfg"),
            "comfy_sampler": metadata.get("sampler"),
            "comfy_scheduler": metadata.get("scheduler"),
            "comfy_denoise": metadata.get("denoise"),
            "comfy_model": metadata.get("model", ""),
        }
        for k, v in fields.items():
            if k in _METADATA_STR_FIELDS and v is not None and not isinstance(v, str):
                v = ", ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
            sample[k] = v
            if v is not None and v != "":
                print(f"[comfyui-plugin]   {k} = {v!r}")

        wf_json = metadata.get("workflow_json")
        if wf_json:
            if "comfy_workflow_json" not in dataset.get_field_schema():
                dataset.add_sample_field("comfy_workflow_json", fo.StringField)
            sample["comfy_workflow_json"] = (
                json.dumps(wf_json) if isinstance(wf_json, dict) else str(wf_json)
            )

    @staticmethod
    def _save_text(dataset, sample_id, save_as, field_name, text_value):
        """Save a text output as a sample field."""
        sample = dataset[sample_id]
        schema = dataset.get_field_schema()

        if save_as == "classification":
            if field_name not in schema:
                dataset.add_sample_field(field_name, fo.EmbeddedDocumentField, embedded_doc_type=fo.Classification)
            sample[field_name] = fo.Classification(label=text_value)
        else:
            if field_name not in schema:
                dataset.add_sample_field(field_name, fo.StringField)
            sample[field_name] = text_value

        sample.save()
        print(f"[comfyui-plugin] saved text field '{field_name}' on sample {sample_id}")


def register(p):
    p.register(ComfyUIPanel)
    p.register(SaveComfyOutput)
