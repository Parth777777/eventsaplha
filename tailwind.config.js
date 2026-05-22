/** @type {import('tailwindcss').Config} */
// AlphaEvent / Tickwave — Tailwind CLI build config.
//
// Replaces the runtime CDN (https://cdn.tailwindcss.com) which adds ~3 MB
// to every page load. After the first build, every HTML page should:
//   1. REMOVE  <script src="https://cdn.tailwindcss.com..."></script>
//   2. REMOVE  the inline tailwind.config = {...} block
//   3. ADD     <link rel="stylesheet" href="./shared/css/dist/tw.min.css">
//
// Build commands (add to package.json — already provisioned):
//   npm install -D tailwindcss @tailwindcss/forms @tailwindcss/container-queries
//   npx tailwindcss -i ./app/shared/css/tw-input.css -o ./app/shared/css/dist/tw.min.css --minify
//   # watch mode for dev:
//   npx tailwindcss -i ./app/shared/css/tw-input.css -o ./app/shared/css/dist/tw.min.css --watch
//
// The color palette below mirrors the inline config in app/index.html so
// the build output is a drop-in replacement.

module.exports = {
  darkMode: 'class',
  content: [
    './app/**/*.html',
    './app/shared/js/**/*.js',
  ],
  theme: {
    extend: {
      colors: {
        // Brand
        primary: '#8eb4e0',
        'primary-container': '#3a6ea8',
        secondary: '#2dd4aa',
        tertiary: '#f29090',
        error: '#f26b6b',

        // Dark theme surfaces — must mirror tokens.css / redesign.css
        background: '#080c12',
        surface: '#0d1118',
        'surface-container-lowest': '#060a0f',
        'surface-container-low': '#0d1118',
        'surface-container': '#111720',
        'surface-container-high': '#161d28',
        'surface-container-highest': '#1c2435',
        'surface-bright': '#222c3d',

        // Text + outlines
        'on-surface': '#dde3ef',
        'on-surface-variant': '#8a94a8',
        outline: '#3d4a5c',
        'outline-variant': '#253040',

        // Semantic
        bull: '#2dd4aa',
        bear: '#f26b6b',
        caution: '#e6b84a',
        info: '#6ba3d6',
      },
      fontFamily: {
        headline: ['Plus Jakarta Sans', 'system-ui', 'sans-serif'],
        body: ['DM Sans', 'system-ui', 'sans-serif'],
        mono: ['Geist Mono', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/container-queries'),
  ],
};
