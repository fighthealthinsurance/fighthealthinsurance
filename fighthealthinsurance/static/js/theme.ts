/**
 * Shared theme constants for Fight Health Insurance UI components.
 * Used by chat_interface.tsx and other React components.
 */

export const THEME = {
  colors: {
    background: "#f4f6fb",
    buttonBackground: "#e6f4c2",
    buttonText: "#5a6b1b",
    primary: "#a5c422",
    primaryHover: "#8fb01e",
    white: "#ffffff",
  },
  spacing: {
    headerMargin: 16,
    xs: 4,
    sm: 8,
    md: 16,
    lg: 24,
    xl: 32,
  },
  borderRadius: {
    small: 3,
    medium: "xl",
    large: 24,
    extraLarge: "xl",
    buttonDefault: "7px",
    card: 12,
    input: 10,
    container: 24,
  },
  buttonSharedStyles: {
    background: "#a5c422",
    color: "#ffffff",
    border: "none",
    boxShadow: "none",
    transition: "background 0.2s",
  },
  shadows: {
    card: "0 4px 32px rgba(0,0,0,0.07)",
  },
} as const;

// Type for the theme object
export type ThemeType = typeof THEME;
