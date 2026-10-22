// The assistant's face: a cluster of stars drawn on a canvas.
//
// Each star has a home on a fuzzy sphere and a lane in a spiral disc. The state decides
// how the cluster blends between those shapes and how it moves:
//   idle       a slowly turning, breathing cluster that twinkles
//   called     a bright flash and a shock ring (its name, a tap, a new message)
//   listening  the cluster opens up and trembles with your voice
//   thinking   the stars fall into a fast spiral, like a small galaxy
//   speaking   ripples run outward from the core in time with the voice
// With reduced motion, one still frame is drawn per state instead of an animation.

'use strict';

const TAU = Math.PI * 2;

// Colours per state: [core, accent]. The cluster eases between them.
const PALETTES = {
  idle: [[150, 196, 255], [196, 170, 255]],
  listening: [[110, 240, 255], [120, 170, 255]],
  thinking: [[176, 140, 255], [255, 140, 220]],
  speaking: [[255, 196, 120], [255, 120, 196]],
};

// Shape and motion targets per state.
const TARGETS = {
  idle: { disc: 0, spread: 1, spin: 0.12, swirl: 0, ripple: 0, jitter: 0, glow: 0.45 },
  listening: { disc: 0, spread: 1.12, spin: 0.35, swirl: 0, ripple: 0, jitter: 1, glow: 0.7 },
  thinking: { disc: 1, spread: 0.92, spin: 0.25, swirl: 1, ripple: 0, jitter: 0, glow: 0.65 },
  speaking: { disc: 0.15, spread: 1.04, spin: 0.2, swirl: 0, ripple: 1, jitter: 0, glow: 0.8 },
};

const lerp = (a, b, t) => a + (b - a) * t;

function gauss() {
  // Box-Muller; good enough for star placement.
  let u = 0;
  while (u === 0) u = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(TAU * Math.random());
}

function makeStars(count) {
  const stars = [];
  for (let i = 0; i < count; i++) {
    // Direction on the unit sphere.
    const z = Math.random() * 2 - 1;
    const a = Math.random() * TAU;
    const s = Math.sqrt(1 - z * z);
    // Denser towards the core, with a few stragglers outside.
    const r = Math.min(1.25, Math.abs(gauss()) * 0.42 + 0.08);
    // Spiral-disc lane: two arms with scatter.
    const arm = i % 2;
    const dr = Math.pow(Math.random(), 0.7);
    stars.push({
      x: Math.cos(a) * s * r,
      y: z * r,
      z: Math.sin(a) * s * r,
      r,
      dr: 0.1 + dr * 0.95,
      da: arm * Math.PI + dr * 4.2 + gauss() * 0.28,
      dy: gauss() * 0.04,
      size: 0.6 + Math.pow(Math.random(), 3) * 2.2,
      tint: Math.random(),
      phase: Math.random() * TAU,
      twinkle: 0.6 + Math.random() * 2.2,
    });
  }
  return stars;
}

export class StarCluster {
  constructor(canvas, { count = 220, reducedMotion = false } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.stars = makeStars(count);
    this.reduced = reducedMotion;
    this.state = 'idle';
    this.params = { ...TARGETS.idle };
    this.colors = PALETTES.idle.map((c) => c.slice());
    this.level = 0; // voice level 0..1 while listening or speaking
    this.levelTarget = 0;
    this.levelSource = null;
    this.flash = 0; // 0..1, decays
    this.rings = []; // shock rings: { t }
    this.angle = 0;
    this.swirlAngle = 0;
    this.time = 0;
    this.last = 0;
    this.frame = 0;
    this.sprites = new Map();
    this.running = false;
    canvas.dataset.motion = reducedMotion ? 'reduced' : 'full';
    canvas.dataset.state = 'idle';
    this.resize();
    if (typeof ResizeObserver === 'function') {
      this.observer = new ResizeObserver(() => this.resize());
      this.observer.observe(canvas);
    }
    this.onVisibility = () => (document.visibilityState === 'hidden' ? this.stop() : this.start());
    document.addEventListener('visibilitychange', this.onVisibility);
    this.start();
  }

  // ------------------------------------------------------------------ public api

  setState(next) {
    if (!TARGETS[next] || next === this.state) return;
    const was = this.state;
    this.state = next;
    this.canvas.dataset.state = next;
    if (next === 'listening' && was === 'idle') this.pulse();
    if (this.reduced) this.snap();
  }

