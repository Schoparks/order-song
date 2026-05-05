import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, withBase } from "../lib/api";
import { clamp, formatTime, parsePlaybackTime } from "../lib/time";
import { readStoredBoolean, readStoredNumber, writeStoredBoolean } from "../lib/storage";
import { playableAudioUrl } from "./audioSource";
import { useDynamicAudioNormalizer } from "./useDynamicAudioNormalizer";
import type { PlaybackEnvelope, PlaybackState, QueueItem, Track } from "../types";

const SEEK_TOLERANCE_MS = 3000;
const NEXT_DEBOUNCE_MS = 2500;
const STREAM_RETRY_LIMIT = 2;
const STALLED_AUDIO_RELOAD_MS = 8000;
const STREAM_RELOAD_COOLDOWN_MS = 10000;
const LOCAL_NEXT_GRACE_MS = 1800;
const LOCAL_NEXT_SYNC_WAIT_MS = 120000;
const METADATA_MIN_GAIN = 0.35;
const METADATA_MAX_GAIN = 2.5;

type NormalizerState = "off" | "active" | "metadata" | "bypassed";

interface SyncAnchor {
  serverTsMs: number;
  effectivePositionMs: number;
}

function roomPositionFromState(pb: PlaybackState | null, anchor: SyncAnchor | null): number {
  if (!pb) return 0;
  if (anchor) {
    return pb.is_playing
      ? Math.max(0, anchor.effectivePositionMs + Date.now() - anchor.serverTsMs)
      : Math.max(0, anchor.effectivePositionMs);
  }
  let position = Number(pb.position_ms || 0);
  if (pb.is_playing && pb.updated_at) {
    const updatedAt = parsePlaybackTime(pb.updated_at);
    if (Number.isFinite(updatedAt)) position += Math.max(0, Date.now() - updatedAt);
  }
  return Math.max(0, position);
}

function metadataGainFromTrack(track: Track | null): number | null {
  if (track?.loudness_gain_db == null || !track.loudness_source) return null;
  const gainDb = Number(track.loudness_gain_db);
  if (!Number.isFinite(gainDb)) return null;
  return clamp(10 ** (gainDb / 20), METADATA_MIN_GAIN, METADATA_MAX_GAIN);
}

