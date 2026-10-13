import { useEffect, useMemo, useState } from "react";
import { BadgePlus, Check, Heart, ListMusic, Plus } from "lucide-react";
import { api } from "../lib/api";
import { UnavailableTrackActions, unavailableTrackFromError } from "./TrackAvailability";
import { trackKey, trackPayload } from "../lib/track";
import type { Playlist, Track } from "../types";
import { CardDialog, EmptyState, TrackCover } from "./common";
import { BilibiliCollectionActions } from "./BilibiliCollectionActions";
import { formatTime } from "../lib/time";

export interface PlaylistMembership {
  item_id: number;
  playlist_id: number;
  playlist_name: string;
}

export function TrackList({
  items,
  token,
  roomId,
  playlists,
  queuedKeys,
  playlistMap,
  onChanged,
  expandBilibiliParts = false,
  prioritizePlaylistMatches = false,
}: {
  items: Array<{ track: Track; meta?: string }>;
  token: string;
  roomId: number;
  playlists: Playlist[];
  queuedKeys: Set<string>;
  playlistMap: Record<string, PlaylistMembership[]>;
  onChanged: () => void;
  expandBilibiliParts?: boolean;
  prioritizePlaylistMatches?: boolean;
}) {
  const visibleItems = useMemo(() => {
    if (!prioritizePlaylistMatches) return items;
    return items
      .map((item, index) => ({
        item,
        index,
        inPlaylist: isTrackInPlaylist(item.track, playlistMap),
      }))
      .sort((a, b) => Number(b.inPlaylist) - Number(a.inPlaylist) || a.index - b.index)
      .map(({ item }) => item);
  }, [items, playlistMap, prioritizePlaylistMatches]);

  if (!visibleItems.length) return <EmptyState title="暂无内容" />;
  return (
    <div className="songList">
      {visibleItems.map(({ track, meta }) => (
        <TrackRow
          key={trackKey(track)}
          track={track}
          meta={meta}
          token={token}
          roomId={roomId}
          playlists={playlists}
          queued={queuedKeys.has(trackKey(track))}
          queuedKeys={queuedKeys}
          playlistMap={playlistMap}
          playlistMemberships={playlistMap[trackKey(track)] || []}
          onChanged={onChanged}
          expandBilibiliParts={expandBilibiliParts}
          prioritizePlaylistMatches={prioritizePlaylistMatches}
        />
      ))}
    </div>
  );
}

function isTrackInPlaylist(track: Track, playlistMap: Record<string, PlaylistMembership[]>): boolean {
  if ((playlistMap[trackKey(track)] || []).length > 0) return true;
  return Array.isArray(track.parts) && track.parts.some((part) => (playlistMap[trackKey(part)] || []).length > 0);
}

