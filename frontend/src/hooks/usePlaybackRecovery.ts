import { useEffect, type MutableRefObject } from "react";

interface PlaybackRecoveryOptions {
  audio: HTMLAudioElement;
  enabled: boolean;
  lastAudioProgressAtRef: MutableRefObject<number>;
  lastAudioTimeRef: MutableRefObject<number>;
  syncAudioToRoomRef: MutableRefObject<(force?: boolean, shouldPlay?: boolean) => void>;
  reloadCurrentStream: (retries?: number, ignoreCooldown?: boolean) => void;
  stalledAfterMs: number;
}

export function usePlaybackRecovery({
  audio, enabled, lastAudioProgressAtRef, lastAudioTimeRef,
  syncAudioToRoomRef, reloadCurrentStream, stalledAfterMs,
}: PlaybackRecoveryOptions) {
  useEffect(() => {
    const observeProgress = () => {
      const now = Date.now();
      const currentSeconds = audio.currentTime || 0;
      // Background timeupdate/interval events can be throttled while the media
      // itself keeps playing. Observe its clock before deciding it is stalled.
      if (!audio.paused && !audio.ended && Math.abs(currentSeconds - lastAudioTimeRef.current) > 0.05) {
        lastAudioProgressAtRef.current = now;
        lastAudioTimeRef.current = currentSeconds;
      }
    };
    const recoverPlayback = (networkRestored = false) => {
      if (!enabled) return;
      observeProgress();
      const needsReload = networkRestored || Boolean(audio.error) || audio.ended
        || Date.now() - lastAudioProgressAtRef.current > stalledAfterMs;
      if (audio.src && needsReload) {
        reloadCurrentStream(2);
      } else {
        // A routine tab switch must not discard buffered media or force-seek.
        syncAudioToRoomRef.current(false, true);
      }
    };
    const onOnline = () => recoverPlayback(true);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") recoverPlayback();
      else if (enabled) observeProgress();
    };
    window.addEventListener("online", onOnline);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [audio, enabled, lastAudioProgressAtRef, lastAudioTimeRef, reloadCurrentStream, stalledAfterMs, syncAudioToRoomRef]);
}
