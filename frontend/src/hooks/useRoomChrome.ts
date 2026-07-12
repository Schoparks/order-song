import { useEffect, useLayoutEffect, useState, type CSSProperties, type RefObject } from "react";

export type RoomConnectionState = "connected" | "reconnecting" | "offline";

export function useRoomConnectionState(roomId: number | null) {
  const [connectionState, setConnectionState] = useState<RoomConnectionState>(
    navigator.onLine ? "reconnecting" : "offline",
  );

  useEffect(() => {
    const online = () => setConnectionState(roomId ? "reconnecting" : "connected");
    const offline = () => setConnectionState("offline");
    window.addEventListener("online", online);
    window.addEventListener("offline", offline);
    return () => {
      window.removeEventListener("online", online);
      window.removeEventListener("offline", offline);
    };
  }, [roomId]);

  return [connectionState, setConnectionState] as const;
}

export function useRoomMenuDismissal(open: boolean, onClose: () => void) {
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest(".roomMenu, .roomMenuButton")) onClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose, open]);
}

export function useAnchoredPopover(
  open: boolean,
  anchorRef: RefObject<HTMLElement>,
  placement: "above" | "below",
  gap = 10,
): CSSProperties {
  const [position, setPosition] = useState<CSSProperties>({});

  useLayoutEffect(() => {
    if (!open) return;
    const update = () => {
      const rect = anchorRef.current?.getBoundingClientRect();
      if (!rect) return;
      const right = Math.max(12, window.innerWidth - rect.right);
      setPosition(placement === "below"
        ? { top: rect.bottom + gap, right, bottom: "auto" }
        : { top: "auto", right, bottom: window.innerHeight - rect.top + gap });
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [anchorRef, gap, open, placement]);

  return position;
}
