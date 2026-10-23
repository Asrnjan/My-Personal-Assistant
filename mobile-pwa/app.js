// Chief of Staff PWA: voice-first client for the assistant backend.
// Plain browser JavaScript, no framework and no build step.

'use strict';

import { StarCluster } from './cluster.js';

const STORAGE = {
  token: 'cos.token',
  apiBase: 'cos.apiBase',
  conversation: 'cos.conversationId',
  iosHintDismissed: 'cos.iosHintDismissed',
  name: 'cos.assistantName',
  wake: 'cos.listenForName',
  keepListening: 'cos.keepListening',
  voice: 'cos.voice',
  voiceSpeed: 'cos.voiceSpeed',
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
  send_email: 'Email',
  reply_email: 'Reply',
  teams_message: 'Teams message',
  book_meeting: 'Book meeting',
  add_contact: 'Add to team',
  remove_contact: 'Remove from team',
};

const ACCOUNT_NAMES = { outlook: 'Outlook', google: 'Google', teams: 'Microsoft Teams' };

// Letter and colour for each app's tile.
const TILES = {
  claude: ['C', '#d97757'],
  google: ['G', '#4285f4'],
  outlook: ['O', '#0f6cbd'],
  teams: ['T', '#6264a7'],
  jira: ['J', '#2684ff'],
  asana: ['A', '#f06a6a'],
  clickup: ['C', '#7b68ee'],
  telegram: ['T', '#2aabee'],
  slack: ['S', '#e01e5a'],
  'teams-webhook': ['T', '#6264a7'],
};

