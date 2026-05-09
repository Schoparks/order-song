import { ArrowUpToLine, Gauge, Pause, Play, Shuffle, SkipForward, Trash2, Volume2, VolumeX } from "lucide-react";
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
  const items = props.value === "queue"
    ? props.queue
    : props.history.filter((item) => !props.queuedKeys.has(trackKey(item.track)));
  if (!items.length) {
    return <EmptyState title={props.value === "queue" ? "队列为空" : "暂无历史"} meta={props.value === "queue" ? "搜索歌曲后点歌。" : "播完或切歌后会出现在这里。"} />;
  }
  return (
    <div className={props.compact ? "songList compact" : "songList"}>
      {props.value === "queue" && items.length > 1 && (
        <button
          className="toolbarButton"
          onClick={async () => {
            await api(`/api/rooms/${props.roomId}/queue/shuffle`, { method: "POST", token: props.token });
            props.onChanged();
          }}
        >
          <Shuffle />一键打乱
        </button>
      )}
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
                <button className="iconButton bumpButton" title="顶歌" aria-label="顶歌" onClick={async () => { await api(`/api/rooms/${props.roomId}/queue/${item.id}/bump`, { method: "POST", token: props.token }); props.onChanged(); }}><ArrowUpToLine /></button>
                <button className="iconButton danger" onClick={async () => { await api(`/api/rooms/${props.roomId}/queue/${item.id}`, { method: "DELETE", token: props.token }); props.onChanged(); }}><Trash2 /></button>
              </>
            ) : (
              <button className="smallButton" onClick={async () => { await api(`/api/rooms/${props.roomId}/queue`, { method: "POST", token: props.token, json: trackPayload(item.track) }); props.onChanged(); }}>再点一次</button>
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
          />
          <span>{audio.currentTimeLabel} / {audio.durationLabel}</span>
        </div>
      </div>
      <div className="playerControls">
        <button className="transportButton" onClick={audio.playPause}>{audio.playback?.is_playing ? <Pause /> : <Play />}</button>
        <button className="transportButton" onClick={audio.next}><SkipForward /></button>
        <div className="volumeCluster">
          <button className="iconButton" onClick={audio.toggleMute}>{audio.volume > 0 ? <Volume2 /> : <VolumeX />}</button>
          <input type="range" min={0} max={100} value={audio.volume} disabled={!audio.playEnabled} onChange={(e) => audio.setVolume(Number(e.target.value))} />
          <label className={`checkPill normalizerPill ${audio.normalizerEnabled ? audio.normalizerState : "off"}`} title={normalizerTitle}>
            <input type="checkbox" checked={audio.normalizerEnabled} onChange={(e) => audio.setNormalizerEnabled(e.target.checked)} />
            <Gauge />音量均衡
          </label>
        </div>
      </div>
    </footer>
  );
}
