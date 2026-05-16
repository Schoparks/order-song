import { useMemo, useState } from "react";
import { Check, Heart, ListMusic, Plus, Shuffle } from "lucide-react";
import { api } from "../lib/api";
import { trackKey, trackPayload } from "../lib/track";
import type { Playlist, Track } from "../types";
import { CardDialog, EmptyState, SegmentedTabs } from "./common";
import { RandomPlaylistOrderDialog } from "./RandomPlaylistOrderDialog";

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

export function BilibiliCollectionActions({
  track,
  parts,
  token,
  roomId,
  playlists,
  onChanged,
}: {
  track: Track;
  parts: Track[];
  token: string;
  roomId: number;
  playlists: Playlist[];
  onChanged: () => void;
}) {
  return (
    <>
      <BilibiliCollectionRandomButton
        track={track}
        parts={parts}
        token={token}
        roomId={roomId}
        onChanged={onChanged}
      />
      <BilibiliCollectionPlaylistButton
        track={track}
        parts={parts}
        token={token}
        playlists={playlists}
        onChanged={onChanged}
      />
    </>
  );
}

function BilibiliCollectionRandomButton({
  track,
  parts,
  token,
  roomId,
  onChanged,
}: {
  track: Track;
  parts: Track[];
  token: string;
  roomId: number;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function randomOrderCollection(count: number) {
    setBusy(true);
    setError("");
    try {
      await api(`/api/rooms/${roomId}/queue/batch`, {
        method: "POST",
        token,
        json: { items: parts.map(trackPayload), count, random: true },
      });
      setOpen(false);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "随机点歌失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button className="smallButton" disabled={busy || parts.length < 1} onClick={() => setOpen(true)}>
        <Shuffle />随机点歌
      </button>
      {open && (
        <RandomPlaylistOrderDialog
          targetId={trackKey(track)}
          targetName={track.title}
          itemCount={parts.length}
          itemLabel={`${parts.length} P`}
          unitLabel="P"
          busy={busy}
          error={error}
          onCancel={() => setOpen(false)}
          onConfirm={randomOrderCollection}
        />
      )}
    </>
  );
}

function BilibiliCollectionPlaylistButton({
  track,
  parts,
  token,
  playlists,
  onChanged,
}: {
  track: Track;
  parts: Track[];
  token: string;
  playlists: Playlist[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
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
