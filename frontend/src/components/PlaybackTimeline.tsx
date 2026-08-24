/**
 * PlaybackTimeline — two-level timeline (overview + zoomed) for PlaybackPage.
 *
 * Overview (40px): full 24h, segment bars + event density dots + viewport rectangle.
 * Zoomed   (100px): current viewport window, event chips with icons, hover tooltip,
 *                   playhead, click-to-seek, drag-to-select export range, wheel zoom.
 */

import React, { useEffect, useRef, useState } from 'react'
import { TimelineSegment, TimelineEvent } from '../api/client'

// ── Shared types ──────────────────────────────────────────────────────────────

export interface Viewport {
  /** start ms (wall clock) */
  start: number
  /** end ms (wall clock) */
  end: number
}

const ZOOM_LEVELS_MS = [
  24 * 60 * 60 * 1000, // 24h
  6 * 60 * 60 * 1000,  // 6h
  60 * 60 * 1000,      // 1h
  15 * 60 * 1000,      // 15m
  5 * 60 * 1000,       // 5m
]

export function clampZoom(spanMs: number): number {
  // Snap to nearest defined zoom span.
  let best = ZOOM_LEVELS_MS[0]
  let bestDiff = Math.abs(spanMs - best)
  for (const z of ZOOM_LEVELS_MS) {
    const d = Math.abs(spanMs - z)
    if (d < bestDiff) { best = z; bestDiff = d }
  }
  return best
}

export function nextZoomIn(spanMs: number): number {
  // ZOOM_LEVELS_MS is ordered large→small; zoom in = return first level strictly smaller
  for (const level of ZOOM_LEVELS_MS) {
    if (level < spanMs) return level
  }
  return ZOOM_LEVELS_MS[ZOOM_LEVELS_MS.length - 1]
}

export function nextZoomOut(spanMs: number): number {
  // zoom out = return first level strictly larger (scanning from small→large)
  for (let i = ZOOM_LEVELS_MS.length - 1; i >= 0; i--) {
    if (ZOOM_LEVELS_MS[i] > spanMs) return ZOOM_LEVELS_MS[i]
  }
  return ZOOM_LEVELS_MS[0]
}

function pad2(n: number): string { return String(n).padStart(2, '0') }

