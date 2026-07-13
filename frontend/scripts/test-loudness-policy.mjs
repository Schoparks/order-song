import assert from "node:assert/strict";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/hooks/loudnessPolicy.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false,
});
const source = result.outputFiles[0].text;
const policy = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

for (const sourceName of ["bilibili", "netease", "qq"]) {
  const track = {
    source: sourceName,
    loudness_source: `${sourceName}:ffmpeg-ebur128`,
    loudness_gain_db: -3,
  };
  assert.equal(policy.hasUsableBackendLoudness(track), true);
  assert.equal(policy.needsBackendLoudness(track), false);
  assert.equal(
    policy.hasUsableBackendLoudness({ ...track, loudness_source: "wrong:ffmpeg-ebur128" }),
    false,
  );
}

const actions = [];
const controls = {
  clearPlayRetry: () => actions.push("clear-play"),
  clearStreamRetry: () => actions.push("clear-stream"),
  pause: () => actions.push("pause"),
};
assert.equal(policy.blockAudioForLoudnessWait(false, controls), false);
assert.deepEqual(actions, []);
assert.equal(policy.blockAudioForLoudnessWait(true, controls), true);
assert.deepEqual(actions, ["clear-play", "clear-stream", "pause"]);

console.log("loudness policy tests passed");
