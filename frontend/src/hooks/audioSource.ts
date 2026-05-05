import type { Track } from "../types";

export function playableAudioUrl(track: Track): string | null {
  if (track.audio_url) return track.audio_url;
  if (track.source === "netease" && track.source_track_id) {
    return `https://music.163.com/song/media/outer/url?id=${encodeURIComponent(track.source_track_id)}.mp3`;
  }
  return null;
}

export function canUseWebAudioForSource(sourceUrl: string | null): boolean {
  if (!sourceUrl) return false;
  try {
    const url = new URL(sourceUrl, location.href);
    return url.origin === location.origin || url.protocol === "blob:" || url.protocol === "data:";
  } catch {
    return false;
  }
}
