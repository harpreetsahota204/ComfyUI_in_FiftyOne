import React, { useCallback, useMemo, useState } from "react";
import {
  ComfyOutputType,
  OutputReadyPayload,
  SaveDestination,
  SliceInfo,
  SAVE_OPTIONS,
} from "./types";
import { COLORS, FONT, INPUT_BASE, LABEL_BASE, OVERLAY_BASE, DIALOG_CARD_BASE } from "./theme";

interface SaveDialogProps {
  payload: OutputReadyPayload;
  onSave: (params: {
    outputType: ComfyOutputType;
    saveAs: SaveDestination;
    fieldName: string;
    payload: OutputReadyPayload;
    copyLabels: string;
  }) => void;
  onCancel: () => void;
  groupSlices?: SliceInfo[];
  /** Available label-field names (filtered server-side to fo.Label
   *  subclasses with non-None value on the source sample). */
  labelFields?: string[];
}

function toSnakeCase(s: string): string {
  // Strip the generic node-type prefix before the first '-' if present.
  // e.g. "Save Image - Close Up" → "Close Up" → "close_up"
  const dashIdx = s.indexOf("-");
  const cleaned = dashIdx >= 0 ? s.slice(dashIdx + 1).trim() : s;

  return (cleaned || s)
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .replace(/([a-z])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");
}

const STYLES = {
  overlay: { ...OVERLAY_BASE },
  dialog: {
    ...DIALOG_CARD_BASE,
    width: "360px",
    maxWidth: "90%",
  },
  title: {
    fontSize: FONT.lg,
    fontWeight: 600,
    color: COLORS.text,
    margin: 0,
  },
  preview: {
    maxWidth: "100%",
    maxHeight: "160px",
    borderRadius: "4px",
    objectFit: "contain" as const,
    backgroundColor: COLORS.bgDark,
  },
  textPreview: {
    backgroundColor: COLORS.bgDark,
    border: `1px solid ${COLORS.borderDialog}`,
    borderRadius: "4px",
    padding: "10px",
    fontSize: FONT.md,
    color: COLORS.textMuted,
    maxHeight: "80px",
    overflow: "auto",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
  },
  meta: {
    fontSize: FONT.sm,
    color: COLORS.textDim,
  },
  fieldGroup: {
    display: "flex",
    flexDirection: "column" as const,
    gap: "4px",
  },
  label: { ...LABEL_BASE },
  select: { ...INPUT_BASE },
  input: { ...INPUT_BASE },
  actions: {
    display: "flex",
    justifyContent: "flex-end",
    gap: "8px",
    marginTop: "4px",
  },
  btnCancel: {
    backgroundColor: COLORS.bgDialog,
    color: COLORS.textMuted,
    border: `1px solid ${COLORS.borderDialog}`,
    borderRadius: "4px",
    padding: "6px 16px",
    fontSize: FONT.md,
    cursor: "pointer",
  },
  btnSave: {
    backgroundColor: COLORS.accent,
    color: COLORS.bg,
    border: "none",
    borderRadius: "4px",
    padding: "6px 16px",
    fontSize: FONT.md,
    fontWeight: 600,
    cursor: "pointer",
  },
};

const SaveDialog: React.FC<SaveDialogProps> = ({
  payload,
  onSave,
  onCancel,
  groupSlices = [],
  labelFields = [],
}) => {
  const options = useMemo(
    () => SAVE_OPTIONS[payload.outputType] || SAVE_OPTIONS.image,
    [payload.outputType]
  );

  const filteredSlices = useMemo(() => {
    const mediaType = payload.outputType === "video" ? "video" : "image";
    return groupSlices.filter((s) => s.mediaType === mediaType);
  }, [groupSlices, payload.outputType]);

  const defaultName = useMemo(
    () => toSnakeCase(payload.nodeTitle || "comfy_output"),
    [payload.nodeTitle]
  );
  const [saveAs, setSaveAs] = useState<SaveDestination>(options[0].value);
  const [fieldName, setFieldName] = useState("");

  // Copy-labels multi-select.  Only meaningful for media saves
  // (image/video → new sample or group slice); text/depth go to fields
  // on the existing sample, so there's nothing to copy onto.
  const showLabelPicker =
    payload.outputType === "image" || payload.outputType === "video";
  const [copyLabelsSet, setCopyLabelsSet] = useState<Set<string>>(new Set());
  const copyLabelsString = useMemo(
    () => Array.from(copyLabelsSet).join(","),
    [copyLabelsSet]
  );

  const handleSave = useCallback(() => {
    const name = fieldName.trim() || defaultName;
    onSave({
      outputType: payload.outputType,
      saveAs,
      fieldName: name,
      payload,
      copyLabels: copyLabelsString,
    });
  }, [onSave, payload, saveAs, fieldName, defaultName, copyLabelsString]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") handleSave();
      if (e.key === "Escape") onCancel();
      e.stopPropagation();
    },
    [handleSave, onCancel]
  );

  const thumbnailSrc = payload.imageDataBase64
    ? `data:image/png;base64,${payload.imageDataBase64}`
    : null;

  return (
    <div style={STYLES.overlay} onClick={onCancel} onKeyDown={handleKeyDown}>
      <div style={STYLES.dialog} onClick={(e) => e.stopPropagation()}>
        <h3 style={STYLES.title}>Save to FiftyOne</h3>

        {payload.outputType === "text" && payload.textValue ? (
          <div style={STYLES.textPreview}>{payload.textValue}</div>
        ) : thumbnailSrc ? (
          <img src={thumbnailSrc} alt="Preview" style={STYLES.preview} />
        ) : null}

        <div style={STYLES.meta}>
          <span>Type: <strong>{payload.outputType.toUpperCase()}</strong></span>
          {payload.nodeTitle && (
            <span> &middot; Source: {payload.nodeTitle}</span>
          )}
        </div>

        <div style={STYLES.fieldGroup}>
          <label style={STYLES.label}>Save as</label>
          <select
            style={STYLES.select}
            value={saveAs}
            onChange={(e) => setSaveAs(e.target.value as SaveDestination)}
          >
            {options.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        {/* When saving as a new sample, the on-disk filename is generated
            server-side from the source filename + timestamp, so we don't
            ask the user for anything. */}
        {saveAs !== "new_sample" && (
          <div style={STYLES.fieldGroup}>
            <label style={STYLES.label}>
              {saveAs === "group_slice" ? "Slice name" : "Field name"}
            </label>
            <input
              style={STYLES.input}
              value={fieldName}
              onChange={(e) => setFieldName(e.target.value)}
              placeholder={defaultName}
              autoFocus
              list={saveAs === "group_slice" && filteredSlices.length > 0
                ? "fo-slice-options"
                : undefined}
            />
            {saveAs === "group_slice" && filteredSlices.length > 0 && (
              <datalist id="fo-slice-options">
                {filteredSlices.map((s) => (
                  <option key={s.name} value={s.name} />
                ))}
              </datalist>
            )}
          </div>
        )}

        {showLabelPicker && (
          <LabelPicker
            available={labelFields}
            selected={copyLabelsSet}
            onChange={setCopyLabelsSet}
          />
        )}

        <div style={STYLES.actions}>
          <button style={STYLES.btnCancel} onClick={onCancel}>
            Cancel
          </button>
          <button style={STYLES.btnSave} onClick={handleSave}>
            Save
          </button>
        </div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// LabelPicker — pill multi-select with type-to-filter dropdown.
//
// Mirrors the inline picker rendered by the ComfyUI bridge over Save
// nodes.  Empty selection = copy nothing (default).  Click a row to
// add/remove a pill; click × on a pill to remove.  No "Select all" /
// "Clear" shortcuts — we keep it deliberately simple.
// ---------------------------------------------------------------------------

interface LabelPickerProps {
  available: string[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}

const LabelPicker: React.FC<LabelPickerProps> = ({ available, selected, onChange }) => {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");

  const matches = useMemo(() => {
    const q = filter.toLowerCase();
    return q ? available.filter((n) => n.toLowerCase().includes(q)) : available;
  }, [available, filter]);

  const toggle = useCallback(
    (name: string) => {
      const next = new Set(selected);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      onChange(next);
    },
    [selected, onChange]
  );

  const remove = useCallback(
    (name: string) => {
      const next = new Set(selected);
      next.delete(name);
      onChange(next);
    },
    [selected, onChange]
  );

  return (
    <div style={STYLES.fieldGroup}>
      <label style={STYLES.label}>Copy labels</label>

      <div
        style={{
          backgroundColor: COLORS.bgDark,
          border: `1px solid ${COLORS.borderDialog}`,
          borderRadius: "4px",
          padding: "4px",
          display: "flex",
          flexWrap: "wrap",
          gap: "4px",
          minHeight: "30px",
          alignItems: "center",
          cursor: available.length === 0 ? "default" : "text",
        }}
        onClick={() => available.length > 0 && setOpen(true)}
      >
        {selected.size === 0 ? (
          <span style={{ color: COLORS.textDim, fontStyle: "italic", padding: "2px 6px", fontSize: FONT.sm }}>
            {available.length === 0
              ? "No labels on this sample"
              : "(none — click to pick)"}
          </span>
        ) : (
          Array.from(selected).map((name) => (
            <span
              key={name}
              style={{
                backgroundColor: COLORS.accent,
                color: COLORS.bg,
                padding: "2px 8px",
                borderRadius: "12px",
                fontSize: FONT.xs,
                display: "inline-flex",
                alignItems: "center",
                gap: "4px",
                cursor: "pointer",
              }}
              onClick={(e) => {
                e.stopPropagation();
                remove(name);
              }}
            >
              {name}
              <span style={{ fontWeight: 700 }}>×</span>
            </span>
          ))
        )}
      </div>

      {open && available.length > 0 && (
        <div
          style={{
            border: `1px solid ${COLORS.borderDialog}`,
            borderRadius: "4px",
            backgroundColor: COLORS.bgDark,
            marginTop: "4px",
            maxHeight: "200px",
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          }}
        >
          <input
            type="text"
            placeholder="Type to filter…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Escape") setOpen(false);
            }}
            autoFocus
            style={{
              ...INPUT_BASE,
              borderRadius: 0,
              border: "none",
              borderBottom: `1px solid ${COLORS.borderDialog}`,
            }}
          />
          <div style={{ overflowY: "auto", flex: 1 }}>
            {matches.length === 0 ? (
              <div
                style={{
                  padding: "8px 10px",
                  color: COLORS.textDim,
                  fontStyle: "italic",
                  fontSize: FONT.sm,
                }}
              >
                (no matches)
              </div>
            ) : (
              matches.map((name) => {
                const isSel = selected.has(name);
                return (
                  <div
                    key={name}
                    onClick={() => toggle(name)}
                    style={{
                      padding: "6px 10px",
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      gap: "8px",
                      color: isSel ? COLORS.accent : COLORS.text,
                      backgroundColor: isSel ? COLORS.bgDialog : "transparent",
                      fontSize: FONT.sm,
                    }}
                  >
                    <span style={{ width: "12px", display: "inline-block" }}>
                      {isSel ? "✓" : ""}
                    </span>
                    <span>{name}</span>
                  </div>
                );
              })
            )}
          </div>
          <div
            style={{
              borderTop: `1px solid ${COLORS.borderDialog}`,
              padding: "4px",
              textAlign: "right",
            }}
          >
            <button
              onClick={() => setOpen(false)}
              style={{
                background: "transparent",
                color: COLORS.textMuted,
                border: "none",
                cursor: "pointer",
                fontSize: FONT.sm,
                padding: "2px 8px",
              }}
            >
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default SaveDialog;
