import { ArrowUpToLine, Gauge, Pause, Play, Shuffle, SkipForward, Trash2, Volume2, VolumeX } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import { trackKey, trackPayload } from "../lib/track";
import { useAudioController } from "../hooks/useAudioController";
import type { QueueItem } from "../types";
import { EmptyState, TrackCover } from "./common";

export type QueueTab = "queue" | "history";

export function QueueTabs(props: {
  value: QueueTab;
  onChange: (value: QueueTab) => void;
  queue: QueueItem[];
  history: QueueItem[];
  queuedKeys: Set<string>;
  token: string;
  roomId: number;
  onChanged: () => void;
  compact?: boolean;
}) {
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState("");
  const items = props.value === "queue"
    ? props.queue
    : props.history.filter((item) => !props.queuedKeys.has(trackKey(item.track)));
  async function runAction(key: string, action: () => Promise<unknown>) {
    setPendingAction(key);
    setActionError("");
    try {
      await action();
      props.onChanged();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "队列操作失败，请重试");
    } finally {
      setPendingAction(null);
    }
  }

  if (!items.length) return <EmptyState title={props.value === "queue" ? "队列为空" : "暂无历史"} meta={props.value === "queue" ? "搜索歌曲后点歌。" : "播完或切歌后会出现在这里。"} />;
  return (
    <div className={props.compact ? "songList compact" : "songList"}>
      {props.value === "queue" && items.length > 1 && (
        <button
          className="toolbarButton"
          disabled={pendingAction !== null}
          onClick={() => void runAction("shuffle", () => api(`/api/rooms/${props.roomId}/queue/shuffle`, { method: "POST", token: props.token }))}
        >
          <Shuffle />{pendingAction === "shuffle" ? "打乱中…" : "一键打乱"}
        </button>
      )}
      {actionError && <p className="actionError" role="alert">{actionError}</p>}
      {items.map((item) => (
        <article className="songRow" key={item.id}>
          <TrackCover track={item.track} />
          <div className="songInfo">
            <strong>{item.track.title}</strong>
            <span>{item.track.artist || "-"} · {item.track.source} · {item.ordered_by.username}</span>
          </div>
          <div className="rowActions">
            {props.value === "queue" ? (
              <>
                <button className="iconButton bumpButton" title="顶歌" aria-label={`将 ${item.track.title} 顶到队列前方`} disabled={pendingAction !== null} onClick={() => void runAction(`bump-${item.id}`, () => api(`/api/rooms/${props.roomId}/queue/${item.id}/bump`, { method: "POST", token: props.token }))}><ArrowUpToLine /></button>
                <button className="iconButton danger" aria-label={`从队列删除 ${item.track.title}`} disabled={pendingAction !== null} onClick={() => void runAction(`delete-${item.id}`, () => api(`/api/rooms/${props.roomId}/queue/${item.id}`, { method: "DELETE", token: props.token }))}><Trash2 /></button>
              </>
            ) : (
              <button className="smallButton" disabled={pendingAction !== null} onClick={() => void runAction(`requeue-${item.id}`, () => api(`/api/rooms/${props.roomId}/queue`, { method: "POST", token: props.token, json: trackPayload(item.track) }))}>再点一次</button>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}

export function PlayerBar({ audio }: { audio: ReturnType<typeof useAudioController> }) {
  const title = audio.track?.title || "未播放";
  const meta = audio.track ? `${audio.track.artist || "-"} · ${audio.track.source}${audio.orderedBy?.username ? ` · ${audio.orderedBy.username} 点播` : ""}` : "-";
  const normalizerTitle = audio.normalizerEnabled
    ? audio.normalizerState === "active"
      ? "音量均衡已应用"
      : audio.normalizerState === "metadata"
        ? "已使用上游响度元数据做隐藏音量修正"
      : audio.normalizerState === "pending"
        ? "正在等待后端响度分析完成，完成后会从同一房间时间开始播放"
      : audio.playEnabled
        ? "当前直链音频无法被浏览器读取实际响度，已跳过"
        : "切到可播放后尝试音量均衡"
    : "音量均衡";
  return (
    <footer className="playerBar glassPanel">
      <div className="nowPlaying">
        <strong>{title}</strong>
        <span>{meta}</span>
        <div className="progressLine">
          <input
            type="range"
            aria-label="播放进度"
            aria-valuetext={`${audio.currentTimeLabel} / ${audio.durationLabel}`}
            min={0}
            max={1000}
            value={Math.round(audio.progressRatio * 1000)}
            onPointerDown={() => audio.setIsSeeking(true)}
            onChange={(e) => {
              const ratio = Number(e.target.value) / 1000;
              audio.setIsSeeking(true);
              audio.setPositionMs(ratio * audio.durationMs);
            }}
            onPointerUp={(e) => audio.commitSeek(Number((e.target as HTMLInputElement).value) / 1000)}
            onPointerCancel={() => audio.setIsSeeking(false)}
            onBlur={(e) => {
              if (audio.isSeeking) void audio.commitSeek(Number(e.currentTarget.value) / 1000);
            }}
            onKeyUp={(e) => {
              if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) {
                void audio.commitSeek(Number(e.currentTarget.value) / 1000);
              }
            }}
          />
          <span>{audio.currentTimeLabel} / {audio.durationLabel}</span>
        </div>
      </div>
      <div className="playerControls">
        <button className="transportButton" aria-label={audio.playback?.is_playing ? "暂停" : "播放"} onClick={audio.playPause}>{audio.playback?.is_playing ? <Pause /> : <Play />}</button>
        <button className="transportButton" aria-label="下一首" onClick={audio.next}><SkipForward /></button>
        <div className="volumeCluster">
          <button className="iconButton" aria-label={audio.volume > 0 ? "静音" : "恢复音量"} onClick={audio.toggleMute}>{audio.volume > 0 ? <Volume2 /> : <VolumeX />}</button>
          <input type="range" aria-label="音量" aria-valuetext={`${audio.volume}%`} min={0} max={100} value={audio.volume} disabled={!audio.playEnabled} onChange={(e) => audio.setVolume(Number(e.target.value))} />
          <label className={`checkPill normalizerPill ${audio.normalizerEnabled ? audio.normalizerState : "off"}`} title={normalizerTitle}>
            <input type="checkbox" checked={audio.normalizerEnabled} onChange={(e) => audio.setNormalizerEnabled(e.target.checked)} />
            <Gauge />音量均衡
          </label>
        </div>
      </div>
    </footer>
  );
}
