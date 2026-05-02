import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, withBase } from "../lib/api";
import { clamp, formatTime, parsePlaybackTime } from "../lib/time";
import { readStoredBoolean, readStoredNumber, writeStoredBoolean } from "../lib/storage";
import type { PlaybackEnvelope, PlaybackState, Track } from "../types";

const SEEK_TOLERANCE_MS = 3000;
const NEXT_DEBOUNCE_MS = 2500;
const STREAM_RETRY_LIMIT = 2;
const STALLED_AUDIO_RELOAD_MS = 8000;
const STREAM_RELOAD_COOLDOWN_MS = 10000;
const NORMALIZER_MIN_GAIN = 0.25;
const NORMALIZER_MAX_GAIN = 4;
const NORMALIZER_CACHE_PREFIX = "volumeNormalizer:v2:";

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

function playableAudioUrl(track: Track): string | null {
  if (track.id && (track.source === "netease" || track.source === "bilibili" || /^https?:\/\//i.test(track.audio_url || ""))) {
    return `/api/tracks/${track.id}/stream`;
  }
  return track.audio_url || null;
}

interface TrackGainAnalysis {
  gain: number;
  rms: number;
  peak: number;
  analyzedAt: number;
}

function normalizerTrackKey(track: Track): string {
  return track.id ? `id:${track.id}` : `${track.source}:${track.source_track_id}`;
}

function trackServerGain(track: Track | null): TrackGainAnalysis | null {
  if (!track || !Number.isFinite(track.normalization_gain)) return null;
  return {
    gain: clamp(Number(track.normalization_gain), NORMALIZER_MIN_GAIN, NORMALIZER_MAX_GAIN),
    rms: Math.max(0, Number(track.normalization_rms || 0)),
    peak: Math.max(0, Number(track.normalization_peak || 0)),
    analyzedAt: track.normalization_analyzed_at ? Date.parse(track.normalization_analyzed_at) || 0 : 0,
  };
}

function readCachedTrackGain(key: string): TrackGainAnalysis | null {
  try {
    const raw = localStorage.getItem(`${NORMALIZER_CACHE_PREFIX}${key}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<TrackGainAnalysis>;
    if (!Number.isFinite(parsed.gain) || !Number.isFinite(parsed.rms) || !Number.isFinite(parsed.peak)) return null;
    return {
      gain: clamp(Number(parsed.gain), NORMALIZER_MIN_GAIN, NORMALIZER_MAX_GAIN),
      rms: Math.max(0, Number(parsed.rms)),
      peak: Math.max(0, Number(parsed.peak)),
      analyzedAt: Number(parsed.analyzedAt) || 0,
    };
  } catch {
    return null;
  }
}

function writeCachedTrackGain(key: string, analysis: TrackGainAnalysis): void {
  try {
    localStorage.setItem(`${NORMALIZER_CACHE_PREFIX}${key}`, JSON.stringify(analysis));
  } catch {
    // Storage may be full or unavailable in private browsing.
  }
}

export function useAudioController(roomId: number | null, token: string | null) {
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
  const hasTrack = track != null;

  const anchorRef = useRef<SyncAnchor | null>(null);
  const lastNextAtRef = useRef(0);
  const playRetryTimerRef = useRef<number | null>(null);
  const streamRetryTimerRef = useRef<number | null>(null);
  const streamRetryCountRef = useRef(0);
  const pendingSeekSecondsRef = useRef<number | null>(null);
  const lastAudioProgressAtRef = useRef(Date.now());
  const lastAudioTimeRef = useRef(0);
  const lastStreamReloadAtRef = useRef(0);
  const currentQueueItemRef = useRef<number | null>(null);
  const currentTrackGainRef = useRef(1);
  const currentTrackGainKeyRef = useRef<string | null>(null);
  const syncAudioToRoomRef = useRef<(force?: boolean, shouldPlay?: boolean) => void>(() => {});
  const audioGraphRef = useRef<{
    ctx: AudioContext;
    source: MediaElementAudioSourceNode;
    gain: GainNode;
  } | null>(null);

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

  const normalizerTrackKeyValue = useMemo(
    () => (track ? normalizerTrackKey(track) : null),
    [track?.id, track?.source, track?.source_track_id],
  );
  const normalizerAudioUrl = useMemo(
    () => (track ? playableAudioUrl(track) : null),
    [track?.audio_url, track?.id, track?.source, track?.source_track_id],
  );
  const serverTrackGain = useMemo(
    () => trackServerGain(track),
    [
      track?.normalization_analyzed_at,
      track?.normalization_gain,
      track?.normalization_peak,
      track?.normalization_rms,
    ],
  );

  const effectiveOutputGain = useCallback((normalizerOn = normalizerEnabled) => {
    const userGain = clamp(volume, 0, 100) / 100;
    return userGain * (normalizerOn ? currentTrackGainRef.current : 1);
  }, [normalizerEnabled, volume]);

  const ensureAudioGraph = useCallback(async () => {
    if (audioGraphRef.current) {
      if (audioGraphRef.current.ctx.state === "suspended") await audioGraphRef.current.ctx.resume().catch(() => {});
      return audioGraphRef.current;
    }
    const AudioCtx = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return null;
    const ctx = new AudioCtx();
    const source = ctx.createMediaElementSource(audio);
    const gain = ctx.createGain();
    audio.volume = 1;
    gain.gain.value = effectiveOutputGain();
    source.connect(gain);
    gain.connect(ctx.destination);
    audioGraphRef.current = { ctx, source, gain };
    return audioGraphRef.current;
  }, [audio, effectiveOutputGain]);

  const applyAudioGraph = useCallback(async (enabled: boolean) => {
    const graph = await ensureAudioGraph();
    if (!graph) return;
    const now = graph.ctx.currentTime;
    graph.gain.gain.cancelScheduledValues(now);
    graph.gain.gain.setTargetAtTime(effectiveOutputGain(enabled), now, 0.08);
  }, [effectiveOutputGain, ensureAudioGraph]);

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

  const seekAudioTo = useCallback((seconds: number) => {
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
      const graph = audioGraphRef.current;
      const resumeGraph =
        graph && graph.ctx.state === "suspended"
          ? graph.ctx.resume().catch(() => {})
          : Promise.resolve();
      resumeGraph.finally(() => {
        audio.play()
          .then(() => {
            clearPlayRetry();
          })
          .catch(() => {
            if (remaining <= 0) return;
            playRetryTimerRef.current = window.setTimeout(() => attempt(remaining - 1), 350);
          });
      });
    };
    attempt(retries);
  }, [audio, clearPlayRetry]);

  const reloadCurrentStream = useCallback((retries = 1, ignoreCooldown = false) => {
    if (!audio.src) return;
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
  }, [audio, clearStreamRetry, getRoomPositionMs, requestAudioPlay]);

  const unlockAudio = useCallback(() => {
    if (!normalizerEnabled && !audioGraphRef.current) return;
    ensureAudioGraph()
      .then(() => applyAudioGraph(normalizerEnabled))
      .catch(() => {});
  }, [applyAudioGraph, ensureAudioGraph, normalizerEnabled]);

  const setPlayEnabled = useCallback((value: boolean) => {
    setPlayEnabledState(value);
    writeStoredBoolean("playEnabled", value);
    if (value) {
      unlockAudio();
      syncAudioToRoomRef.current(true, true);
      requestAudioPlay(3);
    } else {
      clearPlayRetry();
      clearStreamRetry();
      audio.pause();
    }
  }, [audio, clearPlayRetry, clearStreamRetry, requestAudioPlay, unlockAudio]);

  const setNormalizerEnabled = useCallback((value: boolean) => {
    setNormalizerEnabledState(value);
    writeStoredBoolean("volumeNormalizer", value);
    if (value || audioGraphRef.current) applyAudioGraph(value).catch(() => {});
  }, [applyAudioGraph]);

  const setVolume = useCallback((value: number) => {
    const safe = clamp(value, 0, 100);
    setVolumeState(safe);
    localStorage.setItem("volume", String(safe));
    if (safe > 0) {
      setPreviousVolume(safe);
      localStorage.setItem("previousVolume", String(safe));
    }
    if (audioGraphRef.current) {
      audio.volume = 1;
      applyAudioGraph(normalizerEnabled).catch(() => {});
    } else {
      audio.volume = safe / 100;
    }
  }, [applyAudioGraph, audio, normalizerEnabled]);

  const toggleMute = useCallback(() => {
    setVolume(volume > 0 ? 0 : previousVolume || 50);
  }, [previousVolume, setVolume, volume]);

  const syncAudioToRoom = useCallback((force = false, shouldPlay = playEnabled) => {
    const audioUrl = normalizerAudioUrl;
    if (!playback || !audioUrl) {
      clearPlayRetry();
      clearStreamRetry();
      audio.pause();
      if (!audioUrl) {
        audio.removeAttribute("src");
        audio.load();
      }
      currentQueueItemRef.current = null;
      pendingSeekSecondsRef.current = null;
      streamRetryCountRef.current = 0;
      lastAudioProgressAtRef.current = Date.now();
      lastAudioTimeRef.current = 0;
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
      if (!normalizerEnabled && !audioGraphRef.current) {
        requestAudioPlay();
        return;
      }
      ensureAudioGraph().then(() => applyAudioGraph(normalizerEnabled)).finally(() => {
        requestAudioPlay();
      });
    } else {
      clearPlayRetry();
      audio.pause();
    }
  }, [applyAudioGraph, audio, clearPlayRetry, clearStreamRetry, ensureAudioGraph, getDurationMs, normalizerAudioUrl, normalizerEnabled, playEnabled, playback, requestAudioPlay, seekAudioTo]);

  syncAudioToRoomRef.current = syncAudioToRoom;

  const applyPlaybackEnvelope = useCallback((envelope: PlaybackEnvelope) => {
    setPlayback(envelope.playback_state);
    setTrack((previous) => envelope.current_track || (envelope.playback_state.current_queue_item_id ? previous : null));
    setOrderedBy((previous) => envelope.ordered_by ?? (envelope.playback_state.current_queue_item_id ? previous : null));
    anchorRef.current = {
      serverTsMs: envelope.server_ts_ms || Date.now(),
      effectivePositionMs:
        envelope.effective_position_ms ??
        roomPositionFromState(envelope.playback_state, null),
    };
  }, []);

  const next = useCallback(async () => {
    if (!roomId || !token) return;
    const now = Date.now();
    if (now - lastNextAtRef.current < NEXT_DEBOUNCE_MS) return;
    lastNextAtRef.current = now;
    await api(`/api/rooms/${roomId}/controls/next`, {
      method: "POST",
      token,
      json: { expected_queue_item_id: playback?.current_queue_item_id ?? null },
    }).catch(() => {});
  }, [playback?.current_queue_item_id, roomId, token]);

  const playPause = useCallback(async () => {
    if (!roomId || !token) return;
    unlockAudio();
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
    seekAudioTo(desired / 1000);
    await api(`/api/rooms/${roomId}/controls/position`, {
      method: "PATCH",
      token,
      json: { position_ms: desired, expected_queue_item_id: playback?.current_queue_item_id ?? null },
    }).catch(() => {});
  }, [getDurationMs, playback?.current_queue_item_id, roomId, seekAudioTo, token]);

  useEffect(() => {
    const key = normalizerTrackKeyValue;
    const serverGain = normalizerEnabled ? serverTrackGain : null;
    const cached = normalizerEnabled && key && !serverGain ? readCachedTrackGain(key) : null;
    if (currentTrackGainKeyRef.current !== key) {
      currentTrackGainKeyRef.current = key;
      currentTrackGainRef.current = 1;
    }
    if (serverGain) {
      currentTrackGainRef.current = serverGain.gain;
      if (key) writeCachedTrackGain(key, serverGain);
    }
    if (cached) currentTrackGainRef.current = cached.gain;

    if ((normalizerEnabled && key) || audioGraphRef.current) applyAudioGraph(normalizerEnabled).catch(() => {});

    return undefined;
  }, [applyAudioGraph, normalizerEnabled, normalizerTrackKeyValue, serverTrackGain]);

  useEffect(() => {
    if (audioGraphRef.current) {
      audio.volume = 1;
      applyAudioGraph(normalizerEnabled).catch(() => {});
    } else {
      audio.volume = volume / 100;
    }
  }, [applyAudioGraph, audio, normalizerEnabled, volume]);

  useEffect(() => {
    syncAudioToRoom(false);
  }, [syncAudioToRoom]);

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
      const graph = audioGraphRef.current;
      if ((playEnabled || normalizerEnabled || graph) && (!graph || graph.ctx.state === "suspended")) unlockAudio();
      if (playEnabled && playback?.is_playing) requestAudioPlay(1);
    };
    window.addEventListener("pointerdown", onUserActivation, { passive: true });
    window.addEventListener("keydown", onUserActivation);
    return () => {
      window.removeEventListener("pointerdown", onUserActivation);
      window.removeEventListener("keydown", onUserActivation);
    };
  }, [normalizerEnabled, playback?.is_playing, playEnabled, requestAudioPlay, unlockAudio]);

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
    clearPlayRetry();
    clearStreamRetry();
  }, [clearPlayRetry, clearStreamRetry]);

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
      streamRetryTimerRef.current = window.setTimeout(() => reloadCurrentStream(1), 450);
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
  }, [applyPendingSeek, audio, clearStreamRetry, getDurationMs, getRoomPositionMs, isSeeking, next, playback?.is_playing, playEnabled, reloadCurrentStream]);

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