function fmtHMS(ms: number): string {
  const d = new Date(ms)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

function fmtHM(ms: number): string {
  const d = new Date(ms)
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

// ── Overview (24h) ────────────────────────────────────────────────────────────

interface OverviewProps {
  segments: TimelineSegment[]
  events: TimelineEvent[]
  dayStart: Date
  dayEnd: Date
  viewport: Viewport
  playWallMs: number | null
  onViewportChange: (v: Viewport) => void
}

export function TimelineOverview({
  segments, events, dayStart, dayEnd, viewport, playWallMs, onViewportChange,
}: OverviewProps) {
  const ref = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ mode: 'pan' | 'set'; offsetMs?: number } | null>(null)
  const dayMs = dayEnd.getTime() - dayStart.getTime()

  function fracFromMouse(e: React.MouseEvent | MouseEvent): number {
    if (!ref.current) return 0
    const rect = ref.current.getBoundingClientRect()
    return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
  }

  function setViewportCenteredAt(wallMs: number) {
    const span = viewport.end - viewport.start
    let start = wallMs - span / 2
    let end = start + span
    if (start < dayStart.getTime()) { start = dayStart.getTime(); end = start + span }
    if (end > dayEnd.getTime()) { end = dayEnd.getTime(); start = end - span }
    onViewportChange({ start, end })
  }

  function onMouseDown(e: React.MouseEvent) {
    const frac = fracFromMouse(e)
    const wallMs = dayStart.getTime() + frac * dayMs
    if (wallMs >= viewport.start && wallMs <= viewport.end) {
      // Pan: drag inside the existing viewport rectangle.
      dragRef.current = { mode: 'pan', offsetMs: wallMs - viewport.start }
    } else {
      // Recenter the viewport at the click position.
      dragRef.current = { mode: 'set' }
      setViewportCenteredAt(wallMs)
    }
  }

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragRef.current) return
      const frac = fracFromMouse(e)
      const wallMs = dayStart.getTime() + frac * dayMs
      const span = viewport.end - viewport.start
      if (dragRef.current.mode === 'pan' && dragRef.current.offsetMs != null) {
        let start = wallMs - dragRef.current.offsetMs
        let end = start + span
        if (start < dayStart.getTime()) { start = dayStart.getTime(); end = start + span }
        if (end > dayEnd.getTime()) { end = dayEnd.getTime(); start = end - span }
        onViewportChange({ start, end })
      } else {
        setViewportCenteredAt(wallMs)
      }
    }
    const onUp = () => { dragRef.current = null }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
  })

  // Event density: bucket events into 144 bins (one per 10 min) to draw small dots.
  const BUCKETS = 144
  const density = new Array<number>(BUCKETS).fill(0)
  for (const ev of events) {
    const ms = new Date(ev.started_at).getTime() - dayStart.getTime()
    if (ms < 0 || ms >= dayMs) continue
    const idx = Math.min(BUCKETS - 1, Math.floor((ms / dayMs) * BUCKETS))
    density[idx]++
  }
  const maxDensity = Math.max(1, ...density)

  const vpStartFrac = Math.max(0, Math.min(1, (viewport.start - dayStart.getTime()) / dayMs))
  const vpEndFrac = Math.max(0, Math.min(1, (viewport.end - dayStart.getTime()) / dayMs))
  const playFrac = playWallMs != null
    ? Math.max(0, Math.min(1, (playWallMs - dayStart.getTime()) / dayMs))
    : null

  return (
    <div
      ref={ref}
      onMouseDown={onMouseDown}
      title="Click to recenter the zoomed view · drag the box to pan"
      style={{
        position: 'relative', height: 40, width: '100%',
        borderRadius: 'var(--radius)',
        background: 'var(--bg-base)',
        border: '1px solid var(--border)',
        overflow: 'hidden',
        cursor: 'pointer',
        userSelect: 'none',
      }}
    >
      {/* Hour grid */}
      {Array.from({ length: 4 }, (_, i) => (i + 1) * 6).map(h => (
        <div key={h} style={{
          position: 'absolute', top: 0, bottom: 0,
          left: `${(h / 24) * 100}%`, width: 1,
          background: 'var(--border)', opacity: 0.4,
          pointerEvents: 'none',
        }} />
      ))}

      {/* Segments */}
      {segments.map(seg => {
        const s = Math.max(0, (new Date(seg.started_at).getTime() - dayStart.getTime()) / dayMs)
        const eRaw = seg.ended_at
          ? (new Date(seg.ended_at).getTime() - dayStart.getTime()) / dayMs
          : s + 0.001
        const e = Math.min(1, eRaw)
        const width = Math.max(0.0015, e - s)
        return (
          <div
            key={seg.id}
            style={{
              position: 'absolute', top: 6, bottom: 12,
              left: `${s * 100}%`, width: `${width * 100}%`,
              background: seg.has_gap
                ? 'linear-gradient(135deg, #6d28d9, #7c3aed)'
                : 'linear-gradient(135deg, #1d4ed8, #2563eb)',
              borderRadius: 2,
              opacity: 0.85,
              pointerEvents: 'none',
            }} />
        )
      })}

      {/* Event density dots */}
      {density.map((count, i) => {
        if (!count) return null
        const intensity = 0.4 + 0.6 * (count / maxDensity)
        return (
          <div key={i} style={{
            position: 'absolute', bottom: 2,
            left: `${(i / BUCKETS) * 100}%`,
            width: `${(1 / BUCKETS) * 100}%`,
            height: 6,
            display: 'flex', justifyContent: 'center', alignItems: 'flex-end',
            pointerEvents: 'none',
          }}>
            <span style={{
              width: 3, height: 3, borderRadius: '50%',
              background: 'var(--warning)',
              opacity: intensity,
            }} />
          </div>
        )
      })}

      {/* Hour labels */}
      {[0, 6, 12, 18, 24].map(h => (
        <div key={h} style={{
          position: 'absolute', bottom: 1,
          left: `${(h / 24) * 100}%`,
          transform: 'translateX(-50%)',
          fontSize: 8, color: 'var(--text-muted)',
          pointerEvents: 'none',
          fontFamily: 'var(--font-mono)',
          lineHeight: 1,
        }}>
          {h === 0 ? '0' : h === 24 ? '24' : h}
        </div>
      ))}

      {/* Viewport rectangle */}
      <div style={{
        position: 'absolute', top: 0, bottom: 0,
        left: `${vpStartFrac * 100}%`,
        width: `${Math.max(0.4, (vpEndFrac - vpStartFrac) * 100)}%`,
        border: '1.5px solid var(--accent)',
        background: 'rgba(59, 130, 246, 0.08)',
        borderRadius: 3,
        pointerEvents: 'none',
        boxShadow: '0 0 0 1px rgba(0,0,0,0.3) inset',
      }} />

      {/* Playhead */}
      {playFrac != null && (
        <div style={{
          position: 'absolute', top: 0, bottom: 0,
          left: `${playFrac * 100}%`,
          width: 1.5, background: 'var(--danger)',
          boxShadow: '0 0 4px rgba(239,68,68,0.6)',
          pointerEvents: 'none', zIndex: 5,
        }} />
      )}
    </div>
  )
}

