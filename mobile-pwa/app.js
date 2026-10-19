// Chief of Staff PWA: voice-first client for the assistant backend.
// Plain browser JavaScript, no framework and no build step.

'use strict';

const STORAGE = {
  token: 'cos.token',
  apiBase: 'cos.apiBase',
  conversation: 'cos.conversationId',
  iosHintDismissed: 'cos.iosHintDismissed',
};

const BANDS = {
  P1_do_myself: { cls: 'band-p1', code: 'P1', name: 'Do myself' },
  P2_delegate: { cls: 'band-p2', code: 'P2', name: 'Delegate' },
  P3_decline: { cls: 'band-p3', code: 'P3', name: 'Decline' },
  P4_drop: { cls: 'band-p4', code: 'P4', name: 'Drop' },
};

const SUGGESTED = {
  do: 'Do it yourself',
  delegate: 'Delegate',
  decline: 'Decline',
  drop: 'Drop it',
  rollover: 'Roll over',
  open: 'Open',
};

const KINDS = { task: 'Task', meeting: 'Meeting', email_action: 'Email' };

const ACTION_KINDS = {
  delegate: 'Delegate',
  decline_meeting: 'Decline meeting',
  defer: 'Defer',
  close_task: 'Close task',
  drop: 'Drop',
};

const ORB_LABELS = {
  idle: 'Tap to talk',
  listening: 'Listening…',
  thinking: 'Thinking…',
  speaking: 'Speaking…',
};

const ORB_ARIA = {
  idle: 'Start talking',
  listening: 'Stop listening',
  thinking: 'Working on it',
  speaking: 'Stop speaking',
};

const MAX_TURNS = 6;
const RECORD_LIMIT_MS = 15000;
// Recording stops by itself after this much quiet once you have started speaking.
const SILENCE_STOP_MS = 1500;
const SILENCE_LEVEL = 0.02;

const state = {
  brief: null,
  briefKind: new Date().getHours() >= 15 ? 'evening' : 'morning',
  pending: new Map(),
  followups: [],
  turns: [],
  orb: 'idle',
  recognition: null,
  recorder: null,
  recordTimer: null,
  speakToken: 0,
  audio: null,
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  wsPing: null,
  autostartPending: false,
};

// ---------------------------------------------------------------- storage

function store(key, value) {
  try {
    if (value === null || value === undefined || value === '') localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // Storage can be unavailable (private mode); the app still works for this session.
  }
}

function load(key) {
  try {
    return localStorage.getItem(key) || '';
  } catch {
    return '';
  }
}

const getToken = () => load(STORAGE.token);
const getApiBase = () => load(STORAGE.apiBase).replace(/\/+$/, '');

// ---------------------------------------------------------------- dom helpers

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style') el.style.cssText = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function formatWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (sameDay) return time;
  return `${d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })}`;
}

// ---------------------------------------------------------------- toasts

function toast(message, opts = {}) {
  const { title = '', variant = '', timeout = 4500 } = opts;
  const el = h('div', { class: `toast${variant ? ` toast-${variant}` : ''}` },
    title ? h('span', { class: 'toast-title', text: title }) : null,
    h('span', { class: 'toast-body', text: message }));
  const box = $('toasts');
  box.append(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 320);
  }, timeout);
  return el;
}

// ---------------------------------------------------------------- api

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function apiUrl(path) {
  return `${getApiBase()}${path}`;
}

