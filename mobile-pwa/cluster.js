// The assistant's face: a sphere of stars with no edge that behaves like a drop of water.
//
// About 1,200 stars sit in a 3D Gaussian cloud: dense at the core, thinning out into space,
// so there is no outline. Disturbances land on the drop's surface and ripples travel around it:
//   idle       the drop turns and breathes; now and then a faint drip lands
//   called     the whole drop wobbles once and flashes (its name, a tap, a new message)
//   listening  every syllable you say lands somewhere new and the ripples interfere
//   thinking   soft drops land in a ring that circles the sphere
//   speaking   the assistant's syllables land as warm ripples
// Touching the sphere sends ripples out from the exact point touched.
// With reduced motion, one still frame is drawn per state instead of an animation.

'use strict';

const TAU = Math.PI * 2;
const COUNT = 1200;
const MAX_IMPACTS = 12;

// Colours per state: [core, edge, ripple]. The drop eases between them.
const PALETTES = {
  idle: [[232, 240, 255], [140, 168, 255], [255, 236, 210]],
  listening: [[220, 252, 255], [110, 210, 255], [200, 255, 255]],
  thinking: [[238, 228, 255], [160, 130, 255], [255, 190, 240]],
  speaking: [[255, 244, 228], [255, 170, 150], [255, 214, 160]],
};

// Size, spin and glow per state.
const TARGETS = {
  idle: { spread: 1, spin: 0.12, glow: 0.18 },
  listening: { spread: 1.08, spin: 0.2, glow: 0.3 },
  thinking: { spread: 0.96, spin: 0.32, glow: 0.26 },
  speaking: { spread: 1.04, spin: 0.18, glow: 0.34 },
};

const lerp = (a, b, t) => a + (b - a) * t;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function gauss() {
  let u = 0;
  while (u === 0) u = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(TAU * Math.random());
}

function randomDir() {
  const z = Math.random() * 2 - 1;
  const a = Math.random() * TAU;
  const s = Math.sqrt(1 - z * z);
  return [Math.cos(a) * s, z, Math.sin(a) * s];
}

export class StarCluster {
  constructor(canvas, { reducedMotion = false } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.reduced = reducedMotion;
    this.state = 'idle';
    this.params = { ...TARGETS.idle };
    this.colors = PALETTES.idle.map((c) => c.slice());
    this.impacts = []; // { p: [x,y,z], t0, s }
    this.callT0 = -99;
    this.time = 0;
    this.angle = Math.random() * TAU;
    this.tilt = 0.32;
    this.level = 0;
    this.levelTarget = 0;
    this.levelSource = null;
    this.rising = false;
    this.lastPeak = 0;
    this.nextDrip = 2;
    this.ringAngle = 0;
    this.running = false;
    this.makeStars();
    this.sprite = this.makeSprite();
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

  makeStars() {
    const n = COUNT;
    this.hx = new Float32Array(n);
    this.hy = new Float32Array(n);
    this.hz = new Float32Array(n);
    this.dx = new Float32Array(n);
    this.dy = new Float32Array(n);
    this.dz = new Float32Array(n);
    this.r = new Float32Array(n);
    this.size = new Float32Array(n);
    this.phase = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      // A 3D Gaussian: dense core, stars thinning out forever, no edge.
      const x = gauss() * 0.4;
      const y = gauss() * 0.4;
      const z = gauss() * 0.4;
      const r = Math.hypot(x, y, z) || 0.001;
      this.hx[i] = x;
      this.hy[i] = y;
      this.hz[i] = z;
      this.r[i] = r;
      this.dx[i] = x / r;
      this.dy[i] = y / r;
      this.dz[i] = z / r;
      this.size[i] = Math.random() < 0.06 ? 2 + Math.random() * 1.6 : 0.7 + Math.random() * 0.9;
      this.phase[i] = Math.random() * TAU;
    }
  }

  makeSprite() {
    const c = document.createElement('canvas');
    c.width = 32;
    c.height = 32;
    const g = c.getContext('2d');
    const grad = g.createRadialGradient(16, 16, 0, 16, 16, 16);
    grad.addColorStop(0, 'rgba(255,255,255,1)');
    grad.addColorStop(0.25, 'rgba(210,222,255,0.7)');
    grad.addColorStop(1, 'rgba(170,190,255,0)');
    g.fillStyle = grad;
    g.fillRect(0, 0, 32, 32);
    return c;
  }

  // ------------------------------------------------------------------ public api

  setState(next) {
    if (!TARGETS[next] || next === this.state) return;
    const was = this.state;
    this.state = next;
    this.canvas.dataset.state = next;
    if (next === 'listening' && was === 'idle') this.pulse(0.8);
    if (this.reduced) this.snap();
  }

