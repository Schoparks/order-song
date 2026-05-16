import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronLeft, CloudDownload, Pencil, Plus, Search, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import type { Playlist, PlaylistItem, Track } from "../types";
import { CardDialog, ConfirmDialog, EmptyState, ScrollArea } from "./common";
import { RandomPlaylistOrderDialog } from "./RandomPlaylistOrderDialog";
import { TrackList, type PlaylistMembership } from "./TrackList";

export function SearchOverlay({
  token,
  roomId,
  queuedKeys,
  playlistMap,
  playlists,
  historyLimit,
  onClose,
  onChanged,
}: {
  token: string;
  roomId: number;
  queuedKeys: Set<string>;
  playlistMap: Record<string, PlaylistMembership[]>;
  playlists: Playlist[];
  historyLimit: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [query, setQuery] = useState("");
  const [history, setHistory] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem("searchHistory") || "[]") as string[];
    } catch {
      return [];
    }
  });
  const [results, setResults] = useState<Track[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [historyVisible, setHistoryVisible] = useState(false);
  const historyScopeRef = useRef<HTMLDivElement | null>(null);

  async function run(nextQuery = query) {
    const q = nextQuery.trim();
    if (!q) return;
    setBusy(true);
    setError("");
    setHistoryVisible(false);
    const nextHistory = [q, ...history.filter((item) => item !== q)].slice(0, historyLimit);
    setHistory(nextHistory);
    localStorage.setItem("searchHistory", JSON.stringify(nextHistory));
    try {
      setResults(await api<Track[]>(`/api/search?q=${encodeURIComponent(q)}`, { token }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "搜索失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="overlay"
      onPointerDownCapture={(event) => {
        if (!historyVisible) return;
        const node = historyScopeRef.current;
        if (node && !node.contains(event.target as Node)) setHistoryVisible(false);
      }}
    >
      <section className="searchSheet glassPanel">
        <div className="searchSticky">
          <div className="sheetHeader">
            <h2>搜索</h2>
            <button className="iconButton" onClick={onClose}><X /></button>
          </div>
          <div ref={historyScopeRef}>
            <div className="searchInputWrap">
              <Search />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onFocus={() => setHistoryVisible(true)}
                onClick={() => setHistoryVisible(true)}
                onKeyDown={(e) => e.key === "Enter" && run()}
                autoFocus
                placeholder="歌曲、歌手、BV 号或 B 站标题"
              />
              <button className="primaryButton" onClick={() => run()} disabled={busy}>{busy ? "搜索中" : "搜索"}</button>
            </div>
            {historyVisible && !!history.length && (
              <div className="chips">
                {history.map((item) => (
                  <button key={item} onClick={() => { setQuery(item); setHistoryVisible(false); run(item); }}>{item}</button>
                ))}
              </div>
            )}
          </div>
          {error && <p className="hintText">{error}</p>}
        </div>
        <ScrollArea className="searchResults">
          <TrackList
            items={results.map((track) => ({
              track,
              meta: track.source === "bilibili" && track.parts?.length
                ? `bilibili · ${track.artist || "-"} · ${track.parts.length}P`
                : undefined,
            }))}
            token={token}
            roomId={roomId}
            playlists={playlists}
            queuedKeys={queuedKeys}
            playlistMap={playlistMap}
            onChanged={onChanged}
            expandBilibiliParts
            prioritizePlaylistMatches
          />
        </ScrollArea>
      </section>
    </div>
  );
}

export function PlaylistsView({
  token,
  roomId,
  playlists,
  playlistMap,
  queuedKeys,
  onChanged,
}: {
  token: string;
  roomId: number;
  playlists: Playlist[];
  playlistMap: Record<string, PlaylistMembership[]>;
  queuedKeys: Set<string>;
  onChanged: () => void;
}) {
  const [selected, setSelected] = useState<Playlist | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [renameName, setRenameName] = useState("");
  const [neteaseUrl, setNeteaseUrl] = useState("");
  const [neteaseName, setNeteaseName] = useState("");
  const [importMessage, setImportMessage] = useState("");
  const [importing, setImporting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Playlist | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [randomOrderTarget, setRandomOrderTarget] = useState<{ playlist: Playlist; itemCount: number } | null>(null);
  const [randomOrdering, setRandomOrdering] = useState(false);
  const [randomOrderError, setRandomOrderError] = useState("");
  const itemsQuery = useQuery({
    queryKey: ["playlist-items", selected?.id, token],
    queryFn: () => api<PlaylistItem[]>(`/api/playlists/${selected?.id}/items`, { token }),
    enabled: !!selected,
  });

  async function createPlaylist() {
    const name = newName.trim();
    if (!name) return;
    await api("/api/playlists", { method: "POST", token, json: { name } });
    setNewName("");
    setCreateOpen(false);
    onChanged();
  }

  async function renamePlaylist() {
    if (!selected) return;
    const name = renameName.trim();
    if (!name) return;
    await api(`/api/playlists/${selected.id}`, { method: "PATCH", token, json: { name } });
    setSelected({ ...selected, name });
    setRenameOpen(false);
    onChanged();
  }

  async function importNeteasePlaylist() {
    const url = neteaseUrl.trim();
    if (!url) return;
    setImporting(true);
    setImportMessage("");
    try {
      const result = await api<{ playlist_name: string; added: number; skipped: number; total: number }>("/api/playlists/import-netease", {
        method: "POST",
        token,
        json: { url, name: neteaseName.trim() || undefined },
      });
      setNeteaseUrl("");
      setNeteaseName("");
      setImportOpen(false);
      setImportMessage(`已导入「${result.playlist_name}」：${result.added} 首，跳过 ${result.skipped} 首。`);
      onChanged();
    } catch (err) {
      setImportMessage(err instanceof Error ? err.message : "导入失败");
    } finally {
      setImporting(false);
    }
  }

  async function deletePlaylist() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api(`/api/playlists/${deleteTarget.id}`, { method: "DELETE", token });
      setDeleteTarget(null);
      onChanged();
    } finally {
      setDeleting(false);
    }
  }

  function openRandomOrderDialog(playlist: Playlist, itemCount = playlist.item_count || 0) {
    if (itemCount < 1) return;
    setRandomOrderError("");
    setRandomOrderTarget({ playlist, itemCount });
  }

  async function randomOrderPlaylist(count: number) {
    if (!randomOrderTarget) return;
    setRandomOrdering(true);
    setRandomOrderError("");
    try {
      await api(`/api/rooms/${roomId}/queue/playlist`, {
        method: "POST",
        token,
        json: { playlist_id: randomOrderTarget.playlist.id, count },
      });
      setRandomOrderTarget(null);
      onChanged();
    } catch (err) {
      setRandomOrderError(err instanceof Error ? err.message : "随机点歌失败");
    } finally {
      setRandomOrdering(false);
    }
  }

  const randomOrderDialog = randomOrderTarget ? (
    <RandomPlaylistOrderDialog
      targetId={randomOrderTarget.playlist.id}
      targetName={randomOrderTarget.playlist.name}
      itemCount={randomOrderTarget.itemCount}
      busy={randomOrdering}
      error={randomOrderError}
      onCancel={() => setRandomOrderTarget(null)}
      onConfirm={randomOrderPlaylist}
    />
  ) : null;

  if (selected) {
    const selectedItemCount = itemsQuery.data?.length ?? selected.item_count ?? 0;
    return (
      <div className="playlistDetail">
        <div className="sectionToolbar">
          <button className="iconTextButton" onClick={() => setSelected(null)}><ChevronLeft />返回</button>
          <h3 className="editableTitle">
            {selected.name}
            <button
              className="iconButton titleEditButton"
              title="重命名歌单"
              aria-label="重命名歌单"
              onClick={() => {
                setRenameName(selected.name);
                setRenameOpen(true);
              }}
            >
              <Pencil />
            </button>
          </h3>
          <button
            className="glassButton"
            disabled={selectedItemCount < 1}
            onClick={() => openRandomOrderDialog(selected, selectedItemCount)}
          >
            随机点歌
          </button>
        </div>
        <TrackList
          items={(itemsQuery.data || []).map((item) => ({ track: item.track }))}
          token={token}
          roomId={roomId}
          playlists={playlists}
          queuedKeys={queuedKeys}
          playlistMap={playlistMap}
          onChanged={onChanged}
        />
        {renameOpen && (
          <CardDialog title="重命名歌单" onClose={() => setRenameOpen(false)}>
            <form
              className="dialogForm"
              onSubmit={(event) => {
                event.preventDefault();
                renamePlaylist();
              }}
            >
              <input value={renameName} onChange={(event) => setRenameName(event.target.value)} placeholder="歌单名称" />
              <div className="dialogActions">
                <button type="button" className="glassButton" onClick={() => setRenameOpen(false)}>取消</button>
                <button className="primaryButton" disabled={!renameName.trim()}><Check />保存</button>
              </div>
            </form>
          </CardDialog>
        )}
        {randomOrderDialog}
      </div>
    );
  }

  return (
    <div className="playlistList">
      <div className="sectionToolbar">
        <h3>我的歌单</h3>
        <div className="toolbarActions">
          <button className="smallButton" onClick={() => setCreateOpen(true)}><Plus />新建歌单</button>
          <button className="smallButton" onClick={() => setImportOpen(true)}><CloudDownload />导入网易云</button>
        </div>
      </div>
      {importMessage && <p className="hintText">{importMessage}</p>}
      {!playlists.length && <EmptyState title="暂无歌单" meta="搜索后可将歌曲加入歌单。" />}
      {playlists.map((playlist) => (
        <article key={playlist.id} className="playlistRow" onClick={() => setSelected(playlist)}>
          <div>
            <strong>{playlist.name}</strong>
            <span>{playlist.item_count || 0} 首歌曲</span>
          </div>
          <div className="rowActions playlistRowActions">
            <button
              className="smallButton"
              onClick={(e) => {
                e.stopPropagation();
                openRandomOrderDialog(playlist);
              }}
              disabled={(playlist.item_count || 0) < 1}
            >
              随机点歌
            </button>
            <button
              className="iconButton danger playlistDeleteButton"
              onClick={async (e) => {
                e.stopPropagation();
                setDeleteTarget(playlist);
              }}
            >
              <Trash2 />
            </button>
          </div>
        </article>
      ))}
      {randomOrderDialog}
      {deleteTarget && (
        <ConfirmDialog
          title="删除歌单"
          message={`删除歌单“${deleteTarget.name}”？`}
          confirmText="删除"
          busy={deleting}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={deletePlaylist}
        />
      )}
      {createOpen && (
        <CardDialog title="新建歌单" onClose={() => setCreateOpen(false)}>
          <form
            className="dialogForm"
            onSubmit={(event) => {
              event.preventDefault();
              createPlaylist();
            }}
          >
            <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="歌单名称" />
            <div className="dialogActions">
              <button type="button" className="glassButton" onClick={() => setCreateOpen(false)}>取消</button>
              <button className="primaryButton" disabled={!newName.trim()}><Plus />创建</button>
            </div>
          </form>
        </CardDialog>
      )}
      {importOpen && (
        <CardDialog title="导入网易云歌单" onClose={() => setImportOpen(false)}>
          <form
            className="dialogForm"
            onSubmit={(event) => {
              event.preventDefault();
              importNeteasePlaylist();
            }}
          >
            <input value={neteaseUrl} onChange={(event) => setNeteaseUrl(event.target.value)} placeholder="网易云歌单链接或 ID" />
            <input value={neteaseName} onChange={(event) => setNeteaseName(event.target.value)} placeholder="导入后的歌单名，可留空" />
            <div className="dialogActions">
              <button type="button" className="glassButton" onClick={() => setImportOpen(false)} disabled={importing}>取消</button>
              <button className="primaryButton" disabled={importing || !neteaseUrl.trim()}>
                <CloudDownload />{importing ? "导入中" : "导入"}
              </button>
            </div>
          </form>
        </CardDialog>
      )}
    </div>
  );
}
