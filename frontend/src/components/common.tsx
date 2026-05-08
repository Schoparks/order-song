import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowUp, Music2, X } from "lucide-react";
import { blurActiveElement, useDialogViewportLock } from "../hooks/useStableViewportHeight";
import type { Track } from "../types";

export function ScrollArea({ children, className = "" }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [showTop, setShowTop] = useState(false);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const onScroll = () => setShowTop(node.scrollTop > 280);
    onScroll();
    node.addEventListener("scroll", onScroll, { passive: true });
    return () => node.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className={`scrollShell ${className}`}>
      <div ref={ref} className="scrollArea">
        {children}
      </div>
      {showTop && (
        <button className="scrollTopButton" onClick={() => ref.current?.scrollTo({ top: 0, behavior: "smooth" })}>
          <ArrowUp />
        </button>
      )}
    </div>
  );
}

export function CardDialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  useDialogViewportLock();
  const closeDialog = useCallback(() => {
    blurActiveElement();
    onClose();
  }, [onClose]);

  return createPortal(
    <div className="dialogOverlay" onMouseDown={closeDialog}>
      <section className="dialogCard glassPanel" onMouseDown={(event) => event.stopPropagation()}>
        <div className="sheetHeader">
          <h3>{title}</h3>
          <button className="iconButton" onClick={closeDialog}><X /></button>
        </div>
        {children}
      </section>
    </div>,
    document.body,
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmText = "确认",
  busy,
  onCancel,
  onConfirm,
}: {
  title: string;
  message: string;
  confirmText?: string;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  return (
    <CardDialog title={title} onClose={onCancel}>
      <p className="dialogMessage">{message}</p>
      <div className="dialogActions">
        <button className="glassButton" onClick={onCancel} disabled={busy}>取消</button>
        <button className="dangerButton" onClick={() => { void onConfirm(); }} disabled={busy}>{busy ? "处理中" : confirmText}</button>
      </div>
    </CardDialog>
  );
}

function trackCoverSrc(track: Track): string | null {
  if (!track.cover_url) return null;
  if (track.source === "bilibili") {
    if (track.cover_url.startsWith("//")) return `https:${track.cover_url}`;
    return track.cover_url.replace(/^http:\/\//i, "https://");
  }
  return track.cover_url;
}

export function TrackCover({ track }: { track: Track }) {
  const [failed, setFailed] = useState(false);
  const src = trackCoverSrc(track);
  if (!src || failed) return <div className="cover placeholder"><Music2 /></div>;
  return (
    <img
      className="cover"
      src={src}
      alt=""
      loading="lazy"
      referrerPolicy={track.source === "bilibili" ? "no-referrer" : undefined}
      onError={() => setFailed(true)}
    />
  );
}

export function SegmentedTabs({
  value,
  items,
  onChange,
  className = "",
}: {
  value: string;
  items: Array<{ value: string; label: string; icon?: ReactNode }>;
  onChange: (value: string) => void;
  className?: string;
}) {
  return (
    <div className={`segmented ${className}`} role="tablist">
      {items.map((item) => (
        <button key={item.value} className={value === item.value ? "active" : ""} onClick={() => onChange(item.value)}>
          {item.icon}
          {item.label}
        </button>
      ))}
    </div>
  );
}

export function EmptyState({ title, meta }: { title: string; meta?: string }) {
  return (
    <div className="emptyState">
      <Music2 />
      <strong>{title}</strong>
      {meta && <span>{meta}</span>}
    </div>
  );
}