async function api(path, opts = {}) {
  const { method = 'GET', body, auth = true, form, raw = false } = opts;
  const headers = {};
  if (auth) {
    const token = getToken();
    if (!token) throw new ApiError(401, 'Not connected');
    headers.Authorization = `Bearer ${token}`;
  }
  let payload;
  if (form) payload = form;
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(apiUrl(path), { method, headers, body: payload });
  } catch {
    throw new ApiError(0, navigator.onLine ? 'Cannot reach the assistant' : 'You are offline');
  }
  if (res.status === 401 && auth) {
    handleUnauthorized();
    throw new ApiError(401, 'Session expired. Reconnect in Settings.');
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText || 'error'}`.trim();
    try {
      const data = await res.json();
      if (data && typeof data.detail === 'string') detail = data.detail;
    } catch {
      // Body was not JSON; keep the status text.
    }
    throw new ApiError(res.status, detail);
  }
  if (raw) return res;
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

function handleUnauthorized() {
  store(STORAGE.token, null);
  closeSocket();
  updateAuthUi();
}

// ---------------------------------------------------------------- orb state

function setOrb(next) {
  state.orb = next;
  const orb = $('orb');
  orb.dataset.state = next;
  orb.setAttribute('aria-label', ORB_ARIA[next]);
  orb.setAttribute('aria-pressed', next === 'listening' ? 'true' : 'false');
  $('orb-label').textContent = ORB_LABELS[next];
}

function setCaption(text) {
  $('caption').textContent = text || '';
}

// ---------------------------------------------------------------- speech output

function stopSpeaking() {
  state.speakToken += 1;
  try {
    if ('speechSynthesis' in window && window.speechSynthesis) window.speechSynthesis.cancel();
  } catch {
    // Ignore engines that throw on cancel.
  }
  if (state.audio) {
    state.audio.pause();
    state.audio = null;
  }
  if (state.orb === 'speaking') setOrb('idle');
}

function speakWithSynthesis(text, token) {
  return new Promise((resolve) => {
    const synth = window.speechSynthesis;
    const Utterance = window.SpeechSynthesisUtterance;
    if (!synth || !Utterance) {
      resolve(false);
      return;
    }
    let started = false;
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(startTimer);
      resolve(ok);
    };
    const utterance = new Utterance(text);
    utterance.lang = document.documentElement.lang || 'en';
    utterance.rate = 1.02;
    utterance.onstart = () => {
      started = true;
      if (token === state.speakToken) setOrb('speaking');
    };
    utterance.onend = () => finish(true);
    utterance.onerror = (event) => {
      const err = event && event.error;
      // "interrupted"/"canceled" mean we stopped it ourselves: that is not a failure.
      finish(err === 'interrupted' || err === 'canceled');
    };
    // Engines that silently refuse (no user gesture) never fire onstart.
    const startTimer = setTimeout(() => {
      if (!started) {
        try { synth.cancel(); } catch { /* ignore */ }
        finish(false);
      }
    }, 4000);
    try {
      synth.cancel();
      synth.speak(utterance);
    } catch {
      finish(false);
    }
  });
}

async function speakWithServer(text, token) {
  let res;
  try {
    res = await api(`/tts?text=${encodeURIComponent(text.slice(0, 4000))}`, { raw: true });
  } catch {
    return false;
  }
  const blob = await res.blob();
  if (token !== state.speakToken) return true;
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  state.audio = audio;
  try {
    await audio.play();
  } catch {
    URL.revokeObjectURL(url);
    state.audio = null;
    return false;
  }
  if (token === state.speakToken) setOrb('speaking');
  await new Promise((resolve) => {
    audio.onended = resolve;
    audio.onerror = resolve;
    audio.onpause = resolve;
  });
  URL.revokeObjectURL(url);
  if (state.audio === audio) state.audio = null;
  return true;
}

// Speaks text; resolves true when audio played, false when the browser refused.
async function speak(text) {
  if (!text) return true;
  stopSpeaking();
  const token = state.speakToken;
  let ok = await speakWithSynthesis(text, token);
  if (!ok && token === state.speakToken) ok = await speakWithServer(text, token);
  if (token === state.speakToken && state.orb === 'speaking') setOrb('idle');
  return ok;
}

function showTapToHear(text) {
  const overlay = $('tap-to-hear');
  overlay.hidden = false;
  const btn = $('tap-to-hear-btn');
  btn.onclick = async () => {
    overlay.hidden = true;
    const ok = await speak(text);
    if (!ok) toast('Audio is blocked on this device. Read the brief below.', { variant: 'error' });
  };
  btn.focus();
}

// ---------------------------------------------------------------- speech input

function getRecognitionCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function onOrbClick() {
  if (state.orb === 'speaking') {
    stopSpeaking();
    return;
  }
  if (state.orb === 'listening') {
    stopListening();
    return;
  }
  if (state.orb === 'thinking') return;
  if (!requireAuth()) return;
  startListening();
}

function startListening() {
  // iOS exposes webkitSpeechRecognition, but in a home-screen app it often never returns
  // a result, so the orb hangs on "Listening". Record and let the server transcribe.
  const Ctor = isIos() ? null : getRecognitionCtor();
  if (Ctor) startRecognition(Ctor);
  else startRecording();
}

function stopListening() {
  if (state.recognition) {
    try { state.recognition.stop(); } catch { /* already stopped */ }
  }
  if (state.recorder && state.recorder.state !== 'inactive') state.recorder.stop();
}

function startRecognition(Ctor) {
  let finalText = '';
  let interimText = '';
  let handled = false;
  const send = (text) => {
    handled = true;
    state.recognition = null;
    try { rec.stop(); } catch { /* ignore */ }
    sendText(text, { source: 'voice' });
  };
  const rec = new Ctor();
  state.recognition = rec;
  rec.lang = navigator.language || 'en-US';
  rec.interimResults = true;
  rec.continuous = false;
  rec.maxAlternatives = 1;
  rec.onstart = () => setOrb('listening');
  rec.onresult = (event) => {
    let interim = '';
    for (let i = event.resultIndex || 0; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) finalText += result[0].transcript;
      else interim += result[0].transcript;
    }
    interimText = interim;
    setCaption(finalText || interim);
    if (finalText.trim() && !handled) send(finalText.trim());
  };
  rec.onerror = (event) => {
    const err = event && event.error;
    if (err === 'not-allowed' || err === 'service-not-allowed') {
      toast('Microphone access was blocked. Allow it in your browser settings or type instead.', { variant: 'error' });
    } else if (err === 'no-speech') {
      toast('I did not catch that. Tap and try again.');
    } else if (err && err !== 'aborted') {
      toast(`Speech recognition error: ${err}`, { variant: 'error' });
    }
  };
  rec.onend = () => {
    if (state.recognition === rec) state.recognition = null;
    // Some engines end without ever marking a result final; use what was heard.
    if (!handled && interimText.trim()) {
      send(interimText.trim());
      return;
    }
    if (!handled && state.orb === 'listening') {
      setOrb('idle');
      setCaption('');
    }
  };
  setOrb('listening');
  setCaption('');
  try {
    rec.start();
  } catch {
    state.recognition = null;
    setOrb('idle');
    toast('Could not start the microphone.', { variant: 'error' });
  }
}

function pickRecorderMime() {
  const MR = window.MediaRecorder;
  if (!MR || typeof MR.isTypeSupported !== 'function') return '';
  for (const type of ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']) {
    if (MR.isTypeSupported(type)) return type;
  }
  return '';
}

async function startRecording() {
  if (!window.MediaRecorder || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast('Voice input is not supported in this browser. Type your message instead.', { variant: 'error' });
    $('text-input').focus();
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast('Microphone access was blocked. Allow it in your browser settings or type instead.', { variant: 'error' });
    return;
  }
  const mimeType = pickRecorderMime();
  const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
  const chunks = [];
  state.recorder = recorder;
  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size) chunks.push(event.data);
  };
  const stopOnSilence = watchSilence(stream, () => {
    if (recorder.state !== 'inactive') recorder.stop();
  });
  recorder.onstop = async () => {
    clearTimeout(state.recordTimer);
    stopOnSilence();
    state.recorder = null;
    stream.getTracks().forEach((t) => t.stop());
    const type = recorder.mimeType || mimeType || 'audio/webm';
    const blob = new Blob(chunks, { type });
    if (!blob.size) {
      setOrb('idle');
      toast('No audio was recorded. Tap and try again.');
      return;
    }
    setOrb('thinking');
    setCaption('Transcribing…');
    try {
      const form = new FormData();
      const ext = type.includes('mp4') ? 'm4a' : type.includes('ogg') ? 'ogg' : 'webm';
      form.append('audio', blob, `speech.${ext}`);
      const data = await api('/stt', { method: 'POST', form });
      const text = (data && data.text ? data.text : '').trim();
      if (!text) {
        setOrb('idle');
        setCaption('');
        toast('I did not catch that. Tap and try again.');
        return;
      }
      setCaption(text);
      await sendText(text, { source: 'voice' });
    } catch (err) {
      setOrb('idle');
      setCaption('');
      const msg = err.status === 503 ? 'Server speech recognition is not installed. Type instead.' : err.message;
      toast(msg, { variant: 'error', title: 'Voice' });
    }
  };
  recorder.start();
  setOrb('listening');
  setCaption('Listening… tap the orb when you finish.');
  state.recordTimer = setTimeout(() => {
    if (recorder.state !== 'inactive') recorder.stop();
  }, RECORD_LIMIT_MS);
}

// Calls onSilence once the speaker has talked and then gone quiet. Returns a cleanup function.
function watchSilence(stream, onSilence) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return () => {};
  let ctx;
  try {
    ctx = new Ctx();
  } catch {
    return () => {};
  }
  // Safari may create the context suspended because it starts after an await.
  if (ctx.resume) ctx.resume().catch(() => {});
  let analyser;
  try {
    analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
  } catch {
    ctx.close().catch(() => {});
    return () => {};
  }
  const samples = new Float32Array(analyser.fftSize);
  let heard = false;
  let quietSince = 0;
  const timer = setInterval(() => {
    analyser.getFloatTimeDomainData(samples);
    let sum = 0;
    for (const v of samples) sum += v * v;
    const level = Math.sqrt(sum / samples.length);
    const now = Date.now();
    if (level > SILENCE_LEVEL) {
      heard = true;
      quietSince = 0;
    } else if (heard) {
      if (!quietSince) quietSince = now;
      else if (now - quietSince >= SILENCE_STOP_MS) onSilence();
    }
  }, 100);
  return () => {
    clearInterval(timer);
    ctx.close().catch(() => {});
  };
}

// ---------------------------------------------------------------- conversation

function addTurn(role, text) {
  state.turns.push({ role, text });
  if (state.turns.length > MAX_TURNS * 2) state.turns.splice(0, state.turns.length - MAX_TURNS * 2);
  renderTranscript();
}

function renderTranscript() {
  const list = $('transcript');
  const turns = state.turns.slice(-MAX_TURNS);
  list.replaceChildren(...turns.map((t) => h('li', { class: `turn turn-${t.role}` },
    h('span', { class: 'turn-who', text: t.role === 'user' ? 'You' : 'Assistant' }),
    h('span', { class: 'turn-text', text: t.text }))));
  $('transcript-empty').hidden = turns.length > 0;
}

async function converse(text) {
  const conversationId = sessionStorage.getItem(STORAGE.conversation) || null;
  const data = await api('/converse', { method: 'POST', body: { text, conversation_id: conversationId } });
  if (data && data.conversation_id) sessionStorage.setItem(STORAGE.conversation, data.conversation_id);
  return data;
}

function handleConverseResponse(data) {
  if (!data) return;
  if (data.reply) addTurn('assistant', data.reply);
  if (Array.isArray(data.pending_actions)) {
    for (const action of data.pending_actions) {
      if (action.status === 'pending') state.pending.set(action.id, action);
      else state.pending.delete(action.id);
    }
    renderPending();
  }
  if (Array.isArray(data.executed) && data.executed.length) {
    for (const ex of data.executed) {
      state.pending.delete(ex.action_id);
      toast(ex.message || (ex.ok ? 'Done' : 'Failed'), { variant: ex.ok ? 'ok' : 'error', title: ex.ok ? 'Done' : 'Failed' });
    }
    renderPending();
    loadFollowups();
  }
  if (data.needs_confirmation) {
    $('pending-section').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

// Sends a message (typed or spoken) and speaks the reply. Returns the speak result.
async function sendText(text, opts = {}) {
  const { speakReply = true } = opts;
  if (!text) return true;
  if (!requireAuth()) return true;
  stopSpeaking();
  addTurn('user', text);
  setOrb('thinking');
  setCaption('');
  let data;
  try {
    data = await converse(text);
  } catch (err) {
    setOrb('idle');
    toast(err.message, { variant: 'error', title: 'Assistant' });
    return true;
  }
  handleConverseResponse(data);
  setOrb('idle');
  if (!speakReply || !data || !data.reply) return true;
  const ok = await speak(data.reply);
  if (!ok && opts.source === 'voice') toast('Could not play audio. The reply is shown below.');
  return ok;
}

// ---------------------------------------------------------------- brief

function bandInfo(band) {
  return BANDS[band] || { cls: 'band-p0', code: '—', name: 'Unranked' };
}

function renderBriefCard(item, index) {
  const band = bandInfo(item.band);
  const when = formatWhen(item.start || item.due);
  const whenLabel = item.start ? `Starts ${when}` : item.due ? `Due ${when}` : '';
  const titleNode = item.url
    ? h('a', { href: item.url, target: '_blank', rel: 'noopener noreferrer', text: item.title })
    : item.title;
  const suggestion = SUGGESTED[item.suggested] || null;
  return h('article', { class: `card ${band.cls}`, 'data-band': item.band || 'none', 'data-item-id': item.id, style: `--i:${index}` },
    h('div', { class: 'card-top' },
      h('span', { class: 'band-chip', title: band.name }, `${band.code} · ${band.name}`),
      item.kind ? h('span', { class: 'kind-chip', text: KINDS[item.kind] || item.kind }) : null,
      whenLabel ? h('span', { class: 'meta-chip', text: whenLabel }) : null),
    h('h3', { class: 'card-title' }, titleNode),
    item.reason ? h('p', { class: 'card-reason', text: item.reason }) : null,
    suggestion || item.delegate_name
      ? h('div', { class: 'card-foot' },
        suggestion ? h('span', { class: 'suggest' }, h('span', { class: 'arrow', 'aria-hidden': 'true', text: '›' }), `Suggested: ${suggestion}`) : null,
        item.delegate_name ? h('span', { class: 'delegate', text: item.delegate_name, 'aria-label': `Delegate to ${item.delegate_name}` }) : null)
      : null);
}

function renderBrief(brief) {
  state.brief = brief;
  const cards = $('brief-cards');
  const summary = $('brief-summary');
  if (!brief) {
    cards.replaceChildren();
    summary.hidden = true;
    $('brief-empty').hidden = false;
    return;
  }
  if (brief.kind && brief.kind !== state.briefKind && (brief.kind === 'morning' || brief.kind === 'evening')) {
    state.briefKind = brief.kind;
    syncKindButtons();
  }
  const items = Array.isArray(brief.items) ? brief.items : [];
  cards.replaceChildren(...items.map(renderBriefCard));
  summary.hidden = !brief.text;
  $('brief-text').textContent = brief.text || '';
  const meta = [];
  if (brief.kind) meta.push(brief.kind === 'evening' ? 'Evening wrap-up' : 'Morning brief');
  if (brief.estimated_seconds) meta.push(`~${Math.max(1, Math.round(brief.estimated_seconds / 60))} min listen`);
  if (items.length) meta.push(`${items.length} item${items.length === 1 ? '' : 's'}`);
  $('brief-meta').textContent = meta.join(' · ');
  const details = Array.isArray(brief.details) ? brief.details : [];
  $('brief-details').replaceChildren(...details.map((d) => h('li', { text: d })));
  $('brief-details').hidden = true;
  $('tell-more').setAttribute('aria-expanded', 'false');
  $('tell-more').disabled = details.length === 0;
  $('brief-empty').hidden = items.length > 0 || Boolean(brief.text);
  if (!items.length && !brief.text) $('brief-empty').textContent = 'Nothing on your plate. Enjoy the calm.';
}

async function loadBrief(kind = state.briefKind) {
  if (!getToken()) return null;
  try {
    const brief = await api(`/brief/today?kind=${encodeURIComponent(kind)}`);
    renderBrief(brief);
    return brief;
  } catch (err) {
    if (err.status !== 401) toast(err.message, { variant: 'error', title: 'Brief' });
    return null;
  }
}

function syncKindButtons() {
  document.querySelectorAll('.seg[data-kind]').forEach((btn) => {
    btn.setAttribute('aria-pressed', btn.dataset.kind === state.briefKind ? 'true' : 'false');
  });
}

async function tellMore() {
  const details = state.brief && Array.isArray(state.brief.details) ? state.brief.details : [];
  if (!details.length) return;
  const list = $('brief-details');
  list.hidden = false;
  $('tell-more').setAttribute('aria-expanded', 'true');
  await speak(details.join(' '));
}

// The flow used by "/?autostart=brief", Siri Shortcuts and push notifications.
async function runBriefFlow() {
  if (!requireAuth()) {
    state.autostartPending = true;
    return;
  }
  state.autostartPending = false;
  const briefPromise = loadBrief();
  addTurn('user', 'Brief me');
  setOrb('thinking');
  let data;
  try {
    data = await converse('brief me');
  } catch (err) {
    setOrb('idle');
    toast(err.message, { variant: 'error', title: 'Brief' });
    const brief = await briefPromise;
    if (brief && brief.text) showTapToHear(brief.text);
    return;
  }
  handleConverseResponse(data);
  setOrb('idle');
  await briefPromise;
  const text = (data && data.reply) || (state.brief && state.brief.text) || '';
  if (!text) return;
  const ok = await speak(text);
  if (!ok) showTapToHear(text);
}

// ---------------------------------------------------------------- confirmations

function renderPending() {
  const list = $('pending-list');
  const actions = [...state.pending.values()].filter((a) => a.status === 'pending');
  list.replaceChildren(...actions.map((action) => {
    const label = action.title || ACTION_KINDS[action.kind] || 'Action';
    return h('li', { class: 'pending-item', 'data-action-id': action.id },
      h('div', { class: 'item-top' },
        h('span', { class: 'tag', text: ACTION_KINDS[action.kind] || action.kind || 'Action' })),
      h('p', { class: 'item-title', text: label }),
      action.readback ? h('p', { class: 'item-text', text: action.readback }) : null,
      h('div', { class: 'item-actions' },
        h('button', { type: 'button', class: 'btn btn-confirm', 'data-confirm': action.id, 'aria-label': `Confirm: ${label}`, onclick: () => decide(action.id, 'confirm') }, 'Confirm'),
        h('button', { type: 'button', class: 'btn btn-cancel', 'data-cancel': action.id, 'aria-label': `Cancel: ${label}`, onclick: () => decide(action.id, 'cancel') }, 'Cancel')));
  }));
  $('pending-empty').hidden = actions.length > 0;
  const count = $('pending-count');
  count.hidden = actions.length === 0;
  count.textContent = String(actions.length);
}

async function loadPending() {
  if (!getToken()) return;
  try {
    const actions = await api('/actions?status=pending');
    state.pending = new Map((actions || []).map((a) => [a.id, a]));
    renderPending();
  } catch (err) {
    if (err.status !== 401) toast(err.message, { variant: 'error', title: 'Confirmations' });
  }
}

async function decide(id, verb) {
  const row = document.querySelector(`.pending-item[data-action-id="${CSS.escape(id)}"]`);
  const buttons = row ? row.querySelectorAll('button') : [];
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const res = await api(`/actions/${encodeURIComponent(id)}/${verb}`, { method: 'POST' });
    if (verb === 'confirm') {
      const ok = res ? res.ok !== false : true;
      toast((res && res.message) || (ok ? 'Done.' : 'That did not go through.'), { variant: ok ? 'ok' : 'error', title: ok ? 'Confirmed' : 'Failed' });
      loadFollowups();
    } else {
      toast('Cancelled. Nothing was sent.', { title: 'Cancelled' });
    }
    if (row) {
      row.classList.add('leaving');
      await new Promise((r) => setTimeout(r, 200));
    }
    state.pending.delete(id);
    renderPending();
  } catch (err) {
    buttons.forEach((b) => { b.disabled = false; });
    toast(err.message, { variant: 'error', title: verb === 'confirm' ? 'Confirm failed' : 'Cancel failed' });
  }
}

// ---------------------------------------------------------------- delegations

function renderFollowups() {
  const list = $('followups-list');
  list.replaceChildren(...state.followups.map((f) => {
    const bits = [];
    if (f.delegate_name) bits.push(f.delegate_name);
    if (f.due) bits.push(`due ${formatWhen(f.due)}`);
    if (f.nudges_sent) bits.push(`nudged ${f.nudges_sent}×`);
    return h('li', { class: 'followup-item', 'data-followup-id': f.id },
      h('p', { class: 'item-title', text: f.title }),
      h('span', { class: `state state-${f.state}`, text: f.state }),
      bits.length ? h('p', { class: 'item-text', text: bits.join(' · ') }) : null);
  }));
  $('followups-empty').hidden = state.followups.length > 0;
}

async function loadFollowups() {
  if (!getToken()) return;
  try {
    const items = await api('/followups');
    state.followups = Array.isArray(items) ? items : [];
    renderFollowups();
  } catch (err) {
    if (err.status !== 401) toast(err.message, { variant: 'error', title: 'Delegations' });
  }
}

// ---------------------------------------------------------------- websocket

function wsUrl(token) {
  const base = getApiBase();
  const origin = base ? new URL(base, location.href) : new URL(location.href);
  const proto = origin.protocol === 'https:' ? 'wss:' : 'ws:';
  const prefix = base ? origin.pathname.replace(/\/+$/, '') : '';
  return `${proto}//${origin.host}${prefix}/ws?token=${encodeURIComponent(token)}`;
}

