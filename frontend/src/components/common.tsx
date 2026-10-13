import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowUp, Music2, X } from "lucide-react";
import { blurActiveElement, useDialogViewportLock } from "../hooks/useStableViewportHeight";
import type { Track } from "../types";

export function ScrollArea({ children, className = "", resetKey }: { children: ReactNode; className?: string; resetKey?: string }) {
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

  useEffect(() => {
    ref.current?.scrollTo({ top: 0 });
  }, [resetKey]);

  return (
    <div className={`scrollShell ${className}`}>
      <div ref={ref} className="scrollArea">
        {children}
      </div>
      {showTop && (
        <button className="scrollTopButton" aria-label="返回列表顶部" onClick={() => ref.current?.scrollTo({ top: 0, behavior: "smooth" })}>
          <ArrowUp />
        </button>
      )}
    </div>
  );
}

const FOCUSABLE_SELECTOR = "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [contenteditable='true'], [tabindex]:not([tabindex='-1'])";

export function useModalFocus({
  onClose,
  canDismiss = true,
  initialFocusRef,
}: {
  onClose: () => void;
  canDismiss?: boolean;
  initialFocusRef?: { current: HTMLElement | null };
}) {
  const dialogRef = useRef<HTMLElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const initial = initialFocusRef?.current
      || dialog?.querySelector<HTMLElement>(`[data-dialog-autofocus], ${FOCUSABLE_SELECTOR}`);
    const focusFrame = window.requestAnimationFrame(() => (initial || dialog)?.focus());
    return () => {
      window.cancelAnimationFrame(focusFrame);
      const previous = restoreFocusRef.current;
      window.requestAnimationFrame(() => {
        if (previous?.isConnected) previous.focus();
      });
    };
  }, [initialFocusRef]);

  const onDialogKeyDown = useCallback((event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      if (canDismiss) {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter((node) => node.getAttribute("aria-hidden") !== "true" && node.offsetParent !== null);
    if (!focusable.length) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
      event.preventDefault();
      first.focus();
    }
  }, [canDismiss, onClose]);

  return { dialogRef, onDialogKeyDown };
}

export function CardDialog({
  title,
  children,
  onClose,
  busy = false,
  dismissible = true,
  className = "",
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  busy?: boolean;
  dismissible?: boolean;
  className?: string;
}) {
  useDialogViewportLock();
  const titleId = useId();
  const canDismiss = dismissible && !busy;
  const closeDialog = useCallback(() => {
    if (!canDismiss) return;
    blurActiveElement();
    onClose();
  }, [canDismiss, onClose]);
  const { dialogRef, onDialogKeyDown } = useModalFocus({ onClose: closeDialog, canDismiss });

  return createPortal(
    <div className="dialogOverlay" onMouseDown={(event) => event.target === event.currentTarget && closeDialog()}>
      <section
        ref={dialogRef}
        className={`dialogCard glassPanel ${className}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={busy || undefined}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
      >
        <div className="sheetHeader">
          <h3 id={titleId}>{title}</h3>
          {dismissible && (
            <button className="iconButton" aria-label={`关闭${title}`} onClick={closeDialog} disabled={busy}><X /></button>
          )}
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
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const isBusy = !!busy || pending;

  const handleConfirm = useCallback(async () => {
    if (isBusy) return;
    setPending(true);
    setError("");
    try {
      await onConfirm();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败，请重试");
    } finally {
      setPending(false);
    }
  }, [isBusy, onConfirm]);

  return (
    <CardDialog title={title} onClose={onCancel} busy={isBusy}>
      <p className="dialogMessage">{message}</p>
      {error && <p className="actionError" role="alert">{error}</p>}
      <div className="dialogActions">
        <button className="glassButton" onClick={onCancel} disabled={isBusy}>取消</button>
        <button className="dangerButton" onClick={() => { void handleConfirm(); }} disabled={isBusy}>{isBusy ? "处理中" : confirmText}</button>
      </div>
    </CardDialog>
  );
}

export function trackCoverSrc(track: Track): string | null {
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
  label = "切换视图",
}: {
  value: string;
  items: Array<{ value: string; label: string; icon?: ReactNode }>;
  onChange: (value: string) => void;
  className?: string;
  label?: string;
}) {
  const segmentCount = Math.max(1, items.length);
  const segmentIndex = Math.max(0, items.findIndex((item) => item.value === value));
  const segmentStyle = {
    "--segment-count": segmentCount,
    "--segment-index": segmentIndex,
    "--segment-width": `${100 / segmentCount}%`,
    "--segment-shift": `${segmentIndex * 100}%`,
    "--segment-shell-width": `${segmentCount * 132}px`,
  } as CSSProperties;

  return (
    <div className={`segmented ${className}`} role="group" aria-label={label} style={segmentStyle}>
      {items.map((item) => (
        <button
          key={item.value}
          className={value === item.value ? "active" : ""}
          aria-pressed={value === item.value}
          onClick={() => onChange(item.value)}
        >
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
