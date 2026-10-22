// Shared fixtures for the PWA tests: API mocks, fake speech engines, sample data.
// Every backend call is mocked with page.route; no real server is ever contacted.
'use strict';

const TOKEN = 'test-token-123';

const API_PREFIXES = ['/auth/', '/brief/', '/converse', '/actions', '/followups', '/stt', '/tts', '/push/', '/health', '/accounts', '/connections'];

const SAMPLE_BRIEF = {
  kind: 'morning',
  for_date: '2026-10-16',
  text: 'Good morning. Three things need you today: the board deck, the vendor review and a clash at 4 pm.',
  details: [
    'The board deck is due at noon and only you can sign off the numbers.',
    'Rahul can take the vendor review; he did the last one.',
  ],
  items: [
    {
      id: 'it-1', title: 'Finalize board deck', kind: 'task', band: 'P1_do_myself', score: 92,
      reason: 'Board meeting tomorrow; only you can approve the numbers.', start: null, due: '2026-10-16T12:00:00Z',
      url: 'https://example.com/deck', suggested: 'do', delegate_id: null, delegate_name: null,
    },
    {
      id: 'it-2', title: 'Vendor contract review', kind: 'email_action', band: 'P2_delegate', score: 71,
      reason: 'Routine review that Rahul handled last quarter.', start: null, due: null, url: null,
      suggested: 'delegate', delegate_id: 'd-rahul', delegate_name: 'Rahul Mehta',
    },
    {
      id: 'it-3', title: 'Agency pitch meeting', kind: 'meeting', band: 'P3_decline', score: 35,
      reason: 'Clashes with the investor call and adds little value.', start: '2026-10-16T16:00:00Z', due: null, url: null,
      suggested: 'decline', delegate_id: null, delegate_name: null,
    },
    {
      id: 'it-4', title: 'Newsletter draft feedback', kind: 'task', band: 'P4_drop', score: 8,
      reason: 'Low impact; the team already shipped it.', start: null, due: null, url: null,
      suggested: 'drop', delegate_id: null, delegate_name: null,
    },
  ],
  delegations: [],
  word_count: 42,
  estimated_seconds: 95,
  generated_at: '2026-10-16T06:30:00Z',
};

const SAMPLE_PENDING = [
  {
    id: 'act-1', kind: 'delegate', item_id: 'it-2', title: 'Delegate vendor review to Rahul',
    readback: 'I will email Rahul the vendor review, due Monday. Confirm?', status: 'pending', created_at: '2026-10-16T07:00:00Z',
  },
  {
    id: 'act-2', kind: 'decline_meeting', item_id: 'it-3', title: 'Decline agency pitch',
    readback: 'I will decline the 4 pm agency pitch with a polite note. Confirm?', status: 'pending', created_at: '2026-10-16T07:01:00Z',
  },
];

const SAMPLE_FOLLOWUPS = [
  {
    id: 'fu-1', item_id: 'it-9', title: 'Q3 hiring plan', delegate_id: 'd-priya', delegate_name: 'Priya',
    state: 'acknowledged', due: '2026-10-18T17:00:00Z', acknowledged_at: '2026-10-15T09:00:00Z', nudges_sent: 0,
  },
  {
    id: 'fu-2', item_id: 'it-10', title: 'Security audit follow-up', delegate_id: 'd-sam', delegate_name: 'Sam',
    state: 'overdue', due: '2026-10-14T17:00:00Z', acknowledged_at: null, nudges_sent: 2,
  },
];

function converseReply(text, extra = {}) {
  return {
    conversation_id: 'conv-1',
    reply: text,
    pending_actions: [],
    executed: [],
    needs_confirmation: false,
    ...extra,
  };
}

/** The /connections response. `connected` lists app ids. */
function connectionsBody({ connected = [] } = {}) {
  const apps = [
    ['claude', 'Claude', 'brain', [['api_key', 'API key', true, false]]],
    ['jira', 'Jira', 'tasks', [['base_url', 'Your Jira address', false, false], ['email', 'Your Atlassian email', false, false], ['api_token', 'API token', true, false]]],
    ['asana', 'Asana', 'tasks', [['token', 'Personal access token', true, false]]],
    ['telegram', 'Telegram', 'chat', [['bot_token', 'Bot token', true, false], ['chat_id', 'Your chat id', false, true]]],
  ];
  return {
    ready: true,
    apps: apps.map(([id, name, group, fields]) => ({
      id,
      name,
      group,
      description: `${name} description`,
      steps: [`Open ${name}.`, 'Paste it here.'],
      link: `https://example.com/${id}`,
      link_label: 'Get a key',
      connected: connected.includes(id),
      source: connected.includes(id) ? 'app' : null,
      fields: fields.map(([key, label, secret, optional]) => ({
        key, label, secret, optional, placeholder: '', help: '', value: '', filled: connected.includes(id),
      })),
    })),
  };
}

