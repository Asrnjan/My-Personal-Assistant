#!/usr/bin/env node
// Tiny zero-dependency static server for local development and Playwright.
// Usage: node scripts/serve.js [--port 4173] [--host 127.0.0.1]
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const args = process.argv.slice(2);
const argValue = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
};
const PORT = Number(argValue('--port') || process.env.PORT || 4173);
const HOST = argValue('--host') || process.env.HOST || '127.0.0.1';

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};

// Only app files are served; tooling folders stay private.
const BLOCKED = new Set(['node_modules', 'tests', 'scripts', 'test-results', 'playwright-report']);

function send(res, status, body, type) {
  res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff' });
  res.end(body);
}

const server = http.createServer((req, res) => {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    send(res, 405, JSON.stringify({ detail: 'Method not allowed' }), TYPES['.json']);
    return;
  }
  let pathname;
  try {
    pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  } catch {
    send(res, 400, 'Bad request', TYPES['.txt']);
    return;
  }
  if (pathname === '/') pathname = '/index.html';
  const filePath = path.resolve(ROOT, '.' + pathname);
  const rel = path.relative(ROOT, filePath);
  const top = rel.split(path.sep)[0];
  if (rel.startsWith('..') || path.isAbsolute(rel) || BLOCKED.has(top) || top.startsWith('.')) {
    send(res, 404, JSON.stringify({ detail: 'Not found' }), TYPES['.json']);
    return;
  }
  fs.stat(filePath, (err, stat) => {
    if (err || !stat.isFile()) {
      send(res, 404, JSON.stringify({ detail: 'Not found' }), TYPES['.json']);
      return;
    }
    const type = TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
    res.writeHead(200, { 'Content-Type': type, 'Content-Length': stat.size, 'Cache-Control': 'no-cache', 'X-Content-Type-Options': 'nosniff' });
    if (req.method === 'HEAD') {
      res.end();
      return;
    }
    fs.createReadStream(filePath).pipe(res);
  });
});

server.listen(PORT, HOST, () => {
  process.stdout.write(`PWA dev server: http://${HOST}:${PORT}/\n`);
});

for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => server.close(() => process.exit(0)));
