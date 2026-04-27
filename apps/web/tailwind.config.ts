import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        arena: {
          ink: "#1f2933",
          muted: "#60758a",
          paper: "#fbfcfd",
          field: "#ecefe8",
          blue: "#1f6feb",
          green: "#10a37f",
        },
      },
      boxShadow: {
        panel: "0 8px 18px rgba(31, 41, 51, 0.05)",
      },
    },
  },
  plugins: [],
} satisfies Config;