function setConn(kind, text) {
  const el = $('conn-status');
  el.dataset.state = kind;
  el.querySelector('.conn-text').textContent = text;
}

function closeSocket() {
  clearTimeout(state.wsTimer);
  clearInterval(state.wsPing);
  state.wsTimer = null;
  if (state.ws) {
    const ws = state.ws;
    state.ws = null;
    ws.onclose = null;
    try { ws.close(); } catch { /* ignore */ }
  }
  setConn('off', getToken() ? 'Offline' : 'Not connected');
}

function connectSocket() {
  const token = getToken();
  if (!token || !('WebSocket' in window)) return;
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(state.wsTimer);
  setConn('connecting', 'Connecting');
  let ws;
  try {
    ws = new WebSocket(wsUrl(token));
  } catch {
    scheduleReconnect();
    return;
  }
  state.ws = ws;
  ws.onopen = () => {
    state.wsRetry = 0;
    setConn('live', 'Live');
    clearInterval(state.wsPing);
    state.wsPing = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
    }, 25000);
  };
  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    handleSocketMessage(msg);
  };
  ws.onclose = (event) => {
    clearInterval(state.wsPing);
    if (state.ws === ws) state.ws = null;
    if (event && (event.code === 4401 || event.code === 1008)) {
      handleUnauthorized();
      toast('Session expired. Reconnect in Settings.', { variant: 'error' });
      return;
    }
    scheduleReconnect();
  };
  ws.onerror = () => {
    // onclose follows and handles the retry.
  };
}