const VIEWS = ['home', 'team', 'chat', 'apps'];

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
// When to send a request. The speech engine marks a phrase final once it hears you stop;
// then a short extra pause is enough. While the last phrase is still unfinished, wait
// longer, so a pause in the middle of a sentence doesn't cut it off.
const QUICK_PAUSE_MS = 900;
const END_PAUSE_MS = 2000;
// After a spoken reply, how long to wait for a follow-up before going back to waiting
// for the name.
const FOLLOW_UP_MS = 15000;
// Saying one of these ends the back-and-forth instead of listening again.
const END_CONVERSATION = /^\s*(thanks|thank you|thankyou|that'?s all|that is all|that'?s it|stop|bye|goodbye|good bye|never ?mind|no thanks|no thank you|i'?m done|all done)\b/i;
const RECORD_LIMIT_MS = 30000;
// Recording stops by itself after this much quiet once you have started speaking.
const SILENCE_STOP_MS = 1800;
const SILENCE_LEVEL = 0.02;

const state = {
  brief: null,
  briefKind: new Date().getHours() >= 15 ? 'evening' : 'morning',
  pending: new Map(),
  followups: [],
  turns: [],
  accounts: null,
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
  name: 'Atlas',
  wakeRec: null,
  wakeTimer: null,
  view: 'home',
  connections: null,
  micLevel: 0,
  utterances: [],
  // Listening again right after a spoken reply, without the name.
  followUp: false,
  heardSpeech: false,
  discardRecording: false,
  sheetApp: null,
};

// The star cluster that stands in for the assistant; created in init().
let cluster = null;

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
  if (cluster) cluster.setState(next);
  let label = ORB_LABELS[next];
  if (next === 'idle' && wakeOn()) label = `Say “${state.name}” or tap`;
  if (next === 'listening' && state.followUp) label = 'Listening… say “thanks” when you’re done';
  $('orb-label').textContent = label;
  if (next === 'idle') scheduleWake();
  else stopWake();
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

// Chrome stops long utterances after about 15 seconds without firing onend, and can drop
// onend when the utterance object is garbage collected. So long replies are spoken a few
// sentences at a time, every utterance is kept referenced, and a watchdog notices when the
// engine has gone quiet even if no event says so.
const SPEAK_CHUNK_CHARS = 180;

function speechChunks(text) {
  const sentences = text.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [text];
  const chunks = [];
  let current = '';
  for (const sentence of sentences) {
    if (current && (current + sentence).length > SPEAK_CHUNK_CHARS) {
      chunks.push(current.trim());
      current = '';
    }
    current += sentence;
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks.length ? chunks : [text];
}

// Browsers ship several voices; the default one is often an old, robotic desktop voice.
// Unless the user picked one, Atlas uses the most natural female voice for their language:
// Edge's "Online (Natural)" voices first, then Google's network voices, then the rest.
const DEFAULT_SPEED = 1.12;
const FEMALE_VOICES = /\b(aria|jenny|ava|emma|michelle|sonia|libby|maisie|natasha|clara|neerja|ana|samantha|karen|moira|tessa|serena|victoria|zira|hazel|susan|heera|veena|fiona|allison|joanna|salli|kimberly|kendra|ivy|female|woman)\b/i;
const MALE_VOICES = /\b(david|mark|guy|andrew|brian|christopher|eric|roger|steffan|ryan|thomas|william|prabhat|ravi|daniel|alex|fred|george|james|male|man)\b/i;

function voiceScore(voice, lang) {
  const name = voice.name || '';
  const vlang = (voice.lang || '').replace('_', '-').toLowerCase();
  let score = 0;
  if (vlang === lang) score += 20;
  if (vlang.split('-')[0] === lang.split('-')[0]) score += 100;
  if (/natural|neural|online|premium|enhanced/i.test(name)) score += 60;
  if (/^google/i.test(name)) score += 35;
  if (FEMALE_VOICES.test(name)) score += 30;
  if (MALE_VOICES.test(name)) score -= 40;
  return score;
}

function availableVoices() {
  try {
    return (window.speechSynthesis && window.speechSynthesis.getVoices()) || [];
  } catch {
    return [];
  }
}

function userLang() {
  return (navigator.language || document.documentElement.lang || 'en').replace('_', '-').toLowerCase();
}

function bestVoice(voices) {
  const lang = userLang();
  let best = null;
  let bestScore = -Infinity;
  for (const voice of voices) {
    const score = voiceScore(voice, lang);
    if (score > bestScore) {
      best = voice;
      bestScore = score;
    }
  }
  return best;
}

function chosenVoice() {
  const voices = availableVoices();
  const saved = load(STORAGE.voice);
  return (saved && voices.find((v) => v.voiceURI === saved || v.name === saved)) || bestVoice(voices);
}

function voiceSpeed() {
  const speed = parseFloat(load(STORAGE.voiceSpeed));
  return speed > 0.5 && speed < 2 ? speed : DEFAULT_SPEED;
}

function syncVoiceSettings() {
  const select = $('voice-select');
  if (!select) return;
  const voices = availableVoices();
  const saved = load(STORAGE.voice) || '';
  const best = bestVoice(voices);
  const lang = userLang();
  const base = lang.split('-')[0];
  const listed = voices
    .filter((v) => (v.lang || '').toLowerCase().startsWith(base) || v.voiceURI === saved)
    .sort((a, b) => voiceScore(b, lang) - voiceScore(a, lang));
  select.replaceChildren();
  const auto = document.createElement('option');
  auto.value = '';
  auto.textContent = best ? `Best available (${best.name})` : 'Best available';
  select.append(auto);
  for (const voice of listed) {
    const option = document.createElement('option');
    option.value = voice.voiceURI || voice.name;
    option.textContent = voice.name;
    select.append(option);
  }
  select.value = listed.some((v) => (v.voiceURI || v.name) === saved) ? saved : '';
  const speed = $('voice-speed');
  const wanted = String(voiceSpeed());
  speed.value = [...speed.options].some((o) => o.value === wanted) ? wanted : String(DEFAULT_SPEED);
}

function onVoiceChange() {
  store(STORAGE.voice, $('voice-select').value || null);
  speak(`Hi, I'm ${state.name}. This is how I'll sound.`);
}

function onSpeedChange() {
  const value = $('voice-speed').value;
  store(STORAGE.voiceSpeed, value === String(DEFAULT_SPEED) ? null : value);
  speak(`Hi, I'm ${state.name}. This is how fast I'll talk.`);
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
    let quietTicks = 0;
    let watchdog = null;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(startTimer);
      clearInterval(watchdog);
      state.utterances = [];
      resolve(ok);
    };
    const chunks = speechChunks(text);
    const voice = chosenVoice();
    const rate = voiceSpeed();
    state.utterances = chunks.map((chunk, index) => {
      const utterance = new Utterance(chunk);
      if (voice) utterance.voice = voice;
      utterance.lang = (voice && voice.lang) || document.documentElement.lang || 'en';
      utterance.rate = rate;
      utterance.onstart = () => {
        if (!started) {
          started = true;
          // Only after speech has begun can "the engine is quiet" mean "finished".
          watchdog = setInterval(() => {
            quietTicks = synth.speaking || synth.pending ? 0 : quietTicks + 1;
            if (quietTicks >= 3) finish(true);
          }, 400);
        }
        if (token === state.speakToken) setOrb('speaking');
      };
      if (index === chunks.length - 1) utterance.onend = () => finish(true);
      utterance.onerror = (event) => {
        const err = event && event.error;
        // "interrupted"/"canceled" mean we stopped it ourselves: that is not a failure.
        finish(err === 'interrupted' || err === 'canceled');
      };
      return utterance;
    });
    // Engines that silently refuse (no user gesture) never fire onstart.
    const startTimer = setTimeout(() => {
      if (!started) {
        try { synth.cancel(); } catch { /* ignore */ }
        finish(false);
      }
    }, 4000);
    try {
      synth.cancel();
      for (const utterance of state.utterances) synth.speak(utterance);
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

// iPhones only let a page speak once speech has been started inside a tap (when the finger
// lifts, not when it lands). The reply comes
// seconds after the tap that asked for it, so the first tap also speaks a silent utterance,
// which unlocks speech for the rest of the visit.
let speechUnlocked = false;

function unlockSpeech() {
  if (speechUnlocked) return;
  speechUnlocked = true;
  try {
    const synth = window.speechSynthesis;
    const Utterance = window.SpeechSynthesisUtterance;
    if (!synth || !Utterance) return;
    const silent = new Utterance(' ');
    silent.volume = 0;
    synth.speak(silent);
  } catch {
    // Nothing to unlock on this browser.
  }
}

function showTapToHear(text, label = 'Tap to hear your brief') {
  const overlay = $('tap-to-hear');
  overlay.querySelector('.tap-text').textContent = label;
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
    if (state.followUp && state.recorder) state.discardRecording = !state.heardSpeech;
    state.followUp = false;
    stopListening();
    return;
  }
  if (state.orb === 'thinking') return;
  if (!requireAuth()) return;
  startListening();
}

function startListening() {
  stopWake();
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

// Listens for one request. Speech engines split a sentence into several results at every
// short pause; they are joined and sent only after END_PAUSE_MS of quiet (or a tap).
function startRecognition(Ctor) {
  const finals = {}; // result index -> final text (results accumulate within a session)
  let interim = '';
  let handled = false;
  let pauseTimer = null;
  const heard = () => [...Object.keys(finals).sort((a, b) => a - b).map((k) => finals[k]), interim]
    .join(' ').replace(/\s+/g, ' ').trim();
  const send = () => {
    const text = heard();
    if (handled || !text) return;
    handled = true;
    clearTimeout(pauseTimer);
    state.recognition = null;
    try { rec.stop(); } catch { /* ignore */ }
    sendText(text, { source: 'voice' });
  };
  const rec = new Ctor();
  rec.purpose = 'turn';
  state.recognition = rec;
  rec.lang = navigator.language || 'en-US';
  rec.interimResults = true;
  rec.continuous = true;
  rec.maxAlternatives = 1;
  rec.onstart = () => setOrb('listening');
  rec.onresult = (event) => {
    interim = '';
    for (let i = event.resultIndex || 0; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) finals[i] = result[0].transcript;
      else interim += ` ${result[0].transcript}`;
    }
    if (cluster) cluster.bump(0.3);
    setCaption(heard());
    const done = !interim.trim();
    showGotIt(done);
    clearTimeout(pauseTimer);
    pauseTimer = setTimeout(send, done ? QUICK_PAUSE_MS : END_PAUSE_MS);
  };
  rec.onerror = (event) => {
    const err = event && event.error;
    if (err === 'not-allowed' || err === 'service-not-allowed') {
      toast('Microphone access was blocked. Allow it in your browser settings or type instead.', { variant: 'error' });
    } else if (err === 'no-speech') {
      // Silence after a reply just means the conversation is over.
      if (!state.followUp) toast('I did not catch that. Tap and try again.');
    } else if (err && err !== 'aborted') {
      toast(`Speech recognition error: ${err}`, { variant: 'error' });
    }
  };
  rec.onend = () => {
    clearTimeout(pauseTimer);
    if (state.recognition === rec) state.recognition = null;
    // Stopped by a tap or by the engine: send whatever was heard.
    if (!handled && heard()) {
      send();
      return;
    }
    // Chrome gives up after several quiet seconds; while waiting for a reply, keep going
    // until the follow-up window is over.
    if (!handled && state.followUp && Date.now() < (state.followUpUntil || 0)) {
      startRecognition(Ctor);
      return;
    }
    if (!handled && state.orb === 'listening') {
      state.followUp = false;
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

// ---------------------------------------------------------------- the assistant's name

// Speech engines write a name several ways ("at last", "Atlus"), so compare loosely.
function similarity(a, b) {
  const rows = Array.from({ length: a.length + 1 }, (_, i) => [i]);
  for (let j = 1; j <= b.length; j++) rows[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      rows[i][j] = Math.min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
  }
  return 1 - rows[a.length][b.length] / Math.max(a.length, b.length, 1);
}

// What follows the name when the phrase starts with it ("Hey Atlas, brief me" -> "brief me"),
// or null when the name wasn't said.
function matchName(text, name) {
  const words = (text || '').toLowerCase().match(/[a-z0-9']+/g) || [];
  const target = ((name || '').toLowerCase().match(/[a-z0-9']+/g) || []).join('');
  if (!target) return null;
  for (let start = 0; start < Math.min(3, words.length); start++) {
    for (let span = 1; span <= 3; span++) {
      const heard = words.slice(start, start + span).join('');
      if (Math.abs(heard.length - target.length) > Math.max(1, Math.floor(target.length / 4))) continue;
      if (similarity(heard, target) >= 0.8) return words.slice(start + span).join(' ');
    }
  }
  return null;
}

// Listening for the name works where the browser recognises speech continuously (Chrome
// on Android and computers) and, on iPhone, by recording short clips for the server to
// transcribe. Either way only while the app is open on screen.
function wakeSupported() {
  if (isIos()) return Boolean(window.MediaRecorder && navigator.mediaDevices);
  return Boolean(getRecognitionCtor());
}

function wakeOn() {
  return wakeSupported() && load(STORAGE.wake) === '1' && Boolean(getToken());
}

function scheduleWake() {
  clearTimeout(state.wakeTimer);
  if (!wakeOn() || state.wakeRec) return;
  state.wakeTimer = setTimeout(startWake, 400);
}

function stopWake() {
  clearTimeout(state.wakeTimer);
  const rec = state.wakeRec;
  state.wakeRec = null;
  if (rec) {
    if (rec.cancelRequest) rec.cancelRequest(); // a tap or a hidden page drops a half-heard request
    try { rec.abort(); } catch { /* already stopped */ }
  }
}

function startWake() {
  if (!wakeOn() || state.wakeRec || state.orb !== 'idle' || document.visibilityState === 'hidden') return;
  if (isIos()) {
    startWakeClips();
    return;
  }
  const rec = new (getRecognitionCtor())();
  state.wakeRec = rec;
  rec.lang = navigator.language || 'en-US';
  rec.continuous = true;
  rec.interimResults = true;
  rec.purpose = 'wake';
  // Once the name is heard, the rest of the request is collected in this same session
  // (restarting would drop the words said straight after the name) until a pause.
  let start = -1; // index of the result that contained the name
  const parts = {};
  let pauseTimer = null;
  const request = () => Object.keys(parts).sort((a, b) => a - b).map((k) => parts[k])
    .join(' ').replace(/\s+/g, ' ').trim();
  rec.cancelRequest = () => clearTimeout(pauseTimer);
  const finish = () => {
    const text = request();
    stopWake();
    if (text) sendText(text, { source: 'voice' });
    else startListening(); // just the name: listen for the request
  };
  rec.onresult = (event) => {
    for (let i = event.resultIndex || 0; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript;
      if (start < 0 || i === start) {
        const rest = matchName(transcript, state.name);
        if (rest === null) continue;
        if (start < 0) {
          start = i;
          if (cluster) cluster.pulse(1.2);
          showHeardName();
        }
        parts[i] = rest;
      } else if (i > start) {
        parts[i] = transcript;
      }
    }
    if (start < 0) return;
    setCaption(request());
    const last = event.results[event.results.length - 1];
    const done = Boolean(last && last.isFinal) && Boolean(request());
    showGotIt(done);
    clearTimeout(pauseTimer);
    pauseTimer = setTimeout(finish, done ? QUICK_PAUSE_MS : END_PAUSE_MS);
  };
  rec.onerror = (event) => {
    if (event && (event.error === 'not-allowed' || event.error === 'service-not-allowed')) {
      store(STORAGE.wake, null);
      syncWakeToggle();
      toast(`Microphone access was blocked, so I can't listen for “${state.name}”.`, { variant: 'error' });
    }
  };
  rec.onend = () => {
    if (state.wakeRec !== rec) return;
    state.wakeRec = null;
    if (start >= 0) {
      // The engine ended mid-request: act on what was heard.
      clearTimeout(pauseTimer);
      finish();
      return;
    }
    scheduleWake(); // engines stop after a while; keep listening while the app is open
  };
  try {
    rec.start();
  } catch {
    state.wakeRec = null;
  }
}

// iPhone has no continuous speech recognition, so the microphone stays open while the app
// is on screen and every phrase (speech, then a short pause) is sent to the server. A phrase
// that starts with the name is acted on; anything else is dropped. Silence is never sent.
const WAKE_CLIP_MS = 10000;
const WAKE_PAUSE_MS = 900;

async function startWakeClips() {
  const wake = { stopped: false, stream: null, recorder: null };
  wake.abort = () => {
    wake.stopped = true;
    if (wake.recorder && wake.recorder.state !== 'inactive') wake.recorder.stop();
    if (wake.stream) wake.stream.getTracks().forEach((t) => t.stop());
  };
  state.wakeRec = wake;
  try {
    wake.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    if (state.wakeRec === wake) state.wakeRec = null;
    store(STORAGE.wake, null);
    syncWakeToggle();
    toast(`Microphone access was blocked, so I can't listen for “${state.name}”.`, { variant: 'error' });
    return;
  }
  if (wake.stopped) {
    wake.abort();
    return;
  }
  const mimeType = pickRecorderMime();
  const clip = () => {
    if (wake.stopped) return;
    const recorder = mimeType ? new MediaRecorder(wake.stream, { mimeType }) : new MediaRecorder(wake.stream);
    wake.recorder = recorder;
    const chunks = [];
    let spoke = false;
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size) chunks.push(event.data);
    };
    const stopWatching = watchSilence(wake.stream, () => {
      if (recorder.state !== 'inactive') recorder.stop();
    }, { pauseMs: WAKE_PAUSE_MS, onSpeech: () => { spoke = true; } });
    // Short clips, so a phrase is never far from the start of one and silence is cheap.
    const limit = setTimeout(() => {
      if (recorder.state !== 'inactive') recorder.stop();
    }, WAKE_CLIP_MS);
    recorder.onstop = async () => {
      clearTimeout(limit);
      stopWatching();
      if (wake.stopped) return;
      if (!spoke) {
        clip();
        return;
      }
      const type = recorder.mimeType || mimeType || 'audio/webm';
      let text = '';
      try {
        const form = new FormData();
        form.append('audio', new Blob(chunks, { type }), `speech.${type.includes('mp4') ? 'm4a' : 'webm'}`);
        const data = await api('/stt', { method: 'POST', form });
        text = (data && data.text) || '';
      } catch {
        // The server is unreachable or can't transcribe: keep listening.
      }
      if (wake.stopped) return;
      const rest = matchName(text, state.name);
      if (rest === null) {
        clip();
        return;
      }
      if (cluster) cluster.pulse(1.2);
      stopWake();
      if (rest) {
        setCaption(rest);
        sendText(rest, { source: 'voice' });
      } else {
        startListening(); // just the name: listen for the request
      }
    };
    recorder.start();
  };
  clip();
}

// Show at once that a finished phrase was heard, so the short wait before sending doesn't
// look like nothing is happening.
function showGotIt(done) {
  if (state.orb === 'thinking' || state.orb === 'speaking') return;
  let label = ORB_LABELS.listening;
  if (done) label = 'Got it…';
  else if (state.followUp) label = 'Listening… say “thanks” when you’re done';
  $('orb-label').textContent = label;
}

// The name was heard: look like listening without stopping the session that is
// still hearing the rest of the request.
function showHeardName() {
  const orb = $('orb');
  orb.dataset.state = 'listening';
  orb.setAttribute('aria-label', ORB_ARIA.listening);
  $('orb-label').textContent = ORB_LABELS.listening;
  if (cluster) cluster.setState('listening');
}

function syncWakeToggle() {
  const toggle = $('wake-toggle');
  toggle.checked = load(STORAGE.wake) === '1';
  toggle.disabled = !wakeSupported();
  $('wake-label').textContent = `Listen for “${state.name}” while the app is open`;
  $('wake-ios').hidden = !isIos();
  $('wake-unsupported').hidden = isIos() || wakeSupported();
  setOrb(state.orb);
}

function syncKeepToggle() {
  $('keep-toggle').checked = keepListeningOn();
}

function onKeepToggle(event) {
  store(STORAGE.keepListening, event.target.checked ? null : '0');
  syncKeepToggle();
}

function onWakeToggle(event) {
  store(STORAGE.wake, event.target.checked ? '1' : null);
  if (!event.target.checked) stopWake();
  syncWakeToggle();
}

function applyName(name) {
  state.name = (name || '').trim() || 'Atlas';
  store(STORAGE.name, state.name);
  renderTranscript();
  syncWakeToggle();
}

async function loadName() {
  try {
    const health = await api('/health');
    if (health && health.assistant_name) applyName(health.assistant_name);
  } catch {
    // Offline or an older server: keep the saved name.
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
  state.heardSpeech = false;
  const stopOnSilence = watchSilence(stream, () => {
    if (recorder.state !== 'inactive') recorder.stop();
  });
  const canHear = Boolean(state.micWatching);
  recorder.onstop = async () => {
    clearTimeout(state.recordTimer);
    clearTimeout(state.followUpTimer);
    stopOnSilence();
    state.recorder = null;
    stream.getTracks().forEach((t) => t.stop());
    if (state.discardRecording) {
      // Nobody spoke after the reply: end the conversation without uploading silence.
      state.discardRecording = false;
      state.followUp = false;
      setOrb('idle');
      setCaption('');
      return;
    }
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
  setCaption(state.followUp ? '' : 'Listening… tap the orb when you finish.');
  state.recordTimer = setTimeout(() => {
    if (recorder.state !== 'inactive') recorder.stop();
  }, RECORD_LIMIT_MS);
  // After a reply, give up quietly if nothing is said. Needs the level meter to know.
  if (state.followUp && canHear) {
    clearTimeout(state.followUpTimer);
    state.followUpTimer = setTimeout(() => {
      if (!state.heardSpeech && recorder.state !== 'inactive') {
        state.discardRecording = true;
        recorder.stop();
      }
    }, FOLLOW_UP_MS);
  }
}

// Calls onSilence once the speaker has talked and then gone quiet. Returns a cleanup function.
function watchSilence(stream, onSilence, { pauseMs = SILENCE_STOP_MS, onSpeech = null } = {}) {
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
    state.micLevel = level;
    const now = Date.now();
    if (level > SILENCE_LEVEL) {
      if (!heard && onSpeech) onSpeech();
      heard = true;
      state.heardSpeech = true;
      quietSince = 0;
    } else if (heard) {
      if (!quietSince) quietSince = now;
      else if (now - quietSince >= pauseMs) onSilence();
    }
  }, 100);
  if (cluster) cluster.setLevelSource(() => Math.min(1, state.micLevel * 8));
  state.micWatching = true;
  return () => {
    clearInterval(timer);
    state.micWatching = false;
    state.micLevel = 0;
    if (cluster) cluster.setLevelSource(null);
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
    h('span', { class: 'turn-who', text: t.role === 'user' ? 'You' : state.name }),
    h('span', { class: 'turn-text', text: t.text }))));
  $('transcript-empty').hidden = turns.length > 0;
}

async function converse(text) {
  const conversationId = sessionStorage.getItem(STORAGE.conversation) || null;
  const data = await api('/converse', { method: 'POST', body: { text, conversation_id: conversationId } });
  if (data && data.conversation_id) sessionStorage.setItem(STORAGE.conversation, data.conversation_id);
  return data;
}

function showReply(text) {
  const bubble = $('reply-bubble');
  bubble.textContent = text || '';
  bubble.hidden = !text;
}

function handleConverseResponse(data) {
  if (!data) return;
  if (data.reply) {
    addTurn('assistant', data.reply);
    showReply(data.reply);
    if (cluster) cluster.pulse(0.5);
  }
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
  promptConnect(Array.isArray(data.needs_account) ? data.needs_account : []);
  if (data.needs_confirmation) {
    showView('home');
    $('pending-section').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

// The assistant needs an account it can't reach yet: one tap asks for access.
function promptConnect(ids) {
  const box = $('connect-prompt');
  box.hidden = ids.length === 0;
  $('connect-buttons').replaceChildren(...ids.map((id) => {
    const name = ACCOUNT_NAMES[id] || id;
    return h('button', { type: 'button', class: 'btn btn-primary', 'data-connect': id, text: `Allow ${name}`, onclick: () => allowAccount(id) });
  }));
}

async function allowAccount(id) {
  try {
    state.accounts = await api('/accounts');
  } catch (err) {
    toast(err.message, { variant: 'error', title: 'Accounts' });
    return;
  }
  const account = (state.accounts.accounts || []).find((a) => a.id === id);
  if (!account) return;
  if (account.ready) {
    connectAccount(account);
    return;
  }
  showView('apps');
  renderAccounts();
  if (state.accounts.oauth_ready) openAppKeys(account.provider);
  toast(`${account.name} needs a one-time setup first. Follow the steps in Apps.`, { title: account.name });
}

// Sends a message (typed or spoken) and speaks the reply. Returns the speak result.
async function sendText(text, opts = {}) {
  const { speakReply = true } = opts;
  if (!text) return true;
  if (!requireAuth()) return true;
  stopSpeaking();
  state.followUp = false;
  addTurn('user', text);
  if (cluster) cluster.pulse(0.6);
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
  // A tap is enough to play it, even where the browser blocked speaking on its own.
  if (!ok && opts.source === 'voice') showTapToHear(data.reply, 'Tap to hear the answer');
  if (ok && opts.source === 'voice' && keepListeningOn() && !END_CONVERSATION.test(text)) {
    listenForFollowUp();
  }
  return ok;
}

// A spoken exchange carries on without the name: once the reply has been read out,
// listen again. Silence, "thanks" or a tap ends it and Atlas waits for its name again.
function keepListeningOn() {
  return load(STORAGE.keepListening) !== '0';
}

function listenForFollowUp() {
  if (document.visibilityState === 'hidden' || state.orb !== 'idle' || !getToken()) return;
  state.followUp = true;
  state.followUpUntil = Date.now() + FOLLOW_UP_MS;
  setTimeout(() => {
    // The reply may have been interrupted, or something else started meanwhile.
    if (!state.followUp || state.orb !== 'idle') return;
    startListening();
  }, 250);
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
  const open = state.followups.filter((f) => !['acknowledged', 'accepted', 'done', 'closed'].includes(f.state)).length;
  $('team-badge').hidden = open === 0;
  $('team-badge').textContent = String(open);
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

// ---------------------------------------------------------------- accounts

const PROVIDER_LABELS = { google: 'Google', microsoft: 'Microsoft' };

async function loadAccounts() {
  if (!getToken()) {
    $('accounts-list').replaceChildren(h('li', { class: 'hint', text: 'Connect this device first (Settings).' }));
    return;
  }
  try {
    state.accounts = await api('/accounts');
  } catch (err) {
    $('accounts-list').replaceChildren(h('li', { class: 'error', text: err.message }));
    return;
  }
  renderAccounts();
  renderSetup();
}

function appTile(id) {
  const [letter, color] = TILES[id] || [(id || '?')[0].toUpperCase(), '#5b6b8c'];
  return h('span', { class: 'app-tile', style: `--tile:${color}`, 'aria-hidden': 'true', text: letter });
}

function renderAccounts() {
  const data = state.accounts;
  if (!data) return;
  const note = $('accounts-note');
  note.hidden = true;
  if (!data.oauth_ready) {
    note.textContent = 'Accounts can\'t be connected yet: FERNET_KEY is missing in the server\'s .env file.';
    note.hidden = false;
  } else {
    // The sign-in page sends you back to the server's own address. If this app runs on a
    // different one (a phone through a tunnel), connect from the computer instead.
    const back = data.providers.length ? new URL(data.providers[0].redirect_uri) : null;
    if (back && back.origin !== location.origin) {
      note.textContent = `Sign-in returns to ${back.origin}. Connect from a browser on that computer; once connected, the account works everywhere.`;
      note.hidden = false;
    }
  }
  $('accounts-list').replaceChildren(...data.accounts.map((a) => {
    let button;
    if (a.connected) {
      button = h('button', { type: 'button', class: 'btn btn-ghost', text: 'Disconnect', onclick: () => disconnectAccount(a) });
    } else if (a.ready) {
      button = h('button', { type: 'button', class: 'btn btn-primary', text: 'Connect', onclick: () => connectAccount(a) });
    } else {
      button = h('button', { type: 'button', class: 'btn btn-ghost', text: 'Set up', disabled: !data.oauth_ready, onclick: () => openAppKeys(a.provider) });
    }
    button.setAttribute('aria-label', `${button.textContent} ${a.name}`);
    return h('li', { class: 'app-row account', 'data-account': a.id, 'data-connected': String(a.connected) },
      appTile(a.id),
      h('div', { class: 'account-text' },
        h('span', { class: 'account-name', text: a.name }),
        h('span', { class: 'account-desc', text: a.description }),
        h('span', { class: 'account-status', text: a.connected ? 'Connected' : (a.ready ? 'Not connected' : 'Needs one-time setup') })),
      button);
  }));
  syncAppKeysForm();
}

async function connectAccount(account) {
  try {
    const data = await api(`/accounts/${account.id}/connect`, { method: 'POST' });
    location.assign(data.authorization_url);
  } catch (err) {
    toast(err.message, { variant: 'error', title: account.name });
  }
}

async function disconnectAccount(account) {
  const shared = account.provider === 'microsoft' ? ' Outlook and Teams share one Microsoft sign-in, so both will disconnect.' : '';
  if (!window.confirm(`Disconnect ${account.name}?${shared}`)) return;
  try {
    await api(`/accounts/${account.id}`, { method: 'DELETE' });
    toast(`${account.name} disconnected.`);
    await loadAccounts();
  } catch (err) {
    toast(err.message, { variant: 'error', title: account.name });
  }
}

function openAppKeys(provider) {
  $('app-provider').value = provider;
  $('app-keys').open = true;
  syncAppKeysForm();
  $('app-client-id').focus();
}

const APP_KEY_GUIDES = {
  microsoft: {
    link: 'https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
    label: 'Open Microsoft Entra',
    steps: [
      'Open Microsoft Entra and choose New registration.',
      'Name it "Chief of Staff", choose a Web redirect and paste the redirect URI below.',
      'Under Certificates & secrets, create a client secret.',
      'Paste the Application (client) ID, the secret and the Directory (tenant) ID here.',
    ],
  },
  google: {
    link: 'https://console.cloud.google.com/apis/credentials',
    label: 'Open Google Cloud',
    steps: [
      'Open Google Cloud, create a project and enable the Gmail and Calendar APIs.',
      'Create an OAuth client ID of type Web application.',
      'Add the redirect URI below as an authorised redirect URI.',
      'Paste the client ID and client secret here.',
    ],
  },
};

function syncAppKeysForm() {
  const provider = $('app-provider').value;
  const guide = APP_KEY_GUIDES[provider];
  $('app-keys-steps').replaceChildren(...guide.steps.map((step) => h('li', { text: step })));
  $('app-keys-link').href = guide.link;
  $('app-keys-link').textContent = guide.label;
  const info = state.accounts && state.accounts.providers.find((p) => p.id === provider);
  $('redirect-uri').value = info ? info.redirect_uri : '';
  $('tenant-row').hidden = provider !== 'microsoft';
  if (provider === 'microsoft' && info && info.tenant_id) $('app-tenant').value = info.tenant_id;
  const label = PROVIDER_LABELS[provider];
  $('app-keys-state').textContent = info && info.configured
    ? `${label} keys are set${info.keys_source === 'env' ? ' in .env' : ''}. Saving new ones replaces them.`
    : '';
}

async function onSaveAppKeys(event) {
  event.preventDefault();
  const provider = $('app-provider').value;
  const body = {
    client_id: $('app-client-id').value.trim(),
    client_secret: $('app-client-secret').value.trim(),
  };
  if (provider === 'microsoft') body.tenant_id = $('app-tenant').value.trim() || null;
  if (!body.client_id || !body.client_secret) return;
  try {
    await api(`/accounts/apps/${provider}`, { method: 'PUT', body });
    $('app-client-secret').value = '';
    $('app-keys').open = false;
    toast(`${PROVIDER_LABELS[provider]} keys saved. Now tap Connect.`, { variant: 'ok', title: 'Saved' });
    await loadAccounts();
  } catch (err) {
    toast(err.message, { variant: 'error', title: 'Keys not saved' });
  }
}

async function copyRedirect() {
  const value = $('redirect-uri').value;
  try {
    await navigator.clipboard.writeText(value);
    toast('Redirect URI copied.');
  } catch {
    $('redirect-uri').select();
  }
}

// ---------------------------------------------------------------- views

function viewFromHash() {
  const name = location.hash.replace(/^#/, '');
  return VIEWS.includes(name) ? name : 'home';
}

function showView(name, { push = false } = {}) {
  if (!VIEWS.includes(name)) name = 'home';
  const changed = state.view !== name;
  state.view = name;
  document.querySelectorAll('.view[data-view]').forEach((el) => {
    el.hidden = el.dataset.view !== name;
  });
  document.querySelectorAll('.tab[data-tab]').forEach((b) => {
    if (b.dataset.tab === name) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  // Tabs get a history entry so the phone's back button goes back a tab.
  const hash = name === 'home' ? '' : `#${name}`;
  if (push && location.hash !== hash) {
    history.pushState(null, '', `${location.pathname}${location.search}${hash}`);
  }
  if (changed) window.scrollTo({ top: 0 });
  if (name === 'apps' && getToken()) {
    loadAccounts();
    loadConnections();
  }
}

// ---------------------------------------------------------------- apps (Claude, task boards, chat)

const GROUP_LISTS = { brain: 'apps-brain', tasks: 'apps-tasks', chat: 'apps-chat' };

async function loadConnections() {
  if (!getToken()) {
    state.connections = null;
    renderApps();
    return;
  }
  try {
    state.connections = await api('/connections');
  } catch (err) {
    // An older server without in-app connections: keep the rest of the app working.
    state.connections = err.status === 404 ? { ready: false, apps: [], missing: true } : state.connections;
  }
  renderApps();
  renderSetup();
}

function findApp(id) {
  return state.connections && (state.connections.apps || []).find((a) => a.id === id);
}

function renderApps() {
  const data = state.connections;
  for (const id of Object.values(GROUP_LISTS)) $(id).replaceChildren();
  const note = $('apps-note');
  note.hidden = true;
  if (data && !data.ready && !data.missing) {
    note.textContent = 'Apps can\'t be connected yet: FERNET_KEY is missing in the server\'s .env file. Run scripts/dev.sh once to create it.';
    note.hidden = false;
  }
  for (const app of (data && data.apps) || []) {
    const list = $(GROUP_LISTS[app.group] || 'apps-chat');
    const label = app.connected ? 'Manage' : 'Set up';
    list.append(h('li', { class: 'app-row', 'data-app': app.id, 'data-connected': String(app.connected) },
      appTile(app.id),
      h('div', { class: 'account-text' },
        h('span', { class: 'account-name', text: app.name }),
        h('span', { class: 'account-desc', text: app.description }),
        h('span', { class: 'account-status', text: app.connected ? (app.source === 'env' ? 'Connected (from .env)' : 'Connected') : 'Not connected' })),
      h('button', { type: 'button', class: `btn ${app.connected ? 'btn-ghost' : 'btn-primary'}`, 'aria-label': `${label} ${app.name}`, text: label, onclick: () => openAppSheet(app.id) })));
  }
  renderAppsProgress();
}

// The four things that make the assistant useful, in order of importance.
function setupStatus() {
  const accounts = (state.accounts && state.accounts.accounts) || [];
  const apps = (state.connections && state.connections.apps) || [];
  const any = (group) => apps.some((a) => a.group === group && a.connected);
  return {
    device: Boolean(getToken()),
    claude: apps.some((a) => a.id === 'claude' && a.connected),
    email: accounts.some((a) => a.connected),
    tasks: any('tasks'),
    chat: any('chat'),
  };
}

function renderAppsProgress() {
  const s = setupStatus();
  const steps = [s.claude, s.email, s.tasks, s.chat];
  const done = steps.filter(Boolean).length;
  const pct = Math.round((done / steps.length) * 100);
  const bar = $('apps-progress');
  bar.setAttribute('aria-valuenow', String(pct));
  bar.style.setProperty('--pct', `${pct}%`);
  $('apps-progress-label').textContent = !s.device
    ? 'Connect this device first (Settings).'
    : done === steps.length ? 'Everything is connected.' : `${done} of ${steps.length} connected`;
  $('apps-badge').hidden = !s.device || (s.claude && s.email);
}

function renderSetup() {
  const s = setupStatus();
  const known = !s.device || (state.connections && state.accounts);
  const steps = [
    { id: 'device', done: s.device, title: 'Connect this device', text: 'Enter the shared secret from the server.', go: openSettings },
    { id: 'claude', done: s.claude, title: 'Add Claude', text: 'Paste one API key. It powers the thinking.', go: () => openAppSheet('claude') },
    { id: 'email', done: s.email, title: 'Connect email and calendar', text: 'Google or Microsoft, with one sign-in.', go: () => showView('apps', { push: true }) },
    { id: 'tasks', done: s.tasks, title: 'Add a task board', text: 'Optional: Jira, Asana or ClickUp.', go: () => showView('apps', { push: true }) },
    { id: 'chat', done: s.chat, title: 'Add a chat app', text: 'Optional: Telegram, Slack or Teams.', go: () => showView('apps', { push: true }) },
  ];
  const card = $('setup-card');
  // Shown until the essentials are done; the optional steps never nag.
  card.hidden = !known || (s.device && s.claude && s.email);
  $('setup-progress').textContent = `${steps.filter((x) => x.done).length} of ${steps.length}`;
  $('setup-steps').replaceChildren(...steps.map((step, i) => h('li', { class: 'setup-step', 'data-step': step.id, 'data-done': String(step.done) },
    h('span', { class: 'setup-num', 'aria-hidden': 'true', text: step.done ? '✓' : String(i + 1) }),
    h('div', { class: 'setup-text' },
      h('span', { class: 'setup-title', text: step.title }),
      h('span', { class: 'setup-sub', text: step.done ? 'Done' : step.text })),
    step.done ? null : h('button', { type: 'button', class: 'btn btn-primary sm', 'aria-label': step.title, text: 'Start', onclick: step.go }))));
}

function openAppSheet(id) {
  if (!requireAuth()) return;
  const app = findApp(id);
  if (!app) {
    showView('apps', { push: true });
    return;
  }
  state.sheetApp = id;
  const tile = appTile(id);
  $('app-sheet-tile').replaceWith(Object.assign(tile, { id: 'app-sheet-tile' }));
  $('app-sheet-title').textContent = app.connected ? app.name : `Connect ${app.name}`;
  $('app-sheet-desc').textContent = app.description;
  $('app-sheet-steps').replaceChildren(...(app.steps || []).map((step) => h('li', { text: step })));
  const link = $('app-sheet-link');
  link.hidden = !app.link;
  link.href = app.link || '#';
  link.textContent = `${app.link_label || 'Open'} ↗`;
  $('app-sheet-fields').replaceChildren(...app.fields.map((f) => {
    const inputId = `app-field-${f.key}`;
    const keep = f.secret && f.filled;
    return h('div', { class: 'field' },
      h('label', { for: inputId }, f.label, f.optional ? h('span', { class: 'hint-inline', text: ' (optional)' }) : null),
      h('div', { class: 'input-group' },
        h('input', {
          id: inputId,
          name: f.key,
          type: f.secret ? 'password' : 'text',
          value: f.secret ? null : f.value,
          placeholder: keep ? 'Saved. Leave empty to keep it.' : f.placeholder,
          required: !f.optional && !f.filled,
          autocomplete: 'off',
          spellcheck: 'false',
          autocapitalize: 'off',
        })),
      f.help ? h('p', { class: 'hint', text: f.help }) : null);
  }));
  setSheetMessage('');
  $('app-sheet-connect').textContent = app.connected ? 'Save and check' : 'Connect';
  $('app-sheet-disconnect').hidden = !(app.connected && app.source === 'app');
  const dialog = $('app-sheet');
  if (!dialog.open) {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }
  const first = dialog.querySelector('input[required]') || dialog.querySelector('input');
  if (first) first.focus();
}

function setSheetMessage(text, cls = '') {
  const el = $('app-sheet-msg');
  el.textContent = text;
  el.className = `hint ${cls}`.trim();
}

async function onAppSheetSubmit(event) {
  event.preventDefault();
  const id = state.sheetApp;
  const app = findApp(id);
  if (!app) return;
  const values = {};
  for (const input of $('app-sheet-fields').querySelectorAll('input')) {
    if (input.value.trim()) values[input.name] = input.value.trim();
  }
  const btn = $('app-sheet-connect');
  btn.disabled = true;
  setSheetMessage('Checking…');
  if (cluster) cluster.setState('thinking');
  try {
    const res = await api(`/connections/${encodeURIComponent(id)}`, { method: 'PUT', body: { values } });
    setSheetMessage(res.message, 'ok');
    if (cluster) cluster.pulse(1);
    toast(res.message, { title: `${app.name} connected`, variant: 'ok' });
    await loadConnections();
    setTimeout(() => {
      if ($('app-sheet').open && state.sheetApp === id) $('app-sheet').close();
    }, 900);
  } catch (err) {
    setSheetMessage(err.message, 'warn');
  } finally {
    btn.disabled = false;
    if (cluster) cluster.setState(state.orb);
  }
}

async function onAppSheetDisconnect() {
  const id = state.sheetApp;
  const app = findApp(id);
  if (!app || !window.confirm(`Disconnect ${app.name}? Your assistant stops using it.`)) return;
  try {
    await api(`/connections/${encodeURIComponent(id)}`, { method: 'DELETE' });
    $('app-sheet').close();
    toast(`${app.name} disconnected.`);
    await loadConnections();
  } catch (err) {
    setSheetMessage(err.message, 'warn');
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
  state.accounts = null;
  state.connections = null;
  updateAuthUi();
  renderSetup();
  toast('Signed out on this device.');
}

async function refreshAll() {
  renderSetup();
  if (!getToken()) return;
  await Promise.all([loadBrief(), loadPending(), loadFollowups(), loadAccounts(), loadConnections()]);
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
  // Only these count as a tap for Safari; pointerdown and touchstart are too early.
  for (const type of ['touchend', 'click', 'keydown']) {
    document.addEventListener(type, unlockSpeech, { capture: true, passive: true });
  }
  // Touching the sphere sends ripples out from the exact point touched.
  $('orb').addEventListener('pointerdown', (event) => {
    if (cluster) cluster.touch(event.clientX, event.clientY);
  });
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
  $('app-keys-form').addEventListener('submit', onSaveAppKeys);
  $('app-provider').addEventListener('change', syncAppKeysForm);
  $('copy-redirect').addEventListener('click', copyRedirect);
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
    if (document.visibilityState === 'hidden') stopWake();
    else if (state.orb === 'idle') scheduleWake();
  });
  $('wake-toggle').addEventListener('change', onWakeToggle);
  $('keep-toggle').addEventListener('change', onKeepToggle);
  $('voice-select').addEventListener('change', onVoiceChange);
  $('voice-speed').addEventListener('change', onSpeedChange);
  $('voice-test').addEventListener('click', () => speak(`Hi, I'm ${state.name}. This is how I'll sound.`));
  // Chrome and Edge load their voice list after the page, and announce it with this event.
  if (window.speechSynthesis && typeof window.speechSynthesis.addEventListener === 'function') {
    window.speechSynthesis.addEventListener('voiceschanged', syncVoiceSettings);
  }
  document.querySelectorAll('.tab[data-tab]').forEach((b) => b.addEventListener('click', () => showView(b.dataset.tab, { push: true })));
  document.querySelectorAll('[data-goto]').forEach((b) => b.addEventListener('click', () => {
    if ($('settings').open) $('settings').close();
    showView(b.dataset.goto, { push: true });
  }));
  document.querySelectorAll('.chip[data-say]').forEach((b) => b.addEventListener('click', () => sendText(b.dataset.say, { source: 'text' })));
  window.addEventListener('hashchange', () => showView(viewFromHash()));
  $('app-sheet-form').addEventListener('submit', onAppSheetSubmit);
  $('app-sheet-disconnect').addEventListener('click', onAppSheetDisconnect);
  $('app-sheet').addEventListener('click', (event) => {
    if (event.target === $('app-sheet')) $('app-sheet').close();
  });
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 5) return 'Working late';
  if (hour < 12) return 'Good morning';
  if (hour < 17) return 'Good afternoon';
  return 'Good evening';
}

function init() {
  $('today-label').textContent = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  $('greeting').textContent = greeting();
  const reduced = Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  try {
    cluster = new StarCluster($('cluster'), { reducedMotion: reduced });
  } catch {
    cluster = null; // no canvas: the button still works
  }
  bindUi();
  showView(viewFromHash());
  state.name = load(STORAGE.name) || 'Atlas';
  syncWakeToggle();
  syncKeepToggle();
  syncVoiceSettings();
  loadName();
  syncKindButtons();
  setOrb('idle');
  renderTranscript();
  renderPending();
  renderFollowups();
  updateAuthUi();
  renderSetup();
  if (isIos() && !isStandalone()) {
    $('ios-hint').hidden = false;
    $('ios-banner').hidden = load(STORAGE.iosHintDismissed) === '1';
  }
  registerServiceWorker();
  refreshPushState();

  const params = new URLSearchParams(location.search);
  const autostart = params.get('autostart') === 'brief';
  // "Hey Siri, Atlas" opens /?autostart=listen: start listening straight away.
  const listenNow = params.get('autostart') === 'listen';
  // Back from a Google or Microsoft sign-in (see /oauth/<provider>/callback).
  const connected = params.get('connected');
  if (connected) {
    params.delete('connected');
    const label = PROVIDER_LABELS[connected] || 'Your account';
    toast(`${label} is connected. Your assistant will start using it within a few minutes.`, { title: 'Connected', variant: 'ok' });
    if (getToken()) showView('apps');
  }
  if (autostart || connected || listenNow) {
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
      if (listenNow) startListening();
    }
  } else if (autostart) {
    state.autostartPending = true;
    openSettings();
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
