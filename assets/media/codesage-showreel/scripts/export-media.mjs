import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const project = resolve(dirname(fileURLToPath(import.meta.url)), "..");
// The bundled FFmpeg supports palette filters but intentionally omits `fps`.
// Apply output -r instead, and cap the GIF at precisely 225 frames.
const packageName =
  process.platform === "win32"
    ? "@remotion/compositor-win32-x64-msvc"
    : process.platform === "darwin"
      ? `@remotion/compositor-darwin-${process.arch}`
      : `@remotion/compositor-linux-${process.arch}-gnu`;
const binaries = dirname(require.resolve(packageName));
const executable = (name) =>
  resolve(binaries, name + (process.platform === "win32" ? ".exe" : ""));
const ffmpeg = (args) => {
  const result = spawnSync(
    executable("ffmpeg"),
    ["-y", "-v", "warning", ...args],
    { cwd: project, stdio: "inherit", windowsHide: true },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`FFmpeg failed: ${result.status}`);
};
ffmpeg([
  "-i",
  "out/render.mp4",
  "-t",
  "15",
  "-c:v",
  "copy",
  "-c:a",
  "aac",
  "-b:a",
  "192k",
  "-movflags",
  "+faststart",
  "../codesage-showreel.mp4",
]);
ffmpeg([
  "-i",
  "../codesage-showreel.mp4",
  "-vf",
  "scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3",
  "-r",
  "15",
  "-frames:v",
  "225",
  "-loop",
  "0",
  "../codesage-showreel.gif",
]);
const probe = spawnSync(
  executable("ffprobe"),
  [
    "-v",
    "error",
    "-show_entries",
    "format=duration,size:stream=codec_name,width,height,r_frame_rate,nb_frames",
    "-of",
    "json",
    "../codesage-showreel.mp4",
  ],
  { cwd: project, encoding: "utf8", windowsHide: true },
);
if (probe.error) throw probe.error;
if (probe.status !== 0) throw new Error(probe.stderr);
const data = JSON.parse(probe.stdout);
const video = data.streams.find((s) => s.codec_name === "h264");
if (
  !video ||
  video.width !== 1920 ||
  video.height !== 1080 ||
  video.nb_frames !== "900" ||
  video.r_frame_rate !== "60/1" ||
  Number(data.format.duration) !== 15
) {
  throw new Error(
    "Output does not satisfy the 1080p / 60fps / 15-second contract",
  );
}
console.log(
  "Verified: 1920×1080, 60fps, 900 frames, 15.000s. MP4 + looping GIF exported.",
);