function scheduleReconnect() {
  if (!getToken()) return;
  setConn(navigator.onLine ? 'connecting' : 'offline', navigator.onLine ? 'Reconnecting' : 'Offline');
  const delay = Math.min(30000, 1000 * 2 ** state.wsRetry) * (0.75 + Math.random() * 0.5);
  state.wsRetry = Math.min(state.wsRetry + 1, 6);
  clearTimeout(state.wsTimer);
  state.wsTimer = setTimeout(connectSocket, delay);
}

function handleSocketMessage(msg) {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'event') {
    const payload = msg.payload || {};
    if (msg.kind === 'notice') {
      toast(payload.text || 'Update from your assistant', { title: 'Notice' });
    } else if (msg.kind === 'brief' || msg.kind === 'alert') {
      renderBrief(payload);
      const isAlert = msg.kind === 'alert';
      const first = Array.isArray(payload.items) && payload.items[0] ? payload.items[0].title : '';
      toast(isAlert ? (first || payload.text || 'Priority alert') : 'Your brief has been updated.', {
        title: isAlert ? 'P1 alert' : payload.kind === 'evening' ? 'Evening wrap-up' : 'Brief ready',
        variant: isAlert ? 'alert' : '',
        timeout: isAlert ? 8000 : 4500,
      });
      loadPending();
    }
  } else if (msg.type === 'reply') {
    handleConverseResponse(msg);
    if (msg.reply) speak(msg.reply);
  }
}

