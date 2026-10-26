import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';
import bundledModules from './scripts/bundled-modules.mjs';

// Minification drops license comments; this one points to the notices file
// that `npm run build` writes into dist/.
const banner = '/*! Bundled third-party code keeps its own licenses: see Third-Party-Notices.txt at the site or app root. */';
// Emit fonts as files, never data: URIs, so the notices check can attribute them.
const keepFontFiles = (file) => (/\.(?:woff2?|ttf|otf|eot)$/i.test(file) ? false : undefined);

export default defineConfig({
  plugins: [svelte(), bundledModules()],
  // From Vite 7.2 the esbuild minifier drops /*! comments unless this is set.
  esbuild: { legalComments: 'inline' },
  build: {
    assetsInlineLimit: keepFontFiles,
    rollupOptions: { output: { banner } }
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/healthz': 'http://127.0.0.1:8080'
    }
  }
});
