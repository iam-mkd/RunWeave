import { defineConfig } from 'vite';

// The design prototype builds separately so it never ships in the workspace bundle.
export default defineConfig(({ mode }) => ({
  base: mode === 'prototype' ? './' : '/',
  build: mode === 'prototype'
    ? { outDir: 'dist-prototype', rollupOptions: { input: { prototype: 'prototype.html' } } }
    : {},
  server: {
    proxy: {
      '/v1': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
  test: {
    include: ['test/**/*.test.{js,jsx}'],
    environment: 'node',
  },
}));
