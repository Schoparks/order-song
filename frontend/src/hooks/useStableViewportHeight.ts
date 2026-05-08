import { useLayoutEffect } from "react";

const KEYBOARD_HEIGHT_DELTA = 120;
const IOS_VIEWPORT_TOP_OFFSET_LIMIT = 80;
let stableViewportHeight = 0;
let stableViewportWidth = 0;
let freezeViewportUntil = 0;
let sawIosKeyboardInteraction = false;

function isIosSafari() {
  const ua = navigator.userAgent;
  const isIOS = /iP(hone|od|ad)/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  return isIOS && /Safari/i.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/i.test(ua);
}

function syncSafeAreaOverrides() {
  document.documentElement.style.removeProperty("--safe-top");
  document.documentElement.style.removeProperty("--safe-bottom");
}

function readViewportSize() {
  const viewport = window.visualViewport;
  return {
    height: Math.round(viewport?.height || window.innerHeight),
    width: Math.round(viewport?.width || window.innerWidth),
  };
}

function isEditableElement(element: Element | null): boolean {
  if (!(element instanceof HTMLElement)) return false;
  const tagName = element.tagName.toLowerCase();
  return tagName === "input" || tagName === "textarea" || element.isContentEditable;
}

function noteIosKeyboardInteraction() {
  if (isIosSafari()) sawIosKeyboardInteraction = true;
}

function syncIosViewportTopOffset() {
  const root = document.documentElement;
  if (!isIosSafari() || !sawIosKeyboardInteraction || isEditableElement(document.activeElement)) {
    root.style.setProperty("--ios-viewport-top-offset", "0px");
    return;
  }

  const viewport = window.visualViewport;
  const offsetTop = Math.max(0, Math.round(viewport?.offsetTop || 0));
  if (offsetTop > 1 && offsetTop <= IOS_VIEWPORT_TOP_OFFSET_LIMIT) {
    root.style.setProperty("--ios-viewport-top-offset", `${offsetTop}px`);
  } else {
    root.style.setProperty("--ios-viewport-top-offset", "0px");
  }
}

function freezeViewportForKeyboard(ms = 1400) {
  freezeViewportUntil = Math.max(freezeViewportUntil, Date.now() + ms);
}

function syncStableViewportHeight(force = false) {
  syncSafeAreaOverrides();
  if (isIosSafari()) {
    document.documentElement.style.removeProperty("--app-height");
    return;
  }

  const { height, width } = readViewportSize();
  const widthChanged = stableViewportWidth > 0 && Math.abs(width - stableViewportWidth) > 48;
  if (!stableViewportHeight || widthChanged) {
    stableViewportHeight = height;
    stableViewportWidth = width;
  }

  const keyboardSized = stableViewportHeight - height > KEYBOARD_HEIGHT_DELTA;
  const shouldFreeze = keyboardSized && (isEditableElement(document.activeElement) || Date.now() < freezeViewportUntil);
  if (force || !shouldFreeze) {
    stableViewportHeight = height;
    stableViewportWidth = width;
  }

  document.documentElement.style.setProperty("--app-height", `${stableViewportHeight}px`);
}

export function useStableViewportHeight() {
  useLayoutEffect(() => {
    syncSafeAreaOverrides();
    syncStableViewportHeight(true);
    syncIosViewportTopOffset();
    const scheduleSync = () => window.requestAnimationFrame(() => {
      syncSafeAreaOverrides();
      syncStableViewportHeight();
      syncIosViewportTopOffset();
    });
    const scheduleKeyboardRecovery = () => {
      scheduleSync();
      window.setTimeout(scheduleSync, 80);
      window.setTimeout(scheduleSync, 260);
      window.setTimeout(scheduleSync, 600);
      window.setTimeout(scheduleSync, 1000);
      queueIosSafariViewportNudges();
    };
    const handleFocusIn = (event: FocusEvent) => {
      if (!isEditableElement(event.target as Element | null)) return;
      noteIosKeyboardInteraction();
      document.documentElement.style.setProperty("--ios-viewport-top-offset", "0px");
      freezeViewportForKeyboard(2200);
      scheduleSync();
    };
    const handleFocusOut = (event: FocusEvent) => {
      if (!isEditableElement(event.target as Element | null)) return;
      noteIosKeyboardInteraction();
      freezeViewportForKeyboard(2200);
      scheduleKeyboardRecovery();
    };
    const viewport = window.visualViewport;
    window.addEventListener("resize", scheduleSync);
    window.addEventListener("orientationchange", scheduleSync);
    document.addEventListener("focusin", handleFocusIn, true);
    document.addEventListener("focusout", handleFocusOut, true);
    viewport?.addEventListener("resize", scheduleSync);
    viewport?.addEventListener("scroll", scheduleSync);
    return () => {
      window.removeEventListener("resize", scheduleSync);
      window.removeEventListener("orientationchange", scheduleSync);
      document.removeEventListener("focusin", handleFocusIn, true);
      document.removeEventListener("focusout", handleFocusOut, true);
      viewport?.removeEventListener("resize", scheduleSync);
      viewport?.removeEventListener("scroll", scheduleSync);
    };
  }, []);
}

