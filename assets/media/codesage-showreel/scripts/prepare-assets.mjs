// Original deterministic electronic score; no third-party audio samples.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const project = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repo = resolve(project, "../../..");
const spec = JSON.parse(
  readFileSync(resolve(project, "src/metrics.json"), "utf8"),
);
const source = JSON.parse(readFileSync(resolve(repo, spec.source), "utf8"));
const summary = source.summary;
for (const [key, metric] of [
  ["precision", "semantic_match_rate"],
  ["recall", "semantic_recall_rate"],
  ["f1", "semantic_f1"],
]) {
  if (Math.abs(summary[metric] * 100 - spec[key]) > 0.001)
    throw new Error(`Metric mismatch: ${key}`);
}

const rate = 48000;
const duration = 15;
const left = new Float64Array(rate * duration);
const right = new Float64Array(rate * duration);
let rng = 250929;
const noise = () => {
  rng ^= rng << 13;
  rng ^= rng >>> 17;
  rng ^= rng << 5;
  return ((rng >>> 0) / 4294967296) * 2 - 1;
};
const add = (start, length, fn, pan = 0) => {
  const count = Math.floor(length * rate);
  const offset = Math.floor(start * rate);
  for (let i = 0; i < count; i++) {
    const index = offset + i;
    if (index < 0 || index >= left.length) continue;
    const t = i / rate;
    const value = fn(t, i, length);
    left[index] += value * Math.sqrt((1 - pan) / 2);
    right[index] += value * Math.sqrt((1 + pan) / 2);
  }
};
const tau = 2 * Math.PI;
const kick = (start) =>
  add(
    start,
    0.34,
    (t) =>
      0.57 *
      Math.sin(tau * (43 * t + 7.2 * (1 - Math.exp(-t * 34)))) *
      Math.exp(-t * 14),
  );
for (let beat = 0; beat < 30; beat++) {
  kick(beat * 0.5);
  add(
    beat * 0.5 + 0.25,
    0.075,
    (t) => noise() * 0.045 * Math.exp(-t * 70),
    beat % 2 ? 0.4 : -0.4,
  );
  if (beat % 2)
    add(
      beat * 0.5,
      0.14,
      (t) =>
        (noise() * 0.16 + Math.sin(tau * 176 * t) * 0.06) * Math.exp(-t * 28),
    );
}
// D minor, alternating sub-bass and glassy sixteenth-note plucks.
const notes = [146.832, 220, 293.665, 349.228, 293.665, 220, 440, 349.228];
for (let n = 0; n < 60; n++) {
  const hz = notes[n % notes.length];
  add(
    n * 0.25,
    0.44,
    (t) =>
      0.095 *
      (Math.sin(tau * hz * t) + 0.25 * Math.sin(tau * hz * 2 * t)) *
      Math.exp(-t * 13) *
      Math.min(t * 350, 1),
    Math.sin(n * 1.7) * 0.65,
  );
  if (n % 4 === 0)
    add(
      n * 0.25,
      0.85,
      (t) =>
        0.15 *
        Math.sin(((tau * hz) / 4) * t) *
        Math.exp(-t * 4) *
        Math.min(t * 120, 1),
    );
}
for (const time of [1.5, 5.2, 8.8, 11.95]) {
  add(
    time - 0.24,
    0.29,
    (t, _i, len) =>
      noise() * 0.12 * Math.sin((Math.PI * t) / len) ** 2 * (t / len),
    -0.2,
  );
  add(
    time,
    0.52,
    (t) =>
      0.18 *
      Math.sin(tau * (75 * t + 5 * (1 - Math.exp(-t * 18)))) *
      Math.exp(-t * 7),
  );
  add(
    time + 0.03,
    0.48,
    (t) =>
      0.08 *
      (Math.sin(tau * 1174.66 * t) + 0.4 * Math.sin(tau * 1760 * t)) *
      Math.exp(-t * 9),
    0.4,
  );
}
for (const hz of [146.832, 220, 293.665, 349.228, 440]) {
  add(
    12,
    3,
    (t) =>
      0.043 *
      Math.sin(tau * hz * t) *
      (1 - Math.exp(-t * 12)) *
      Math.exp(-t * 0.9),
  );
}
let peak = 0;
for (let i = 0; i < left.length; i++) {
  const t = i / rate;
  const envelope = Math.min(1, t / 0.015, (duration - t) / 0.35);
  left[i] *= envelope;
  right[i] *= envelope;
  peak = Math.max(peak, Math.abs(left[i]), Math.abs(right[i]));
}
const pcm = Buffer.alloc(44 + left.length * 4);
pcm.write("RIFF", 0);
pcm.writeUInt32LE(pcm.length - 8, 4);
pcm.write("WAVE", 8);
pcm.write("fmt ", 12);
pcm.writeUInt32LE(16, 16);
pcm.writeUInt16LE(1, 20);
pcm.writeUInt16LE(2, 22);
pcm.writeUInt32LE(rate, 24);
pcm.writeUInt32LE(rate * 4, 28);
pcm.writeUInt16LE(4, 32);
pcm.writeUInt16LE(16, 34);
pcm.write("data", 36);
pcm.writeUInt32LE(pcm.length - 44, 40);
for (let i = 0; i < left.length; i++) {
  pcm.writeInt16LE(Math.round((left[i] / peak) * 0.86 * 32767), 44 + i * 4);
  pcm.writeInt16LE(Math.round((right[i] / peak) * 0.86 * 32767), 46 + i * 4);
}
mkdirSync(resolve(project, "public"), { recursive: true });
writeFileSync(resolve(project, "public/score.wav"), pcm);
console.log(
  "Verified all 3 AACR metrics; synthesized 15.000s stereo score (48 kHz).",
);