// ── Zoomed timeline ───────────────────────────────────────────────────────────

interface ZoomedProps {
  segments: TimelineSegment[]
  events: TimelineEvent[]
  viewport: Viewport
  dayStartMs: number
  dayEndMs: number
  playWallMs: number | null
  exportStartMs: number | null
  exportEndMs: number | null
  onSeek: (wallMs: number, showToast?: boolean) => void
  onEventClick: (evt: TimelineEvent) => void
  onExportDrag: (startMs: number, endMs: number) => void
  onViewportChange: (v: Viewport) => void
}

const EVENT_ICON: Record<string, string> = {
  motion: 'M',
  face:   'F',
  person: 'P',
  vehicle: 'V',
}

function eventColor(type: string): string {
  if (type === 'face' || type === 'person') return '#10b981' // green
  if (type === 'vehicle') return '#6366f1'                   // indigo
  return 'var(--warning)'                                    // motion (amber)
}

export function TimelineZoomed({
  segments, events, viewport, dayStartMs, dayEndMs, playWallMs, exportStartMs, exportEndMs,
  onSeek, onEventClick, onExportDrag, onViewportChange,
}: ZoomedProps) {
  const ref = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ mode: 'seek' | 'export'; startMs: number } | null>(null)
  const [hover, setHover] = useState<{ x: number; ms: number } | null>(null)

  const spanMs = viewport.end - viewport.start

  function msFromMouse(e: React.MouseEvent | MouseEvent): number {
    if (!ref.current) return viewport.start
    const rect = ref.current.getBoundingClientRect()
    const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    return viewport.start + frac * spanMs
  }

  function xFromMs(ms: number): number {
    return ((ms - viewport.start) / spanMs) * 100
  }

  function onMouseDown(e: React.MouseEvent) {
    const ms = msFromMouse(e)
    if (e.shiftKey) {
      dragRef.current = { mode: 'export', startMs: ms }
      onExportDrag(ms, ms)
    } else {
      dragRef.current = { mode: 'seek', startMs: ms }
      onSeek(ms, true)
    }
  }

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!ref.current) return
      const rect = ref.current.getBoundingClientRect()
      if (e.clientX >= rect.left && e.clientX <= rect.right
        && e.clientY >= rect.top && e.clientY <= rect.bottom) {
        const ms = msFromMouse(e)
        setHover({ x: e.clientX - rect.left, ms })
      } else if (!dragRef.current) {
        setHover(null)
      }
      if (!dragRef.current) return
      const ms = msFromMouse(e)
      if (dragRef.current.mode === 'seek') onSeek(ms, false)
      else {
        const s = dragRef.current.startMs
        onExportDrag(Math.min(s, ms), Math.max(s, ms))
      }
    }
    const onUp = () => { dragRef.current = null }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
  })

  function onWheel(e: React.WheelEvent) {
    e.preventDefault()
    const dir = e.deltaY < 0 ? 'in' : 'out'
    const newSpan = dir === 'in' ? nextZoomIn(spanMs) : nextZoomOut(spanMs)
    if (newSpan === spanMs) return
    // Zoom around mouse position so the point under the cursor stays put.
    const rect = ref.current?.getBoundingClientRect()
    const frac = rect ? (e.clientX - rect.left) / rect.width : 0.5
    const anchorMs = viewport.start + frac * spanMs
    let start = anchorMs - frac * newSpan
    let end = start + newSpan
    // Clamp to the day so ticks never show times from an adjacent day.
    if (start < dayStartMs) { start = dayStartMs; end = Math.min(dayEndMs, start + newSpan) }
    if (end > dayEndMs) { end = dayEndMs; start = Math.max(dayStartMs, end - newSpan) }
    onViewportChange({ start, end })
  }

  // Tick marks: aim for ~6–10 ticks across the viewport.
  function tickInterval(span: number): number {
    const candidates = [
      10 * 1000, 30 * 1000,
      60 * 1000, 2 * 60 * 1000, 5 * 60 * 1000,
      10 * 60 * 1000, 15 * 60 * 1000, 30 * 60 * 1000,
      60 * 60 * 1000, 2 * 60 * 60 * 1000, 3 * 60 * 60 * 1000,
      6 * 60 * 60 * 1000,
    ]
    for (const c of candidates) {
      if (span / c <= 12) return c
    }
    return candidates[candidates.length - 1]
  }
  const interval = tickInterval(spanMs)
  const ticks: number[] = []
  const firstTick = Math.ceil(viewport.start / interval) * interval
  for (let t = firstTick; t <= viewport.end; t += interval) ticks.push(t)

  return (
    <div style={{ position: 'relative' }}>
      <div
        ref={ref}
        onMouseDown={onMouseDown}
        onWheel={onWheel}
        onMouseLeave={() => { if (!dragRef.current) setHover(null) }}
        title="Click to seek · Shift+drag to select export · scroll to zoom"
        style={{
          position: 'relative', height: 100, width: '100%',
          borderRadius: 'var(--radius)',
          background: 'var(--bg-base)',
          border: '1px solid var(--border)',
          overflow: 'hidden',
          cursor: 'crosshair',
          userSelect: 'none',
        }}
      >
        {/* Tick gridlines */}
        {ticks.map((t, i) => (
          <div key={`tg-${i}`} style={{
            position: 'absolute', top: 0, bottom: 18,
            left: `${xFromMs(t)}%`, width: 1,
            background: 'var(--border)', opacity: 0.35,
            pointerEvents: 'none',
          }} />
        ))}

        {/* Segments (clipped to viewport) */}
        {segments.map(seg => {
          const segStart = new Date(seg.started_at).getTime()
          const segEnd = seg.ended_at ? new Date(seg.ended_at).getTime() : Date.now()
          if (segEnd < viewport.start || segStart > viewport.end) return null
          const left = xFromMs(Math.max(segStart, viewport.start))
          const right = xFromMs(Math.min(segEnd, viewport.end))
          return (
            <div
              key={seg.id}
              style={{
                position: 'absolute', top: 14, height: 32,
                left: `${left}%`, width: `${Math.max(0.15, right - left)}%`,
                background: seg.has_gap
                  ? 'linear-gradient(135deg, #6d28d9, #7c3aed)'
                  : 'linear-gradient(135deg, #1d4ed8, #2563eb)',
                borderRadius: 3,
                opacity: 0.9,
                pointerEvents: 'none',
                boxShadow: 'inset 0 -1px 0 rgba(0,0,0,0.2)',
              }}
              title={`Segment #${seg.id}${seg.has_gap ? ' (gap)' : ''}`}
            />
          )
        })}

        {/* Export range overlay */}
        {exportStartMs != null && exportEndMs != null
          && exportEndMs > viewport.start && exportStartMs < viewport.end && (
          <div style={{
            position: 'absolute', top: 12, height: 36,
            left: `${xFromMs(Math.max(exportStartMs, viewport.start))}%`,
            width: `${
              xFromMs(Math.min(exportEndMs, viewport.end))
              - xFromMs(Math.max(exportStartMs, viewport.start))
            }%`,
            background: 'var(--accent-dim)',
            border: '1px solid var(--accent)',
            borderRadius: 3,
            pointerEvents: 'none',
            zIndex: 3,
          }} />
        )}

        {/* Event chips (≥10px wide with icon) */}
        {events.map(evt => {
          const evStart = new Date(evt.started_at).getTime()
          const evEnd = evt.ended_at ? new Date(evt.ended_at).getTime() : evStart + 1000
          if (evEnd < viewport.start || evStart > viewport.end) return null
          const left = xFromMs(Math.max(evStart, viewport.start))
          const right = xFromMs(Math.min(evEnd, viewport.end))
          // Convert percent to pixel-equivalent for minimum width enforcement
          const widthPct = Math.max(0.05, right - left)
          const icon = EVENT_ICON[evt.type] ?? evt.type.charAt(0).toUpperCase()
          const color = eventColor(evt.type)
          return (
            <div
              key={evt.id}
              onClick={e => { e.stopPropagation(); onEventClick(evt) }}
              onMouseDown={e => e.stopPropagation()}
              style={{
                position: 'absolute', top: 54, height: 28,
                left: `${left}%`,
                width: `${widthPct}%`,
                minWidth: 10,
                background: color,
                borderRadius: 3,
                cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 10, fontWeight: 700, color: '#0a0f1d',
                fontFamily: 'var(--font-mono)',
                boxShadow: '0 1px 3px rgba(0,0,0,0.4)',
                zIndex: 4,
                overflow: 'hidden',
                whiteSpace: 'nowrap',
              }}
              title={`${evt.type}${evt.label ? ' — ' + evt.label : ''}\n${fmtHMS(evStart)}`}
            >
              {icon}
            </div>
          )
        })}

        {/* Hover marker line */}
        {hover && (
          <div style={{
            position: 'absolute', top: 0, bottom: 18,
            left: hover.x, width: 1,
            background: 'var(--text-muted)', opacity: 0.6,
            pointerEvents: 'none', zIndex: 6,
          }} />
        )}

        {/* Playhead */}
        {playWallMs != null && playWallMs >= viewport.start && playWallMs <= viewport.end && (
          <div style={{
            position: 'absolute', top: 0, bottom: 18,
            left: `${xFromMs(playWallMs)}%`,
            width: 2, background: 'var(--danger)',
            boxShadow: '0 0 6px rgba(239,68,68,0.6)',
            pointerEvents: 'none', zIndex: 8,
          }} />
        )}

        {/* Tick labels */}
        {ticks.map((t, i) => (
          <div key={`tl-${i}`} style={{
            position: 'absolute', bottom: 3,
            left: `${xFromMs(t)}%`,
            transform: 'translateX(-50%)',
            fontSize: 9, color: 'var(--text-muted)',
            pointerEvents: 'none',
            fontFamily: 'var(--font-mono)',
            whiteSpace: 'nowrap',
          }}>
            {spanMs <= 5 * 60 * 1000 ? fmtHMS(t) : fmtHM(t)}
          </div>
        ))}
      </div>

      {/* Hover tooltip */}
      {hover && (
        <div style={{
          position: 'absolute',
          left: Math.max(0, Math.min((ref.current?.clientWidth ?? 0) - 70, hover.x - 35)),
          top: -22,
          padding: '2px 6px',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-sm)',
          fontSize: 10, fontFamily: 'var(--font-mono)',
          color: 'var(--text-secondary)',
          pointerEvents: 'none',
          whiteSpace: 'nowrap',
          zIndex: 10,
        }}>
          {fmtHMS(hover.ms)}
        </div>
      )}
    </div>
  )
}