/** The /accounts response. `connected` and `configured` list account and provider ids. */
function accountsBody({ connected = [], configured = [], redirect = 'http://127.0.0.1:4173' } = {}) {
  const kinds = [
    ['google', 'Google', 'Gmail and Google Calendar', 'google'],
    ['outlook', 'Outlook', 'Outlook email and calendar', 'microsoft'],
    ['teams', 'Microsoft Teams', 'Read and send your Teams chats (work or school account)', 'microsoft'],
  ];
  return {
    oauth_ready: true,
    accounts: kinds.map(([id, name, description, provider]) => ({
      id, name, description, provider, connected: connected.includes(id), ready: configured.includes(provider),
    })),
    providers: ['google', 'microsoft'].map((id) => ({
      id,
      name: id === 'google' ? 'Google' : 'Microsoft',
      configured: configured.includes(id),
      keys_source: configured.includes(id) ? 'app' : null,
      redirect_uri: `${redirect}/oauth/${id}/callback`,
      tenant_id: null,
    })),
  };
}

function isApiUrl(url) {
  return API_PREFIXES.some((p) => url.pathname === p.replace(/\/$/, '') || url.pathname.startsWith(p));
}

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type',
  'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
};

/**
 * Mocks every backend endpoint. `overrides` maps "METHOD /path" to a function
 * (call) => ({ status, json } | { status, body, contentType }) or to a plain JSON value.
 * Returns the list of recorded calls.
 */
