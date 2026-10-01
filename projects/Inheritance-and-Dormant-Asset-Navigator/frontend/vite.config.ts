import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({ plugins: [react()], server: { port: 5187, strictPort: true, proxy: { '/v1': 'http://127.0.0.1:8731', '/docs': 'http://127.0.0.1:8731' } } });
