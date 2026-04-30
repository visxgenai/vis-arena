import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        arena: {
          bg: "#0d1117",
          surface: "#161b22",
          card: "#1c2128",
          border: "#30363d",
          ink: "#e6edf3",
          muted: "#8b949e",
          orange: "#e8a838",
          teal: "#30c878",
          blue: "#4a9eea",
          red: "#e06040",
          pink: "#d46090",
          cyan: "#6aacda",
          yellow: "#c8a828",
          green: "#10a37f",
          paper: "#fbfcfd",
          field: "#0d1117",
        },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "Fira Code", "monospace"],
        display: ["Playfair Display", "Georgia", "serif"],
      },
      boxShadow: {
        panel: "0 8px 18px rgba(0, 0, 0, 0.25)",
        glow: "0 0 20px rgba(232, 168, 56, 0.15)",
      },
      animation: {
        "fade-in": "fadeIn 0.5s ease-out",
        "slide-up": "slideUp 0.6s ease-out",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideUp: {
          "0%": { opacity: "0", transform: "translateY(20px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
