import { ChevronDown, Gauge, Music2, Pause, Play, SkipForward, Volume2, VolumeX, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useAudioController } from "../hooks/useAudioController";
import { useAnchoredPopover } from "../hooks/useRoomChrome";
import { useDialogViewportLock } from "../hooks/useStableViewportHeight";
import { TrackCover, trackCoverSrc, useModalFocus } from "./common";

type AudioController = ReturnType<typeof useAudioController>;

function Progress({ audio, compact = false }: { audio: AudioController; compact?: boolean }) {
  if (compact) {
    return <div className="npProgressCompact" role="progressbar" aria-label="播放进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(audio.progressRatio * 100)}><span style={{ width: `${audio.progressRatio * 100}%` }} /></div>;
  }
  return (
    <div className="npProgress">
      <input
        type="range"
        aria-label="播放进度"
        aria-valuetext={`${audio.currentTimeLabel} / ${audio.durationLabel}`}
        min={0}
        max={1000}
        value={Math.round(audio.progressRatio * 1000)}
        onPointerDown={() => audio.setIsSeeking(true)}
        onChange={(event) => {
          const ratio = Number(event.target.value) / 1000;
          audio.setIsSeeking(true);
          audio.setPositionMs(ratio * audio.durationMs);
        }}
        onPointerUp={(event) => audio.commitSeek(Number((event.target as HTMLInputElement).value) / 1000)}
        onPointerCancel={() => audio.setIsSeeking(false)}
        onBlur={(event) => audio.isSeeking && void audio.commitSeek(Number(event.currentTarget.value) / 1000)}
        onKeyUp={(event) => {
          if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) {
            void audio.commitSeek(Number(event.currentTarget.value) / 1000);
          }
        }}
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
    ? audio.normalizerState === "active" ? "音量均衡已应用" : audio.normalizerState === "pending" ? "正在分析响度" : "已启用音量均衡"
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
