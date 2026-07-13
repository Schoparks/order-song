import assert from "node:assert/strict";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/hooks/progressSeek.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false,
});
const source = result.outputFiles[0].text;
const { createProgressSeekCoordinator } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

function createHarness() {
  const scheduled = [];
  const seeking = [];
  const previews = [];
  const commits = [];
  const coordinator = createProgressSeekCoordinator({
    schedule: (callback) => {
      scheduled.push(callback);
      return () => {
        const index = scheduled.indexOf(callback);
        if (index >= 0) scheduled.splice(index, 1);
      };
    },
    onSeekingChange: (value) => seeking.push(value),
    onPreview: (ratio) => previews.push(ratio),
    onCommit: (ratio) => commits.push(ratio),
  });
  return {
    coordinator,
    commits,
    previews,
    seeking,
    flush: () => {
      while (scheduled.length) scheduled.shift()();
    },
  };
}

{
  const harness = createHarness();
  harness.coordinator.begin(0.1);
  harness.coordinator.finish(0.1);
  harness.coordinator.preview(0.7);
  harness.flush();

  assert.deepEqual(harness.commits, [0.7], "late input must replace the pointerup value");
  assert.equal(harness.coordinator.isSeeking(), false);
  assert.equal(harness.seeking.at(-1), false);
}

{
  const harness = createHarness();
  harness.coordinator.begin(0.1);
  harness.coordinator.preview(0.4);
  harness.coordinator.preview(0.7);
  harness.coordinator.finish();
  harness.flush();

  assert.deepEqual(harness.commits, [0.7], "outside release must commit the latest preview");
  assert.equal(harness.coordinator.isSeeking(), false);
  assert.equal(harness.seeking.at(-1), false);
}

{
  const harness = createHarness();
  harness.coordinator.begin(0.2);
  harness.coordinator.preview(0.8);
  harness.coordinator.finish(0.8);
  harness.coordinator.finish(0.8);
  harness.coordinator.finish();
  harness.flush();

  assert.deepEqual(harness.commits, [0.8], "duplicate release paths must commit once");
  assert.equal(harness.coordinator.isSeeking(), false);
}

for (const endGesture of [
  (coordinator) => coordinator.cancel(),
  (coordinator) => coordinator.dispose(),
]) {
  const harness = createHarness();
  harness.coordinator.begin(0.3);
  harness.coordinator.preview(0.6);
  harness.coordinator.finish();
  endGesture(harness.coordinator);
  harness.flush();

  assert.deepEqual(harness.commits, [], "cancel and unmount must not commit a stale gesture");
  assert.equal(harness.coordinator.isSeeking(), false);
  assert.equal(harness.seeking.at(-1), false);
}

console.log("progress seek tests passed");
