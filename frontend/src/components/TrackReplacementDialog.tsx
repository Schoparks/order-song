import { useEffect, useRef, useState } from "react";
import { Check, ListMusic, Search } from "lucide-react";
import { api } from "../lib/api";
import { trackKey, trackPayload } from "../lib/track";
import { formatTime } from "../lib/time";
import type { Track } from "../types";
import { CardDialog, EmptyState, ScrollArea, SegmentedTabs, TrackCover } from "./common";
import { unavailableTrackFromError } from "./TrackAvailability";

type SearchSource = "all" | "bilibili" | "netease" | "qq";

export function TrackReplacementDialog({ track, token, qqMusicEnabled, onClose, onReplaced }: {
  track: Track; token: string; qqMusicEnabled: boolean; onClose: () => void; onReplaced: (message: string) => void;
}) {
  const [query, setQuery] = useState(track.title);
  const [source, setSource] = useState<SearchSource>("all");
  const [submitted, setSubmitted] = useState("");
  const [results, setResults] = useState<Track[]>([]);
  const [searching, setSearching] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [selected, setSelected] = useState<Track | null>(null);
  const [error, setError] = useState("");
  const requestRef = useRef<AbortController | null>(null);
  useEffect(() => () => requestRef.current?.abort(), []);

  async function search(nextSource = source, value = query) {
    const q = value.trim();
    if (!q || replacing) return;
    requestRef.current?.abort();
    const request = new AbortController();
    requestRef.current = request;
    setSubmitted(q);
    setSearching(true);
    setSelected(null);
    setResults([]);
    setError("");
    try {
      const items = await api<Track[]>(`/api/search?q=${encodeURIComponent(q)}&source=${nextSource}`, { token, signal: request.signal });
      if (requestRef.current === request) setResults(items);
    } catch (caught) {
      if (!request.signal.aborted && requestRef.current === request) setError(caught instanceof Error ? caught.message : "搜索失败");
    } finally {
      if (requestRef.current === request) setSearching(false);
    }
  }

  async function replace() {
    if (!selected || replacing) return;
    setReplacing(true);
    setError("");
    try {
      const result = await api<{ replaced: number; track: Track }>(`/api/playlists/tracks/${track.id}/replace`, { method: "POST", token, json: trackPayload(selected) });
      onReplaced(result.replaced ? `已在你的 ${result.replaced} 个歌单中替换为“${result.track.title}”。` : "你的歌单中已没有这首歌曲。");
    } catch (caught) {
      const unavailable = unavailableTrackFromError(caught);
      if (unavailable) {
        const update = (item: Track): Track => trackKey(item) === trackKey(unavailable) ? unavailable : { ...item, parts: item.parts?.map(update) };
        setResults((items) => items.map(update));
        setSelected(null);
      }
      setError(caught instanceof Error ? caught.message : "替换失败，请重试");
    } finally {
      setReplacing(false);
    }
  }

  return <CardDialog title="重新搜索" className="replacementDialog" onClose={onClose} busy={replacing}>
    <p className="dialogMessage">选择一首歌曲，替换你所有歌单中的“{track.title}”。</p>
    <form onSubmit={(event) => { event.preventDefault(); void search(); }}>
      <label className="fieldLabel searchFieldLabel" htmlFor="replacement-search">搜索歌曲</label>
      <div className="searchInputWrap"><Search /><input id="replacement-search" data-dialog-autofocus value={query} onChange={(event) => setQuery(event.target.value)} disabled={replacing} placeholder="歌曲、歌手、BV 号或 B 站标题" /><button className="primaryButton" disabled={searching || replacing || !query.trim()}>{searching ? "搜索中" : "搜索"}</button></div>
    </form>
    <SegmentedTabs className="replacementSources" label="搜索来源" value={source} items={[{ value: "all", label: "全部" }, { value: "bilibili", label: "B站" }, { value: "netease", label: "网易云" }, ...(qqMusicEnabled ? [{ value: "qq", label: "QQ音乐" }] : [])]} onChange={(value) => {
      if (replacing) return;
      const next = value as SearchSource;
      setSource(next);
      if (submitted) void search(next, submitted);
    }} />
    {error && <p className="actionError" role="alert">{error}</p>}
    <ScrollArea className="replacementResults" resetKey={`${submitted}:${source}`}>
      {searching ? <EmptyState title="搜索中…" /> : results.length ? results.map((result) => <ReplacementResult key={trackKey(result)} track={result} original={track} selected={selected} busy={replacing} onSelect={setSelected} />) : <EmptyState title={submitted ? "没有找到歌曲" : "输入歌名后点击搜索"} />}
    </ScrollArea>
    <div className="replacementFooter">
      {selected && <p className="hintText" role="status">已选：{selected.title}</p>}
      <div className="dialogActions"><button className="glassButton" onClick={onClose} disabled={replacing}>取消</button><button className="primaryButton" disabled={!selected || replacing || searching} onClick={() => void replace()}><Check />{replacing ? "替换中…" : "替换所有歌单"}</button></div>
    </div>
  </CardDialog>;
}

function ReplacementResult({ track, original, selected, busy, onSelect }: {
  track: Track; original: Track; selected: Track | null; busy: boolean; onSelect: (track: Track) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const parts = track.parts || [];
  const same = trackKey(track) === trackKey(original);
  const unavailable = track.availability === "unavailable";
  const checked = selected && trackKey(track) === trackKey(selected);
  return <>
    <article className={`songRow replacementRow ${unavailable ? "songRowUnavailable" : ""}`}>
      <TrackCover track={track} /><div className="songInfo"><strong>{track.title}</strong><span>{track.artist || "未知歌手"} · {track.source}{track.duration_ms ? ` · ${formatTime(track.duration_ms)}` : ""}</span>{unavailable && <span className="unavailableLabel">已失效</span>}</div>
      {parts.length ? <button className="smallButton" aria-expanded={expanded} disabled={busy} onClick={() => setExpanded(!expanded)}><ListMusic />{expanded ? "收起" : `展开 ${parts.length} P`}</button> : <button className={`smallButton ${checked ? "active" : ""}`} aria-pressed={!!checked} disabled={busy || same || unavailable} onClick={() => onSelect(track)}>{checked ? "已选" : same ? "原歌曲" : unavailable ? "已失效" : "选择"}</button>}
    </article>
    {expanded && <div className="songPartsCard">{parts.map((part) => <ReplacementResult key={trackKey(part)} track={part} original={original} selected={selected} busy={busy} onSelect={onSelect} />)}</div>}
  </>;
}
