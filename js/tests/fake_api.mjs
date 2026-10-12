// A tiny stand-in for the Busy Meter plugin API (node:http on 127.0.0.1, random port), for client tests. Same as the
// Python tests' fake_api.py: it applies the app's transport rules (Origin -> 403, Host must be 127.0.0.1:<port>, no
// query strings, JSON bodies only), records every request, and also answers DELETE /v1/register/{id} (withdraw).

import { randomBytes } from 'node:crypto';
import http from 'node:http';

const error = (code, message) => ({ error: code, message });

export class FakeApi {
  constructor() {
    this.requests = [];
    this.registrations = new Map();
    this.tokens = new Map();
    this.answer = null; // null = leave pending; true/false = answer on the next poll
    this.expire = false; // answer denied/expired on the next poll
    this.forced = new Map(); // "METHOD /path" -> [status, body] once
    this.raw = null; // (req, res) => void: answer the next request by hand (garbage, silence, ...)
    this.statusBody = { level: 4, paused: false, source: 'teams' };
    this.active = new Map();
    this.sockets = new Set();
    this.server = http.createServer((req, res) => this.handle(req, res));
    this.server.on('connection', (s) => {
      this.sockets.add(s);
      s.on('close', () => this.sockets.delete(s));
    });
  }

  async start() {
    await new Promise((resolve) => this.server.listen(0, '127.0.0.1', resolve));
    this.port = this.server.address().port;
    return this;
  }

  async stop() {
    for (const s of this.sockets) s.destroy();
    await new Promise((resolve) => this.server.close(resolve));
  }

  approveToken(pluginId, wants = null) {
    const token = randomBytes(32).toString('base64url');
    this.tokens.set(token, { id: pluginId, wants: wants || ['signals', 'status'], enabled: true });
    return token;
  }

  pending(id) {
    return [...this.registrations.entries()].find(([, r]) => r.id === id && r.state === 'pending');
  }

  handle(req, res) {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks);
      const record = { method: req.method, path: req.url, headers: { ...req.headers }, rawHeaders: [...req.rawHeaders], body: raw };
      this.requests.push(record);
      if (this.raw) {
        const fn = this.raw;
        this.raw = null;
        return fn(req, res);
      }
      this.route(req, res, raw);
    });
  }

  send(res, status, body) {
    const data = Buffer.from(JSON.stringify(body));
    const headers = { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': data.length };
    if (status === 429) headers['Retry-After'] = '1';
    res.writeHead(status, headers);
    res.end(data);
  }

  route(req, res, raw) {
    const method = req.method;
    const path = req.url;
    const key = `${method} ${path.split('?')[0]}`;
    if (req.headers.origin !== undefined) return this.send(res, 403, error('forbidden_origin', "Requests from web pages aren't accepted."));
    if (req.headers.host !== `127.0.0.1:${this.port}`) return this.send(res, 400, error('bad_host', 'Host must be 127.0.0.1:port.'));
    if (path.includes('?')) return this.send(res, 400, error('bad_request', 'No query strings.'));
    if (this.forced.has(key)) {
      const [status, body] = this.forced.get(key);
      this.forced.delete(key);
      return this.send(res, status, body);
    }
    let body = null;
    if (method === 'POST' || method === 'PUT') {
      if ((req.headers['content-type'] || '').split(';')[0].trim().toLowerCase() !== 'application/json') {
        return this.send(res, 415, error('unsupported_media_type', 'Content-Type must be application/json.'));
      }
      try {
        body = JSON.parse(raw.toString('utf8'));
      } catch {
        return this.send(res, 400, error('bad_json', 'Not valid JSON.'));
      }
    }

    if (path === '/v1/register' && method === 'POST') {
      if (this.pending(body.id)) return this.send(res, 409, error('already_pending', 'A request for this plugin id is already waiting.'));
      const requestId = randomBytes(16).toString('base64url');
      this.registrations.set(requestId, { id: body.id, state: 'pending', body });
      return this.send(res, 202, { requestId, state: 'pending', expiresInSeconds: 120 });
    }
    if (path.startsWith('/v1/register/') && method === 'GET') {
      const id = path.slice('/v1/register/'.length);
      const request = this.registrations.get(id);
      if (!request) return this.send(res, 404, error('not_found', 'No such request.'));
      this.decide(request);
      if (request.state === 'approved') {
        this.registrations.delete(id);
        const token = this.approveToken(request.id, request.body.wants);
        return this.send(res, 200, { state: 'approved', pluginId: request.id, token });
      }
      if (request.state === 'denied') return this.send(res, 200, { state: 'denied', reason: request.reason });
      return this.send(res, 200, { state: 'pending' });
    }
    if (path.startsWith('/v1/register/') && method === 'DELETE') {
      const id = path.slice('/v1/register/'.length);
      const request = this.registrations.get(id);
      if (!request) return this.send(res, 404, error('not_found', 'No such request.'));
      this.decide(request);
      if (request.state === 'pending') {
        this.registrations.delete(id);
        return this.send(res, 200, { state: 'withdrawn' });
      }
      return this.send(res, 409, { error: 'already_answered', state: request.state, message: 'The user already answered this request.' });
    }

    if (!['/v1/signals', '/v1/status', '/v1/alert'].includes(path)) return this.send(res, 404, error('not_found', 'No such endpoint.'));
    const auth = req.headers.authorization || '';
    const plugin = auth.startsWith('Bearer ') ? this.tokens.get(auth.slice('Bearer '.length)) : undefined;
    if (!plugin) return this.send(res, 401, error('unauthorized', 'Missing or unknown token.'));
    if (!plugin.enabled) return this.send(res, 403, error('plugin_off', 'This plugin is switched off in Busy Meter.'));
    if (path === '/v1/status' && method === 'GET') return this.send(res, 200, { ...this.statusBody });
    if (path === '/v1/signals' && method === 'PUT') {
      const ttl = body.ttlSeconds;
      if (!Number.isInteger(ttl) || ttl < 1 || ttl > 600) return this.send(res, 400, error('bad_ttl', 'ttlSeconds must be 1-600.'));
      const active = 'level' in body ? [`level-${body.level}`] : [...(body.active || [])];
      this.active.set(plugin.id, active);
      return this.send(res, 200, { active, expiresInSeconds: ttl });
    }
    if (path === '/v1/signals' && method === 'DELETE') {
      this.active.set(plugin.id, []);
      return this.send(res, 200, { active: [], expiresInSeconds: 0 });
    }
    if (path === '/v1/alert' && method === 'POST') return this.send(res, 200, { shown: true });
    return this.send(res, 405, error('method_not_allowed', 'Method not allowed.'));
  }

  decide(request) {
    if (request.state !== 'pending') return;
    if (this.expire) {
      request.state = 'denied';
      request.reason = 'expired';
    } else if (this.answer !== null) {
      request.state = this.answer ? 'approved' : 'denied';
      request.reason = 'denied';
    }
  }
}