// ---------------------------------------------------------------- push

function isIos() {
  const ua = navigator.userAgent || '';
  return /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function isStandalone() {
  return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true;
}

function urlBase64ToUint8Array(base64) {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function enablePush() {
  const status = $('push-state');
  const setStatus = (text, cls = '') => {
    status.textContent = text;
    status.className = `hint ${cls}`.trim();
  };
  if (isIos() && !isStandalone()) {
    $('ios-hint').hidden = false;
    setStatus('Install the app to your home screen first.', 'warn');
    return;
  }
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    setStatus('Push notifications are not supported in this browser.', 'warn');
    return;
  }
  if (!requireAuth()) return;
  const btn = $('push-btn');
  btn.disabled = true;
  try {
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      setStatus('Notifications are blocked. Allow them in your browser settings.', 'warn');
      return;
    }
    const { key } = await api('/push/vapid-public-key');
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key) });
    }
    await api('/push/subscribe', { method: 'POST', body: sub.toJSON() });
    setStatus('Push notifications are on.', 'ok');
    toast('You will get your brief and P1 alerts here.', { title: 'Notifications on', variant: 'ok' });
  } catch (err) {
    setStatus(`Could not enable notifications: ${err.message}`, 'warn');
  } finally {
    btn.disabled = false;
  }
}

