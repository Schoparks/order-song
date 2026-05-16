import { useCallback, useEffect, useRef, useState } from "react";
import { Shuffle } from "lucide-react";
import { CardDialog } from "./common";

export function RandomPlaylistOrderDialog({
  targetId,
  targetName,
  itemCount,
  itemLabel,
  unitLabel = "首",
  busy,
  error,
  onCancel,
  onConfirm,
}: {
  targetId: number | string;
  targetName: string;
  itemCount: number;
  itemLabel?: string;
  unitLabel?: string;
  busy: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: (count: number) => void | Promise<void>;
}) {
  const maxCount = Math.max(1, itemCount);
  const [count, setCount] = useState(maxCount);
  const [editing, setEditing] = useState(false);
  const [draftCount, setDraftCount] = useState(() => String(maxCount));
  const countInputRef = useRef<HTMLInputElement>(null);

  const clampCount = useCallback(
    (value: number) => Math.min(maxCount, Math.max(1, Math.round(value))),
    [maxCount],
  );

  useEffect(() => {
    setCount(maxCount);
    setDraftCount(String(maxCount));
    setEditing(false);
  }, [targetId, maxCount]);

  useEffect(() => {
    if (!editing) return;
    countInputRef.current?.focus();
    countInputRef.current?.select();
  }, [editing]);

  function updateCount(value: number) {
    const next = clampCount(value);
    setCount(next);
    setDraftCount(String(next));
  }

  function commitDraft() {
    const parsed = Number(draftCount);
    updateCount(Number.isFinite(parsed) ? parsed : count);
    setEditing(false);
  }

  return (
    <CardDialog title="随机点歌" onClose={onCancel}>
      <form
        className="randomOrderForm"
        onSubmit={(event) => {
          event.preventDefault();
          void onConfirm(count);
        }}
      >
        <div className="randomOrderTarget">
          <strong>{targetName}</strong>
          <span>{itemLabel || `${itemCount} 首歌曲`}</span>
        </div>
        <div className="randomRangeLabels">
          <span>1 {unitLabel}</span>
          <span>全部</span>
        </div>
        <div className="randomCountRow">
          <input
            aria-label="随机点歌数量"
            type="range"
            min={1}
            max={maxCount}
            step={1}
            value={count}
            onChange={(event) => updateCount(Number(event.target.value))}
          />
          {editing ? (
            <input
              ref={countInputRef}
              className="randomCountInput"
              type="number"
              inputMode="numeric"
              min={1}
              max={maxCount}
              value={draftCount}
              onChange={(event) => {
                setDraftCount(event.target.value);
                const parsed = Number(event.target.value);
                if (Number.isFinite(parsed)) setCount(clampCount(parsed));
              }}
              onBlur={commitDraft}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  commitDraft();
                }
                if (event.key === "Escape") {
                  setDraftCount(String(count));
                  setEditing(false);
                }
              }}
            />
          ) : (
            <button
              type="button"
              className="randomCountValue"
              title="双击输入数量"
              onDoubleClick={() => setEditing(true)}
            >
              {count === maxCount ? "全部" : `${count} ${unitLabel}`}
            </button>
          )}
        </div>
        {error && <p className="hintText dangerHint">{error}</p>}
        <div className="dialogActions">
          <button type="button" className="glassButton" onClick={onCancel} disabled={busy}>取消</button>
          <button className="primaryButton" disabled={busy || itemCount < 1}><Shuffle />{busy ? "点歌中" : "点歌"}</button>
        </div>
      </form>
    </CardDialog>
  );
}
