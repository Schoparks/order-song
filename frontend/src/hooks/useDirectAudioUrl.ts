import { useCallback, useRef, type Dispatch, type SetStateAction } from "react";
import { api } from "../lib/api";
import type { Track } from "../types";

interface Options {
  roomId: number | null;
  track: Track | null;
  token: string | null;
  enabled: boolean;
  waiting: boolean;
  setTrack: Dispatch<SetStateAction<Track | null>>;
  setAudioError: Dispatch<SetStateAction<string | null>>;
  stopLocalAudio: () => void;
  onAvailabilityChanged?: () => void;
}

type AudioUrlResult = Pick<Track, "audio_url" | "availability" | "unavailable_reason" | "availability_checked_at"
  | "loudness_gain_db" | "loudness_peak" | "loudness_source" | "loudness_error">;

export function useDirectAudioUrl(options: Options) {
  const current = useRef(options);
  current.current = options;
  const generation = useRef(0);
  const inFlight = useRef<{ key: string; request: Promise<string | null> } | null>(null);

  return useCallback((force = false): Promise<string | null> => {
    const initial = current.current;
    const { track, token, enabled, waiting, roomId } = initial;
    if (!enabled || waiting || !token || !track?.id || track.availability === "unavailable") return Promise.resolve(null);
    const key = `${roomId}:${token}:${track.id}:${force}`;
    if (inFlight.current?.key === key) return inFlight.current.request;
    const requestGeneration = ++generation.current;
    const stillCurrent = () => {
      const latest = current.current;
      return generation.current === requestGeneration && latest.roomId === roomId
        && latest.enabled && !latest.waiting && latest.token === token && latest.track?.id === track.id;
    };
    const request = (async () => {
      try {
        const result = await api<AudioUrlResult>(`/api/tracks/${track.id}/audio-url${force ? "?force=true" : ""}`, { token });
        if (!stillCurrent()) return null;
        const latest = current.current;
        const changes = Object.fromEntries(Object.entries(result).filter(([name]) => [
          "audio_url", "availability", "unavailable_reason", "availability_checked_at",
          "loudness_gain_db", "loudness_peak", "loudness_source", "loudness_error",
        ].includes(name))) as AudioUrlResult;
        latest.setTrack(previous => previous && previous.id === track.id ? { ...previous, ...changes } : previous);
        if (result.availability === "unavailable") {
          latest.stopLocalAudio();
          latest.setAudioError(result.unavailable_reason || "歌曲已失效，请重新搜索替换或移出所有歌单");
        } else if (result.audio_url) {
          latest.setAudioError(null);
        } else if (Object.prototype.hasOwnProperty.call(result, "audio_url")) {
          latest.setAudioError("歌曲播放地址暂不可用，请稍后重试");
        }
        if (result.availability && result.availability !== track.availability) latest.onAvailabilityChanged?.();
        return result.availability === "unavailable" ? null : result.audio_url || null;
      } catch (error) {
        if (stillCurrent()) current.current.setAudioError("播放地址刷新失败，请稍后重试");
        throw error;
      }
    })();
    inFlight.current = { key, request };
    void request.finally(() => {
      if (inFlight.current?.request === request) inFlight.current = null;
    }).catch(() => {});
    return request;
  }, []);
}