async function refreshPushState() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg ? await reg.pushManager.getSubscription() : null;
    if (sub) {
      $('push-state').textContent = 'Push notifications are on.';
      $('push-state').className = 'hint ok';
    }
  } catch {
    // Push state is informational only.
  }
}

// ---------------------------------------------------------------- auth + settings

function requireAuth() {
  if (getToken()) return true;
  toast('Connect first: enter your shared secret.', { title: 'Not connected' });
  openSettings();
  return false;
}

function updateAuthUi() {
  const token = getToken();
  $('connect-banner').hidden = Boolean(token);
  $('logout-btn').hidden = !token;
  $('auth-state').textContent = token ? 'Connected. Your device is signed in.' : 'Not connected.';
  $('auth-state').className = token ? 'hint ok' : 'hint';
  if (!token) setConn('off', 'Not connected');
}

function openSettings() {
  const dialog = $('settings');
  $('api-base-input').value = load(STORAGE.apiBase);
  $('login-error').hidden = true;
  if (!dialog.open) {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }
  if (!getToken()) $('secret-input').focus();
}

async function onLogin(event) {
  event.preventDefault();
  const input = $('secret-input');
  const secret = input.value.trim();
  const errorEl = $('login-error');
  errorEl.hidden = true;
  if (!secret) return;
  const btn = $('login-btn');
  btn.disabled = true;
  try {
    const data = await api('/auth/token', { method: 'POST', auth: false, body: { secret, client_name: 'pwa' } });
    if (!data || !data.access_token) throw new ApiError(500, 'The server did not return a token.');
    store(STORAGE.token, data.access_token);
    input.value = '';
    updateAuthUi();
    toast('Your assistant is connected.', { title: 'Connected', variant: 'ok' });
    const dialog = $('settings');
    if (dialog.open) dialog.close();
    await refreshAll();
    connectSocket();
    if (state.autostartPending) runBriefFlow();
  } catch (err) {
    errorEl.textContent = err.status === 401 ? 'That secret was not accepted.' : err.message;
    errorEl.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

function onSaveApiBase(event) {
  event.preventDefault();
  const value = $('api-base-input').value.trim().replace(/\/+$/, '');
  if (value) {
    try {
      const u = new URL(value);
      if (u.protocol !== 'http:' && u.protocol !== 'https:') throw new Error('bad protocol');
    } catch {
      toast('Enter a full URL like https://assistant.example.com', { variant: 'error' });
      return;
    }
  }
  store(STORAGE.apiBase, value || null);
  toast(value ? `Using ${value}` : 'Using this site as the server.', { title: 'Server saved' });
  closeSocket();
  connectSocket();
  refreshAll();
}

function onLogout() {
  store(STORAGE.token, null);
  sessionStorage.removeItem(STORAGE.conversation);
  closeSocket();
  state.pending.clear();
  state.followups = [];
  renderPending();
  renderFollowups();
  renderBrief(null);
  updateAuthUi();
  toast('Signed out on this device.');
}

async function refreshAll() {
  if (!getToken()) return;
  await Promise.all([loadBrief(), loadPending(), loadFollowups()]);
}

// ---------------------------------------------------------------- boot

function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('sw.js').catch(() => {
    // Offline support is optional; the app works without it.
  });
  navigator.serviceWorker.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.type === 'navigate' && typeof data.url === 'string') {
      const url = new URL(data.url, location.href);
      if (url.searchParams.get('autostart') === 'brief') runBriefFlow();
    }
  });
}

