import type { Track } from "../types";

export function escapeText(value: unknown): string {
  return String(value ?? "");
}

export function trackKey(track: Pick<Track, "source" | "source_track_id">): string {
  return `${track.source}:${track.source_track_id}`;
}

export function trackPayload(track: Track): Omit<Track, "id" | "audio_url"> & { audio_url?: string | null } {
  return {
    source: track.source,
    source_track_id: track.source_track_id,
    title: track.title,
    artist: track.artist,
    duration_ms: track.duration_ms,
    cover_url: track.cover_url,
    audio_url: track.audio_url,
  };
}
