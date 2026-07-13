import { ChevronDown, Gauge, Music2, Pause, Play, SkipForward, Volume2, VolumeX, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import { useAudioController } from "../hooks/useAudioController";
import { createProgressSeekCoordinator, type ProgressSeekCoordinator } from "../hooks/progressSeek";
import { useAnchoredPopover } from "../hooks/useRoomChrome";
import { useDialogViewportLock } from "../hooks/useStableViewportHeight";
import { TrackCover, trackCoverSrc, useModalFocus } from "./common";

type AudioController = ReturnType<typeof useAudioController>;
const PROGRESS_KEY_CONTROLS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);

function progressRatio(input: HTMLInputElement): number {
  return Number(input.value) / 1000;
}

function useProgressSeek(audio: AudioController) {
  const audioRef = useRef(audio);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const coordinatorRef = useRef<ProgressSeekCoordinator | null>(null);
  audioRef.current = audio;

  useEffect(() => {
    const coordinator = createProgressSeekCoordinator({
      schedule: (callback) => {
        const frame = window.requestAnimationFrame(callback);
        return () => window.cancelAnimationFrame(frame);
      },
      onSeekingChange: (seeking) => audioRef.current.setIsSeeking(seeking),
      onPreview: (ratio) => audioRef.current.setPositionMs(ratio * audioRef.current.durationMs),
      onCommit: (ratio) => { void audioRef.current.commitSeek(ratio); },
    });
    coordinatorRef.current = coordinator;

    const finishFromInput = () => {
      const input = inputRef.current;
      coordinator.finish(input ? progressRatio(input) : undefined);
    };
    const cancel = () => coordinator.cancel();
    window.addEventListener("pointerup", finishFromInput);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("blur", finishFromInput);
    return () => {
      window.removeEventListener("pointerup", finishFromInput);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("blur", finishFromInput);
      if (coordinatorRef.current === coordinator) coordinatorRef.current = null;
      coordinator.dispose();
    };
  }, []);

  const begin = (event: ReactPointerEvent<HTMLInputElement>) => {
    const coordinator = coordinatorRef.current;
    if (!coordinator) return;
    coordinator.begin(progressRatio(event.currentTarget));
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // The window-level release listener remains the fallback.
    }
  };
  const preview = (event: ChangeEvent<HTMLInputElement>) => {
    const coordinator = coordinatorRef.current;
    if (!coordinator) return;
    const ratio = progressRatio(event.currentTarget);
    const standaloneChange = !coordinator.isSeeking();
    if (standaloneChange) coordinator.begin(ratio);
    coordinator.preview(ratio);
    if (standaloneChange) coordinator.finish();
  };
  const finish = (input: HTMLInputElement) => coordinatorRef.current?.finish(progressRatio(input));
  const cancel = () => coordinatorRef.current?.cancel();
  const onKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (PROGRESS_KEY_CONTROLS.has(event.key) && !coordinatorRef.current?.isSeeking()) {
      coordinatorRef.current?.begin(progressRatio(event.currentTarget));
    }
  };
  const onKeyUp = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (PROGRESS_KEY_CONTROLS.has(event.key)) finish(event.currentTarget);
  };

  return { inputRef, begin, preview, finish, cancel, onKeyDown, onKeyUp };
}

function Progress({ audio, compact = false }: { audio: AudioController; compact?: boolean }) {
  const seek = useProgressSeek(audio);
  if (compact) {
    return <div className="npProgressCompact" role="progressbar" aria-label="播放进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(audio.progressRatio * 100)}><span style={{ width: `${audio.progressRatio * 100}%` }} /></div>;
  }
  return (
    <div className="npProgress">
      <input
        ref={seek.inputRef}
        type="range"
        aria-label="播放进度"
        aria-valuetext={`${audio.currentTimeLabel} / ${audio.durationLabel}`}
        min={0}
        max={1000}
        value={Math.round(audio.progressRatio * 1000)}
        onPointerDown={seek.begin}
        onChange={seek.preview}
        onPointerUp={(event) => seek.finish(event.currentTarget)}
        onPointerCancel={seek.cancel}
        onLostPointerCapture={(event) => seek.finish(event.currentTarget)}
        onBlur={(event) => seek.finish(event.currentTarget)}
        onKeyDown={seek.onKeyDown}
        onKeyUp={seek.onKeyUp}
      />
      <span>{audio.currentTimeLabel} / {audio.durationLabel}</span>
    </div>
  );
}

function TrackIdentity({ audio, large = false }: { audio: AudioController; large?: boolean }) {
  return (
    <div className={`npIdentity ${large ? "large" : ""}`}>
      {audio.track ? <TrackCover track={audio.track} /> : <div className="cover placeholder"><Music2 /></div>}
      <div className="npText">
        <strong>{audio.track?.title || "等待第一首歌"}</strong>
        <span>{audio.track ? `${audio.track.artist || "未知歌手"} · ${audio.track.source}` : "搜索并点一首喜欢的歌"}</span>
        {audio.audioError && <em role="alert">{audio.audioError}</em>}
        {audio.orderedBy?.username && <em>{audio.orderedBy.username} 点播</em>}
      </div>
    </div>
  );
}

function Transport({ audio }: { audio: AudioController }) {
  return (
    <div className="npTransport">
      <button className="transportButton primaryTransport" aria-label={audio.playback?.is_playing ? "暂停" : "播放"} onClick={audio.playPause}>
        {audio.playback?.is_playing ? <Pause /> : <Play />}
      </button>
      <button className="transportButton" aria-label="下一首" onClick={audio.next}><SkipForward /></button>
    </div>
  );
}