export function blurActiveElement() {
  const active = document.activeElement;
  if (active instanceof HTMLElement) {
    if (isEditableElement(active)) noteIosKeyboardInteraction();
    freezeViewportForKeyboard();
    active.blur();
  }
}

function restoreViewportScroll() {
  window.scrollTo(0, 0);
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
}

function nudgeIosSafariViewport() {
  if (!isIosSafari() || !sawIosKeyboardInteraction || isEditableElement(document.activeElement)) return;

  const { body, documentElement } = document;
  if (body.style.position === "fixed") return;

  const previous = {
    htmlOverflowY: documentElement.style.overflowY,
    htmlScrollBehavior: documentElement.style.scrollBehavior,
    bodyOverflowY: body.style.overflowY,
    bodyHeight: body.style.height,
    bodyMinHeight: body.style.minHeight,
  };
  const minHeight = Math.max(window.innerHeight, documentElement.clientHeight, Math.round(window.visualViewport?.height || 0)) + 2;

  documentElement.style.scrollBehavior = "auto";
  documentElement.style.overflowY = "auto";
  body.style.overflowY = "auto";
  body.style.height = "auto";
  body.style.minHeight = `${minHeight}px`;

  window.scrollTo(0, 1);
  documentElement.scrollTop = 1;
  body.scrollTop = 1;

  window.requestAnimationFrame(() => {
    restoreViewportScroll();
    window.requestAnimationFrame(() => {
      documentElement.style.overflowY = previous.htmlOverflowY;
      documentElement.style.scrollBehavior = previous.htmlScrollBehavior;
      body.style.overflowY = previous.bodyOverflowY;
      body.style.height = previous.bodyHeight;
      body.style.minHeight = previous.bodyMinHeight;
      restoreViewportScroll();
      syncIosViewportTopOffset();
    });
  });
}

function queueIosSafariViewportNudges() {
  if (!isIosSafari() || !sawIosKeyboardInteraction) return;
  window.setTimeout(nudgeIosSafariViewport, 80);
  window.setTimeout(nudgeIosSafariViewport, 260);
  window.setTimeout(nudgeIosSafariViewport, 600);
}

export function beginMobileKeyboardDismissal() {
  noteIosKeyboardInteraction();
  freezeViewportForKeyboard(2200);
  blurActiveElement();
  restoreViewportScroll();
  syncStableViewportHeight();
  syncIosViewportTopOffset();
  queueIosSafariViewportNudges();
}

export function resetMobileViewport() {
  freezeViewportForKeyboard();
  blurActiveElement();
  const restore = restoreViewportScroll;
  syncStableViewportHeight();
  syncIosViewportTopOffset();
  queueIosSafariViewportNudges();
  restore();
  window.requestAnimationFrame(restore);
  window.setTimeout(restore, 80);
  window.setTimeout(restore, 260);
  window.setTimeout(restore, 600);
}

function wait(ms: number) {
  return new Promise<void>((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

export async function settleMobileViewportBeforeRouteChange() {
  beginMobileKeyboardDismissal();
  restoreViewportScroll();
  const viewport = window.visualViewport;
  if (!viewport) {
    await wait(250);
    resetMobileViewport();
    return;
  }

  const startedAt = Date.now();
  let lastHeight = viewport.height;
  let stableFrames = 0;
  while (Date.now() - startedAt < 700) {
    await wait(50);
    syncStableViewportHeight();
    syncIosViewportTopOffset();
    const heightDelta = Math.abs(viewport.height - lastHeight);
    const offsetSettled = Math.abs(viewport.offsetTop) < 1;
    stableFrames = heightDelta < 1 && offsetSettled ? stableFrames + 1 : 0;
    lastHeight = viewport.height;
    if (stableFrames >= 2) break;
  }
  resetMobileViewport();
}

export function useDialogViewportLock() {
  useLayoutEffect(() => {
    const { body, documentElement } = document;
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    const previous = {
      bodyPosition: body.style.position,
      bodyTop: body.style.top,
      bodyLeft: body.style.left,
      bodyRight: body.style.right,
      bodyWidth: body.style.width,
      bodyOverflow: body.style.overflow,
      htmlOverflow: documentElement.style.overflow,
    };

    body.style.position = "fixed";
    body.style.top = `-${scrollY}px`;
    body.style.left = `-${scrollX}px`;
    body.style.right = "0";
    body.style.width = "100%";
    body.style.overflow = "hidden";
    documentElement.style.overflow = "hidden";

    return () => {
      body.style.position = previous.bodyPosition;
      body.style.top = previous.bodyTop;
      body.style.left = previous.bodyLeft;
      body.style.right = previous.bodyRight;
      body.style.width = previous.bodyWidth;
      body.style.overflow = previous.bodyOverflow;
      documentElement.style.overflow = previous.htmlOverflow;

      const restoreScroll = () => window.scrollTo(scrollX, scrollY);
      window.requestAnimationFrame(restoreScroll);
      window.setTimeout(restoreScroll, 80);
    };
  }, []);
}
