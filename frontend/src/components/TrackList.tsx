import { useMemo, useState } from "react";
import { BadgePlus, Check, Heart, ListMusic, Plus } from "lucide-react";
import { api } from "../lib/api";
import { trackKey, trackPayload } from "../lib/track";
import type { Playlist, Track } from "../types";
import { CardDialog, EmptyState, SegmentedTabs, TrackCover } from "./common";

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
}: {
  items: Array<{ track: Track; meta?: string }>;
  token: string;
  roomId: number;
  playlists: Playlist[];
  queuedKeys: Set<string>;
  playlistMap: Record<string, PlaylistMembership[]>;
  onChanged: () => void;
  expandBilibiliParts?: boolean;
}) {
  if (!items.length) return <EmptyState title="暂无内容" />;
  return (
    <div className="songList">
      {items.map(({ track, meta }) => (
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
        />
      ))}
    </div>
  );
}

function TrackRow({
  track,
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
}) {
  const [busy, setBusy] = useState<"queue" | "playlist" | null>(null);
  const [expanded, setExpanded] = useState(false);
  const playlisted = playlistMemberships.length > 0;
  const parts = track.source === "bilibili" && Array.isArray(track.parts) ? track.parts : [];
  const expandable = expandBilibiliParts && parts.length > 0;
  return (
    <>
      <article className={`songRow ${expandable ? "songRowExpandable" : ""}`}>
        <TrackCover track={track} />
        <div className="songInfo">
          <strong>{track.title}</strong>
          <span>{meta || `${track.source} · ${track.artist || "-"}`}</span>
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
              <BilibiliCollectionPlaylistButton
                track={track}
                parts={parts}
                token={token}
                playlists={playlists}
                busy={busy === "playlist"}
                setBusy={(value) => setBusy(value ? "playlist" : null)}
                onChanged={onChanged}
              />
            </>
          ) : (
            <>
              <button
                className={`smallButton ${queued ? "active" : ""}`}
                disabled={busy === "queue"}
                onClick={async () => {
                  setBusy("queue");
                  await api(`/api/rooms/${roomId}/queue`, { method: "POST", token, json: trackPayload(track) }).finally(() => setBusy(null));
                  onChanged();
                }}
              >
                <BadgePlus />{queued ? "已点" : "点歌"}
              </button>
              <PlaylistQuickButton
                track={track}
                token={token}
                playlists={playlists}
                memberships={playlistMemberships}
                playlisted={playlisted}
                busy={busy === "playlist"}
                setBusy={(value) => setBusy(value ? "playlist" : null)}
                onChanged={onChanged}
              />
            </>
          )}
        </div>
      </article>
      {expandable && expanded && (
        <div className="songPartsCard" role="region" aria-label={`${track.title} 分P`}>
          <TrackList
            items={parts.map((part, index) => ({
              track: part,
              meta: `P${index + 1} · ${part.source} · ${part.artist || "-"}`,
            }))}
            token={token}
            roomId={roomId}
            playlists={playlists}
            queuedKeys={queuedKeys}
            playlistMap={playlistMap}
            onChanged={onChanged}
          />
        </div>
      )}
    </>
  );
}

type CollectionMode = "create" | "existing";

interface BilibiliCollectionImportResult {
  ok: boolean;
  added: number;
  skipped: number;
  total: number;
  track_total?: number;
  results: Array<{
    playlist_id: number;
    playlist_name: string;
    added: number;
    skipped: number;
  }>;
}

function BilibiliCollectionPlaylistButton({
  track,
  parts,
  token,
  playlists,
  busy,
  setBusy,
  onChanged,
}: {
  track: Track;
  parts: Track[];
  token: string;
  playlists: Playlist[];
  busy: boolean;
  setBusy: (value: boolean) => void;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="smallButton" disabled={busy} onClick={() => setOpen(true)}>
        <Heart />入歌单
      </button>
      {open && (
        <BilibiliCollectionPlaylistDialog
          track={track}
          parts={parts}
          token={token}
          playlists={playlists}
          busy={busy}
          setBusy={setBusy}
          onClose={() => setOpen(false)}
          onChanged={onChanged}
        />
      )}
    </>
  );
}

function BilibiliCollectionPlaylistDialog({
  track,
  parts,
  token,
  playlists,
  busy,
  setBusy,
  onClose,
  onChanged,
}: {
  track: Track;
  parts: Track[];
  token: string;
  playlists: Playlist[];
  busy: boolean;
  setBusy: (value: boolean) => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [mode, setMode] = useState<CollectionMode>("create");
  const [newName, setNewName] = useState(() => track.title.trim() || "B站合集");
  const [checkedIds, setCheckedIds] = useState(() => new Set<number>());
  const [error, setError] = useState("");
  const selectedIds = useMemo(() => Array.from(checkedIds), [checkedIds]);

  async function importCollection(nextMode: CollectionMode) {
    const name = newName.trim();
    if (nextMode === "create" && !name) return;
    if (nextMode === "existing" && !selectedIds.length) return;
    setError("");
    setBusy(true);
    try {
      await api<BilibiliCollectionImportResult>("/api/playlists/import-bilibili-collection", {
        method: "POST",
        token,
        json: nextMode === "create"
          ? { mode: "create", name, tracks: parts.map(trackPayload) }
          : { mode: "existing", playlist_ids: selectedIds, tracks: parts.map(trackPayload) },
      });
      onChanged();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存合集失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <CardDialog title="合集加入歌单" onClose={onClose}>
      <div className="collectionSummary">
        <strong>{track.title}</strong>
        <span>{track.artist || "-"} · 共 {parts.length} P</span>
      </div>
      <SegmentedTabs
        className="collectionModeTabs"
        value={mode}
        items={[
          { value: "create", label: "新建歌单", icon: <Plus /> },
          { value: "existing", label: "加入已有", icon: <ListMusic /> },
        ]}
        onChange={(value) => setMode(value === "existing" ? "existing" : "create")}
      />
      {mode === "create" ? (
        <form
          className="dialogForm"
          onSubmit={(event) => {
            event.preventDefault();
            void importCollection("create");
          }}
        >
          <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="歌单名称" />
          {error && <p className="hintText">{error}</p>}
          <div className="dialogActions">
            <button type="button" className="glassButton" onClick={onClose} disabled={busy}>取消</button>
            <button className="primaryButton" disabled={busy || !newName.trim()}>
              <Plus />{busy ? "保存中" : "创建并加入"}
            </button>
          </div>
        </form>
      ) : (
        <>
          {playlists.length ? (
            <div className="playlistPickerList">
              {playlists.map((playlist) => (
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
            <EmptyState title="暂无歌单" meta="可先新建歌单。" />
          )}
          {error && <p className="hintText">{error}</p>}
          <div className="dialogActions">
            <button className="glassButton" onClick={onClose} disabled={busy}>取消</button>
            <button className="primaryButton" onClick={() => { void importCollection("existing"); }} disabled={busy || !selectedIds.length}>
              <Check />{busy ? "保存中" : "加入选中歌单"}
            </button>
          </div>
        </>
      )}
    </CardDialog>
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
    <CardDialog title="收藏到歌单" onClose={onClose}>
      <div className="pickerTrack">
        <TrackCover track={track} />
        <div className="songInfo">
          <strong>{track.title}</strong>
          <span>{track.artist || "-"} · {track.source}</span>
        </div>
      </div>
      <form className="inlineCreate dialogCreate" onSubmit={(event) => { event.preventDefault(); createPlaylist(); }}>
        <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="新歌单" />
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
