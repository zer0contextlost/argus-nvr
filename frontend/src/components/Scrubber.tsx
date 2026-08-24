/**
 * Scrubber — single horizontal timeline bar for PlaybackPage.
 *
 * Layout:
 *   [zoom chips: 1h 3h 6h 24h]    [time readout: 21:47:14]
 *   [═══════════●══════════]  ← bar (56px)
 *    tick labels below bar
 *
 * Zoom chips center the viewport on the current playhead (or viewport
 * midpoint) and clamp to dayStart/dayEnd.
 *
 * Pointer events (mouse + touch) handle seek-on-drag.
 * Auto-follow is the parent's responsibility; this component is stateless
 * with respect to the viewport.
 */

import React, { useEffect, useRef, useState } from 'react'
import { TimelineSegment, TimelineEvent, withToken } from '../api/client'

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ScrubberProps {
  segments: TimelineSegment[]
  events: TimelineEvent[]
  dayStartMs: number
  dayEndMs: number
  playWallMs: number | null
  viewport: { start: number; end: number }
  onSeek: (wallMs: number) => void
  onViewportChange: (v: { start: number; end: number }) => void
  cameraId?: number
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ZOOM_OPTIONS: { label: string; ms: number }[] = [
  { label: '1h',  ms:  3_600_000 },
  { label: '3h',  ms: 10_800_000 },
  { label: '6h',  ms: 21_600_000 },
  { label: '24h', ms: 86_400_000 },
]

// Tick interval candidates in ascending order.
const TICK_INTERVALS_MS = [
  30_000,        // 30s
  60_000,        // 1m
  300_000,       // 5m
  600_000,       // 10m
  900_000,       // 15m
  1_800_000,     // 30m
  3_600_000,     // 1h
  7_200_000,     // 2h
  10_800_000,    // 3h
  21_600_000,    // 6h
]

const GAP_THRESHOLD_MS = 5_000

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

function fmtHM(ms: number): string {
  const d = new Date(ms)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

function fmtHMS(ms: number): string {
  const d = new Date(ms)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

function pickTickInterval(spanMs: number): number {
  for (const iv of TICK_INTERVALS_MS) {
    if (spanMs / iv <= 8) return iv
  }
  return TICK_INTERVALS_MS[TICK_INTERVALS_MS.length - 1]
}

function eventColor(type: string): string {
  if (type === 'motion') return '#f59e0b'          // amber
  if (type === 'object' || type === 'face' || type === 'person') return '#10b981' // green
  if (type === 'vehicle') return '#6366f1'          // indigo
  return '#f59e0b'
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function Scrubber({
  segments,
  events,
  dayStartMs,
  dayEndMs,
  playWallMs,
  viewport,
  onSeek,
  onViewportChange,
  cameraId,
}: ScrubberProps) {
  const barRef = useRef<HTMLDivElement>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [hoverMs, setHoverMs] = useState<number | null>(null)
  const [thumbUrl, setThumbUrl] = useState('')

  // Debounce thumbnail URL update so we don't hammer the API on every pixel
  useEffect(() => {
    if (hoverMs == null || !cameraId) return
    const timer = setTimeout(() => {
      setThumbUrl(withToken(
        `/api/recordings/thumbnail?camera_id=${cameraId}&t=${new Date(hoverMs).toISOString()}`
      ))
    }, 250)
    return () => clearTimeout(timer)
  }, [hoverMs, cameraId])

  const spanMs = viewport.end - viewport.start

  // ------------------------------------------------------------------
  // Coordinate helpers
  // ------------------------------------------------------------------

  function wallMsFromPointerX(clientX: number): number {
    if (!barRef.current) return viewport.start
    const rect = barRef.current.getBoundingClientRect()
    const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width))
    return viewport.start + frac * spanMs
  }

  function pctFromMs(ms: number): number {
    return ((ms - viewport.start) / spanMs) * 100
  }

  // ------------------------------------------------------------------
  // Zoom chip handler
  // ------------------------------------------------------------------

  function handleZoom(newSpanMs: number) {
    const center =
      playWallMs !== null
        ? playWallMs
        : (viewport.start + viewport.end) / 2

    let start = center - newSpanMs / 2
    let end = start + newSpanMs

    if (start < dayStartMs) {
      start = dayStartMs
      end = Math.min(dayEndMs, start + newSpanMs)
    }
    if (end > dayEndMs) {
      end = dayEndMs
      start = Math.max(dayStartMs, end - newSpanMs)
    }

    onViewportChange({ start, end })
  }

  // ------------------------------------------------------------------
  // Pointer events
  // ------------------------------------------------------------------

  function handlePointerDown(e: React.PointerEvent<HTMLDivElement>) {
    e.currentTarget.setPointerCapture(e.pointerId)
    const wallMs = wallMsFromPointerX(e.clientX)
    onSeek(wallMs)
    setIsDragging(true)
    setHoverMs(null)
  }

  function handlePointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const wallMs = wallMsFromPointerX(e.clientX)
    if (isDragging) {
      onSeek(wallMs)
    } else {
      setHoverMs(wallMs)
    }
  }

  function handlePointerUp() {
    setIsDragging(false)
  }

  function handlePointerLeave() {
    if (!isDragging) setHoverMs(null)
  }

  // ------------------------------------------------------------------
  // Tick calculation
  // ------------------------------------------------------------------

  const tickIntervalMs = pickTickInterval(spanMs)
  const ticks: number[] = []
  const firstTick = Math.ceil(viewport.start / tickIntervalMs) * tickIntervalMs
  for (let t = firstTick; t <= viewport.end; t += tickIntervalMs) {
    ticks.push(t)
  }
  const useHMS = tickIntervalMs < 60_000

  // ------------------------------------------------------------------
  // Gap regions between consecutive segments (within viewport)
  // ------------------------------------------------------------------

  interface GapRect { leftPct: number; widthPct: number }
  const gaps: GapRect[] = []
  for (let i = 0; i < segments.length - 1; i++) {
    const gapStartMs = segments[i].ended_at
      ? new Date(segments[i].ended_at!).getTime()
      : null
    if (gapStartMs === null) continue
    const gapEndMs = new Date(segments[i + 1].started_at).getTime()
    const gapSize = gapEndMs - gapStartMs
    if (gapSize <= GAP_THRESHOLD_MS) continue
    if (gapEndMs < viewport.start || gapStartMs > viewport.end) continue
    const clampedStart = Math.max(gapStartMs, viewport.start)
    const clampedEnd = Math.min(gapEndMs, viewport.end)
    gaps.push({
      leftPct: pctFromMs(clampedStart),
      widthPct: pctFromMs(clampedEnd) - pctFromMs(clampedStart),
    })
  }

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  const currentTimeLabel = playWallMs !== null ? fmtHMS(playWallMs) : '--:--:--'

  return (
    <div style={{ width: '100%', userSelect: 'none' }}>
      {/* Top row: zoom chips + time readout */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 6,
        }}
      >
        {/* Zoom chips */}
        <div style={{ display: 'flex', gap: 4 }}>
          {ZOOM_OPTIONS.map(({ label, ms }) => {
            const active = Math.round(spanMs) === ms
            return (
              <button
                key={label}
                onClick={() => handleZoom(ms)}
                style={{
                  padding: '2px 8px',
                  fontSize: 11,
                  fontFamily: 'var(--font-mono)',
                  fontWeight: active ? 700 : 400,
                  background: active ? 'var(--accent)' : 'var(--bg-base)',
                  color: active ? '#fff' : 'var(--text-secondary)',
                  border: '1px solid var(--border)',
                  borderRadius: 999,
                  cursor: 'pointer',
                  transition: 'var(--transition)',
                  lineHeight: 1.6,
                }}
              >
                {label}
              </button>
            )
          })}
        </div>

        {/* Time readout */}
        <span
          style={{
            fontSize: 13,
            fontFamily: 'var(--font-mono)',
            color: playWallMs !== null ? 'var(--text-secondary)' : 'var(--text-muted)',
            letterSpacing: '0.04em',
          }}
        >
          {currentTimeLabel}
        </span>
      </div>

      {/* Bar wrapper — relative so thumbnail can float above */}
      <div style={{ position: 'relative' }}>

      {/* Thumbnail preview popup */}
      {hoverMs != null && cameraId != null && (
        <div style={{
          position: 'absolute',
          bottom: 'calc(100% + 8px)',
          left: `${Math.max(5, Math.min(95, pctFromMs(hoverMs)))}%`,
          transform: 'translateX(-50%)',
          zIndex: 50,
          pointerEvents: 'none',
          background: 'var(--bg-surface)',
          border: '1px solid var(--border)',
          borderRadius: 6,
          overflow: 'hidden',
          boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
          minWidth: 160,
        }}>
          {thumbUrl && (
            <img
              src={thumbUrl}
              alt=""
              style={{ display: 'block', width: 160, height: 90, objectFit: 'cover' }}
              onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
            />
          )}
          <div style={{
            fontSize: 10, fontFamily: 'var(--font-mono)', textAlign: 'center',
            padding: '3px 6px', background: 'rgba(0,0,0,0.7)', color: '#fff',
          }}>
            {fmtHMS(hoverMs)}
          </div>
        </div>
      )}

      {/* Bar */}
      <div
        ref={barRef}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerLeave={handlePointerLeave}
        style={{
          position: 'relative',
          height: 56,
          width: '100%',
          background: 'var(--bg-base)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
          overflow: 'hidden',
          cursor: isDragging ? 'grabbing' : 'crosshair',
        }}
      >
        {/* Tick gridlines */}
        {ticks.map((t, i) => (
          <div
            key={`tg-${i}`}
            style={{
              position: 'absolute',
              top: 0,
              bottom: 0,
              left: `${pctFromMs(t)}%`,
              width: 1,
              background: 'var(--border)',
              opacity: 0.4,
              pointerEvents: 'none',
            }}
          />
        ))}

        {/* Gap hatching */}
        {gaps.map((g, i) => (
          <div
            key={`gap-${i}`}
            style={{
              position: 'absolute',
              top: 0,
              bottom: 0,
              left: `${g.leftPct}%`,
              width: `${g.widthPct}%`,
              background:
                'repeating-linear-gradient(' +
                '45deg,' +
                'rgba(255,255,255,0.05) 0px,' +
                'rgba(255,255,255,0.05) 4px,' +
                'transparent 4px,' +
                'transparent 8px' +
                ')',
              pointerEvents: 'none',
            }}
          />
        ))}

        {/* Segments */}
        {segments.map(seg => {
          const segStart = new Date(seg.started_at).getTime()
          const segEnd = seg.ended_at
            ? new Date(seg.ended_at).getTime()
            : Date.now()
          if (segEnd < viewport.start || segStart > viewport.end) return null
          const leftPct = pctFromMs(Math.max(segStart, viewport.start))
          const rightPct = pctFromMs(Math.min(segEnd, viewport.end))
          const widthPct = Math.max(0.1, rightPct - leftPct)
          return (
            <div
              key={seg.id}
              style={{
                position: 'absolute',
                top: 10,
                height: 24,
                left: `${leftPct}%`,
                width: `${widthPct}%`,
                background: '#2563eb',
                opacity: 0.85,
                borderRadius: 3,
                pointerEvents: 'none',
              }}
            />
          )
        })}

        {/* Event dots */}
        {events.map(evt => {
          const evMs = new Date(evt.started_at).getTime()
          if (evMs < viewport.start || evMs > viewport.end) return null
          const leftPct = pctFromMs(evMs)
          return (
            <div
              key={evt.id}
              title={`${evt.type}${evt.label ? ` — ${evt.label}` : ''}\n${fmtHMS(evMs)}`}
              style={{
                position: 'absolute',
                top: 6,
                left: `${leftPct}%`,
                transform: 'translateX(-50%)',
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: eventColor(evt.type),
                pointerEvents: 'none',
                boxShadow: '0 0 0 1.5px rgba(0,0,0,0.4)',
              }}
            />
          )
        })}

        {/* Playhead */}
        {playWallMs !== null &&
          playWallMs >= viewport.start &&
          playWallMs <= viewport.end && (
            <>
              {/* Triangle handle */}
              <div
                style={{
                  position: 'absolute',
                  top: 0,
                  left: `${pctFromMs(playWallMs)}%`,
                  transform: 'translateX(-50%)',
                  width: 0,
                  height: 0,
                  borderLeft: '4px solid transparent',
                  borderRight: '4px solid transparent',
                  borderTop: '8px solid var(--danger)',
                  pointerEvents: 'none',
                  zIndex: 9,
                }}
              />
              {/* Vertical line */}
              <div
                style={{
                  position: 'absolute',
                  top: 0,
                  bottom: 0,
                  left: `${pctFromMs(playWallMs)}%`,
                  width: 2,
                  transform: 'translateX(-50%)',
                  background: 'var(--danger)',
                  boxShadow: '0 0 4px rgba(239,68,68,0.55)',
                  pointerEvents: 'none',
                  zIndex: 8,
                }}
              />
            </>
          )}

        {/* Tick labels (rendered inside bar, pinned to bottom) */}
        {ticks.map((t, i) => (
          <div
            key={`tl-${i}`}
            style={{
              position: 'absolute',
              bottom: 3,
              left: `${pctFromMs(t)}%`,
              transform: 'translateX(-50%)',
              fontSize: 9,
              fontFamily: 'var(--font-mono)',
              color: 'var(--text-muted)',
              pointerEvents: 'none',
              whiteSpace: 'nowrap',
            }}
          >
            {useHMS ? fmtHMS(t) : fmtHM(t)}
          </div>
        ))}
      </div>
      </div>{/* end bar wrapper */}
    </div>
  )
}