async function mockApi(page, overrides = {}) {
  const calls = [];
  const defaults = {
    'POST /auth/token': () => ({ json: { access_token: TOKEN, token_type: 'bearer', client_name: 'pwa' } }),
    'GET /brief/today': () => ({ json: SAMPLE_BRIEF }),
    'POST /converse': () => ({ json: converseReply('Okay.') }),
    'GET /actions': () => ({ json: [] }),
    'GET /followups': () => ({ json: [] }),
    'POST /stt': () => ({ json: { text: '' } }),
    'GET /tts': () => ({ status: 503, json: { detail: 'Voice engines are not installed' } }),
    'GET /push/vapid-public-key': () => ({ json: { key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U' } }),
    'POST /push/subscribe': () => ({ status: 201, json: { ok: true } }),
    'GET /health': () => ({ json: { status: 'ok' } }),
    'GET /accounts': () => ({ json: accountsBody() }),
    'GET /connections': () => ({ json: connectionsBody() }),
  };

  await page.route(/https:\/\/fonts\.(googleapis|gstatic)\.com\//, (route) => route.abort());

  await page.route((url) => isApiUrl(url), async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === 'OPTIONS') {
      await route.fulfill({ status: 204, headers: CORS });
      return;
    }
    let body = null;
    const raw = req.postData();
    if (raw) {
      try {
        body = JSON.parse(raw);
      } catch {
        body = raw;
      }
    }
    const call = {
      method: req.method(),
      path: url.pathname,
      search: url.search,
      host: url.host,
      headers: req.headers(),
      body,
      raw: req.postDataBuffer(),
    };
    calls.push(call);

    const exact = `${call.method} ${call.path}`;
    let handler = overrides[exact] ?? defaults[exact];
    if (handler === undefined) {
      const actionMatch = call.path.match(/^\/actions\/([^/]+)\/(confirm|cancel)$/);
      if (actionMatch && call.method === 'POST') {
        const [, id, verb] = actionMatch;
        const key = `POST /actions/:id/${verb}`;
        handler = overrides[key] ?? (verb === 'confirm'
          ? () => ({ json: { action: { id, status: 'executed' }, ok: true, message: 'Done: email sent to Rahul.' } })
          : () => ({ json: { id, status: 'cancelled' } }));
      }
    }
    if (handler === undefined) {
      await route.fulfill({ status: 404, headers: CORS, contentType: 'application/json', body: JSON.stringify({ detail: 'Not mocked' }) });
      return;
    }
    const out = typeof handler === 'function' ? await handler(call) : { json: handler };
    if (out.json !== undefined) {
      await route.fulfill({ status: out.status || 200, headers: CORS, contentType: 'application/json', body: JSON.stringify(out.json) });
    } else {
      await route.fulfill({ status: out.status || 200, headers: CORS, contentType: out.contentType || 'application/octet-stream', body: out.body || '' });
    }
  });

  return calls;
}

/** Mocks the /ws endpoint. Returns an object exposing the latest server-side socket. */
async function mockSocket(page) {
  const handle = { socket: null, url: null, received: [] };
  await page.routeWebSocket(/\/ws(\?|$)/, (ws) => {
    handle.socket = ws;
    handle.url = ws.url();
    ws.onMessage((message) => {
      let msg;
      try {
        msg = JSON.parse(String(message));
      } catch {
        return;
      }
      handle.received.push(msg);
      if (msg.type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  return handle;
}

async function withToken(page, token = TOKEN) {
  await page.addInitScript((t) => {
    try {
      if (!sessionStorage.getItem('__tokenSeeded')) {
        localStorage.setItem('cos.token', t);
        sessionStorage.setItem('__tokenSeeded', '1');
      }
    } catch {
      // ignore
    }
  }, token);
}

/**
 * Installs fake speech engines before any page script runs.
 * options.recognition: true to provide a fake SpeechRecognition (default), false to remove it.
 * options.synthesis: 'ok' (default) or 'blocked' (speak fails until window.__allowSpeech = true).
 * options.mediaRecorder: true to install a fake getUserMedia + MediaRecorder.
 */
async function stubSpeech(page, options = {}) {
  const opts = { recognition: true, synthesis: 'ok', mediaRecorder: false, ...options };
  await page.addInitScript((o) => {
    window.__spoken = [];
    window.__recognitions = [];
    window.__allowSpeech = o.synthesis !== 'blocked';

    if (o.recognition) {
      class FakeSpeechRecognition {
        constructor() {
          this.lang = 'en-US';
          this.interimResults = false;
          this.continuous = false;
          this.started = false;
          this.ended = false;
          window.__recognitions.push(this);
        }
        start() {
          this.started = true;
          setTimeout(() => this.onstart && this.onstart(), 0);
        }
        stop() {
          if (this.ended) return;
          this.ended = true;
          setTimeout(() => this.onend && this.onend(), 0);
        }
        abort() {
          this.stop();
        }
        __hear(text) {
          const result = [{ transcript: text, confidence: 0.5 }];
          result.isFinal = false;
          this.onresult({ resultIndex: 0, results: [result] });
        }
        __say(text) {
          const alternative = { transcript: text, confidence: 0.97 };
          const result = [alternative];
          result.isFinal = true;
          this.onresult({ resultIndex: 0, results: [result] });
        }
        // Like a real engine in continuous mode: each phrase after a short pause becomes
        // a new final result, and event.results holds the whole session so far.
        __continue(text) {
          this.__results = this.__results || [];
          const result = [{ transcript: text, confidence: 0.97 }];
          result.isFinal = true;
          this.__results.push(result);
          this.onresult({ resultIndex: this.__results.length - 1, results: [...this.__results] });
        }
      }
      window.SpeechRecognition = FakeSpeechRecognition;
      window.webkitSpeechRecognition = FakeSpeechRecognition;
    } else {
      window.SpeechRecognition = undefined;
      window.webkitSpeechRecognition = undefined;
    }

    class FakeUtterance {
      constructor(text) {
        this.text = text;
      }
    }
    const synth = {
      speaking: false,
      cancel() {},
      getVoices() {
        return [];
      },
      speak(utterance) {
        if (!window.__allowSpeech) {
          setTimeout(() => utterance.onerror && utterance.onerror({ error: 'not-allowed' }), 0);
          return;
        }
        window.__spoken.push(utterance.text);
        setTimeout(() => {
          if (utterance.onstart) utterance.onstart();
          // 'no-end' copies Chrome's bug: the speech finishes but onend never fires.
          if (o.synthesis !== 'no-end') setTimeout(() => utterance.onend && utterance.onend(), 20);
        }, 0);
      },
    };
    Object.defineProperty(window, 'speechSynthesis', { configurable: true, get: () => synth });
    window.SpeechSynthesisUtterance = FakeUtterance;

    if (o.mediaRecorder) {
      const fakeStream = { getTracks: () => [{ stop() {} }] };
      Object.defineProperty(navigator, 'mediaDevices', {
        configurable: true,
        value: { getUserMedia: async () => fakeStream },
      });
      class FakeMediaRecorder {
        static isTypeSupported(type) {
          return type === 'audio/webm';
        }
        constructor(stream, options) {
          this.stream = stream;
          this.state = 'inactive';
          this.mimeType = (options && options.mimeType) || 'audio/webm';
        }
        start() {
          this.state = 'recording';
        }
        stop() {
          this.state = 'inactive';
          setTimeout(() => {
            if (this.ondataavailable) this.ondataavailable({ data: new Blob(['fake-audio-bytes'], { type: this.mimeType }) });
            if (this.onstop) this.onstop();
          }, 0);
        }
      }
      window.MediaRecorder = FakeMediaRecorder;
    }
  }, opts);
}

/** Standard setup for an authenticated page: speech stubs, API + socket mocks, token. */
async function setup(page, { overrides = {}, speech = {}, token = true } = {}) {
  await stubSpeech(page, speech);
  const calls = await mockApi(page, overrides);
  const socket = await mockSocket(page);
  if (token) await withToken(page);
  return { calls, socket };
}

function findCalls(calls, method, path) {
  return calls.filter((c) => c.method === method && c.path === path);
}

module.exports = {
  connectionsBody,
  accountsBody,
  TOKEN,
  SAMPLE_BRIEF,
  SAMPLE_PENDING,
  SAMPLE_FOLLOWUPS,
  converseReply,
  mockApi,
  mockSocket,
  stubSpeech,
  withToken,
  setup,
  findCalls,
};
