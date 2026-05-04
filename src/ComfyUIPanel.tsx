import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useRecoilValueLoadable } from "recoil";
import { modalGroupSlice as fosModalGroupSlice } from "@fiftyone/state";
import { useOperatorExecutor } from "@fiftyone/operators";
import { usePluginClient } from "./hooks/usePluginClient";
import {
  ComfyOutputType,
  OutputReadyPayload,
  SaveDestination,
  ServerStatus,
  SliceInfo,
  TemplateInfo,
} from "./types";
import { COLORS, FONT, INPUT_BASE, BUTTON_BASE, LABEL_BASE, OVERLAY_BASE, DIALOG_CARD_BASE } from "./theme";
import { MSG } from "./messageTypes";
import { onDialogStateChange, showSaveDialog } from "./dialogHost";

const PLUGIN_NAME = "@harpreetsahota/comfyui-plugin";
const _DBG = (...args: any[]) => console.log("%c[fo-panel]", "color:#f0a500;font-weight:bold", ...args);

// ---------------------------------------------------------------------------
// Module-level cache — survives React unmount/remount (panel position change)
// ---------------------------------------------------------------------------

interface ServerStateCache {
  iframeUrl: string;
  serverPort: number;
  serverStatus: string;
  configPath: string;
  sampleFilename: string;
}

let _cachedServerState: ServerStateCache | null = null;

interface CachedSliceInfo {
  slices: SliceInfo[];
  heatmapFields: string[];
  labelFields: string[];
  /** True iff the dataset has a group field.  Used by ``executeSave``
   *  to detect the flat→grouped migration without relying on the save
   *  operator's return value (FiftyOne's useOperatorExecutor.execute
   *  resolves with undefined in some versions). */
  datasetIsGrouped: boolean;
}

/**
 * Module-level mutable state that must survive React unmount/remount
 * (e.g. when the panel is repositioned in the FiftyOne UI).
 *
 * - persistedIframe / persistedIframeSrc: the DOM iframe element and its URL
 * - bridgeReady: true once the ComfyUI bridge has sent BRIDGE_READY
 * - serverInitialized: true after the first successful initialize() call
 * - lastFilepath: tracks the previous sample filepath to avoid spurious
 *   SAMPLE_CHANGED messages when reload_dataset re-sets the same value
 * - cachedSliceInfo: typed slice + heatmap field + label field data
 *   forwarded to the bridge
 * - flatGroupedNotice: flips true when ``executeSave`` detects a
 *   flat→grouped state transition (datasetIsGrouped went from false
 *   to true across a save); the panel renders a one-time refresh
 *   banner until the user dismisses it
 * - depthSavedNotice: flips true after every successful depth-heatmap
 *   save; the panel renders a refresh banner reminding the user that
 *   the new heatmap won't render in the modal until a browser refresh
 *   (FiftyOne's heatmap renderer caches aggressively and sometimes
 *   throws ``Cannot perform Construct on a detached ArrayBuffer`` on
 *   first paint after a fresh save).  Cleared on dismiss; re-fires
 *   on the next depth save.
 * - maskSavedNotice: flips true after a successful detections- or
 *   segmentation-save that involves a mask. Same FiftyOne caching
 *   problem as heatmaps — newly saved masks often don't render until
 *   the page is refreshed.
 */
const _module = {
  persistedIframe: null as HTMLIFrameElement | null,
  persistedIframeSrc: "",
  bridgeReady: false,
  serverInitialized: false,
  lastFilepath: "",
  cachedSliceInfo: {
    slices: [],
    heatmapFields: [],
    labelFields: [],
    datasetIsGrouped: false,
  } as CachedSliceInfo,
  flatGroupedNotice: false,
  depthSavedNotice: false,
  maskSavedNotice: false,
};

// ---------------------------------------------------------------------------
// Module-level postMessage handler — survives React unmount/remount so
// messages from the iframe are never silently dropped during the gap between
// effect cleanup and re-registration.
// ---------------------------------------------------------------------------

type MessageCallback = (data: any) => void;
let _messageCallbackRef: MessageCallback | null = null;

function _globalMessageHandler(event: MessageEvent) {
  const msgType = event.data?.type;
  if (!msgType) return;

  if (msgType === MSG.BRIDGE_READY) {
    _DBG("postMessage(global): BRIDGE_READY received (was bridgeReady=", _module.bridgeReady, ")");
    _module.bridgeReady = true;

    const ci = _module.cachedSliceInfo;
    const hasInfo =
      ci.slices.length > 0 ||
      ci.heatmapFields.length > 0 ||
      ci.labelFields.length > 0;
    if (hasInfo && _module.persistedIframe?.contentWindow) {
      _module.persistedIframe.contentWindow.postMessage(
        {
          type: MSG.SLICE_INFO,
          slices: ci.slices,
          heatmapFields: ci.heatmapFields,
          labelFields: ci.labelFields,
        },
        "*"
      );
      _DBG("postMessage(global): sent cached SLICE_INFO on BRIDGE_READY, slices=", ci.slices, "heatmapFields=", ci.heatmapFields, "labelFields=", ci.labelFields);
    }
    return;
  }

  // Forward all fiftyone_* messages to the current React callback
  if (String(msgType).startsWith("fiftyone_") && _messageCallbackRef) {
    _messageCallbackRef(event.data);
  }
}

