import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import vm from "node:vm";
import ts from "typescript";

// Run the real controller and effects with a deterministic media clock. The
// normalizer's Web Audio graph is unrelated to tab recovery and is stubbed out.
function createHarness({ enabled = true, playing = true, waiting = false, availability = "unknown", sourceName = "bilibili", audioUrl = "https://audio.example.test/direct.mp4", audioResult = {} } = {}) {
  let now = 100_000;
  let cursor = 0;
  let dirty = false;
  let result;
  const slots = [];
  const pendingEffects = [];
  const intervals = new Map();
  let timerId = 0;
  let audioRequests = 0;
  let availabilityChanges = 0;
  let activeRoomId = 1;
  const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!slots[index]) slots[index] = { value: typeof initial === "function" ? initial() : initial };
      return [slots[index].value, (update) => {
        const value = typeof update === "function" ? update(slots[index].value) : update;
        if (!Object.is(value, slots[index].value)) { slots[index].value = value; dirty = true; }
      }];
    },
    useRef(value) {
      const index = cursor++;
      return slots[index] ??= { current: value };
    },
    useMemo(factory, deps) {
      const index = cursor++;
      if (!slots[index] || !sameDeps(slots[index].deps, deps)) slots[index] = { value: factory(), deps };
      return slots[index].value;
    },
    useCallback(callback, deps) { return react.useMemo(() => callback, deps); },
    useEffect(effect, deps) {
      const index = cursor++;
      if (slots[index] && sameDeps(slots[index].deps, deps)) return;
      pendingEffects.push(() => {
        slots[index]?.cleanup?.();
        slots[index] = { deps, cleanup: effect() };
      });
    },
  };
  class FakeAudio extends EventTarget {
    src = "";
    paused = true;
    ended = false;
    error = null;
    duration = 240;
    time = 0;
    calls = { load: 0, seek: 0, play: 0 };
    get currentTime() { return this.time; }
    set currentTime(value) { this.calls.seek++; this.time = value; }
    load() { this.calls.load++; this.paused = true; }
    play() { this.calls.play++; this.paused = false; return Promise.resolve(); }
    pause() { this.paused = true; }
    removeAttribute(name) { if (name === "src") this.src = ""; }
  }
  const window = Object.assign(new EventTarget(), {
    setInterval(callback) { const id = ++timerId; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout() { return ++timerId; },
    clearTimeout() {},
  });
  const document = Object.assign(new EventTarget(), { visibilityState: "visible" });
  const values = new Map([["playEnabled", enabled ? "1" : "0"]]);
  class Clock extends Date { static now() { return now; } }
  const context = vm.createContext({
    Date: Clock, URL, console, window, document, Audio: FakeAudio,
    location: { href: "http://localhost/", pathname: "/", origin: "http://localhost" },
    localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) },
  });
  const cache = new Map();
  const normalizer = { applyOutputVolume: () => false, unlockAudioGraph: () => Promise.resolve(), cleanupAudioGraph() {} };
  function load(file) {
    if (cache.has(file)) return cache.get(file);
    const source = ts.transpileModule(readFileSync(file, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    const module = { exports: {} };
    cache.set(file, module.exports);
    const require = name => {
      if (name === "react") return react;
      if (name === "./useDynamicAudioNormalizer") return { useDynamicAudioNormalizer: () => normalizer };
      if (name.endsWith("/api")) return { api: async path => {
        if (path.includes("/audio-url")) { audioRequests++; return typeof audioResult === "function" ? audioResult(path) : audioResult; }
        return {};
      }, withBase: path => path };
      return load(resolve(dirname(file), `${name}.ts`));
    };
    vm.runInContext(`(function(require,module,exports){${source}\n})`, context)(require, module, module.exports);
    return module.exports;
  }
  const { useAudioController } = load(resolve("src/hooks/useAudioController.ts"));
  function render() {
    let count = 0;
    do {
      assert.ok(count++ < 20, "controller must settle");
      dirty = false;
      cursor = 0;
      result = useAudioController(activeRoomId, "test-token", [], false, () => availabilityChanges++);
      while (pendingEffects.length) pendingEffects.shift()();
    } while (dirty);
  }
  render();
  result.applyPlaybackEnvelope({
    playback_state: { room_id: 1, current_queue_item_id: 10, mode: "play_enabled", is_playing: playing, position_ms: 10_000 },
    effective_position_ms: 10_000,
    current_track: { id: 1, source: sourceName, availability, source_track_id: "BVtest", duration_ms: 240_000, audio_url: audioUrl },
    loudness_waiting: waiting,
  });
  render();
  const audio = result.audio;
  audio.calls = { load: 0, seek: 0, play: 0 };
  return {
    audio,
    get controller() { return result; },
    switchRoom(id) { activeRoomId = id; render(); },
    updateTrack(changes) {
      result.applyPlaybackEnvelope({ playback_state: result.playback, current_track: { ...result.track, ...changes } });
      render();
    },
    get audioRequests() { return audioRequests; },
    get availabilityChanges() { return availabilityChanges; },
    async flush() { await new Promise(resolve => setImmediate(resolve)); render(); },
    hide() { document.visibilityState = "hidden"; document.dispatchEvent(new Event("visibilitychange")); },
    show() { document.visibilityState = "visible"; document.dispatchEvent(new Event("visibilitychange")); },
    online() { window.dispatchEvent(new Event("online")); },
    advance(ms, media = true) { now += ms; if (media) audio.time += ms / 1000; },
    tick() { for (const callback of intervals.values()) callback(); render(); },
  };
}

for (const hiddenMs of [500, 30_000]) {
  const h = createHarness();
  h.hide();
  h.advance(hiddenMs);
  h.show();
  h.tick();
  assert.equal(h.audio.calls.load, 0, "healthy tab return must not reload direct audio");
  assert.equal(h.audio.calls.seek, 0, "healthy tab return must preserve the current position");
}

{
  const h = createHarness();
  h.hide();
  h.advance(10_000, false);
  h.show();
  assert.equal(h.audio.calls.load, 1, "a frozen stream must recover when the tab returns");
  h.online();
  assert.equal(h.audio.calls.load, 1, "online immediately after visibility recovery must not reload twice");
}
{
  const h = createHarness();
  h.online();
  assert.equal(h.audio.calls.load, 1, "network reconnect must still recover the stream");
  h.show();
  assert.equal(h.audio.calls.load, 1, "visibility immediately after online must not reload twice");
}
{
  const h = createHarness();
  h.audio.error = { code: 2 };
  h.show();
  assert.equal(h.audio.calls.load, 1, "failed media must recover on return");
}
{
  const h = createHarness();
  h.hide();
  h.advance(4_000, false);
  h.show();
  assert.equal(h.audio.calls.seek, 1, "room drift above three seconds must still resync");
  assert.equal(h.audio.calls.load, 0, "ordinary drift correction must keep buffered audio");
}
{
  const h = createHarness();
  h.audio.paused = true;
  h.show();
  assert.equal(h.audio.calls.play, 1, "interrupted local playback must resume when the room is playing");
  assert.equal(h.audio.calls.load, 0, "a recent media pause does not require a reload");
}
for (const options of [{ enabled: false }, { playing: false }, { waiting: true }]) {
  const h = createHarness(options);
  h.hide();
  h.advance(30_000, false);
  h.show();
  h.online();
  assert.deepEqual(h.audio.calls, { load: 0, seek: 0, play: 0 }, "inactive or loudness-waiting playback must not touch media");
}
for (const sourceName of ["bilibili", "netease", "qq"]) {
  const h = createHarness({ availability: "unavailable", sourceName });
  h.tick();
  const initialPosition = h.controller.positionMs;
  assert.equal(h.audio.src, "", "known unavailable tracks must never load even a persisted or fallback URL");
  assert.match(h.controller.audioError, /失效/);
  h.advance(30_000, false);
  h.show();
  h.online();
  h.tick();
  assert.equal(h.controller.positionMs, initialPosition, "unavailable tracks must not show advancing progress");
  assert.deepEqual(h.audio.calls, { load: 0, seek: 0, play: 0 });
  assert.equal(h.audioRequests, 0, "known unavailable tracks must not enter a recovery loop");
}
{
  const h = createHarness({ audioResult: { availability: "unavailable", audio_url: null, unavailable_reason: "歌曲已下架" } });
  h.online();
  await h.flush();
  assert.equal(h.audio.src, "", "server-confirmed unavailable playback must unload immediately");
  assert.equal(h.controller.audioError, "歌曲已下架");
  assert.equal(h.availabilityChanges, 1, "new unavailable status must refresh playlist and queue views");
  h.audio.calls = { load: 0, seek: 0, play: 0 };
  h.advance(30_000, false);
  h.online();
  h.tick();
  assert.deepEqual(h.audio.calls, { load: 0, seek: 0, play: 0 });
  assert.equal(h.audioRequests, 1);
}
{
  const h = createHarness({ audioResult: { availability: "unknown", audio_url: null } });
  h.audio.error = { code: 2 };
  h.show();
  await h.flush();
  assert.equal(h.controller.track.availability, "unknown", "browser/network failure must never mark a track unavailable");
  assert.equal(h.availabilityChanges, 0);
}
for (const options of [{ enabled: false }, { waiting: true }]) {
  const h = createHarness(options);
  h.online();
  h.advance(30_000, false);
  h.tick();
  await h.flush();
  assert.equal(h.audioRequests, 0, "order-only and loudness-waiting modes must not resolve audio URLs");
}
{
  const replies = [];
  const h = createHarness({ audioUrl: null, audioResult: () => new Promise(resolve => replies.push(resolve)) });
  assert.equal(replies.length, 1);
  h.updateTrack({ audio_url: "https://audio.example.test/new.mp4" });
  h.online();
  assert.equal(replies.length, 2);
  replies[1]({ availability: "unavailable", audio_url: null });
  await h.flush();
  replies[0]({ availability: "available", audio_url: "https://audio.example.test/obsolete.mp4" });
  await h.flush();
  assert.equal(h.controller.track.availability, "unavailable", "late initial URL response must not overwrite newer forced verification");
  assert.equal(h.audio.src, "");
}
{
  let reply;
  const h = createHarness({ audioResult: () => new Promise(resolve => { reply = resolve; }) });
  h.online();
  h.switchRoom(2);
  reply({ availability: "unavailable", audio_url: null });
  await h.flush();
  assert.equal(h.controller.track.availability, "unknown", "old room resolution must not stop playback in a new room");
  assert.equal(h.availabilityChanges, 0);
}
console.log("audio recovery and availability tests passed");
