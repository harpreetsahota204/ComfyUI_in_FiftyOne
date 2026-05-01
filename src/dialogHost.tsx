/**
 * Dialog host — renders save dialogs into a *separate* React root mounted
 * on ``document.body``, decoupled from the main ``ComfyUIPanel`` tree.
 *
 * Why this exists
 * ---------------
 * FiftyOne's ``useOperatorExecutor`` and ``usePanelEvent`` hooks subscribe
 * to Recoil atoms internally.  After our save operator runs and triggers a
 * dataset reload, those atoms enter a loading state.  React's concurrent
 * rendering then defers/drops state updates queued against the panel —
 * including ``setSaveDialogPayload`` — and the dialog never appears.
 *
 * By rendering the dialog in its own ``ReactDOM.createRoot()`` tree, the
 * dialog has its own dispatcher, its own scheduling, and its own state.
 * It is unaffected by what's happening in the panel's tree.
 *
 * Public API
 * ----------
 * - ``showSaveDialog(params)`` — open the dialog with the given payload
 *   and callbacks.  Auto-dismisses when ``onSave``/``onCancel`` resolve.
 * - ``onDialogStateChange(listener)`` — subscribe to open/close events;
 *   used by the panel to manage iframe z-index/pointer-events.
 */

import React, { useEffect, useState } from "react";
import * as ReactDOM from "react-dom/client";
import SaveDialog from "./SaveDialog";
import {
  ComfyOutputType,
  OutputReadyPayload,
  SaveDestination,
  SliceInfo,
} from "./types";

const _DBG = (...args: any[]) =>
  console.log("%c[fo-host]", "color:#9b59b6;font-weight:bold", ...args);

export interface SaveDialogShowParams {
  payload: OutputReadyPayload;
  groupSlices: SliceInfo[];
  /** Label-field names available on the source sample (drives the
   *  "Copy labels" multi-select inside the dialog).  Empty array =
   *  no labels available; the dialog shows a placeholder message. */
  labelFields: string[];
  onSave: (params: {
    outputType: ComfyOutputType;
    saveAs: SaveDestination;
    fieldName: string;
    payload: OutputReadyPayload;
    /** Comma-separated list of label fields the user chose to copy.
     *  ``""`` means copy nothing. */
    copyLabels: string;
  }) => void;
  onCancel?: () => void;
}

// ---------------------------------------------------------------------------
// Module-level state
//
// Lives outside any React tree.  ``_setStateFn`` is the host root's
// internal state setter, captured by the host component on mount and
// nulled on unmount.  If ``showSaveDialog`` is called before the host
// root has rendered, the params are stashed in ``_pendingState`` and the
// host picks them up via ``useState``'s initial value.
// ---------------------------------------------------------------------------

let _root: ReactDOM.Root | null = null;
let _pendingState: SaveDialogShowParams | null = null;
let _setStateFn: ((s: SaveDialogShowParams | null) => void) | null = null;
let _listeners: Array<(open: boolean) => void> = [];

// ---------------------------------------------------------------------------
// Internal host component — owns the dialog state in its own React tree.
// ---------------------------------------------------------------------------

const DialogHostRoot: React.FC = () => {
  const [state, setState] = useState<SaveDialogShowParams | null>(_pendingState);

  // Expose the setter to module scope so showSaveDialog can drive us.
  // Also flush any pending state that arrived before this mount.
  useEffect(() => {
    _setStateFn = setState;
    if (_pendingState) {
      setState(_pendingState);
      _pendingState = null;
    }
    return () => {
      _setStateFn = null;
    };
  }, []);

  // Notify panel listeners when the dialog opens or closes so they can
  // adjust iframe z-index / pointer-events.
  useEffect(() => {
    const open = !!state;
    for (const listener of _listeners) {
      try {
        listener(open);
      } catch (err) {
        console.error("[comfyui-plugin] dialog listener error:", err);
      }
    }
  }, [state]);

  if (!state) return null;

  return (
    <SaveDialog
      payload={state.payload}
      groupSlices={state.groupSlices}
      labelFields={state.labelFields}
      onSave={(params) => {
        try {
          state.onSave(params);
        } catch (err) {
          console.error("[comfyui-plugin] dialog onSave error:", err);
        }
        setState(null);
      }}
      onCancel={() => {
        try {
          state.onCancel?.();
        } catch (err) {
          console.error("[comfyui-plugin] dialog onCancel error:", err);
        }
        setState(null);
      }}
    />
  );
};

function _ensureMounted() {
  if (_root) return;
  const container = document.createElement("div");
  container.id = "fo-comfyui-plugin-dialog-host";
  document.body.appendChild(container);
  _root = ReactDOM.createRoot(container);
  _root.render(<DialogHostRoot />);
  _DBG("dialog host mounted");
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function showSaveDialog(params: SaveDialogShowParams) {
  _ensureMounted();
  if (_setStateFn) {
    _setStateFn(params);
  } else {
    // Host hasn't finished mounting yet — pick up via useState init.
    _pendingState = params;
  }
  _DBG("showSaveDialog:", params.payload.nodeTitle);
}

export function onDialogStateChange(
  listener: (open: boolean) => void,
): () => void {
  _listeners.push(listener);
  return () => {
    _listeners = _listeners.filter((l) => l !== listener);
  };
}