// Register once at module load time — never removed.
window.addEventListener("message", _globalMessageHandler);

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const STYLES = {
  container: {
    display: "flex",
    flexDirection: "column" as const,
    height: "100%",
    width: "100%",
    backgroundColor: COLORS.bg,
    color: COLORS.text,
    fontFamily: FONT.family,
    position: "relative" as const,
    overflow: "hidden" as const,
  },
  toolbar: {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "6px 12px",
    backgroundColor: COLORS.bgLight,
    borderBottom: `1px solid ${COLORS.border}`,
    flexShrink: 0,
    minHeight: "40px",
    flexWrap: "wrap" as const,
  },
  statusDot: (status: ServerStatus) => ({
    width: "10px",
    height: "10px",
    borderRadius: "50%",
    backgroundColor:
      status === "ready"
        ? COLORS.accent
        : status === "starting"
        ? COLORS.warning
        : COLORS.error,
    flexShrink: 0,
  }),
  statusText: {
    fontSize: FONT.sm,
    color: COLORS.textMuted,
    minWidth: "60px",
  },
  select: {
    ...INPUT_BASE,
    backgroundColor: COLORS.bg,
    padding: "4px 8px",
    fontSize: FONT.sm,
    cursor: "pointer",
    maxWidth: "200px",
  },
  button: { ...BUTTON_BASE },
  buttonDanger: {
    backgroundColor: COLORS.danger,
  },
  noticeBanner: {
    backgroundColor: "#FFEB52",
    color: "#1a1a2e",
    padding: "10px 16px",
    display: "flex",
    alignItems: "center",
    gap: "12px",
    fontSize: FONT.md,
    flexShrink: 0,
    borderBottom: "1px solid rgba(0,0,0,0.15)",
  } as const,
  noticeDismiss: {
    background: "transparent",
    border: "1px solid rgba(0,0,0,0.4)",
    color: "#1a1a2e",
    borderRadius: "4px",
    padding: "4px 12px",
    fontSize: FONT.sm,
    cursor: "pointer",
    fontWeight: 600,
  } as const,
  iframeWrapper: {
    position: "relative" as const,
    flex: 1,
    overflow: "hidden" as const,
  },
  centerMessage: {
    display: "flex",
    flexDirection: "column" as const,
    alignItems: "center",
    justifyContent: "center",
    flex: 1,
    gap: "12px",
    padding: "24px",
    textAlign: "center" as const,
  },
  messageTitle: {
    fontSize: FONT.xl,
    fontWeight: 600,
    color: COLORS.text,
  },
  messageBody: {
    fontSize: FONT.md,
    color: COLORS.textMuted,
    maxWidth: "400px",
    lineHeight: "1.5",
  },
  configPanel: {
    padding: "16px",
    display: "flex",
    flexDirection: "column" as const,
    gap: "12px",
    flex: 1,
  },
  inputGroup: {
    display: "flex",
    flexDirection: "column" as const,
    gap: "4px",
  },
  label: { ...LABEL_BASE },
  input: { ...INPUT_BASE, backgroundColor: COLORS.bg },
  spinner: {
    width: "18px",
    height: "18px",
    border: `2px solid ${COLORS.border}`,
    borderTopColor: COLORS.accent,
    borderRadius: "50%",
    animation: "comfyui-spin 0.8s linear infinite",
  },
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

let _mountCount = 0;

const ComfyUIPanel: React.FC<any> = ({ data, schema }) => {
  const mountId = useRef(++_mountCount);

  useEffect(() => {
    _DBG(`MOUNT #${mountId.current} | _module=`, JSON.stringify({
      persistedIframe: !!_module.persistedIframe,
      persistedIframeSrc: _module.persistedIframeSrc,
      bridgeReady: _module.bridgeReady,
      serverInitialized: _module.serverInitialized,
      lastFilepath: _module.lastFilepath,
    }), "| _cachedServerState=", _cachedServerState);
    return () => {
      _DBG(`UNMOUNT #${mountId.current} | _module.persistedIframe=`, !!_module.persistedIframe);
    };
  }, []);

  // Panel method URIs come from the Python render() → types.View(...)
  const uris = useMemo(() => ({
    initialize: schema?.view?.initialize ?? "",
    start_server: schema?.view?.start_server ?? "",
    stop_server: schema?.view?.stop_server ?? "",
    load_template: schema?.view?.load_template ?? "",
    save_template: schema?.view?.save_template ?? "",
    update_config: schema?.view?.update_config ?? "",
    get_group_slices: schema?.view?.get_group_slices ?? "",
    inject_slice: schema?.view?.inject_slice ?? "",
    trigger_reload: schema?.view?.trigger_reload ?? "",
  }), [schema]);

  // Sample state comes from Python on_load / on_change_* via set_state
  const currentSampleId = data?.current_sample_id || "";
  const currentFilepath = data?.current_filepath || "";

  // Server state — initialized from module-level cache if available
  const c = _cachedServerState;
  const [serverStatus, setServerStatus] = useState<ServerStatus>(
    (c?.serverStatus as ServerStatus) || ""
  );
  const [serverPort, setServerPort] = useState(c?.serverPort || 8188);
  const [serverError, setServerError] = useState("");
  const [iframeUrl, setIframeUrl] = useState(c?.iframeUrl || "");
  const [sampleFilename, setSampleFilename] = useState(c?.sampleFilename || "");

  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<string>("");
  const [showSettings, setShowSettings] = useState(false);
  const [configPath, setConfigPath] = useState(c?.configPath || "");
  const [configPort, setConfigPort] = useState(String(c?.serverPort || 8188));
  const [saving, setSaving] = useState(false);
  const [hostDialogOpen, setHostDialogOpen] = useState(false);
  const [lastAvailableOutputs, setLastAvailableOutputs] = useState<
    { filename: string; subfolder: string; nodeId: number | null; promptId: string | null }[]
  >([]);
  const [templateNameDialog, setTemplateNameDialog] = useState<{ workflow: any } | null>(null);
  const [templateNameInput, setTemplateNameInput] = useState("");
  const [groupSlices, setGroupSlices] = useState<SliceInfo[]>([]);
  const [labelFields, setLabelFields] = useState<string[]>([]);
  const [showFlatGroupedBanner, setShowFlatGroupedBanner] = useState(_module.flatGroupedNotice);
  const [showDepthSavedBanner, setShowDepthSavedBanner] = useState(_module.depthSavedNotice);
  const [showMaskSavedBanner, setShowMaskSavedBanner] = useState(_module.maskSavedNotice);

  // ── Read modalGroupSlice as a Loadable, NOT via useRecoilValue ───────
  //
  // `useRecoilValue` *suspends* the component while the atom is loading.
  // FiftyOne re-loads dataset state after every save (`reload_dataset()`),
  // which puts modalGroupSlice into a loading state.  Suspending this
  // panel causes its render queue to defer, which has previously broken
  // multi-save flows.  The Loadable variant returns a status object
  // instead of suspending, so renders continue normally.  We only consume
  // the value when it is ready ("hasValue"); otherwise treat it as
  // "no slice change yet".
  const modalSliceLoadable = useRecoilValueLoadable<string | null>(fosModalGroupSlice);
  const activeModalSlice =
    modalSliceLoadable.state === "hasValue" ? modalSliceLoadable.contents : null;

  /** Apply server state from a Python panel-method return value and update cache. */
  const applyServerState = useCallback((result: Record<string, any>) => {
    if (!result) return;
    if (result.server_status) setServerStatus(result.server_status as ServerStatus);
    if (result.server_error !== undefined) setServerError(result.server_error);
    if (result.iframe_url !== undefined) setIframeUrl(result.iframe_url);
    if (result.sample_filename !== undefined) setSampleFilename(result.sample_filename);
    if (result.comfyui_path) setConfigPath(result.comfyui_path);
    if (result.server_port) {
      setServerPort(result.server_port);
      setConfigPort(String(result.server_port));
    }

    const prev = _cachedServerState;
    _cachedServerState = {
      iframeUrl: result.iframe_url ?? prev?.iframeUrl ?? "",
      serverPort: result.server_port ?? prev?.serverPort ?? 8188,
      serverStatus: result.server_status ?? prev?.serverStatus ?? "",
      configPath: result.comfyui_path ?? prev?.configPath ?? "",
      sampleFilename: result.sample_filename ?? prev?.sampleFilename ?? "",
    };
  }, []);

  const iframeRef = useRef<HTMLIFrameElement>(_module.persistedIframe);
  const iframeContainerRef = useRef<HTMLDivElement>(null);
  // Mirrors `dialogOpen` so the iframe positioning function (running on
  // a 500 ms interval) can decide whether to re-enable pointer-events
  // without forcing the whole effect to tear down/re-attach on each
  // dialog open/close.
  const _dialogOpenRef = useRef(false);

  // Tracks whether the user clicked "Save Template" — used by the
  // postMessage handler below to know that an incoming WORKFLOW_DATA
  // reply was solicited by us (vs. arriving for some other reason).
  const pendingSaveTemplateRef = useRef(false);

  const client = usePluginClient(uris);
  const saveExecutor = useOperatorExecutor(`${PLUGIN_NAME}/save_comfy_output`);
  const templatesExecutor = useOperatorExecutor(
    `${PLUGIN_NAME}/get_comfy_templates`
  );

  // ── Fetch group slices from Python and forward to iframe ─────────────

  const sendSlicesToIframe = useCallback((result: {
    slices?: { name: string; media_type: string }[];
    heatmap_fields?: string[];
    label_fields?: string[];
    dataset_is_grouped?: boolean;
  }) => {
    const typedSlices: SliceInfo[] = (result.slices || [])
      .filter((s) => s.name !== "original")
      .map((s) => ({ name: s.name, mediaType: s.media_type }));
    const heatmapFields = result.heatmap_fields || [];
    const labelFieldsArr = result.label_fields || [];
    const datasetIsGrouped = !!result.dataset_is_grouped;

    setGroupSlices(typedSlices);
    setLabelFields(labelFieldsArr);
    _module.cachedSliceInfo = {
      slices: typedSlices,
      heatmapFields,
      labelFields: labelFieldsArr,
      datasetIsGrouped,
    };

    if (_module.bridgeReady && iframeRef.current?.contentWindow) {
      iframeRef.current.contentWindow.postMessage(
        {
          type: MSG.SLICE_INFO,
          slices: typedSlices,
          heatmapFields,
          labelFields: labelFieldsArr,
        },
        "*"
      );
      _DBG("sent SLICE_INFO to iframe, slices=", typedSlices, "heatmapFields=", heatmapFields, "labelFields=", labelFieldsArr, "datasetIsGrouped=", datasetIsGrouped);
    } else {
      _DBG("sendSlicesToIframe: cached (bridge not ready), slices=", typedSlices, "heatmapFields=", heatmapFields, "labelFields=", labelFieldsArr, "datasetIsGrouped=", datasetIsGrouped);
    }
  }, []);

  /** Returns a Promise so callers can await the slice list update before
   *  reading ``_module.cachedSliceInfo`` (e.g. to diff datasetIsGrouped
   *  across a save call).  Errors are swallowed and logged. */
  const refreshGroupSlices = useCallback(() => {
    return client.getGroupSlices().then((result) => {
      sendSlicesToIframe(result);
    }).catch((err) => {
      console.warn("[comfyui-plugin] refreshGroupSlices error:", err);
    });
  }, [client, sendSlicesToIframe]);

  // ── Initialize: call Python to check server + inject sample ─────────
  //
  // Runs on mount and again when currentFilepath arrives / changes.
  // If we have a cached iframe URL (panel repositioned), skip the spinner
  // and run initialize in the background to verify + re-inject sample.

  useEffect(() => {
    _DBG("initialize effect: currentFilepath=", currentFilepath, "serverInitialized=", _module.serverInitialized, "_module.lastFilepath=", _module.lastFilepath);

    if (!currentFilepath && _module.serverInitialized) {
      _DBG("initialize effect: SKIPPED (no filepath + already initialized)");
      return;
    }

    const hadCache = !!_cachedServerState?.iframeUrl;
    const filepathActuallyChanged = currentFilepath !== _module.lastFilepath;
    _DBG("initialize effect: hadCache=", hadCache, "filepathActuallyChanged=", filepathActuallyChanged);

    client
      .initialize({ filepath: currentFilepath })
      .then((result) => {
        _DBG("initialize result:", result?.server_status, "sample_filename=", result?.sample_filename, "iframe_url=", result?.iframe_url);
        _module.serverInitialized = true;
        _module.lastFilepath = currentFilepath;
        applyServerState(result);

        if (hadCache && result.server_status !== "ready") {
          _DBG("initialize: cache invalidated (server not ready)");
          _cachedServerState = null;
        }

        // Only notify the bridge when the sample actually changed
        // (not after a save→reload that re-sets the same filepath)
        if (filepathActuallyChanged && result.sample_filename && _module.bridgeReady) {
          _DBG("initialize: sending SAMPLE_CHANGED to iframe");
          iframeRef.current?.contentWindow?.postMessage(
            { type: MSG.SAMPLE_CHANGED },
            "*"
          );
        } else {
          _DBG("initialize: NOT sending SAMPLE_CHANGED (filepathChanged=", filepathActuallyChanged, "sample_filename=", result?.sample_filename, "bridgeReady=", _module.bridgeReady, ")");
        }

        if (result.server_status === "ready") {
          refreshGroupSlices();
        }
      })
      .catch((err: any) =>
        console.error("[comfyui-plugin] initialize error:", err)
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentFilepath]);

  // ── Load templates when filepath changes ─────────────────────────────

  useEffect(() => {
    if (!currentFilepath) return;
    _DBG("templates effect: calling get_comfy_templates with filepath=", currentFilepath);
    templatesExecutor
      .execute({ filepath: currentFilepath })
      .then((res: any) => {
        const result = res?.result || res;
        const tpls = result?.templates || [];
        _DBG("templates effect: got", tpls.length, "templates, default=", result?.default, "ids=", tpls.map((t: any) => t.id));
        setTemplates(tpls);
        if (result?.default && !selectedTemplate) {
          setSelectedTemplate(result.default);
        }
      })
      .catch((err: any) =>
        console.warn("[comfyui-plugin] template load error:", err)
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentFilepath]);

  // ── Save logic (shared by auto-save and dialog-save) ─────────────────

  const executeSave = useCallback(
    async (
      outputType: ComfyOutputType,
      saveAs: SaveDestination,
      fieldName: string,
      payload: OutputReadyPayload,
      copyLabelsOverride?: string,
    ) => {
      // We don't bail when React-side state is empty — the operator
      // self-heals via ctx.current_sample / ctx.group_slice (see
      // Python side).  Just log and let the operator decide.
      const copyLabels = copyLabelsOverride ?? payload.copyLabels ?? "";

      // Snapshot grouped state *before* the save so we can diff
      // afterward.  The Python operator does perform the flat→grouped
      // migration when applicable, but useOperatorExecutor.execute may
      // resolve with undefined depending on the FiftyOne version, so we
      // can't rely on a "migrated_from_flat" flag in the result —
      // detecting via state transition is unambiguous.
      const wasGrouped = _module.cachedSliceInfo.datasetIsGrouped;

      // Send the active slice name explicitly to the operator.  Recoil's
      // modalGroupSlice atom is the only reliable source of truth for
      // "which slice tab is the user looking at?" — ctx.group_slice
      // server-side is not consistently populated in operator context,
      // and React's data.current_sample_id can lag behind the active
      // slice.  The operator uses this to resolve the save target.
      const activeSliceForSave = activeModalSlice;

      setSaving(true);
      _DBG(
        "executeSave: target —",
        "currentSampleId=", currentSampleId,
        "currentFilepath=", currentFilepath,
        "active_slice=", activeSliceForSave,
        "wasGrouped=", wasGrouped,
      );
      // We deliberately don't log ``extras`` — for detections it can
      // be MB-sized JSON (boxesJson). The bridge already logs a
      // structured per-type summary at OUTPUT_READY emit time.
      _DBG(
        "executeSave: config —",
        "outputType=", outputType,
        "saveAs=", saveAs,
        "fieldName=", fieldName,
        "copyLabels=", copyLabels,
        "autoSave=", !!payload.autoSave,
        "prompt_id=", payload.promptId,
        "nodeTitle=", payload.nodeTitle,
        "extrasKeys=", Object.keys(payload.extras || {}),
      );

      // Detection / segmentation extras ride alongside the standard
      // params.  The Python operator pulls them from ctx.params with
      // snake_case keys; everything else uses the existing channels.
      const extras = payload.extras || {};
      try {
        await saveExecutor.execute({
          sample_id: currentSampleId,
          original_filepath: currentFilepath,
          port: serverPort,
          output_type: outputType,
          save_as: saveAs,
          field_name: fieldName,
          prompt_id: payload.promptId,
          node_title: payload.nodeTitle,
          workflow_name: payload.workflowName || "",
          image_data: payload.imageDataBase64 || "",
          comfyui_filename: payload.filename || "",
          comfyui_subfolder: payload.subfolder || "",
          text_value: payload.textValue || "",
          copy_labels: copyLabels,
          active_slice: activeSliceForSave ?? "",
          // Detection-specific (passed verbatim, ignored by other types)
          image_height: extras.imageHeight ?? 0,
          image_width: extras.imageWidth ?? 0,
          boxes_json: extras.boxesJson ?? "",
          pred_labels_json: extras.predLabelsJson ?? "",
          scores_json: extras.scoresJson ?? "",
          masks_filename: extras.masksFilename ?? "",
          fallback_labels: extras.fallbackLabels ?? "",
          // Segmentation-specific
          mask_targets: extras.maskTargets ?? "",
        });

        // Wait for the slice-list refresh so cachedSliceInfo is current
        // by the time we diff datasetIsGrouped below.
        await refreshGroupSlices();

        const isNowGrouped = _module.cachedSliceInfo.datasetIsGrouped;
        if (!wasGrouped && isNowGrouped && saveAs === "group_slice") {
          _DBG("executeSave: flat→grouped transition detected, raising banner");
          _module.flatGroupedNotice = true;
          setShowFlatGroupedBanner(true);
        }

        // Depth heatmap saves don't always render correctly without a
        // browser refresh — FiftyOne's heatmap worker caches the prior
        // (or null) heatmap and sometimes throws on the new ArrayBuffer.
        // Surface a banner so the user knows to refresh.
        if (outputType === "depth") {
          _DBG("executeSave: depth save complete, raising refresh banner");
          _module.depthSavedNotice = true;
          setShowDepthSavedBanner(true);
        }

        // Detections / segmentation saves with masks have the same
        // caching pitfall as heatmaps — newly attached masks often
        // don't render until the page is refreshed.  Banner reminds
        // the user.
        if (outputType === "detections" || outputType === "segmentation") {
          _DBG(`executeSave: ${outputType} save complete, raising refresh banner`);
          _module.maskSavedNotice = true;
          setShowMaskSavedBanner(true);
        }

        _DBG("executeSave: triggering dataset reload via panel method");
        client.triggerReload();
      } catch (err) {
        console.error("[comfyui-plugin] save error:", err);
      } finally {
        setSaving(false);
      }
    },
    [currentSampleId, currentFilepath, serverPort, saveExecutor, refreshGroupSlices, client, activeModalSlice]
  );

  // ── Output handling ──────────────────────────────────────────────────
  //
  // Two paths:
  //  - autoSave=true (FO_Save* nodes): bypass the dialog, save immediately
  //  - autoSave=false (right-click "Save to FiftyOne"): open the host
  //    dialog (rendered in a separate React root — see dialogHost.tsx for
  //    why this matters)

  // Routes an OutputReadyPayload to either an immediate save (FO_Save*
  // nodes set autoSave=true) or the host-rendered save dialog (right-click
  // "Save to FiftyOne").  No queue — see dialogHost.tsx for why.
  const handleOutput = useCallback(
    (payload: OutputReadyPayload) => {
      if (payload.autoSave) {
        _DBG("handleOutput: autoSave=true, saving directly (saveMode=", payload.saveMode, "fieldName=", payload.fieldName, "copyLabels=", payload.copyLabels, ")");
        executeSave(
          payload.outputType,
          payload.saveMode || "group_slice",
          payload.fieldName || "comfy_output",
          payload,
        );
        return;
      }
      _DBG("handleOutput: autoSave=false, opening dialog for", payload.nodeTitle);
      showSaveDialog({
        payload,
        groupSlices,
        labelFields,
        onSave: (params) =>
          executeSave(
            params.outputType,
            params.saveAs,
            params.fieldName,
            params.payload,
            params.copyLabels,
          ),
      });
    },
    [executeSave, groupSlices, labelFields]
  );

  // ── Wire the module-level message handler to React state setters ─────
  //
  // The actual `window.addEventListener("message", ...)` lives at module
  // scope (see _globalMessageHandler) so it is NEVER removed — avoiding
  // the gap between effect cleanup and re-registration that caused
  // OUTPUT_READY messages to be silently dropped.

  useEffect(() => {
    _messageCallbackRef = (data: any) => {
      const msgType = data?.type;
      _DBG("postMessage(cb): received type=", msgType);

      if (msgType === MSG.OUTPUT_READY) {
        // Don't dump `extras` — for detections it can hold MBs of JSON.
        // The bridge already logs a structured per-type summary.
        _DBG(
          "postMessage(cb): OUTPUT_READY",
          "outputType=", data.outputType,
          "nodeTitle=", data.nodeTitle,
          "saveMode=", data.saveMode,
          "fieldName=", data.fieldName,
          "autoSave=", !!data.autoSave,
          "promptId=", data.promptId,
          "filename=", data.filename || "(empty)",
          "copyLabels=", data.copyLabels || "(empty)",
          "hasImageData=", !!data.imageDataBase64,
        );
        handleOutput({
          outputType: data.outputType || "image",
          nodeTitle: data.nodeTitle || "",
          nodeId: data.nodeId ?? null,
          promptId: data.promptId || null,
          workflowName: data.workflowName || "",
          filename: data.filename,
          subfolder: data.subfolder,
          textValue: data.textValue,
          imageDataBase64: data.imageDataBase64,
          autoSave: data.autoSave || false,
          saveMode: data.saveMode,
          fieldName: data.fieldName,
          copyLabels: data.copyLabels,
          extras: data.extras,
        });
        return;
      }

      if (msgType === MSG.OUTPUT_AVAILABLE) {
        _DBG("postMessage(cb): OUTPUT_AVAILABLE, outputs=", data.outputs?.length);
        const outputs = (data.outputs || []).map((o: any) => ({
          filename: o.filename,
          subfolder: o.subfolder || "",
          nodeId: data.nodeId ?? null,
          promptId: data.promptId || null,
        }));
        setLastAvailableOutputs(outputs);
        return;
      }

      if (msgType === MSG.WORKFLOW_DATA && pendingSaveTemplateRef.current) {
        pendingSaveTemplateRef.current = false;
        setTemplateNameDialog({ workflow: data.workflow });
        setTemplateNameInput("");
        return;
      }
    };

    return () => {
      _messageCallbackRef = null;
    };
  }, [handleOutput]);

  // ── Inject CSS keyframes for spinner ─────────────────────────────────

  useEffect(() => {
    const styleId = "comfyui-plugin-keyframes";
    if (document.getElementById(styleId)) return;
    const style = document.createElement("style");
    style.id = styleId;
    style.textContent = `@keyframes comfyui-spin { to { transform: rotate(360deg); } }`;
    document.head.appendChild(style);
  }, []);

  // ── Persist iframe across unmount/remount (panel reposition) ────────
  //
  // CRITICAL: The iframe is appended to document.body (not to the React
  // container) and is NEVER removed from the DOM.  Removing an iframe from
  // the DOM destroys its contentWindow, which forces ComfyUI to fully
  // reload and lose all workflow state.  Instead we position the iframe
  // over the container using fixed coordinates and hide/show it on
  // mount/unmount.

  useEffect(() => {
    const container = iframeContainerRef.current;
    _DBG("iframe effect: container=", !!container, "iframeUrl=", iframeUrl, "persistedIframe=", !!_module.persistedIframe, "persistedSrc=", _module.persistedIframeSrc);

    if (!container || !iframeUrl) {
      _DBG("iframe effect: SKIPPED (no container or url)");
      return;
    }

    let iframe = _module.persistedIframe;

    if (iframe && _module.persistedIframeSrc === iframeUrl) {
      _DBG("iframe effect: REUSING persisted iframe, contentWindow=", !!iframe.contentWindow);
    } else {
      _DBG("iframe effect: CREATING new iframe (old persisted=", !!iframe, ")");
      if (iframe) iframe.remove();
      // The new bridge has not posted BRIDGE_READY yet — reset the flag so
      // we don't post messages into a half-loaded iframe.  It flips back
      // to true via _globalMessageHandler when the new bridge announces.
      _module.bridgeReady = false;
      iframe = document.createElement("iframe");
      iframe.style.cssText =
        "position:fixed;border:none;z-index:9999;pointer-events:auto;";
      iframe.tabIndex = 0;
      iframe.allow = "clipboard-read; clipboard-write";
      iframe.src = iframeUrl;
      document.body.appendChild(iframe);
      _module.persistedIframe = iframe;
      _module.persistedIframeSrc = iframeUrl;
    }

    iframeRef.current = iframe;

    // Position the iframe over the container using fixed coordinates.
    //
    // Visibility logic:
    //   - container offscreen / 0-sized / display:none ancestor →
    //     hide AND disable pointer events (otherwise the iframe stays
    //     parked at z-index 9999 covering whatever the user is now
    //     looking at, e.g. another panel in a split view).
    //   - container visible → show and let the dialog effect own
    //     pointer-events.
    //
    // We also clamp the iframe's rect to the viewport so it can't
    // bleed past the actual screen area in any pathological layout.
    let lastSig = "";
    let lastVisible: boolean | null = null;
    const positionIframe = () => {
      if (!iframe || !container) return;

      // checkVisibility() (Chromium 105+, FF 125+) handles display:none,
      // visibility:hidden, and content-visibility uniformly.  Fallback
      // to offsetParent + getComputedStyle for older browsers.
      let containerVisible: boolean;
      const checkFn = (container as any).checkVisibility?.bind(container);
      if (typeof checkFn === "function") {
        containerVisible = checkFn({ checkVisibilityCSS: true, contentVisibilityAuto: true });
      } else {
        const cs = getComputedStyle(container);
        containerVisible =
          container.offsetParent !== null &&
          cs.visibility !== "hidden" &&
          cs.display !== "none";
      }

      const rect = container.getBoundingClientRect();
      const hasArea = rect.width > 0 && rect.height > 0;

      if (!containerVisible || !hasArea) {
        if (lastVisible !== false) {
          _DBG("positionIframe: container HIDDEN (visible=", containerVisible, "rect=", rect, ") → hiding iframe");
          lastVisible = false;
        }
        iframe.style.visibility = "hidden";
        iframe.style.pointerEvents = "none";
        return;
      }

      // Clamp to the viewport so the iframe never overlaps anything
      // outside the visible window — defensive against layouts where
      // a parent has overflow that pushes the rect partially off-screen.
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const clampedTop = Math.max(0, rect.top);
      const clampedLeft = Math.max(0, rect.left);
      const clampedWidth = Math.max(0, Math.min(rect.right, vw) - clampedLeft);
      const clampedHeight = Math.max(0, Math.min(rect.bottom, vh) - clampedTop);

      iframe.style.top = `${clampedTop}px`;
      iframe.style.left = `${clampedLeft}px`;
      iframe.style.width = `${clampedWidth}px`;
      iframe.style.height = `${clampedHeight}px`;
      iframe.style.visibility = "visible";
      // Restore interactivity if we previously parked the iframe in
      // pointer-events:none.  The dialog effect (separate useEffect)
      // owns the "behind dialog" override; we only flip back to "auto"
      // if no dialog is currently open.  We read it via a ref so this
      // function can stay idempotent across React renders.
      if (!_dialogOpenRef.current) {
        iframe.style.pointerEvents = "auto";
      }

      const sig = `${clampedTop},${clampedLeft},${clampedWidth},${clampedHeight}`;
      if (sig !== lastSig || lastVisible !== true) {
        _DBG("positionIframe: container VISIBLE → iframe rect=", sig);
        lastSig = sig;
        lastVisible = true;
      }
    };

    positionIframe();

    // Re-position on resize / scroll / layout shifts.
    const observer = new ResizeObserver(positionIframe);
    observer.observe(container);
    // Also re-position on viewport resize and on window scroll — split
    // view drag-resize emits scroll events on ancestor scrollers but
    // not always ResizeObserver entries.
    window.addEventListener("resize", positionIframe);
    window.addEventListener("scroll", positionIframe, true);
    const interval = setInterval(positionIframe, 500);

    return () => {
      _DBG("iframe effect CLEANUP: HIDING iframe (NOT removing from DOM)");
      observer.disconnect();
      window.removeEventListener("resize", positionIframe);
      window.removeEventListener("scroll", positionIframe, true);
      clearInterval(interval);
      if (iframe) {
        iframe.style.visibility = "hidden";
        iframe.style.pointerEvents = "none";
      }
    };
  }, [iframeUrl]);

  // ── Push iframe behind dialogs when they are open ─────────────────────
  //
  // The iframe lives on document.body at z-index 9999, so any same-tree
  // dialog (templateNameDialog) would otherwise stack below it.  The save
  // dialog is rendered in its own React root via the dialog host; its
  // open state is reported via onDialogStateChange.

  // Subscribe to the host's open/close events.
  useEffect(() => onDialogStateChange(setHostDialogOpen), []);

  const dialogOpen = hostDialogOpen || !!templateNameDialog;

  useEffect(() => {
    _dialogOpenRef.current = dialogOpen;
    const iframe = _module.persistedIframe;
    if (!iframe) return;
    if (dialogOpen) {
      iframe.style.zIndex = "-1";
      iframe.style.pointerEvents = "none";
    } else {
      iframe.style.zIndex = "9999";
      iframe.style.pointerEvents = "auto";
    }
  }, [dialogOpen]);

  const handleTemplateChange = useCallback(
    async (e: React.ChangeEvent<HTMLSelectElement>) => {
      const templateId = e.target.value;
      setSelectedTemplate(templateId);
      if (!templateId) return;

      const result = await client.loadTemplate(templateId, sampleFilename, currentFilepath);
      if (result?.workflow && iframeRef.current?.contentWindow) {
        iframeRef.current.contentWindow.postMessage(
          {
            type: MSG.LOAD_WORKFLOW,
            workflow: result.workflow,
          },
          "*"
        );
      }
    },
    [client, sampleFilename, currentFilepath]
  );

  const handleSaveConfig = useCallback(async () => {
    await client.updateConfig({
      comfyui_path: configPath,
      comfyui_port: parseInt(configPort, 10) || 8188,
    });
    setShowSettings(false);
    setServerStatus("starting");
    client.startServer().then(applyServerState);
  }, [client, configPath, configPort, applyServerState]);

  const handleRetry = useCallback(() => {
    setServerStatus("starting");
    client.startServer().then(applyServerState);
  }, [client, applyServerState]);

  const handleSaveAsTemplate = useCallback(() => {
    if (!iframeRef.current?.contentWindow) return;
    pendingSaveTemplateRef.current = true;
    iframeRef.current.contentWindow.postMessage(
      { type: MSG.GET_WORKFLOW },
      "*"
    );
  }, []);

  const handleSaveLastOutput = useCallback(() => {
    if (lastAvailableOutputs.length === 0) return;
    const out = lastAvailableOutputs[0];
    handleOutput({
      outputType: "image",
      nodeTitle: "Last Output",
      nodeId: out.nodeId ?? null,
      promptId: out.promptId,
      workflowName: "",
      filename: out.filename,
      subfolder: out.subfolder,
    });
    setLastAvailableOutputs([]);
  }, [lastAvailableOutputs, handleOutput]);

  // ── Auto-inject when the active modal slice changes ────────────────
  //
  // We deliberately do NOT gate on ``currentSampleId`` — when the modal
  // is opened via a ``groupId=…`` URL (no explicit ``id=``), React's
  // panel state often stays empty even though the modal IS showing a
  // sample.  Server-side ``inject_slice`` validates ``ctx.current_sample``
  // itself; if it can't resolve the sample, it returns a clean error
  // and we simply log it.  Better to attempt the inject than silently
  // skip and leave the LoadImage preview stale.
  const prevSliceRef = useRef<string | null>(null);
  useEffect(() => {
    if (!activeModalSlice) {
      _DBG("slice change effect: no activeModalSlice yet (Recoil state=", modalSliceLoadable.state, ") — skipping");
      return;
    }
    if (activeModalSlice === prevSliceRef.current) return;
    prevSliceRef.current = activeModalSlice;

    _DBG("slice tab changed: activeModalSlice=", activeModalSlice, "currentSampleId=", currentSampleId, "currentFilepath=", currentFilepath);

    client.injectSlice(activeModalSlice).then((result) => {
      _DBG("inject_slice result:", result);
      if (result?.error) {
        console.warn("[comfyui-plugin] inject_slice error:", result.error);
        return;
      }
      if (result?.sample_filename && _module.bridgeReady) {
        setSampleFilename(result.sample_filename);
        _DBG("slice tab: sending SAMPLE_CHANGED to iframe");
        iframeRef.current?.contentWindow?.postMessage(
          { type: MSG.SAMPLE_CHANGED },
          "*"
        );
      }
      // Different slices can have different label-field sets (a slice may
      // have annotations the original lacks, or vice versa).  Refresh so
      // the "Copy labels" picker stays accurate for the visible sample.
      refreshGroupSlices();
    }).catch((err) => {
      console.error("[comfyui-plugin] inject_slice call failed:", err);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeModalSlice, client, refreshGroupSlices]);

  const handleConfirmTemplateName = useCallback(() => {
    const name = templateNameInput.trim();
    if (!name || !templateNameDialog) return;
    const workflow = templateNameDialog.workflow;
    setTemplateNameDialog(null);
    client.saveTemplate(name, workflow).then((result) => {
      if (result.error) {
        console.error("[comfyui-plugin] save template error:", result.error);
      } else if (currentFilepath) {
        templatesExecutor.execute({ filepath: currentFilepath }).then((res: any) => {
          const r = res?.result || res;
          setTemplates(r?.templates || []);
        });
      }
    });
  }, [templateNameInput, templateNameDialog, client, currentFilepath, templatesExecutor]);

  // ── Render ───────────────────────────────────────────────────────────

  const statusLabel = useMemo(() => {
    switch (serverStatus) {
      case "ready":
        return "Ready";
      case "starting":
        return "Starting...";
      case "not_running":
        return "Not running";
      case "not_found":
        return "Not found";
      case "error":
        return "Error";
      case "timeout":
        return "Timeout";
      case "stopped":
        return "Stopped";
      default:
        return "Connecting...";
    }
  }, [serverStatus]);

  if (showSettings) {
    return (
      <div style={STYLES.container}>
        <div style={STYLES.toolbar}>
          <span style={{ fontSize: FONT.md, fontWeight: 600 }}>Settings</span>
          <div style={{ flex: 1 }} />
          <button
            style={STYLES.button}
            onClick={() => setShowSettings(false)}
          >
            Cancel
          </button>
          <button
            style={{ ...STYLES.button, backgroundColor: COLORS.accent, color: COLORS.bg }}
            onClick={handleSaveConfig}
          >
            Save & Restart
          </button>
        </div>
        <div style={STYLES.configPanel}>
          <div style={STYLES.inputGroup}>
            <label style={STYLES.label}>ComfyUI Path</label>
            <input
              style={STYLES.input}
              value={configPath}
              onChange={(e) => setConfigPath(e.target.value)}
              placeholder="~/comfy/ComfyUI"
            />
          </div>
          <div style={STYLES.inputGroup}>
            <label style={STYLES.label}>Port</label>
            <input
              style={{ ...STYLES.input, width: "80px" }}
              value={configPort}
              onChange={(e) => setConfigPort(e.target.value)}
              placeholder="8188"
            />
          </div>
        </div>
      </div>
    );
  }

  const showIframe = serverStatus === "ready" && iframeUrl;

  // All notice banners use the same yellow styling and dismiss button.
  // Each banner is gated by a (state, setter, _module-mirror-key) trio so
  // the displayed/dismissed state survives panel unmount/remount.
  const banners: {
    key: string;
    visible: boolean;
    title: string;
    body: string;
    onDismiss: () => void;
  }[] = [
    {
      key: "flat-grouped",
      visible: showFlatGroupedBanner,
      title: "Dataset converted to grouped.",
      body:
        "Once the job finishes: refresh the browser, close the sample " +
        "modal, then reopen it to see the new slice(s).",
      onDismiss: () => {
        _module.flatGroupedNotice = false;
        setShowFlatGroupedBanner(false);
      },
    },
    {
      key: "depth-saved",
      visible: showDepthSavedBanner,
      title: "Depth map saved.",
      body: "Refresh the browser to see the heatmap on the sample.",
      onDismiss: () => {
        _module.depthSavedNotice = false;
        setShowDepthSavedBanner(false);
      },
    },
    {
      key: "mask-saved",
      visible: showMaskSavedBanner,
      title: "Detections / segmentation saved.",
      body: "Refresh the browser to see the masks on the sample.",
      onDismiss: () => {
        _module.maskSavedNotice = false;
        setShowMaskSavedBanner(false);
      },
    },
  ];

  return (
    <div style={STYLES.container}>
      {banners
        .filter((b) => b.visible)
        .map((b) => (
          <div key={b.key} style={STYLES.noticeBanner}>
            <strong>{b.title}</strong>
            <span style={{ flex: 1 }}>{b.body}</span>
            <button
              onClick={b.onDismiss}
              style={STYLES.noticeDismiss}
              aria-label="Dismiss notice"
            >
              Dismiss
            </button>
          </div>
        ))}

      {/* Save dialog is rendered in a separate React root via dialogHost.tsx */}

      {/* Template name dialog */}
      {templateNameDialog && (
        <div
          style={OVERLAY_BASE}
          onClick={() => setTemplateNameDialog(null)}
        >
          <div
            style={{ ...DIALOG_CARD_BASE, width: "320px", gap: "12px" }}
            onClick={(e) => e.stopPropagation()}
          >
            <span style={{ fontSize: FONT.lg, fontWeight: 600, color: COLORS.text }}>
              Save Template
            </span>
            <input
              style={{ ...INPUT_BASE, backgroundColor: COLORS.bg }}
              value={templateNameInput}
              onChange={(e) => setTemplateNameInput(e.target.value)}
              placeholder="Template name"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === "Enter") handleConfirmTemplateName();
                if (e.key === "Escape") setTemplateNameDialog(null);
                e.stopPropagation();
              }}
            />
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
              <button style={STYLES.button} onClick={() => setTemplateNameDialog(null)}>
                Cancel
              </button>
              <button
                style={{ ...STYLES.button, backgroundColor: COLORS.accent, color: COLORS.bg }}
                onClick={handleConfirmTemplateName}
              >
                Save
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Toolbar */}
      <div style={STYLES.toolbar}>
        <div style={STYLES.statusDot(serverStatus)} />
        <span style={STYLES.statusText}>{statusLabel}</span>

        {templates.length > 0 && (
          <select
            style={STYLES.select}
            value={selectedTemplate}
            onChange={handleTemplateChange}
          >
            <option value="">Load template...</option>
            {templates.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        )}

        <div style={{ flex: 1 }} />

        {saving && (
          <span style={{ fontSize: FONT.sm, color: COLORS.accent }}>Saving...</span>
        )}

        {lastAvailableOutputs.length > 0 && !saving && (
          <button
            style={{ ...STYLES.button, backgroundColor: COLORS.accent, color: COLORS.bg }}
            onClick={handleSaveLastOutput}
          >
            Save to FiftyOne
          </button>
        )}

        {serverStatus === "ready" && (
          <button
            style={{ ...STYLES.button, ...STYLES.buttonDanger }}
            onClick={() => client.stopServer().then(applyServerState)}
          >
            Stop
          </button>
        )}

        {(serverStatus === "not_running" ||
          serverStatus === "stopped" ||
          serverStatus === "error" ||
          serverStatus === "timeout") && (
          <button style={STYLES.button} onClick={handleRetry}>
            Start Server
          </button>
        )}

        {serverStatus === "ready" && (
          <button
            style={STYLES.button}
            onClick={handleSaveAsTemplate}
            title="Save current workflow as a reusable template"
          >
            Save Template
          </button>
        )}

        <button
          style={STYLES.button}
          onClick={() => setShowSettings(true)}
          title="Settings"
        >
          Settings
        </button>
      </div>

      {/* Main content */}
      {showIframe ? (
        <div style={STYLES.iframeWrapper}>
          <div ref={iframeContainerRef} style={{ position: "absolute", inset: 0, pointerEvents: "none" }} />
        </div>
      ) : (
        <div style={STYLES.centerMessage}>
          {serverStatus === "starting" || serverStatus === "" ? (
            <>
              <div style={STYLES.spinner} />
              <div style={STYLES.messageTitle}>Starting ComfyUI...</div>
              <div style={STYLES.messageBody}>
                This may take a minute while models are loaded.
              </div>
            </>
          ) : serverStatus === "not_found" ? (
            <>
              <div style={STYLES.messageTitle}>ComfyUI Not Found</div>
              <div style={STYLES.messageBody}>
                {serverError ||
                  "Could not find ComfyUI installation. Click Settings to configure the path."}
              </div>
              <button style={STYLES.button} onClick={() => setShowSettings(true)}>
                Configure Path
              </button>
            </>
          ) : serverStatus === "error" || serverStatus === "timeout" ? (
            <>
              <div style={STYLES.messageTitle}>Connection Error</div>
              <div style={STYLES.messageBody}>
                {serverError || "Could not connect to ComfyUI server."}
              </div>
              <div style={{ display: "flex", gap: "8px" }}>
                <button style={STYLES.button} onClick={handleRetry}>
                  Retry
                </button>
                <button style={STYLES.button} onClick={() => setShowSettings(true)}>
                  Settings
                </button>
              </div>
            </>
          ) : (
            <>
              <div style={STYLES.messageTitle}>ComfyUI Server</div>
              <div style={STYLES.messageBody}>
                Server is not running. Click below to start it, or open
                Settings to configure the path.
              </div>
              <div style={{ display: "flex", gap: "8px" }}>
                <button
                  style={{
                    ...STYLES.button,
                    backgroundColor: COLORS.accent,
                    color: COLORS.bg,
                  }}
                  onClick={handleRetry}
                >
                  Start Server
                </button>
                <button style={STYLES.button} onClick={() => setShowSettings(true)}>
                  Settings
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
};

export default ComfyUIPanel;
