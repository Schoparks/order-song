export interface ProgressSeekCoordinatorOptions {
  schedule: (callback: () => void) => () => void;
  onSeekingChange: (seeking: boolean) => void;
  onPreview: (ratio: number) => void;
  onCommit: (ratio: number) => void;
}

export interface ProgressSeekCoordinator {
  begin: (ratio: number) => void;
  preview: (ratio: number) => void;
  finish: (ratio?: number) => void;
  cancel: () => void;
  dispose: () => void;
  isSeeking: () => boolean;
}

function safeRatio(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

export function createProgressSeekCoordinator(
  options: ProgressSeekCoordinatorOptions,
): ProgressSeekCoordinator {
  let seeking = false;
  let disposed = false;
  let draftRatio = 0;
  let cancelScheduledFinish: (() => void) | null = null;

  const setSeeking = (value: boolean) => {
    if (seeking === value) return;
    seeking = value;
    options.onSeekingChange(value);
  };

  const clearScheduledFinish = () => {
    cancelScheduledFinish?.();
    cancelScheduledFinish = null;
  };

  const commitNow = () => {
    clearScheduledFinish();
    if (!seeking || disposed) return;
    setSeeking(false);
    options.onCommit(draftRatio);
  };

  const begin = (ratio: number) => {
    if (disposed) return;
    if (cancelScheduledFinish) commitNow();
    draftRatio = safeRatio(ratio);
    setSeeking(true);
  };

  const preview = (ratio: number) => {
    if (disposed || !seeking) return;
    draftRatio = safeRatio(ratio);
    options.onPreview(draftRatio);
  };

  const finish = (ratio?: number) => {
    if (disposed || !seeking) return;
    if (ratio !== undefined) {
      draftRatio = safeRatio(ratio);
      options.onPreview(draftRatio);
    }
    if (cancelScheduledFinish) return;
    cancelScheduledFinish = options.schedule(() => {
      cancelScheduledFinish = null;
      commitNow();
    });
  };

  const cancel = () => {
    if (disposed) return;
    clearScheduledFinish();
    setSeeking(false);
  };

  const dispose = () => {
    if (disposed) return;
    clearScheduledFinish();
    setSeeking(false);
    disposed = true;
  };

  return {
    begin,
    preview,
    finish,
    cancel,
    dispose,
    isSeeking: () => seeking,
  };
}
