import { createContext, useContext, useState } from "react";
import { Search, Trash2 } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { Track } from "../types";
import { CardDialog, ConfirmDialog, TrackCover } from "./common";
import { trackKey } from "../lib/track";

export const TrackRepairContext = createContext<{ onRepair: (track: Track) => void; canRepair: (track: Track) => boolean }>({ onRepair: () => {}, canRepair: () => false });

export function unavailableTrackFromError(error: unknown): Track | null {
  if (!(error instanceof ApiError) || !error.detail || typeof error.detail !== "object") return null;
  const detail = error.detail as { code?: string; track?: Track };
  return detail.code === "track_unavailable" && detail.track?.availability === "unavailable" ? detail.track : null;
}

export function UnavailableTrackActions({ track, token, onChanged }: {
  track: Track; token: string; onChanged: () => void;
}) {
  const { onRepair, canRepair } = useContext(TrackRepairContext);
  const [confirmRemove, setConfirmRemove] = useState(false);
  if (!canRepair(track)) return null;
  return <>
    <button className="smallButton" onClick={() => onRepair(track)} disabled={!track.id}><Search />重新搜索</button>
    <button className="smallButton danger" onClick={() => setConfirmRemove(true)} disabled={!track.id}><Trash2 />移出所有歌单</button>
    {confirmRemove && <ConfirmDialog title="移出所有歌单" message={`将“${track.title}”从你的所有歌单移除？`} confirmText="全部移除" onCancel={() => setConfirmRemove(false)} onConfirm={async () => {
      await api(`/api/playlists/tracks/${track.id}`, { method: "DELETE", token });
      setConfirmRemove(false);
      onChanged();
    }} />}
  </>;
}

export interface BatchOrderResult { skipped_unavailable?: Track[]; added?: number }

export function UnavailableTracksDialog({ tracks, added, onClose }: {
  tracks: Track[]; added?: number; onClose: () => void;
}) {
  return <CardDialog title="部分歌曲已失效" onClose={onClose}>
    <p className="dialogMessage">{added ? `已点 ${added} 首，` : ""}以下 {tracks.length} 首歌曲已跳过。</p>
    <div className="unavailableTrackList">
      {tracks.map((track) => <div className="pickerTrack" key={trackKey(track)}>
        <TrackCover track={track} />
        <div className="songInfo"><strong>{track.title}</strong><span>{track.artist || "未知歌手"} · {track.source}</span><span className="unavailableLabel">{track.unavailable_reason || "歌曲已失效"}</span></div>
      </div>)}
    </div>
    <div className="dialogActions"><button className="primaryButton" onClick={onClose}>知道了</button></div>
  </CardDialog>;
}
