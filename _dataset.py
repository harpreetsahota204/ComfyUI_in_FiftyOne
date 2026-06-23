"""Grouped-dataset helpers: migration, schema, slice resolution."""

import bson
import fiftyone as fo
from fiftyone import ViewField as F
from fiftyone.core.odm.database import get_db_conn

from ._constants import GROUP_FIELD, ORIGINAL_SLICE


def _ensure_grouped(dataset: fo.Dataset, sample_id: str) -> str:
    """Ensure the dataset is grouped and the sample belongs to a group.

    Returns the sample's group id.  If the dataset was flat, this
    performs the flat→grouped migration in place via a bulk MongoDB
    write.  React detects the migration *after the fact* by diffing
    ``dataset_is_grouped`` from ``get_group_slices`` calls before and
    after a save (it doesn't rely on a flag returned from here),
    because FiftyOne's ``useOperatorExecutor.execute`` is unreliable
    about propagating operator return values across versions.
    """
    sample = dataset[sample_id]
    gf = dataset.group_field

    if not gf:
        dataset.add_group_field(GROUP_FIELD, default=ORIGINAL_SLICE)
        dataset.add_group_slice(ORIGINAL_SLICE, "image")
        gf = dataset.group_field

        # Raw MongoDB for bulk group assignment — the ORM would require
        # loading, modifying, and saving every sample individually.
        db = get_db_conn()
        coll = db[dataset._sample_collection_name]

        target_group_id = None
        n = 0
        for doc in coll.find({gf: {"$exists": False}}):
            g = fo.Group()
            coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {gf: {
                    "_id": bson.ObjectId(g.id),
                    "_cls": "Group",
                    "name": ORIGINAL_SLICE,
                }}},
            )
            if str(doc["_id"]) == sample_id:
                target_group_id = g.id
            n += 1

        dataset.reload()
        print(f"[comfyui-plugin] converted {n} samples to grouped ('{ORIGINAL_SLICE}' slice)")

        if target_group_id is None:
            raise RuntimeError(f"Sample {sample_id} not found during group conversion")
        return target_group_id

    if sample[gf] is None:
        group = fo.Group()
        sample[gf] = group.element(ORIGINAL_SLICE)
        sample.save()
        return group.id

    return sample[gf].id


def _ensure_comfy_fields(dataset: fo.Dataset):
    """Declare ComfyUI metadata fields if not already present."""
    schema = dataset.get_field_schema()
    fields = {
        "comfy_workflow_name": fo.StringField,
        "comfy_prompt": fo.StringField,
        "comfy_negative_prompt": fo.StringField,
        "comfy_seed": fo.IntField,
        "comfy_steps": fo.IntField,
        "comfy_cfg": fo.FloatField,
        "comfy_sampler": fo.StringField,
        "comfy_scheduler": fo.StringField,
        "comfy_denoise": fo.FloatField,
        "comfy_model": fo.StringField,
        "comfy_node_title": fo.StringField,
        "comfy_prompt_id": fo.StringField,
        # Method-A durable fallback for workflow reload: the UI/graph form
        # of the workflow that generated this sample (see _comfy_io and
        # the get_sample_workflow panel method).
        "comfy_workflow_ui_json": fo.StringField,
    }
    for name, ftype in fields.items():
        if name not in schema:
            dataset.add_sample_field(name, ftype)


def _get_sample_label_fields(dataset: fo.Dataset, sample: fo.Sample) -> list:
    """Return label field names with non-None values on *sample*.

    Used to populate the "Copy labels" pickers on Save nodes and in the
    save dialog.  Filtering on:

    - ``EmbeddedDocumentField`` whose ``document_type`` is a
      ``fo.Label`` subclass — excludes generic embedded docs, vector
      embeddings, brain results, etc.
    - non-None value on the source sample — empty fields would be
      useless to copy.
    """
    out = []
    for name, field in dataset.get_field_schema().items():
        if not isinstance(field, fo.EmbeddedDocumentField):
            continue
        doc_type = getattr(field, "document_type", None)
        if doc_type is None:
            continue
        try:
            if not issubclass(doc_type, fo.Label):
                continue
        except TypeError:
            continue
        if sample.get_field(name) is not None:
            out.append(name)
    return out


def _sample_in_slice(dataset, gf: str, group_id, slice_name: str):
    """Return the sample in ``slice_name`` for the group ``group_id``, or None.

    Looks up the sample that shares the group of ``group_id`` but lives on a
    different slice.  Shared by ``_resolve_active_slice_sample`` and
    ``ComfyUIPanel.inject_slice``.
    """
    return (
        dataset
        .select_group_slices(slice_name)
        .match(F(f"{gf}._id") == bson.ObjectId(group_id))
        .first()
    )


def _resolve_active_slice_sample(ctx, slice_override: str = "") -> tuple:
    """Return ``(sample_id, filepath)`` for the slice the user is viewing.

    FiftyOne's ``ctx.current_sample`` always points to the group's
    default ("original") slice sample, regardless of which slice tab
    the user has selected.  This helper looks up the actual visible
    slice's sample.

    Active-slice resolution order:

    1. ``slice_override`` (if non-empty) — the most reliable source,
       since the React panel can pass the slice name directly from
       Recoil's ``modalGroupSlice`` atom.  Used by the save operator,
       where ``ctx.group_slice`` is not consistently populated.
    2. ``ctx.group_slice`` — works in lifecycle hooks and panel
       methods, may be ``None`` in operator context.
    3. None → keep ``ctx.current_sample`` (default-slice case or flat
       dataset).

    Returns ``("", "")`` if no sample is loaded at all.
    """
    if not ctx.current_sample:
        return "", ""
    dataset = ctx.dataset
    try:
        sample = dataset[ctx.current_sample]
    except Exception as exc:
        print(f"[comfyui-plugin] _resolve_active_slice_sample: lookup error: {exc}")
        return "", ""

    gf = dataset.group_field
    sample_id = ctx.current_sample
    filepath = sample.filepath

    active_slice = slice_override or ctx.group_slice or ""

    if gf and active_slice:
        group_elem = sample[gf]
        if group_elem and group_elem.name != active_slice:
            try:
                slice_sample = _sample_in_slice(
                    dataset, gf, group_elem.id, active_slice
                )
                if slice_sample is not None:
                    sample_id = slice_sample.id
                    filepath = slice_sample.filepath
            except Exception as exc:
                print(f"[comfyui-plugin] _resolve_active_slice_sample: slice lookup error: {exc}")

    return sample_id, filepath


def _ensure_compatible_slice(dataset: fo.Dataset, media_type: str) -> str:
    """Return a group-slice name compatible with ``media_type``.

    Tries the dataset's default slice first, then any existing slice with
    a matching media type, and finally creates a new slice named
    ``ORIGINAL_SLICE`` (for image) or ``media_type`` (for video, etc.).

    Used when saving as a "new sample" into a grouped dataset: every
    sample must have a group field, and that group's slice must match
    the new sample's media type.
    """
    media_types = dataset.group_media_types or {}

    default = dataset.default_group_slice
    if default and media_types.get(default) == media_type:
        return default

    for name, mt in media_types.items():
        if mt == media_type:
            return name

    name = ORIGINAL_SLICE if media_type == "image" else media_type
    if name not in dataset.group_slices:
        dataset.add_group_slice(name, media_type)
    return name
