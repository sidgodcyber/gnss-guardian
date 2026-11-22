import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import fs from 'fs';
import path from 'path';
import type { IncomingMessage, ServerResponse } from 'http';
import { defineConfig, type Plugin } from 'vite';

// Session folders written by `gnss-guardian replay` (default: ../session). Read-only, local only.
const SESSIONS_DIR = path.resolve(process.env.GG_SESSIONS ?? path.resolve(import.meta.dirname, '../session'));
const SERVED = new Set(['dashboard.json', 'dashboard_video.webm']);
const NAME = /^(?!\.\.?$)[A-Za-z0-9._-]+$/;

function listSessions(): string[] {
  if (!fs.existsSync(SESSIONS_DIR)) return [];
  return fs
    .readdirSync(SESSIONS_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory() && NAME.test(d.name) && fs.existsSync(path.join(SESSIONS_DIR, d.name, 'dashboard.json')))
    .map((d) => d.name)
    .sort();
}

function sendFile(req: IncomingMessage, res: ServerResponse, file: string) {
  const size = fs.statSync(file).size;
  const type = file.endsWith('.json') ? 'application/json' : 'video/webm';
  const range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? '');
  res.setHeader('Accept-Ranges', 'bytes');
  res.setHeader('Content-Type', type);
  res.setHeader('Cache-Control', 'no-cache');
  if (range && (range[1] || range[2])) {
    const start = range[1] ? Number(range[1]) : Math.max(0, size - Number(range[2]));
    const end = range[1] && range[2] ? Math.min(Number(range[2]), size - 1) : size - 1;
    if (start >= size || start > end) {
      res.statusCode = 416;
      res.setHeader('Content-Range', `bytes */${size}`);
      res.end();
      return;
    }
    res.statusCode = 206;
    res.setHeader('Content-Range', `bytes ${start}-${end}/${size}`);
    res.setHeader('Content-Length', String(end - start + 1));
    fs.createReadStream(file, { start, end }).pipe(res);
    return;
  }
  res.setHeader('Content-Length', String(size));
  fs.createReadStream(file).pipe(res);
}

function sessionFolders(): Plugin {
  const handler = (req: IncomingMessage, res: ServerResponse, next: () => void) => {
    const url = (req.url ?? '').split('?')[0];
    if (!url.startsWith('/sessions/')) return next();
    if (url === '/sessions/index.json') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ dir: SESSIONS_DIR, sessions: listSessions() }));
      return;
    }
    const [name, file, ...rest] = url.slice('/sessions/'.length).split('/').map(decodeURIComponent);
    const target = name && file ? path.join(SESSIONS_DIR, name, file) : '';
    if (rest.length || !NAME.test(name ?? '') || !SERVED.has(file ?? '') || !fs.existsSync(target)) {
      res.statusCode = 404;
      res.end('not found');
      return;
    }
    sendFile(req, res, target);
  };
  return {
    name: 'gnss-guardian-sessions',
    configureServer: (server) => void server.middlewares.use(handler),
    configurePreviewServer: (server) => void server.middlewares.use(handler),
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), sessionFolders()],
  server: { host: '127.0.0.1', port: 3000 },
  preview: { host: '127.0.0.1', port: 3000 },
});