function SoundControls({ audio }: { audio: AudioController }) {
  const normalizerTitle = audio.normalizerEnabled
    ? audio.normalizerState === "active"
      ? "本机动态音量均衡已应用"
      : audio.normalizerState === "metadata"
        ? "服务端响度均衡已应用"
        : audio.normalizerState === "pending"
          ? "正在等待服务端分析响度"
          : "服务端响度均衡不可用"
    : "音量均衡";
  return (
    <div className="npSound">
      <button className="iconButton" aria-label={audio.volume > 0 ? "静音" : "恢复音量"} onClick={audio.toggleMute}>
        {audio.volume > 0 ? <Volume2 /> : <VolumeX />}
      </button>
      <input type="range" aria-label="音量" min={0} max={100} value={audio.volume} disabled={!audio.playEnabled} onChange={(event) => audio.setVolume(Number(event.target.value))} />
      <label className={`checkPill normalizerPill ${audio.normalizerEnabled ? audio.normalizerState : "off"}`} title={normalizerTitle}>
        <input type="checkbox" checked={audio.normalizerEnabled} onChange={(event) => audio.setNormalizerEnabled(event.target.checked)} />
        <Gauge />均衡
      </label>
    </div>
  );
}

function Ambient({ audio }: { audio: AudioController }) {
  const src = audio.track ? trackCoverSrc(audio.track) : null;
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return (
    <div className="npVisualClip" aria-hidden="true">
      {!src || failed
        ? <div className="npAmbient fallback" />
        : <img className="npAmbient" src={src} alt="" referrerPolicy="no-referrer" onError={() => setFailed(true)} />}
    </div>
  );
}

export function NowPlayingDock({ audio }: { audio: AudioController }) {
  const [soundOpen, setSoundOpen] = useState(false);
  const soundAreaRef = useRef<HTMLDivElement | null>(null);
  const soundToggleRef = useRef<HTMLButtonElement | null>(null);
  const soundPopoverRef = useRef<HTMLDivElement | null>(null);
  const soundPopoverPosition = useAnchoredPopover(soundOpen, soundToggleRef, "above", 14);
  useEffect(() => {
    if (!soundOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (
        event.target instanceof Node
        && !soundAreaRef.current?.contains(event.target)
        && !soundPopoverRef.current?.contains(event.target)
      ) setSoundOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setSoundOpen(false);
        soundToggleRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [soundOpen]);
  return (
    <footer className="nowPlayingDock glassPanel">
      <Ambient audio={audio} />
      <TrackIdentity audio={audio} />
      <div className="npCenter"><Progress audio={audio} /></div>
      <div className="npRight" ref={soundAreaRef}>
        <Transport audio={audio} />
        <button ref={soundToggleRef} className={`iconButton soundToggle ${soundOpen ? "active" : ""}`} aria-label="本机声音设置" aria-haspopup="dialog" aria-controls="desktop-sound-popover" aria-expanded={soundOpen} onClick={() => setSoundOpen((value) => !value)}><Volume2 /><ChevronDown /></button>
        {soundOpen && createPortal(<div ref={soundPopoverRef} id="desktop-sound-popover" className="npSoundPopover" style={soundPopoverPosition} role="dialog" aria-label="本机声音设置">
          <div className="desktopPlaybackToggle">
            <span><strong>本机播放</strong><small>关闭后仍可点歌和控制房间</small></span>
            <label className="liquidSwitch"><input type="checkbox" checked={audio.playEnabled} onChange={(event) => audio.setPlayEnabled(event.target.checked)} /><span aria-hidden="true" /><b>{audio.playEnabled ? "已开启" : "已关闭"}</b></label>
          </div>
          <SoundControls audio={audio} />
        </div>, document.body)}
      </div>
    </footer>
  );
}

export function MiniPlayer({ audio, onOpen }: { audio: AudioController; onOpen: () => void }) {
  return (
    <section className="miniPlayer glassPanel">
      <button className="miniPlayerMain" onClick={onOpen} aria-haspopup="dialog" aria-label="展开播放器">
        <TrackIdentity audio={audio} />
      </button>
      <Transport audio={audio} />
      <Progress audio={audio} compact />
    </section>
  );
}

export function PlayerSheet({ audio, onClose }: { audio: AudioController; onClose: () => void }) {
  useDialogViewportLock();
  const titleId = useId();
  const { dialogRef, onDialogKeyDown } = useModalFocus({ onClose });
  return (
    <div className="playerSheetOverlay" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section ref={dialogRef} className="playerSheet glassPanel" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} onKeyDown={onDialogKeyDown}>
        <Ambient audio={audio} />
        <div className="sheetHeader"><span className="eyebrow" id={titleId}>正在播放</span><button className="iconButton" aria-label="关闭播放器" onClick={onClose}><X /></button></div>
        <TrackIdentity audio={audio} large />
        <Progress audio={audio} />
        <Transport audio={audio} />
        <section className="localSoundCard">
          <div><strong>本机播放</strong><span>关闭后仍可点歌和控制房间</span></div>
          <label className="liquidSwitch"><input type="checkbox" checked={audio.playEnabled} onChange={(event) => audio.setPlayEnabled(event.target.checked)} /><span aria-hidden="true" /><b>{audio.playEnabled ? "已开启" : "已关闭"}</b></label>
          <SoundControls audio={audio} />
        </section>
      </section>
    </div>
  );
}
