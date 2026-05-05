export const COLORS = {
  bg: "#1a1a2e",
  bgLight: "#16213e",
  bgDialog: "#1e1e2e",
  bgDark: "#12121e",
  border: "#0f3460",
  borderDialog: "#3a3a5c",
  text: "#e0e0e0",
  textMuted: "#a0a0b0",
  textDim: "#808098",
  accent: "#4ecca3",
  danger: "#7b2d26",
  warning: "#f0a500",
  error: "#e74c3c",
  noticeYellow: "#FFEB52",
} as const;

export const FONT = {
  family:
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  xs: "11px",
  sm: "12px",
  md: "13px",
  lg: "15px",
  xl: "16px",
} as const;

export const INPUT_BASE = {
  backgroundColor: COLORS.bgLight,
  color: COLORS.text,
  border: `1px solid ${COLORS.border}`,
  borderRadius: "4px",
  padding: "6px 8px",
  fontSize: FONT.md,
} as const;

export const BUTTON_BASE = {
  backgroundColor: COLORS.border,
  color: COLORS.text,
  border: "none",
  borderRadius: "4px",
  padding: "4px 10px",
  fontSize: FONT.sm,
  cursor: "pointer",
  whiteSpace: "nowrap" as const,
} as const;

export const LABEL_BASE = {
  fontSize: FONT.xs,
  color: COLORS.textMuted,
  textTransform: "uppercase" as const,
  letterSpacing: "0.5px",
} as const;

export const OVERLAY_BASE = {
  position: "fixed" as const,
  inset: 0,
  backgroundColor: "rgba(0, 0, 0, 0.6)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 10000,
} as const;

export const DIALOG_CARD_BASE = {
  backgroundColor: COLORS.bgDialog,
  border: `1px solid ${COLORS.borderDialog}`,
  borderRadius: "8px",
  padding: "20px",
  display: "flex",
  flexDirection: "column" as const,
  gap: "16px",
  boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
} as const;