  /** A bright flash and a ring: the name was heard, the button tapped, a message sent. */
  pulse(strength = 1) {
    this.flash = Math.min(1.4, this.flash + strength);
    this.rings.push({ t: 0, strength });
    if (this.rings.length > 4) this.rings.shift();
    this.canvas.dataset.pulses = String(Number(this.canvas.dataset.pulses || 0) + 1);
    if (this.reduced) this.draw();
  }

  /** Something was heard: a small kick proportional to how much changed. */
  bump(amount = 0.35) {
    this.levelTarget = Math.min(1, this.levelTarget + amount);
    this.flash = Math.min(1, this.flash + amount * 0.4);
  }

  /** Feed a live 0..1 level (microphone or audio output). Pass null to stop. */
  setLevelSource(fn) {
    this.levelSource = typeof fn === 'function' ? fn : null;
  }

  destroy() {
    this.stop();
    if (this.observer) this.observer.disconnect();
    document.removeEventListener('visibilitychange', this.onVisibility);
  }

  // ------------------------------------------------------------------ loop

  start() {
    if (this.reduced) {
      this.snap();
      return;
    }
    if (this.running) return;
    this.running = true;
    this.last = performance.now();
    const tick = (now) => {
      if (!this.running) return;
      const dt = Math.min(0.05, (now - this.last) / 1000);
      this.last = now;
      this.step(dt);
      this.draw();
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  stop() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    this.dpr = dpr;
    if (this.reduced || !this.running) this.draw();
  }

  // Jump straight to the state's look (reduced motion).
  snap() {
    Object.assign(this.params, TARGETS[this.state]);
    this.colors = PALETTES[this.state].map((c) => c.slice());
    this.level = this.state === 'listening' || this.state === 'speaking' ? 0.5 : 0;
    this.draw();
  }

  step(dt) {
    this.time += dt;
    this.frame += 1;
    const target = TARGETS[this.state];
    const ease = 1 - Math.exp(-dt * 3.2);
    for (const key of Object.keys(target)) this.params[key] = lerp(this.params[key], target[key], ease);
    const palette = PALETTES[this.state];
    for (let i = 0; i < 2; i++) {
      for (let c = 0; c < 3; c++) this.colors[i][c] = lerp(this.colors[i][c], palette[i][c], ease);
    }

    // Voice level: a live source if there is one, otherwise a believable rhythm.
    let live = null;
    if (this.levelSource) {
      try {
        live = this.levelSource();
      } catch {
        live = null;
      }
    }
    if (typeof live === 'number' && Number.isFinite(live)) {
      this.levelTarget = Math.max(this.levelTarget * 0.9, Math.min(1, live));
    } else if (this.state === 'speaking') {
      const syllables = 0.5 + 0.5 * Math.sin(this.time * 11) * Math.sin(this.time * 3.7 + 1);
      this.levelTarget = Math.max(this.levelTarget * 0.92, 0.25 + syllables * 0.6);
    } else if (this.state === 'listening') {
      this.levelTarget = Math.max(this.levelTarget * 0.94, 0.12 + 0.1 * Math.sin(this.time * 2.3));
    } else {
      this.levelTarget *= 0.9;
    }
    this.level = lerp(this.level, this.levelTarget, 1 - Math.exp(-dt * 12));

    this.flash *= Math.exp(-dt * 3.5);
    for (const ring of this.rings) ring.t += dt;
    this.rings = this.rings.filter((ring) => ring.t < 1.4);
    this.angle += dt * this.params.spin * (1 + this.level * 0.8);
    this.swirlAngle += dt * (0.6 + this.params.swirl * 1.8);
  }

  // ------------------------------------------------------------------ drawing

  sprite(rgb) {
    const key = rgb.map((v) => Math.round(v / 16)).join(',');
    let sprite = this.sprites.get(key);
    if (sprite) return sprite;
    if (this.sprites.size > 256) this.sprites.clear();
    const size = 64;
    sprite = document.createElement('canvas');
    sprite.width = size;
    sprite.height = size;
    const g = sprite.getContext('2d');
    const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    const [r, gr, b] = rgb.map((v) => Math.round(v));
    grad.addColorStop(0, 'rgba(255,255,255,1)');
    grad.addColorStop(0.18, `rgba(${r},${gr},${b},0.95)`);
    grad.addColorStop(0.45, `rgba(${r},${gr},${b},0.28)`);
    grad.addColorStop(1, `rgba(${r},${gr},${b},0)`);
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    this.sprites.set(key, sprite);
    return sprite;
  }

  draw() {
    const { ctx, canvas } = this;
    const w = canvas.width;
    const h = canvas.height;
    if (!ctx || !w || !h) return;
    const p = this.params;
    const [core, accent] = this.colors;
    const cx = w / 2;
    const cy = h / 2;
    const unit = Math.min(w, h) * 0.34;
    const breathe = this.reduced ? 1 : 1 + Math.sin(this.time * 1.4) * 0.025;
    const scale = unit * p.spread * breathe * (1 + this.flash * 0.18 + this.level * 0.12 * (1 - p.disc));

    ctx.clearRect(0, 0, w, h);
    ctx.globalCompositeOperation = 'lighter';

    // Soft nebula behind the stars.
    const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, unit * 1.45);
    const ga = Math.min(0.55, p.glow * 0.32 + this.flash * 0.25 + this.level * 0.15);
    glow.addColorStop(0, `rgba(${core.map(Math.round).join(',')},${ga})`);
    glow.addColorStop(0.45, `rgba(${accent.map(Math.round).join(',')},${ga * 0.35})`);
    glow.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);