  /** The whole drop wobbles once and flashes: the name was heard, a tap, a message sent. */
  pulse(strength = 1) {
    this.callT0 = this.time;
    this.callStrength = clamp(strength, 0.3, 1.4);
    // A drop lands on the side facing you, too.
    this.addImpact(this.facingDir(), 0.6 * this.callStrength);
    this.canvas.dataset.pulses = String(Number(this.canvas.dataset.pulses || 0) + 1);
    if (this.reduced) this.draw();
  }

  /** Something was heard (a recognised word): a ripple lands somewhere new. */
  bump(amount = 0.35) {
    this.levelTarget = Math.min(1, this.levelTarget + amount);
    this.addImpact(randomDir(), clamp(amount * 1.2, 0.2, 0.7));
  }

  /** Ripples from a point on the canvas, in CSS pixels relative to the canvas. */
  touch(clientX, clientY, strength = 0.9) {
    const rect = this.canvas.getBoundingClientRect();
    const unit = Math.min(rect.width, rect.height) * 0.36;
    let nx = (clientX - rect.left - rect.width / 2) / unit;
    let ny = (clientY - rect.top - rect.height / 2) / unit;
    const len = Math.hypot(nx, ny);
    if (len > 0.98) {
      nx *= 0.98 / len;
      ny *= 0.98 / len;
    }
    const nz = Math.sqrt(Math.max(0, 1 - nx * nx - ny * ny));
    this.addImpact(this.unproject(nx, ny, nz), strength);
    this.canvas.dataset.touches = String(Number(this.canvas.dataset.touches || 0) + 1);
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

  // ------------------------------------------------------------------ helpers

  addImpact(p, s) {
    this.impacts.push({ p, t0: this.time, s });
    if (this.impacts.length > MAX_IMPACTS) this.impacts.shift();
    if (this.reduced) this.draw();
  }

  // Undo the tilt, then the spin, to turn a view direction into one on the drop.
  unproject(nx, ny, nz) {
    const ct = Math.cos(this.tilt);
    const st = Math.sin(this.tilt);
    const y1 = ny * ct + nz * st;
    const z1 = -ny * st + nz * ct;
    const ca = Math.cos(this.angle);
    const sa = Math.sin(this.angle);
    return [nx * ca + z1 * sa, y1, -nx * sa + z1 * ca];
  }

  facingDir() {
    return this.unproject((Math.random() - 0.5) * 0.6, (Math.random() - 0.5) * 0.6, 0.85);
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
    this.draw();
  }

  step(dt) {
    this.time += dt;
    const t = this.time;
    const target = TARGETS[this.state];
    const ease = 1 - Math.exp(-dt * 3);
    for (const key of Object.keys(target)) this.params[key] = lerp(this.params[key], target[key], ease);
    const palette = PALETTES[this.state];
    for (let i = 0; i < 3; i++) {
      for (let c = 0; c < 3; c++) this.colors[i][c] = lerp(this.colors[i][c], palette[i][c], ease);
    }

    // Voice level: a live source when there is one, otherwise a believable rhythm while speaking.
    let live = null;
    if (this.levelSource) {
      try {
        live = this.levelSource();
      } catch {
        live = null;
      }
    }
    if (typeof live === 'number' && Number.isFinite(live)) {
      this.levelTarget = clamp(live, 0, 1);
    } else if (this.state === 'speaking') {
      const s = Math.max(0, Math.sin(t * 9.3) * 0.65 + Math.sin(t * 3.4 + 1.3) * 0.45 + 0.15);
      this.levelTarget = Math.min(1, s * 0.85);
    } else {
      this.levelTarget *= Math.exp(-dt * 4);
    }
    this.level = lerp(this.level, this.levelTarget, 1 - Math.exp(-dt * 14));

    // The top of each syllable lands as a drop somewhere new.
    const up = this.levelTarget > this.level + 0.02;
    if (this.rising && !up && this.level > 0.16 && t - this.lastPeak > 0.16
      && (this.state === 'listening' || this.state === 'speaking')) {
      this.lastPeak = t;
      this.addImpact(randomDir(), clamp(this.level * 1.1, 0.3, 1));
    }
    this.rising = up;

    if (this.state === 'thinking') {
      // Soft drops land in a ring that circles the drop.
      this.ringAngle += dt * 2.4;
      if (t - this.lastPeak > 0.28) {
        this.lastPeak = t;
        const a = this.ringAngle;
        this.addImpact([Math.cos(a) * 0.94, Math.sin(a * 0.5) * 0.34, Math.sin(a) * 0.94], 0.35);
      }
    } else if (this.state === 'idle' && t > this.nextDrip) {
      // Now and then a faint drip, so the drop never looks frozen.
      this.addImpact(randomDir(), 0.22);
      this.nextDrip = t + 3.5 + Math.random() * 3;
    }

    this.impacts = this.impacts.filter((im) => t - im.t0 < 3.5);
    this.angle += dt * this.params.spin * (1 + this.level * 0.6);
  }

  // ------------------------------------------------------------------ drawing

  draw() {
    const { ctx, canvas } = this;
    const w = canvas.width;
    const h = canvas.height;
    if (!ctx || !w || !h) return;
    const t = this.time;
    const p = this.params;
    const [core, edge, ripple] = this.colors;
    const cx = w / 2;
    const cy = h / 2;
    const unit = Math.min(w, h) * 0.36;
    const dpr = this.dpr || 1;

    // Name called: the drop expands, overshoots and settles.
    const ct = t - this.callT0;
    const callK = ct < 3 ? Math.exp(-ct * 2.6) * (this.callStrength || 1) : 0;
    const wobble = 1 + 0.22 * callK * Math.sin(ct * 9);
    const flash = ct < 3 ? Math.exp(-ct * 3) * (this.callStrength || 1) : 0;
    const breathe = this.reduced ? 1 : 1 + 0.02 * Math.sin(t * 1.3);
    const scale = unit * p.spread * wobble * breathe;

    ctx.globalCompositeOperation = 'source-over';
    ctx.clearRect(0, 0, w, h);
    ctx.globalCompositeOperation = 'lighter';

    // Soft glow behind the drop.
    const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, unit * 1.25);
    const ga = Math.min(0.6, p.glow + this.level * 0.2 + flash * 0.3);
    glow.addColorStop(0, `rgba(${edge.map(Math.round).join(',')},${ga})`);
    glow.addColorStop(0.5, `rgba(${edge.map(Math.round).join(',')},${ga * 0.25})`);
    glow.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);

