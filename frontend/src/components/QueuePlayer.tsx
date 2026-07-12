import { ArrowUpToLine, Shuffle, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import { trackKey, trackPayload } from "../lib/track";
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
  const playingIndex = items.findIndex((item) => item.status === "playing");
  const nextIndex = props.value === "queue" ? items.findIndex((item, index) => index > playingIndex && item.status === "queued") : -1;
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
      {items.map((item, index) => (
        <article className={`songRow ${index === playingIndex ? "queueCurrent" : index === nextIndex ? "queueNext" : ""}`} key={item.id}>
          <TrackCover track={item.track} />
          <div className="songInfo">
            <strong>{item.track.title}</strong>
            <span>{index === playingIndex ? "当前" : index === nextIndex ? "下一首" : item.track.artist || "-"} · {item.track.source} · {item.ordered_by.username}</span>
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
