import type { Track, TrackSource } from "../types";

const BACKEND_LOUDNESS_SOURCES: ReadonlySet<TrackSource> = new Set([
  "bilibili",
  "netease",
  "qq",
]);

export function expectedBackendLoudnessSource(source: TrackSource): string | null {
  return BACKEND_LOUDNESS_SOURCES.has(source) ? `${source}:ffmpeg-ebur128` : null;
}

export function usesBackendLoudness(track: Track | null): boolean {
  return track != null && expectedBackendLoudnessSource(track.source) != null;
}

export function hasUsableBackendLoudness(track: Track | null): boolean {
  if (!track) return false;
  const expectedSource = expectedBackendLoudnessSource(track.source);
  if (!expectedSource || track.loudness_source !== expectedSource) return false;
  return track.loudness_gain_db != null && Number.isFinite(Number(track.loudness_gain_db));
}

export function needsBackendLoudness(track: Track | null): boolean {
  return usesBackendLoudness(track) && !hasUsableBackendLoudness(track);
}

export function shouldBlockAudioForLoudnessWait(loudnessWaiting: boolean): boolean {
  return loudnessWaiting;
}

interface LoudnessWaitAudioControls {
  clearPlayRetry: () => void;
  clearStreamRetry: () => void;
  pause: () => void;
}

export function blockAudioForLoudnessWait(
  loudnessWaiting: boolean,
  controls: LoudnessWaitAudioControls,
): boolean {
  if (!shouldBlockAudioForLoudnessWait(loudnessWaiting)) return false;
  controls.clearPlayRetry();
  controls.clearStreamRetry();
  controls.pause();
  return true;
}