function bindUi() {
  $('orb').addEventListener('click', onOrbClick);
  $('text-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('text-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    sendText(text, { source: 'text' });
  });
  $('open-settings').addEventListener('click', openSettings);
  document.querySelectorAll('[data-action="open-settings"]').forEach((b) => b.addEventListener('click', openSettings));
  document.querySelectorAll('[data-action="dismiss-ios"]').forEach((b) => b.addEventListener('click', () => {
    store(STORAGE.iosHintDismissed, '1');
    $('ios-banner').hidden = true;
  }));
  $('login-form').addEventListener('submit', onLogin);
  $('api-form').addEventListener('submit', onSaveApiBase);
  $('logout-btn').addEventListener('click', onLogout);
  $('push-btn').addEventListener('click', enablePush);
  $('refresh-brief').addEventListener('click', () => loadBrief());
  $('refresh-followups').addEventListener('click', () => loadFollowups());
  $('tell-more').addEventListener('click', tellMore);
  $('play-brief').addEventListener('click', () => {
    if (state.brief && state.brief.text) speak(state.brief.text);
  });
  document.querySelectorAll('.seg[data-kind]').forEach((btn) => btn.addEventListener('click', () => {
    state.briefKind = btn.dataset.kind;
    syncKindButtons();
    loadBrief();
  }));
  $('settings').addEventListener('click', (event) => {
    if (event.target === $('settings')) $('settings').close();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('tap-to-hear').hidden) $('tap-to-hear').hidden = true;
  });
  window.addEventListener('online', () => {
    connectSocket();
    refreshAll();
  });
  window.addEventListener('offline', () => setConn('offline', 'Offline'));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && getToken()) connectSocket();
  });
}

function init() {
  $('today-label').textContent = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  bindUi();
  syncKindButtons();
  setOrb('idle');
  renderTranscript();
  renderPending();
  renderFollowups();
  updateAuthUi();
  if (isIos() && !isStandalone()) {
    $('ios-hint').hidden = false;
    $('ios-banner').hidden = load(STORAGE.iosHintDismissed) === '1';
  }
  registerServiceWorker();
  refreshPushState();

  const params = new URLSearchParams(location.search);
  const autostart = params.get('autostart') === 'brief';
  if (autostart) {
    params.delete('autostart');
    const qs = params.toString();
    history.replaceState(null, '', `${location.pathname}${qs ? `?${qs}` : ''}${location.hash}`);
  }

  if (getToken()) {
    connectSocket();
    if (autostart) {
      Promise.all([loadPending(), loadFollowups()]);
      runBriefFlow();
    } else {
      refreshAll();
    }
  } else if (autostart) {
    state.autostartPending = true;
    openSettings();
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