export function useAudioController(roomId: number | null, token: string | null, fallbackQueue: QueueItem[] = []) {
  const audio = useMemo(() => {
    const element = new Audio();
    element.preload = "none";
    return element;
  }, []);
  const [playEnabled, setPlayEnabledState] = useState(() => readStoredBoolean("playEnabled"));
  const [normalizerEnabled, setNormalizerEnabledState] = useState(() => readStoredBoolean("volumeNormalizer"));
  const [volume, setVolumeState] = useState(() => readStoredNumber("volume", 50));
  const [previousVolume, setPreviousVolume] = useState(() => readStoredNumber("previousVolume", 50, 1, 100));
  const [playback, setPlayback] = useState<PlaybackState | null>(null);
  const [track, setTrack] = useState<Track | null>(null);
  const [orderedBy, setOrderedBy] = useState<{ id: number; username: string } | null>(null);
  const [positionMs, setPositionMs] = useState(0);
  const [durationMs, setDurationMs] = useState(0);
  const [isSeeking, setIsSeeking] = useState(false);
  const [normalizerState, setNormalizerState] = useState<NormalizerState>("off");
  const hasTrack = track != null;

  const volumeRef = useRef(volume);
  const anchorRef = useRef<SyncAnchor | null>(null);
  const lastNextAtRef = useRef(0);
  const playRetryTimerRef = useRef<number | null>(null);
  const streamRetryTimerRef = useRef<number | null>(null);
  const optimisticNextTimerRef = useRef<number | null>(null);
  const pendingServerAdvanceRef = useRef<{ expectedQueueItemId: number | null; requestedAt: number } | null>(null);
  const fallbackQueueRef = useRef<QueueItem[]>(fallbackQueue);
  const refreshedAudioTrackRef = useRef<string | null>(null);
  const streamRetryCountRef = useRef(0);
  const pendingSeekSecondsRef = useRef<number | null>(null);
  const lastAudioProgressAtRef = useRef(Date.now());
  const lastAudioTimeRef = useRef(0);
  const lastStreamReloadAtRef = useRef(0);
  const currentQueueItemRef = useRef<number | null>(null);
  const syncAudioToRoomRef = useRef<(force?: boolean, shouldPlay?: boolean) => void>(() => {});
  const {
    applyOutputVolume: applyDynamicOutputVolume,
    unlockAudioGraph,
    cleanupAudioGraph,
  } = useDynamicAudioNormalizer(audio);

  const getDurationMs = useCallback(() => {
    if (audio.duration && Number.isFinite(audio.duration) && audio.duration > 0) {
      return audio.duration * 1000;
    }
    return Number(track?.duration_ms || 0);
  }, [audio, track]);

  const getRoomPositionMs = useCallback(() => {
    const duration = getDurationMs();
    const raw = roomPositionFromState(playback, anchorRef.current);
    return duration > 0 ? clamp(raw, 0, duration) : raw;
  }, [getDurationMs, playback]);

  const normalizerAudioUrl = useMemo(
    () => (track ? playableAudioUrl(track) : null),
    [track?.audio_url, track?.id, track?.source, track?.source_track_id],
  );

  const applyOutputVolume = useCallback((enabled = normalizerEnabled, nextVolume = volume, sourceUrl = normalizerAudioUrl) => {
    const active = applyDynamicOutputVolume(enabled, nextVolume, sourceUrl);
    if (!enabled) {
      setNormalizerState("off");
      return active;
    }
    if (active) {
      setNormalizerState("active");
      return true;
    }
    const metadataGain = sourceUrl ? metadataGainFromTrack(track) : null;
    if (metadataGain != null) {
      applyDynamicOutputVolume(false, clamp(nextVolume * metadataGain, 0, 100), sourceUrl);
      setNormalizerState("metadata");
      return true;
    }
    setNormalizerState("bypassed");
    return active;
  }, [applyDynamicOutputVolume, normalizerAudioUrl, normalizerEnabled, track, volume]);

  const clearPlayRetry = useCallback(() => {
    if (playRetryTimerRef.current != null) {
      window.clearTimeout(playRetryTimerRef.current);
      playRetryTimerRef.current = null;
    }
  }, []);

  const clearStreamRetry = useCallback(() => {
    if (streamRetryTimerRef.current != null) {
      window.clearTimeout(streamRetryTimerRef.current);
      streamRetryTimerRef.current = null;
    }
  }, []);

  const clearOptimisticNext = useCallback(() => {
    if (optimisticNextTimerRef.current != null) {
      window.clearTimeout(optimisticNextTimerRef.current);
      optimisticNextTimerRef.current = null;
    }
    pendingServerAdvanceRef.current = null;
  }, []);

  const stopLocalAudio = useCallback(() => {
    clearPlayRetry();
    clearStreamRetry();
    audio.pause();
    cleanupAudioGraph();
    if (audio.src) {
      audio.removeAttribute("src");
      try {
        audio.load();
      } catch {
        // Removing src is enough; load() only cancels pending fetches where supported.
      }
    }
    audio.volume = clamp(volumeRef.current, 0, 100) / 100;
    currentQueueItemRef.current = null;
    refreshedAudioTrackRef.current = null;
    pendingSeekSecondsRef.current = null;
    streamRetryCountRef.current = 0;
    lastAudioProgressAtRef.current = Date.now();
    lastAudioTimeRef.current = 0;
  }, [audio, cleanupAudioGraph, clearPlayRetry, clearStreamRetry]);

  const seekAudioTo = useCallback((seconds: number) => {
    if (!audio.src) {
      pendingSeekSecondsRef.current = null;
      return;
    }
    const safeSeconds = Math.max(0, seconds);
    pendingSeekSecondsRef.current = safeSeconds;
    try {
      audio.currentTime = safeSeconds;
      pendingSeekSecondsRef.current = null;
    } catch {
      // Some streams reject seeks until metadata/canplay fires.
    }
  }, [audio]);

  const applyPendingSeek = useCallback(() => {
    const seconds = pendingSeekSecondsRef.current;
    if (seconds == null) return;
    try {
      audio.currentTime = seconds;
      pendingSeekSecondsRef.current = null;
    } catch {
      // Keep the pending seek for the next media readiness event.
    }
  }, [audio]);

  const requestAudioPlay = useCallback((retries = 2) => {
    if (!audio.src) return;
    clearPlayRetry();
    const attempt = (remaining: number) => {
      audio.play()
        .then(() => {
          clearPlayRetry();
        })
        .catch(() => {
          if (remaining <= 0) return;
          playRetryTimerRef.current = window.setTimeout(() => attempt(remaining - 1), 350);
        });
    };
    attempt(retries);
  }, [audio, clearPlayRetry]);

  const reloadCurrentStream = useCallback((retries = 1, ignoreCooldown = false) => {
    if (!playEnabled || !audio.src) return;
    const now = Date.now();
    if (!ignoreCooldown && now - lastStreamReloadAtRef.current < STREAM_RELOAD_COOLDOWN_MS) return;
    lastStreamReloadAtRef.current = now;
    lastAudioProgressAtRef.current = now;
    const targetMs = getRoomPositionMs();
    if (Number.isFinite(targetMs)) pendingSeekSecondsRef.current = Math.max(0, targetMs / 1000);
    clearStreamRetry();
    try {
      audio.load();
    } catch {
      // Reload is best-effort; play retries below will handle the recoverable cases.
    }
    requestAudioPlay(retries);
  }, [audio, clearStreamRetry, getRoomPositionMs, playEnabled, requestAudioPlay]);

  const unlockAudio = useCallback((force = false) => {
    if (!force && !playEnabled) return;
    applyOutputVolume(normalizerEnabled);
    unlockAudioGraph().catch(() => {});
    requestAudioPlay(1);
  }, [applyOutputVolume, normalizerEnabled, playEnabled, requestAudioPlay, unlockAudioGraph]);

  const setPlayEnabled = useCallback((value: boolean) => {
    setPlayEnabledState(value);
    writeStoredBoolean("playEnabled", value);
    if (value) {
      unlockAudio(true);
      syncAudioToRoomRef.current(true, true);
      requestAudioPlay(3);
    } else {
      stopLocalAudio();
    }
  }, [requestAudioPlay, stopLocalAudio, unlockAudio]);

  const setNormalizerEnabled = useCallback((value: boolean) => {
    setNormalizerEnabledState(value);
    writeStoredBoolean("volumeNormalizer", value);
    if (playEnabled) {
      applyOutputVolume(value);
    } else {
      setNormalizerState(value ? "bypassed" : "off");
    }
  }, [applyOutputVolume, playEnabled]);

  const setVolume = useCallback((value: number) => {
    const safe = clamp(value, 0, 100);
    setVolumeState(safe);
    localStorage.setItem("volume", String(safe));
    if (safe > 0) {
      setPreviousVolume(safe);
      localStorage.setItem("previousVolume", String(safe));
    }
    if (playEnabled) {
      applyOutputVolume(normalizerEnabled, safe);
    } else {
      audio.volume = safe / 100;
    }
  }, [applyOutputVolume, audio, normalizerEnabled, playEnabled]);

  const toggleMute = useCallback(() => {
    setVolume(volume > 0 ? 0 : previousVolume || 50);
  }, [previousVolume, setVolume, volume]);

  const syncAudioToRoom = useCallback((force = false, shouldPlay = playEnabled) => {
    const audioUrl = normalizerAudioUrl;
    if (!playEnabled) {
      stopLocalAudio();
      return;
    }
    if (!playback || !audioUrl) {
      stopLocalAudio();
      return;
    }
    if (currentQueueItemRef.current !== playback.current_queue_item_id) {
      currentQueueItemRef.current = playback.current_queue_item_id;
      streamRetryCountRef.current = 0;
      pendingSeekSecondsRef.current = null;
      lastAudioProgressAtRef.current = Date.now();
      lastAudioTimeRef.current = 0;
      force = true;
    }
    const nextSrc = withBase(audioUrl);
    if (audio.src !== new URL(nextSrc, location.href).href) {
      audio.src = nextSrc;
      audio.load();
      streamRetryCountRef.current = 0;
      pendingSeekSecondsRef.current = null;
      lastAudioProgressAtRef.current = Date.now();
      lastAudioTimeRef.current = 0;
      force = true;
    }

    const duration = getDurationMs();
    const targetMs = roomPositionFromState(playback, anchorRef.current);
    if (shouldPlay && Number.isFinite(targetMs) && targetMs >= 0) {
      const boundedMs = duration > 1000 ? clamp(targetMs, 0, duration - 500) : targetMs;
      const diffMs = Math.abs((audio.currentTime || 0) * 1000 - boundedMs);
      if (force || diffMs > SEEK_TOLERANCE_MS) {
        seekAudioTo(boundedMs / 1000);
      }
    }

    if (!shouldPlay) {
      clearPlayRetry();
      audio.pause();
      return;
    }
    if (playback.is_playing) {
      applyOutputVolume(normalizerEnabled);
      requestAudioPlay();
    } else {
      clearPlayRetry();
      audio.pause();
    }
  }, [applyOutputVolume, audio, clearPlayRetry, clearStreamRetry, getDurationMs, normalizerAudioUrl, normalizerEnabled, playEnabled, playback, requestAudioPlay, seekAudioTo, stopLocalAudio, track]);

  syncAudioToRoomRef.current = syncAudioToRoom;

  useEffect(() => {
    volumeRef.current = volume;
  }, [volume]);

  useEffect(() => {
    fallbackQueueRef.current = fallbackQueue;
  }, [fallbackQueue]);

  const refreshDirectAudioUrl = useCallback(async (force = false) => {
    if (!playEnabled || !token || !track?.id) return null;
    const query = force ? "?force=true" : "";
    const result = await api<{
      track_id: number;
      audio_url?: string | null;
      loudness_gain_db?: number | null;
      loudness_peak?: number | null;
      loudness_source?: string | null;
      loudness_error?: string | null;
    }>(`/api/tracks/${track.id}/audio-url${query}`, { token });
    const nextUrl = result.audio_url || null;
    if (nextUrl || result.loudness_gain_db != null || result.loudness_peak != null || result.loudness_source || result.loudness_error) {
      setTrack((previous) => (
        previous && previous.id === track.id
          ? {
              ...previous,
              audio_url: nextUrl || previous.audio_url,
              loudness_gain_db: result.loudness_gain_db ?? previous.loudness_gain_db,
              loudness_peak: result.loudness_peak ?? previous.loudness_peak,
              loudness_source: result.loudness_source ?? previous.loudness_source,
              loudness_error: result.loudness_error ?? previous.loudness_error,
            }
          : previous
      ));
    }
    return nextUrl;
  }, [playEnabled, token, track?.id]);

  const fallbackNextQueueItem = useCallback((currentQueueItemId: number | null) => {
    const queue = fallbackQueueRef.current;
    if (!queue.length) return null;
    const startIndex = currentQueueItemId == null ? -1 : queue.findIndex((item) => item.id === currentQueueItemId);
    const candidates = startIndex >= 0 ? queue.slice(startIndex + 1) : queue;
    return candidates.find((item) => item.status === "queued") || null;
  }, []);

  const applyLocalQueueItem = useCallback((item: QueueItem) => {
    const now = Date.now();
    anchorRef.current = { serverTsMs: now, effectivePositionMs: 0 };
    setPlayback((previous) => ({
      room_id: previous?.room_id ?? roomId ?? 0,
      mode: previous?.mode ?? "play_enabled",
      current_queue_item_id: item.id,
      is_playing: true,
      position_ms: 0,
      volume: previous?.volume ?? 50,
      updated_at: new Date(now).toISOString(),
    }));
    setTrack(item.track);
    setOrderedBy(item.ordered_by);
    setPositionMs(0);
    setDurationMs(Number(item.track.duration_ms || 0));
  }, [roomId]);

  const scheduleOptimisticNext = useCallback((expectedQueueItemId: number | null) => {
    if (optimisticNextTimerRef.current != null) window.clearTimeout(optimisticNextTimerRef.current);
    pendingServerAdvanceRef.current = { expectedQueueItemId, requestedAt: Date.now() };
    optimisticNextTimerRef.current = window.setTimeout(() => {
      optimisticNextTimerRef.current = null;
      const pending = pendingServerAdvanceRef.current;
      if (!pending) return;
      const item = fallbackNextQueueItem(pending.expectedQueueItemId);
      if (item) applyLocalQueueItem(item);
    }, LOCAL_NEXT_GRACE_MS);
  }, [applyLocalQueueItem, fallbackNextQueueItem]);

  const applyPlaybackEnvelope = useCallback((envelope: PlaybackEnvelope) => {
    const pending = pendingServerAdvanceRef.current;
    if (
      pending &&
      pending.expectedQueueItemId !== null &&
      envelope.playback_state.current_queue_item_id === pending.expectedQueueItemId &&
      Date.now() - pending.requestedAt < LOCAL_NEXT_SYNC_WAIT_MS
    ) {
      return;
    }
    if (pending && envelope.playback_state.current_queue_item_id !== pending.expectedQueueItemId) {
      clearOptimisticNext();
    }
    setPlayback(envelope.playback_state);
    setTrack((previous) => envelope.current_track || (envelope.playback_state.current_queue_item_id ? previous : null));
    setOrderedBy((previous) => envelope.ordered_by ?? (envelope.playback_state.current_queue_item_id ? previous : null));
    anchorRef.current = {
      serverTsMs: envelope.server_ts_ms || Date.now(),
      effectivePositionMs:
        envelope.effective_position_ms ??
        roomPositionFromState(envelope.playback_state, null),
    };
  }, [clearOptimisticNext]);

  const next = useCallback(async () => {
    if (!roomId || !token) return;
    const now = Date.now();
    if (now - lastNextAtRef.current < NEXT_DEBOUNCE_MS) return;
    lastNextAtRef.current = now;
    const expectedQueueItemId = playback?.current_queue_item_id ?? null;
    scheduleOptimisticNext(expectedQueueItemId);
    await api(`/api/rooms/${roomId}/controls/next`, {
      method: "POST",
      token,
      json: { expected_queue_item_id: expectedQueueItemId },
    }).catch(() => {});
  }, [playback?.current_queue_item_id, roomId, scheduleOptimisticNext, token]);

  const playPause = useCallback(async () => {
    if (!roomId || !token) return;
    if (playEnabled) unlockAudio();
    if (playback?.current_queue_item_id && playback.is_playing) {
      const pos = Math.round(playEnabled && !audio.paused ? audio.currentTime * 1000 : getRoomPositionMs());
      await api(`/api/rooms/${roomId}/controls/pause`, {
        method: "POST",
        token,
        json: { position_ms: pos, expected_queue_item_id: playback.current_queue_item_id },
      });
    } else {
      await api(`/api/rooms/${roomId}/controls/play`, { method: "POST", token });
    }
  }, [audio, getRoomPositionMs, playEnabled, playback, roomId, token, unlockAudio]);

  const commitSeek = useCallback(async (ratio: number) => {
    if (!roomId || !token) return;
    const duration = getDurationMs();
    const desired = Math.floor(clamp(ratio, 0, 1) * duration);
    setIsSeeking(false);
    if (playEnabled) seekAudioTo(desired / 1000);
    setPositionMs(desired);
    await api(`/api/rooms/${roomId}/controls/position`, {
      method: "PATCH",
      token,
      json: { position_ms: desired, expected_queue_item_id: playback?.current_queue_item_id ?? null },
    }).catch(() => {});
  }, [getDurationMs, playback?.current_queue_item_id, playEnabled, roomId, seekAudioTo, token]);

  useEffect(() => {
    if (playEnabled) {
      applyOutputVolume(normalizerEnabled);
    } else {
      audio.volume = clamp(volume, 0, 100) / 100;
      setNormalizerState(normalizerEnabled ? "bypassed" : "off");
    }
  }, [applyOutputVolume, audio, normalizerAudioUrl, normalizerEnabled, playEnabled, volume]);

  useEffect(() => {
    syncAudioToRoom(false);
  }, [syncAudioToRoom]);

  useEffect(() => {
    if (!playEnabled) {
      refreshedAudioTrackRef.current = null;
      return;
    }
    if (!track?.id || !token) return;
    const key = `${track.id}:${track.source}:${track.source_track_id}`;
    if (refreshedAudioTrackRef.current === key) return;
    const needsUrl = !playableAudioUrl(track);
    const needsLoudness = normalizerEnabled && track.source === "netease" && track.loudness_gain_db == null && track.loudness_peak == null;
    const shouldRefresh = needsUrl || track.source === "bilibili" || needsLoudness;
    if (!shouldRefresh) return;
    refreshedAudioTrackRef.current = key;
    refreshDirectAudioUrl(track.source === "bilibili" || needsLoudness).catch(() => {
      refreshedAudioTrackRef.current = null;
    });
  }, [normalizerEnabled, playEnabled, refreshDirectAudioUrl, token, track?.audio_url, track?.id, track?.loudness_gain_db, track?.loudness_peak, track?.source, track?.source_track_id]);

  useEffect(() => {
    const onCanPlay = () => {
      applyPendingSeek();
      if (playEnabled && playback?.is_playing) requestAudioPlay(1);
    };
    audio.addEventListener("canplay", onCanPlay);
    audio.addEventListener("loadeddata", onCanPlay);
    return () => {
      audio.removeEventListener("canplay", onCanPlay);
      audio.removeEventListener("loadeddata", onCanPlay);
    };
  }, [applyPendingSeek, audio, playEnabled, playback?.is_playing, requestAudioPlay]);

  useEffect(() => {
    const onUserActivation = () => {
      if (playEnabled) unlockAudio();
      if (playEnabled && playback?.is_playing) requestAudioPlay(1);
    };
    window.addEventListener("pointerdown", onUserActivation, { passive: true });
    window.addEventListener("keydown", onUserActivation);
    return () => {
      window.removeEventListener("pointerdown", onUserActivation);
      window.removeEventListener("keydown", onUserActivation);
    };
  }, [playback?.is_playing, playEnabled, requestAudioPlay, unlockAudio]);

  useEffect(() => {
    const recoverPlayback = () => {
      if (!playEnabled || !playback?.is_playing || !hasTrack) return;
      syncAudioToRoomRef.current(true, true);
      reloadCurrentStream(2, true);
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") recoverPlayback();
    };
    window.addEventListener("online", recoverPlayback);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("online", recoverPlayback);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [hasTrack, playback?.is_playing, playEnabled, reloadCurrentStream]);

  useEffect(() => () => {
    stopLocalAudio();
    clearOptimisticNext();
  }, [clearOptimisticNext, stopLocalAudio]);

  useEffect(() => {
    const onEnded = () => next();
    const onMetadata = () => {
      applyPendingSeek();
      setDurationMs(getDurationMs());
    };
    const onTime = () => {
      const currentSeconds = audio.currentTime || 0;
      if (Math.abs(currentSeconds - lastAudioTimeRef.current) > 0.05) {
        lastAudioProgressAtRef.current = Date.now();
        lastAudioTimeRef.current = currentSeconds;
        streamRetryCountRef.current = 0;
      }
      if (!isSeeking) {
        setPositionMs(playEnabled && !audio.paused ? currentSeconds * 1000 : getRoomPositionMs());
      }
      setDurationMs(getDurationMs());
    };
    const onPlaying = () => {
      lastAudioProgressAtRef.current = Date.now();
      lastAudioTimeRef.current = audio.currentTime || 0;
    };
    const onError = () => {
      if (!playEnabled || !playback?.is_playing || !audio.src || streamRetryCountRef.current >= STREAM_RETRY_LIMIT) return;
      streamRetryCountRef.current += 1;
      clearStreamRetry();
      streamRetryTimerRef.current = window.setTimeout(() => {
        refreshDirectAudioUrl(true)
          .then((nextUrl) => {
            if (nextUrl) {
              const nextSrc = withBase(nextUrl);
              audio.src = nextSrc;
            }
          })
          .catch(() => {})
          .finally(() => reloadCurrentStream(1, true));
      }, 450);
    };
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("loadedmetadata", onMetadata);
    audio.addEventListener("playing", onPlaying);
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("error", onError);
    return () => {
      audio.removeEventListener("ended", onEnded);
      audio.removeEventListener("loadedmetadata", onMetadata);
      audio.removeEventListener("playing", onPlaying);
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("error", onError);
    };
  }, [applyPendingSeek, audio, clearStreamRetry, getDurationMs, getRoomPositionMs, isSeeking, next, playback?.is_playing, playEnabled, refreshDirectAudioUrl, reloadCurrentStream, track]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (playEnabled && playback?.is_playing && hasTrack && audio.src) {
        const now = Date.now();
        const currentSeconds = audio.currentTime || 0;
        const moving = !audio.paused && !audio.ended && Math.abs(currentSeconds - lastAudioTimeRef.current) > 0.05;
        if (moving) {
          lastAudioProgressAtRef.current = now;
          lastAudioTimeRef.current = currentSeconds;
        } else if (now - lastAudioProgressAtRef.current > STALLED_AUDIO_RELOAD_MS) {
          reloadCurrentStream(2);
        }
      }
      if (!isSeeking) setPositionMs(playEnabled && !audio.paused ? (audio.currentTime || 0) * 1000 : getRoomPositionMs());
      setDurationMs(getDurationMs());
    }, 250);
    return () => window.clearInterval(timer);
  }, [audio, getDurationMs, getRoomPositionMs, hasTrack, isSeeking, playEnabled, playback?.is_playing, reloadCurrentStream]);

  return {
    audio,
    playback,
    track,
    orderedBy,
    playEnabled,
    normalizerEnabled,
    normalizerState,
    normalizerStatusLabel: normalizerEnabled
      ? normalizerState === "active"
        ? "生效"
        : normalizerState === "metadata"
          ? "元数据"
        : playEnabled
          ? "不可测"
          : "待播放"
      : "",
    volume,
    positionMs,
    durationMs,
    progressRatio: durationMs > 0 ? clamp(positionMs / durationMs, 0, 1) : 0,
    currentTimeLabel: formatTime(positionMs),
    durationLabel: formatTime(durationMs),
    isSeeking,
    setIsSeeking,
    setPlayEnabled,
    setNormalizerEnabled,
    setVolume,
    toggleMute,
    unlockAudio,
    applyPlaybackEnvelope,
    playPause,
    next,
    commitSeek,
    setPositionMs,
  };
}