    const cosA = Math.cos(this.angle);
    const sinA = Math.sin(this.angle);
    const tilt = 0.95;
    const cosT = Math.cos(tilt);
    const sinT = Math.sin(tilt);
    const t = this.time;
    const projected = [];

    for (const s of this.stars) {
      // Sphere home, rotated about the vertical axis.
      let x = s.x * cosA - s.z * sinA;
      let z = s.x * sinA + s.z * cosA;
      let y = s.y;

      // Spiral disc home: inner lanes turn faster, like a galaxy.
      if (p.disc > 0.001) {
        const omega = this.swirlAngle * (1.2 / Math.sqrt(s.dr + 0.15));
        const a = s.da + omega;
        const dx = Math.cos(a) * s.dr;
        const dz = Math.sin(a) * s.dr;
        // Tilt the disc towards the viewer.
        const ty = s.dy * cosT - dz * sinT;
        const tz = s.dy * sinT + dz * cosT;
        x = lerp(x, dx, p.disc);
        y = lerp(y, ty, p.disc);
        z = lerp(z, tz, p.disc);
      }

      // Ripples outward from the core while speaking.
      let push = 1;
      if (p.ripple > 0.001) push += p.ripple * this.level * 0.22 * Math.sin(t * 9 - s.r * 9);
      // Trembling with the voice while listening.
      if (p.jitter > 0.001) push += p.jitter * this.level * 0.3 * Math.sin(t * 13 + s.phase * 3);
      // A flash throws the stars outwards, more for the outer ones.
      push += this.flash * 0.25 * s.r;

      x *= push;
      y *= push;
      z *= push;

      const depth = 2.6 / (2.6 - z);
      projected.push({
        s,
        z,
        px: cx + x * scale * depth,
        py: cy + y * scale * depth,
        depth,
      });
    }

    projected.sort((a, b) => a.z - b.z);
    for (const q of projected) {
      const s = q.s;
      const tw = this.reduced ? 0.85 : 0.65 + 0.35 * Math.sin(t * s.twinkle + s.phase);
      const bright = Math.min(1, (0.35 + 0.55 * (q.depth - 0.6)) * tw + this.flash * 0.5 + this.level * 0.25);
      const rgb = [
        lerp(core[0], accent[0], s.tint),
        lerp(core[1], accent[1], s.tint),
        lerp(core[2], accent[2], s.tint),
      ];
      const size = s.size * q.depth * (this.dpr || 1) * (3.2 + this.flash * 2 + this.level * 1.5);
      ctx.globalAlpha = Math.max(0, Math.min(1, bright));
      ctx.drawImage(this.sprite(rgb), q.px - size, q.py - size, size * 2, size * 2);
    }

    // Shock rings.
    ctx.globalAlpha = 1;
    for (const ring of this.rings) {
      const k = ring.t / 1.4;
      ctx.strokeStyle = `rgba(${core.map(Math.round).join(',')},${(1 - k) * 0.55 * ring.strength})`;
      ctx.lineWidth = (1 - k) * 3 * (this.dpr || 1);
      ctx.beginPath();
      ctx.arc(cx, cy, unit * (0.35 + k * 1.1), 0, TAU);
      ctx.stroke();
    }
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
  }
}
