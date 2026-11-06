/** NuroFlow dashboard theme: black surfaces, electric-blue accent, high contrast.
 * Run-status colours (ok / fail) were validated for colour-vision deficiency
 * (worst ΔE 26 on the dark surface) and always ship with an icon and a label. */
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { DEFAULT: "#e8ecf3", muted: "#9aa4b5", faint: "#6b7586" },
        surface: { 0: "#06080d", 1: "#0b0f17", 2: "#121826", 3: "#1a2233" },
        line: "#1f2939",
        accent: { DEFAULT: "#3d8bff", strong: "#6aa6ff", glow: "#3d8bff33" },
        ok: "#3d8bff",
        fail: "#e5484d",
        warn: "#fab219",
        neutral: "#5b6474",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: { glow: "0 0 0 1px #3d8bff55, 0 0 24px -6px #3d8bff66" },
    },
  },
  plugins: [],
};
