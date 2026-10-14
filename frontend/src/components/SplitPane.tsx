
import { useEffect, useRef, useState, type ReactNode } from "react";

export default function SplitPane({
  first,
  second,
  storageKey,
  initial = 0.52,
  wideAt = 768,
}: {
  first: ReactNode;
  second: ReactNode;
  storageKey: string;
  initial?: number;
  wideAt?: number;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const ratioRef = useRef(initial);

  const [wide, setWide] = useState(
    () => window.matchMedia(`(min-width: ${wideAt}px)`).matches
  );

  const [ratio, setRatio] = useState(() => {
    const saved = Number(window.localStorage.getItem(storageKey));
    const value = Number.isFinite(saved) && saved > 0 ? saved : initial;
    const clamped = Math.min(0.8, Math.max(0.2, value));
    ratioRef.current = clamped;
    return clamped;
  });

  useEffect(() => {
    const media = window.matchMedia(`(min-width: ${wideAt}px)`);
    const apply = () => setWide(media.matches);

    apply();
    media.addEventListener("change", apply);

    return () => media.removeEventListener("change", apply);
  }, [wideAt]);

  function move(client: number) {
    const rect = frame.current?.getBoundingClientRect();
    if (!rect) return;

    const next = wide
      ? (client - rect.left) / rect.width
      : (client - rect.top) / rect.height;

    const clamped = Math.min(0.8, Math.max(0.2, next));
    ratioRef.current = clamped;
    setRatio(clamped);
  }

  function finish() {
    window.localStorage.setItem(storageKey, String(ratioRef.current));
  }

  const firstStyle = wide
    ? { width: `${ratio * 100}%` }
    : { height: `${ratio * 100}%` };

  return (
    <div
      ref={frame}
      className={`flex h-full min-h-0 w-full ${
        wide ? "flex-row" : "flex-col"
      }`}
      onDragOver={(event) => {
        event.preventDefault();
        if (event.clientX === 0 && event.clientY === 0) return;
        move(wide ? event.clientX : event.clientY);
      }}
    >
      <div
        className="min-h-0 min-w-0 overflow-x-hidden overflow-y-auto"
        style={firstStyle}
      >
        {first}
      </div>

      <button
        type="button"
        draggable
        aria-orientation={wide ? "vertical" : "horizontal"}
        aria-label="Resize panels"
        className={`shrink-0 border-0 bg-line p-0 hover:bg-lime ${
          wide
            ? "w-1.5 cursor-col-resize"
            : "h-1.5 cursor-row-resize"
        }`}
        onDragStart={(event) => {
          event.dataTransfer.effectAllowed = "move";
        }}
        onDrag={(event) => {
          if (event.clientX === 0 && event.clientY === 0) return;
          move(wide ? event.clientX : event.clientY);
        }}
        onDragEnd={finish}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
          move(wide ? event.clientX : event.clientY);
        }}
        onPointerUp={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
          finish();
        }}
      />

      <div className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto">
        {second}
      </div>
    </div>
  );
}