function TrackRow({
  track: initialTrack,
  meta,
  token,
  roomId,
  playlists,
  queued,
  queuedKeys,
  playlistMap,
  playlistMemberships,
  onChanged,
  expandBilibiliParts,
  prioritizePlaylistMatches,
}: {
  track: Track;
  meta?: string;
  token: string;
  roomId: number;
  playlists: Playlist[];
  queued: boolean;
  queuedKeys: Set<string>;
  playlistMap: Record<string, PlaylistMembership[]>;
  playlistMemberships: PlaylistMembership[];
  onChanged: () => void;
  expandBilibiliParts: boolean;
  prioritizePlaylistMatches: boolean;
}) {
  const [failedTrack, setFailedTrack] = useState<Track | null>(null);
  useEffect(() => { setFailedTrack(null); }, [initialTrack]);
  const track = initialTrack.availability === "unavailable" ? initialTrack : failedTrack || initialTrack;
  const unavailable = track.availability === "unavailable";
  const [busy, setBusy] = useState<"queue" | "playlist" | null>(null);
  const [queueError, setQueueError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const playlisted = playlistMemberships.length > 0;
  const parts = track.source === "bilibili" && Array.isArray(track.parts) ? track.parts : [];
  const expandable = expandBilibiliParts && parts.length > 0;
  return (
    <>
      <article className={`songRow ${expandable ? "songRowExpandable" : ""} ${unavailable ? "songRowUnavailable" : ""}`}>
        <TrackCover track={track} />
        <div className="songInfo">
          <strong>{track.title}</strong>
          <span>{meta || `${track.source} · ${track.artist || "-"}`}</span>
          {unavailable && <span className="unavailableLabel">已失效 · {track.unavailable_reason || "无法播放"}</span>}
        </div>
        <div className="rowActions">
          {expandable ? (
            <>
              <button
                className={`smallButton ${expanded ? "active" : ""}`}
                aria-expanded={expanded}
                onClick={() => setExpanded((value) => !value)}
              >
                <ListMusic />{expanded ? "收起" : "展开"}
              </button>
              {expanded && (
                <BilibiliCollectionActions
                  track={track}
                  parts={parts}
                  token={token}
                  roomId={roomId}
                  playlists={playlists}
                  onChanged={onChanged}
                />
              )}
            </>
          ) : (
            <>
              <button
                className={`smallButton ${queued ? "active" : ""}`}
                disabled={busy !== null || unavailable}
                onClick={async () => {
                  setBusy("queue");
                  setQueueError("");
                  try {
                    await api(`/api/rooms/${roomId}/queue`, { method: "POST", token, json: trackPayload(track) });
                    onChanged();
                  } catch (error) {
                    const failed = unavailableTrackFromError(error);
                    if (failed) { setFailedTrack(failed); onChanged(); }
                    setQueueError(error instanceof Error ? error.message : "点歌失败，请重试");
                  } finally {
                    setBusy(null);
                  }
                }}
              >
                <BadgePlus />{unavailable ? "已失效" : busy === "queue" ? "点歌中…" : queued ? "已点" : "点歌"}
              </button>
              {unavailable ? <UnavailableTrackActions track={track} token={token} onChanged={onChanged} /> : <PlaylistQuickButton
                track={track}
                token={token}
                playlists={playlists}
                memberships={playlistMemberships}
                playlisted={playlisted}
                busy={busy === "playlist"}
                setBusy={(value) => setBusy(value ? "playlist" : null)}
                onChanged={onChanged}
              />}
            </>
          )}
        </div>
      </article>
      {queueError && <p className="actionError rowActionError" role="alert">{queueError}</p>}
      {expandable && expanded && (
        <div className="songPartsCard" role="region" aria-label={`${track.title} 分P`}>
          <TrackList
            items={parts.map((part, index) => ({
              track: part,
              meta: `P${part.part_number ?? index + 1} · ${part.artist || "未知歌手"}${part.duration_ms ? ` · ${formatTime(part.duration_ms)}` : ""}`,
            }))}
            token={token}
            roomId={roomId}
            playlists={playlists}
            queuedKeys={queuedKeys}
            playlistMap={playlistMap}
            onChanged={onChanged}
            prioritizePlaylistMatches={prioritizePlaylistMatches}
          />
        </div>
      )}
    </>
  );
}

function PlaylistQuickButton({
  track,
  token,
  playlists,
  memberships,
  playlisted,
  busy,
  setBusy,
  onChanged,
}: {
  track: Track;
  token: string;
  playlists: Playlist[];
  memberships: PlaylistMembership[];
  playlisted: boolean;
  busy: boolean;
  setBusy: (value: boolean) => void;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className={`smallButton ${playlisted ? "active" : ""}`} disabled={busy} onClick={() => setOpen(true)}>
        <Heart />{playlisted ? "已收藏" : "入歌单"}
      </button>
      {open && (
        <PlaylistPickerCard
          track={track}
          token={token}
          playlists={playlists}
          memberships={memberships}
          busy={busy}
          setBusy={setBusy}
          onClose={() => setOpen(false)}
          onChanged={onChanged}
        />
      )}
    </>
  );
}

function PlaylistPickerCard({
  track,
  token,
  playlists,
  memberships,
  busy,
  setBusy,
  onClose,
  onChanged,
}: {
  track: Track;
  token: string;
  playlists: Playlist[];
  memberships: PlaylistMembership[];
  busy: boolean;
  setBusy: (value: boolean) => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [localPlaylists, setLocalPlaylists] = useState(playlists);
  const [checkedIds, setCheckedIds] = useState(() => new Set(memberships.map((item) => item.playlist_id)));
  const [newName, setNewName] = useState("");
  const [error, setError] = useState("");
  const membershipByPlaylist = useMemo(
    () => new Map(memberships.map((item) => [item.playlist_id, item])),
    [memberships],
  );

  async function createPlaylist() {
    const name = newName.trim();
    if (!name) return;
    setError("");
    setBusy(true);
    try {
      const created = await api<Playlist>("/api/playlists", { method: "POST", token, json: { name } });
      setLocalPlaylists((items) => [...items, created]);
      setCheckedIds((previous) => {
        const next = new Set(previous);
        next.add(created.id);
        return next;
      });
      setNewName("");
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建歌单失败");
    } finally {
      setBusy(false);
    }
  }

  async function saveMemberships() {
    setError("");
    setBusy(true);
    try {
      for (const membership of memberships) {
        if (!checkedIds.has(membership.playlist_id)) {
          await api(`/api/playlists/${membership.playlist_id}/items/${membership.item_id}`, { method: "DELETE", token });
        }
      }
      for (const playlist of localPlaylists) {
        if (checkedIds.has(playlist.id) && !membershipByPlaylist.has(playlist.id)) {
          await api(`/api/playlists/${playlist.id}/items`, { method: "POST", token, json: trackPayload(track) });
        }
      }
      onChanged();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存收藏失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <CardDialog title="收藏到歌单" onClose={onClose} busy={busy}>
      <div className="pickerTrack">
        <TrackCover track={track} />
        <div className="songInfo">
          <strong>{track.title}</strong>
          <span>{track.artist || "-"} · {track.source}</span>
        </div>
      </div>
      <form className="inlineCreate dialogCreate" onSubmit={(event) => { event.preventDefault(); createPlaylist(); }}>
        <label className="fieldLabel growField"><span>新歌单名称</span><input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="例如：通勤" /></label>
        <button className="smallButton" disabled={busy || !newName.trim()}><Plus />创建</button>
      </form>
      {localPlaylists.length ? (
        <div className="playlistPickerList">
          {localPlaylists.map((playlist) => (
            <label className="checkRow" key={playlist.id}>
              <input
                type="checkbox"
                checked={checkedIds.has(playlist.id)}
                onChange={(event) => {
                  setCheckedIds((previous) => {
                    const next = new Set(previous);
                    if (event.target.checked) next.add(playlist.id);
                    else next.delete(playlist.id);
                    return next;
                  });
                }}
              />
              <span>{playlist.name}</span>
              <em>{playlist.item_count || 0} 首</em>
            </label>
          ))}
        </div>
      ) : (
        <EmptyState title="暂无歌单" meta="创建一个歌单后再保存。" />
      )}
      {error && <p className="hintText">{error}</p>}
      <div className="dialogActions">
        <button className="glassButton" onClick={onClose} disabled={busy}>取消</button>
        <button className="primaryButton" onClick={saveMemberships} disabled={busy}>
          <Check />{busy ? "保存中" : "保存"}
        </button>
      </div>
    </CardDialog>
  );
}