    // Eight colour steps from the core outward, plus the ripple colour.
    const shades = [];
    for (let k = 0; k < 8; k++) {
      const f = k / 7;
      shades.push(`rgb(${Math.round(lerp(core[0], edge[0], f))},${Math.round(lerp(core[1], edge[1], f))},${Math.round(lerp(core[2], edge[2], f))})`);
    }
    const rippleColor = `rgb(${ripple.map(Math.round).join(',')})`;

    const ca = Math.cos(this.angle);
    const sa = Math.sin(this.angle);
    const ctl = Math.cos(this.tilt);
    const stl = Math.sin(this.tilt);
    const impacts = this.impacts;
    const ni = impacts.length;

    for (let i = 0; i < COUNT; i++) {
      const dx = this.dx[i];
      const dy = this.dy[i];
      const dz = this.dz[i];
      const r = this.r[i];
      const outer = Math.min(1, r * 1.6); // the surface moves, the core stays calm
      let push = 0;
      let lit = 0;

      for (let k = 0; k < ni; k++) {
        const im = impacts[k];
        const et = t - im.t0;
        const q = im.p;
        const th = Math.acos(clamp(dx * q[0] + dy * q[1] + dz * q[2], -1, 1));
        const front = et * 1.7;
        if (th > front + 0.5) continue;
        const f = im.s * 0.22 * Math.exp(-et * 1.2) * Math.sin(th * 8 - et * 13)
          * Math.exp(-Math.max(0, th - front) * 7) * outer;
        push += f;
        lit += Math.abs(f) * 4;
      }
      push += callK * 0.18 * r * Math.sin(ct * 9);

      const s = 1 + push / Math.max(r, 0.05);
      const x = this.hx[i] * s;
      const y = this.hy[i] * s;
      const z = this.hz[i] * s;

      // Spin, tilt, perspective.
      const x1 = x * ca - z * sa;
      const z1 = x * sa + z * ca;
      const y2 = y * ctl - z1 * stl;
      const z2 = y * stl + z1 * ctl;
      const depth = 3 / (3 - z2);
      const px = cx + x1 * scale * depth;
      const py = cy + y2 * scale * depth;

      const tw = this.reduced ? 0.9 : 0.75 + 0.25 * Math.sin(t * 2.1 + this.phase[i]);
      const coreK = Math.max(0, 1 - r * 0.9);
      const alpha = clamp((0.18 + coreK * 0.55) * (0.55 + (depth - 0.75) * 0.9) * tw
        + lit * 0.5 + flash * 0.3 + this.level * 0.1, 0.03, 1);
      const sz = this.size[i] * dpr * depth * (1 + lit * 0.6 + flash * 0.4) * 1.15;
      ctx.globalAlpha = alpha;
      if (this.size[i] > 1.9) {
        ctx.drawImage(this.sprite, px - sz * 2.2, py - sz * 2.2, sz * 4.4, sz * 4.4);
      } else {
        ctx.fillStyle = lit > 0.35 ? rippleColor : shades[Math.min(7, Math.floor(r * 7))];
        ctx.fillRect(px - sz / 2, py - sz / 2, sz, sz);
      }
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
  }
}
