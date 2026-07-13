import { useEffect, useId, useRef, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronLeft, CloudDownload, Pencil, Plus, Search, Trash2, X } from "lucide-react";
import { api } from "../lib/api";
import { formatTime } from "../lib/time";
import { useDialogViewportLock } from "../hooks/useStableViewportHeight";
import type { Playlist, PlaylistItem, Track } from "../types";
import { CardDialog, ConfirmDialog, EmptyState, ScrollArea, useModalFocus } from "./common";
import { RandomPlaylistOrderDialog } from "./RandomPlaylistOrderDialog";
import { TrackList, type PlaylistMembership } from "./TrackList";

type SearchSource = "all" | "bilibili" | "netease" | "qq";
const searchCache = new Map<string, Track[]>();

export function SearchOverlay({
  token,
  roomId,
  queuedKeys,
  playlistMap,
  playlists,
  historyLimit,
  qqMusicEnabled,
  onClose,
  onChanged,
}: {
  token: string;
  roomId: number;
  queuedKeys: Set<string>;
  playlistMap: Record<string, PlaylistMembership[]>;
  playlists: Playlist[];
  historyLimit: number;
  qqMusicEnabled: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  useDialogViewportLock();
  const titleId = useId();
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const { dialogRef, onDialogKeyDown } = useModalFocus({ onClose, initialFocusRef: searchInputRef });
  const [query, setQuery] = useState("");
  const [history, setHistory] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem("searchHistory") || "[]") as string[];
    } catch {
      return [];
    }
  });
  const [results, setResults] = useState<Track[]>([]);
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [source, setSource] = useState<SearchSource>("all");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [historyVisible, setHistoryVisible] = useState(false);
  const historyScopeRef = useRef<HTMLDivElement | null>(null);
  const searchRequestRef = useRef<{ id: number; controller: AbortController } | null>(null);
  const sourceSegmentCount = qqMusicEnabled ? 4 : 3;
  const sourceSegmentIndex = Math.min(sourceSegmentCount - 1, Math.max(0, ["all", "bilibili", "netease", "qq"].indexOf(source)));
  const sourceSegmentStyle = {
    "--segment-count": sourceSegmentCount,
    "--segment-index": sourceSegmentIndex,
    "--segment-width": `${100 / sourceSegmentCount}%`,
    "--segment-shift": `${sourceSegmentIndex * 100}%`,
  } as CSSProperties;

  useEffect(() => () => searchRequestRef.current?.controller.abort(), []);

  async function run(nextQuery = query, nextSource = source, writeHistory = true) {
    const q = nextQuery.trim();
    if (!q) return;
    setSubmittedQuery(q);
    setBusy(true);
    setError("");
    setHistoryVisible(false);
    if (writeHistory) {
      const nextHistory = [q, ...history.filter((item) => item !== q)].slice(0, historyLimit);
      setHistory(nextHistory);
      localStorage.setItem("searchHistory", JSON.stringify(nextHistory));
    }
    searchRequestRef.current?.controller.abort();
    const request = { id: (searchRequestRef.current?.id || 0) + 1, controller: new AbortController() };
    searchRequestRef.current = request;
    try {
      const cacheKey = `${token}:${q.toLocaleLowerCase()}:${nextSource}`;
      const cached = searchCache.get(cacheKey);
      const nextResults = cached || await api<Track[]>(`/api/search?q=${encodeURIComponent(q)}&source=${nextSource}`, { token, signal: request.controller.signal });
      if (!cached) {
        searchCache.set(cacheKey, nextResults);
        if (searchCache.size > 60) searchCache.delete(searchCache.keys().next().value as string);
      }
      if (searchRequestRef.current?.id === request.id) setResults(nextResults);
    } catch (err) {
      if (!(err instanceof Error && err.name === "AbortError") && searchRequestRef.current?.id === request.id) {
        setResults([]);
        setError(err instanceof Error ? err.message : "搜索失败");
      }
    } finally {
      if (searchRequestRef.current?.id === request.id) setBusy(false);
    }
  }

  function changeSource(nextSource: SearchSource) {
    setSource(nextSource);
    if (submittedQuery) void run(submittedQuery, nextSource, false);
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
      <section
        id="search-overlay"
        ref={dialogRef}
        className="searchSheet glassPanel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
      >
        <div className="searchSticky">
          <div className="sheetHeader">
            <h2 id={titleId}>搜索</h2>
            <button className="iconButton" aria-label="关闭搜索" onClick={onClose}><X /></button>
          </div>
          <div ref={historyScopeRef}>
            <label className="fieldLabel searchFieldLabel" htmlFor="song-search-input">搜索歌曲</label>
            <div className="searchInputWrap">
              <Search />
              <input
                ref={searchInputRef}
                id="song-search-input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onFocus={() => setHistoryVisible(true)}
                onClick={() => setHistoryVisible(true)}
                onKeyDown={(e) => e.key === "Enter" && run()}
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
          {error && <p className="actionError" role="alert">{error}</p>}
          <div className="sourceFilters" role="radiogroup" aria-label="搜索来源" style={sourceSegmentStyle}>
            {([['all', '全部'], ['bilibili', 'B站'], ['netease', '网易云'], ...(qqMusicEnabled ? [['qq', 'QQ音乐'] as const] : [])] as const).map(([value, label]) => (
              <button key={value} role="radio" className={source === value ? "active" : ""} aria-checked={source === value} onClick={() => changeSource(value)}>{label}</button>
            ))}
          </div>
        </div>
        <ScrollArea className="searchResults" resetKey={`${submittedQuery}:${source}`}>
          <TrackList
            items={results.map((track) => ({
              track,
              meta: [
                track.source === "bilibili" ? "B站" : track.source === "netease" ? "网易云" : track.source === "qq" ? "QQ音乐" : track.source,
                track.artist || "未知歌手",
                track.parts?.length ? `合集 · 可点 ${track.parts.length} P` : track.duration_ms ? formatTime(track.duration_ms) : null,
              ].filter(Boolean).join(" · "),
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
  qqMusicEnabled,
  onChanged,
}: {
  token: string;
  roomId: number;
  playlists: Playlist[];
  playlistMap: Record<string, PlaylistMembership[]>;
  queuedKeys: Set<string>;
  qqMusicEnabled: boolean;
  onChanged: () => void;
}) {
  const [selected, setSelected] = useState<Playlist | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [renameName, setRenameName] = useState("");
  const [savingDialog, setSavingDialog] = useState<"create" | "rename" | null>(null);
  const [dialogError, setDialogError] = useState("");
  const [playlistUrl, setPlaylistUrl] = useState("");
  const [playlistName, setPlaylistName] = useState("");
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
    setSavingDialog("create");
    setDialogError("");
    try {
      await api("/api/playlists", { method: "POST", token, json: { name } });
      setNewName("");
      setCreateOpen(false);
      onChanged();
    } catch (error) {
      setDialogError(error instanceof Error ? error.message : "创建歌单失败");
    } finally {
      setSavingDialog(null);
    }
  }

  async function renamePlaylist() {
    if (!selected) return;
    const name = renameName.trim();
    if (!name) return;
    setSavingDialog("rename");
    setDialogError("");
    try {
      await api(`/api/playlists/${selected.id}`, { method: "PATCH", token, json: { name } });
      setSelected({ ...selected, name });
      setRenameOpen(false);
      onChanged();
    } catch (error) {
      setDialogError(error instanceof Error ? error.message : "重命名失败");
    } finally {
      setSavingDialog(null);
    }
  }

  async function importPlaylist() {
    const url = playlistUrl.trim();
    if (!url) return;
    setImporting(true);
    setImportMessage("");
    try {
      const result = await api<{ playlist_name: string; added: number; skipped: number; total: number }>("/api/playlists/import", {
        method: "POST",
        token,
        json: { url, name: playlistName.trim() || undefined },
      });
      setPlaylistUrl("");
      setPlaylistName("");
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
                setDialogError("");
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
          <CardDialog title="重命名歌单" onClose={() => setRenameOpen(false)} busy={savingDialog === "rename"}>
            <form
              className="dialogForm"
              onSubmit={(event) => {
                event.preventDefault();
                renamePlaylist();
              }}
            >
              <label className="fieldLabel"><span>歌单名称</span><input value={renameName} onChange={(event) => setRenameName(event.target.value)} placeholder="请输入新名称" /></label>
              {dialogError && <p className="actionError" role="alert">{dialogError}</p>}
              <div className="dialogActions">
                <button type="button" className="glassButton" onClick={() => setRenameOpen(false)} disabled={savingDialog === "rename"}>取消</button>
                <button className="primaryButton" disabled={!renameName.trim() || savingDialog === "rename"}><Check />{savingDialog === "rename" ? "保存中…" : "保存"}</button>
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
          <button className="smallButton" onClick={() => { setDialogError(""); setCreateOpen(true); }}><Plus />新建歌单</button>
          <button className="smallButton" onClick={() => { setImportMessage(""); setImportOpen(true); }}><CloudDownload />导入歌单</button>
        </div>
      </div>
      {!importOpen && importMessage && <p className="hintText">{importMessage}</p>}
      {!playlists.length && <EmptyState title="暂无歌单" meta="搜索后可将歌曲加入歌单。" />}
      {playlists.map((playlist) => (
        <article key={playlist.id} className="playlistRow">
          <button className="playlistOpenButton" aria-label={`打开歌单 ${playlist.name}`} onClick={() => setSelected(playlist)}>
            <strong>{playlist.name}</strong>
            <span>{playlist.item_count || 0} 首歌曲</span>
          </button>
          <div className="rowActions playlistRowActions">
            <button
              className="smallButton"
              onClick={() => {
                openRandomOrderDialog(playlist);
              }}
              disabled={(playlist.item_count || 0) < 1}
            >
              随机点歌
            </button>
            <button
              className="iconButton danger playlistDeleteButton"
              aria-label={`删除歌单 ${playlist.name}`}
              onClick={() => {
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
        <CardDialog title="新建歌单" onClose={() => setCreateOpen(false)} busy={savingDialog === "create"}>
          <form
            className="dialogForm"
            onSubmit={(event) => {
              event.preventDefault();
              createPlaylist();
            }}
          >
            <label className="fieldLabel"><span>歌单名称</span><input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="例如：我的最爱" /></label>
            {dialogError && <p className="actionError" role="alert">{dialogError}</p>}
            <div className="dialogActions">
              <button type="button" className="glassButton" onClick={() => setCreateOpen(false)} disabled={savingDialog === "create"}>取消</button>
              <button className="primaryButton" disabled={!newName.trim() || savingDialog === "create"}><Plus />{savingDialog === "create" ? "创建中…" : "创建"}</button>
            </div>
          </form>
        </CardDialog>
      )}
      {importOpen && (
        <CardDialog title="导入歌单" onClose={() => setImportOpen(false)} busy={importing}>
          <form
            className="dialogForm"
            onSubmit={(event) => {
              event.preventDefault();
              importPlaylist();
            }}
          >
            <label className="fieldLabel"><span>歌单链接</span><input value={playlistUrl} onChange={(event) => setPlaylistUrl(event.target.value)} placeholder={qqMusicEnabled ? "粘贴网易云或 QQ 音乐歌单链接" : "粘贴网易云歌单链接"} /></label>
            <p className="hintText">请粘贴包含歌单 ID 的完整详情链接，暂不支持需要跳转的分享短链。</p>
            <label className="fieldLabel"><span>导入后的歌单名（可留空）</span><input value={playlistName} onChange={(event) => setPlaylistName(event.target.value)} placeholder="默认使用原歌单名" /></label>
            {importMessage && <p className="actionError" role="alert">{importMessage}</p>}
            <div className="dialogActions">
              <button type="button" className="glassButton" onClick={() => setImportOpen(false)} disabled={importing}>取消</button>
              <button className="primaryButton" disabled={importing || !playlistUrl.trim()}>
                <CloudDownload />{importing ? "导入中" : "导入"}
              </button>
            </div>
          </form>
        </CardDialog>
      )}
    </div>
  );
}
