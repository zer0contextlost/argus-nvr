/**
 * LivePage — multi-camera live view grid (1/4/6/9 layouts).
 *
 * Features:
 *   - Layout selection persisted in localStorage.
 *   - Double-click any camera tile (multi-cam) to open it in a fullscreen overlay.
 *     Close with double-click, the X button, or Escape.
 *   - In layout=1 and in the expanded overlay, scroll to zoom and drag to pan.
 */

import React, { useEffect, useRef, useState } from 'react'
import { liveApi, camerasApi, settingsApi, Camera, StreamInfo } from '../api/client'
import { WebRTCPlayer } from '../components/WebRTCPlayer'
import { EmptyState, LoadingState } from '../components/ui/Primitives'

type Layout = 1 | 4 | 6 | 9

const LAYOUT_STORAGE_KEY       = 'sentinel_live_layout'
const ORDER_STORAGE_KEY        = 'argus_live_order'
const HIDE_OFFLINE_STORAGE_KEY = 'argus_live_hide_offline'

function bestGrid(n: number): { cols: number; rows: number } {
  if (n <= 1) return { cols: 1, rows: 1 }
  if (n === 2) return { cols: 2, rows: 1 }
  if (n === 3) return { cols: 3, rows: 1 }
  if (n === 4) return { cols: 2, rows: 2 }
  if (n <= 6)  return { cols: 3, rows: 2 }
  if (n <= 8)  return { cols: 4, rows: 2 }
  if (n <= 9)  return { cols: 3, rows: 3 }
  const cols = Math.ceil(Math.sqrt(n))
  return { cols, rows: Math.ceil(n / cols) }
}

function getSavedLayout(): Layout {
  try {
    const v = localStorage.getItem(LAYOUT_STORAGE_KEY)
    if (v === '1' || v === '4' || v === '6' || v === '9') return Number(v) as Layout
  } catch { /* ignore */ }
  return 4
}

const layoutButtons: { label: string; value: Layout; cols: number; rows: number }[] = [
  { label: '1',  value: 1, cols: 1, rows: 1 },
  { label: '4',  value: 4, cols: 2, rows: 2 },
  { label: '6',  value: 6, cols: 3, rows: 2 },
  { label: '9',  value: 9, cols: 3, rows: 3 },
]

function LayoutIcon({ cols, rows, active }: { cols: number; rows: number; active: boolean }) {
  const cells = Array.from({ length: cols * rows })
  const gap = cols > 2 ? 1.5 : 2
  return (
    <svg width="20" height="16" viewBox="0 0 20 16" aria-hidden="true">
      {cells.map((_, i) => {
        const col = i % cols
        const row = Math.floor(i / cols)
        const cellW = (20 - gap * (cols - 1)) / cols
        const cellH = (16 - gap * (rows - 1)) / rows
        const x = col * (cellW + gap)
        const y = row * (cellH + gap)
        return (
          <rect
            key={i}
            x={x} y={y}
            width={cellW} height={cellH}
            rx={1}
            fill={active ? 'var(--accent)' : 'var(--text-muted)'}
            opacity={active ? 0.9 : 0.5}
          />
        )
      })}
    </svg>
  )
}

const cycleBtn: React.CSSProperties = {
  width: 36, height: 36,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  background: 'rgba(0,0,0,0.6)',
  border: '1px solid rgba(255,255,255,0.2)',
  borderRadius: 'var(--radius-full)',
  color: '#fff',
  cursor: 'pointer',
  backdropFilter: 'blur(4px)',
}

function EmptyTile({ index }: { index: number }) {
  return (
    <div style={{
      background: 'var(--bg-surface)',
      border: '1px solid var(--border-subtle)',
      borderRadius: 'var(--radius)',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      gap: 8,
      color: 'var(--text-muted)',
      position: 'relative',
      overflow: 'hidden',
    }}>
      <div style={{
        position: 'absolute', inset: 0,
        backgroundImage: `
          linear-gradient(var(--border-subtle) 1px, transparent 1px),
          linear-gradient(90deg, var(--border-subtle) 1px, transparent 1px)
        `,
        backgroundSize: '32px 32px',
        opacity: 0.4,
      }} />
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.4">
        <path d="M23 7 16 12 23 17V7z"/>
        <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
      </svg>
      <span style={{ fontSize: 11, opacity: 0.5, position: 'relative' }}>
        Slot {index + 1}
      </span>
    </div>
  )
}

// ── ZoomableWrapper ────────────────────────────────────────────────────────────

interface ZoomableWrapperProps {
  children: React.ReactNode
  active: boolean
  /** Called on double-click when zoom is already at 1× (not consuming the event for reset). */
  onDoubleClickWhenReset?: () => void
}

function ZoomableWrapper({ children, active, onDoubleClickWhenReset }: ZoomableWrapperProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const stateRef = useRef({ zoom: 1, panX: 0, panY: 0 })
  const [, setTick] = useState(0)
  const dragRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)
  const isDragging = !!dragRef.current

  const rerender = () => setTick(n => n + 1)

  const resetZoom = () => {
    stateRef.current = { zoom: 1, panX: 0, panY: 0 }
    dragRef.current = null
    rerender()
  }

  // Reset zoom when deactivated (e.g. layout change).
  useEffect(() => {
    if (!active) resetZoom()
  }, [active])

  // Non-passive wheel listener so we can preventDefault (stops page scroll while zooming).
  useEffect(() => {
    const el = containerRef.current
    if (!el || !active) return
    const handleWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const mx = e.clientX - rect.left
      const my = e.clientY - rect.top
      const s = stateRef.current
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15
      const newZoom = Math.max(1, Math.min(8, s.zoom * factor))
      if (newZoom <= 1) {
        stateRef.current = { zoom: 1, panX: 0, panY: 0 }
      } else {
        stateRef.current = {
          zoom: newZoom,
          panX: mx - (mx - s.panX) * (newZoom / s.zoom),
          panY: my - (my - s.panY) * (newZoom / s.zoom),
        }
      }
      rerender()
    }
    el.addEventListener('wheel', handleWheel, { passive: false })
    return () => el.removeEventListener('wheel', handleWheel)
  }, [active])

  // Global mouse listeners for drag-to-pan.
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!dragRef.current) return
      const d = dragRef.current
      const s = stateRef.current
      stateRef.current = { ...s, panX: d.panX + (e.clientX - d.startX), panY: d.panY + (e.clientY - d.startY) }
      rerender()
    }
    const handleMouseUp = () => { dragRef.current = null; rerender() }
    window.addEventListener('mousemove', handleMouseMove)
    window.addEventListener('mouseup', handleMouseUp)
    return () => { window.removeEventListener('mousemove', handleMouseMove); window.removeEventListener('mouseup', handleMouseUp) }
  }, [])

  const { zoom, panX, panY } = stateRef.current

  const onMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!active || zoom <= 1) return
    e.preventDefault()
    dragRef.current = { startX: e.clientX, startY: e.clientY, panX, panY }
    rerender()
  }

  const onDoubleClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (!active) return
    e.stopPropagation()
    if (zoom > 1) {
      resetZoom()
    } else {
      onDoubleClickWhenReset?.()
    }
  }

  const cursor = !active ? 'inherit'
    : zoom > 1 ? (isDragging ? 'grabbing' : 'grab')
    : 'zoom-in'

  return (
    <div
      ref={containerRef}
      onMouseDown={onMouseDown}
      onDoubleClick={onDoubleClick}
      style={{ width: '100%', height: '100%', overflow: 'hidden', position: 'relative', cursor }}
    >
      <div
        style={{
          width: '100%', height: '100%',
          transform: `translate(${panX}px, ${panY}px) scale(${zoom})`,
          transformOrigin: '0 0',
          willChange: 'transform',
        }}
      >
        {children}
      </div>

      {active && zoom > 1 && (
        <div style={{
          position: 'absolute', top: 10, left: 10, zIndex: 50,
          background: 'rgba(0,0,0,0.65)',
          backdropFilter: 'blur(4px)',
          border: '1px solid rgba(255,255,255,0.18)',
          borderRadius: 6,
          padding: '3px 10px',
          display: 'flex', alignItems: 'center', gap: 8,
          pointerEvents: 'none',
        }}>
          <span style={{ color: '#fff', fontSize: 12, fontFamily: 'monospace', fontWeight: 700 }}>
            {zoom.toFixed(1)}×
          </span>
          <span style={{ color: 'rgba(255,255,255,0.55)', fontSize: 10 }}>dbl-click to reset</span>
        </div>
      )}

      {active && zoom === 1 && (
        <div style={{
          position: 'absolute', bottom: 32, left: '50%', transform: 'translateX(-50%)',
          background: 'rgba(0,0,0,0.45)',
          backdropFilter: 'blur(4px)',
          borderRadius: 20,
          padding: '3px 12px',
          fontSize: 10,
          color: 'rgba(255,255,255,0.45)',
          pointerEvents: 'none',
          whiteSpace: 'nowrap',
          zIndex: 20,
        }}>
          Scroll to zoom
        </div>
      )}
    </div>
  )
}

// ── LivePage ───────────────────────────────────────────────────────────────────

export function LivePage() {
  const [streams, setStreams] = useState<StreamInfo[]>([])
  const [cameras, setCameras] = useState<Camera[]>([])
  const [layout, setLayout] = useState<Layout>(getSavedLayout)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedStream, setExpandedStream] = useState<StreamInfo | null>(null)
  const [selectedGroup, setSelectedGroup] = useState('All')

  // Drag-to-reorder
  const [cameraOrder, setCameraOrder] = useState<number[]>(() => {
    try { return JSON.parse(localStorage.getItem(ORDER_STORAGE_KEY) || '[]') } catch { return [] }
  })
  const [draggedId, setDraggedId] = useState<number | null>(null)
  const [dragOverId, setDragOverId] = useState<number | null>(null)

  // Hide offline cameras.
  // connStates tracks each camera as 'unknown' (initial), 'online', or 'offline'.
  // Only cameras that were online and then lost connection are marked 'offline'.
  // Cameras still in the initial connecting phase stay 'unknown' and are never hidden.
  const [hideOffline, setHideOffline] = useState(() => {
    try { return localStorage.getItem(HIDE_OFFLINE_STORAGE_KEY) === '1' } catch { return false }
  })
  const [connStates, setConnStates] = useState<Map<number, 'unknown' | 'online' | 'offline'>>(new Map())

  // Camera cycle / kiosk mode
  const [cycleActive, setCycleActive] = useState(false)
  const [cyclePaused, setCyclePaused] = useState(false)
  const [cycleIndex, setCycleIndex] = useState(0)
  const [cycleIntervalMs, setCycleIntervalMs] = useState(10_000)
  const [cycleStreams, setCycleStreams] = useState<StreamInfo[]>([])

  useEffect(() => {
    Promise.all([liveApi.list(), camerasApi.list()])
      .then(([s, c]) => { setStreams(s); setCameras(c) })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))

    // Refresh is_recording state periodically so the REC badge stays accurate.
    const poll = setInterval(() => {
      liveApi.list().then(s => setStreams(prev => {
        // Merge: update is_recording without disrupting stream identity/order.
        const byId = new Map(s.map(x => [x.camera_id, x.is_recording]))
        return prev.map(p => ({ ...p, is_recording: byId.get(p.camera_id) ?? p.is_recording }))
      })).catch(() => { /* silently ignore poll errors */ })
    }, 15_000)
    return () => clearInterval(poll)
  }, [])

  // Merge incoming streams into the stored order (add new, remove deleted).
  useEffect(() => {
    if (streams.length === 0) return
    setCameraOrder(prev => {
      const ids = streams.map(s => s.camera_id)
      const merged = [
        ...prev.filter(id => ids.includes(id)),
        ...ids.filter(id => !prev.includes(id)),
      ]
      try { localStorage.setItem(ORDER_STORAGE_KEY, JSON.stringify(merged)) } catch { /* ignore */ }
      return merged
    })
  }, [streams])

  const groups = ['All', ...Array.from(new Set(cameras.filter(c => c.cam_group).map(c => c.cam_group))).sort()]

  const visibleStreams = selectedGroup === 'All'
    ? streams
    : streams.filter(s => cameras.find(c => c.id === s.camera_id)?.cam_group === selectedGroup)

  // Apply saved display order.
  const orderedStreams = cameraOrder.length === 0
    ? visibleStreams
    : [...visibleStreams].sort((a, b) => {
        const ai = cameraOrder.indexOf(a.camera_id)
        const bi = cameraOrder.indexOf(b.camera_id)
        return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi)
      })

  // Filter out cameras explicitly marked offline (never hides 'unknown').
  // Always keep the currently-fullscreened camera in the list even if it
  // transiently flags offline (brief ICE hiccup, watchdog stall-restart) --
  // otherwise it silently drops out of `visible` below, the promoted
  // fullscreen tile (see the grid map further down) unmounts, and the user
  // is dumped back to the grid mid-view with no indication why.
  const displayStreams = hideOffline
    ? orderedStreams.filter(s =>
        connStates.get(s.camera_id) !== 'offline' || s.camera_id === expandedStream?.camera_id)
    : orderedStreams

  const offlineCount = orderedStreams.filter(s => connStates.get(s.camera_id) === 'offline').length

  const setLayoutAndSave = (l: Layout) => {
    setLayout(l)
    try { localStorage.setItem(LAYOUT_STORAGE_KEY, String(l)) } catch { /* ignore */ }
  }

  const handleConnectionChange = (cameraId: number, connected: boolean) => {
    setConnStates(prev => {
      const current = prev.get(cameraId) ?? 'unknown'
      const next = connected ? 'online'
        : current === 'online' ? 'offline'  // was online → now offline
        : current                            // still unknown → stay unknown (initial connect attempt)
      if (next === current) return prev
      return new Map(prev).set(cameraId, next)
    })
  }

  // ── Camera cycle / kiosk mode ───────────────────────────────────────────────

  const stopCycle = () => {
    setCycleActive(false)
    setCyclePaused(false)
    setCycleStreams([])
    setExpandedStream(null)
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {})
  }

  const startCycle = async () => {
    if (displayStreams.length === 0) return
    // Prefer cameras with a confirmed live connection; fall back to all visible.
    const live = displayStreams.filter(s => connStates.get(s.camera_id) !== 'offline')
    const pool = live.length > 0 ? live : displayStreams
    // Fetch configured interval from settings (best-effort; use 10 s default).
    let ms = 10_000
    try {
      const s = await settingsApi.getSettings()
      const parsed = parseInt(s.cycle_interval_seconds ?? '10', 10)
      if (!isNaN(parsed) && parsed >= 3) ms = parsed * 1000
    } catch { /* ignore — use default */ }
    setCycleIntervalMs(ms)
    setCycleStreams(pool)
    setCycleIndex(0)
    setCyclePaused(false)
    setCycleActive(true)
    document.documentElement.requestFullscreen().catch(() => {})
  }

  // Advance expanded stream whenever cycle index changes.
  useEffect(() => {
    if (!cycleActive || cycleStreams.length === 0) return
    setExpandedStream(cycleStreams[cycleIndex % cycleStreams.length])
  }, [cycleActive, cycleIndex, cycleStreams])

  // Cycle timer.
  useEffect(() => {
    if (!cycleActive || cyclePaused || cycleStreams.length <= 1) return
    const t = setInterval(
      () => setCycleIndex(prev => (prev + 1) % cycleStreams.length),
      cycleIntervalMs,
    )
    return () => clearInterval(t)
  }, [cycleActive, cyclePaused, cycleIntervalMs, cycleStreams.length])

  // Stop cycle if the user exits fullscreen via browser controls (F11/Esc).
  useEffect(() => {
    const handler = () => { if (!document.fullscreenElement && cycleActive) stopCycle() }
    document.addEventListener('fullscreenchange', handler)
    return () => document.removeEventListener('fullscreenchange', handler)
  }, [cycleActive])

  // ── Drag-to-reorder handlers ─────────────────────────────────────────────────

  // Drag-to-reorder handlers.
  const handleDragStart = (e: React.DragEvent, cameraId: number) => {
    setDraggedId(cameraId)
    e.dataTransfer.effectAllowed = 'move'
  }
  const handleDragOver = (e: React.DragEvent, cameraId: number) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (cameraId !== dragOverId) setDragOverId(cameraId)
  }
  const handleDrop = (e: React.DragEvent, targetId: number) => {
    e.preventDefault()
    if (draggedId === null || draggedId === targetId) { setDraggedId(null); setDragOverId(null); return }
    setCameraOrder(prev => {
      const next = [...prev]
      const fromIdx = next.indexOf(draggedId)
      const toIdx   = next.indexOf(targetId)
      if (fromIdx === -1 || toIdx === -1) return prev
      next.splice(fromIdx, 1)
      next.splice(toIdx, 0, draggedId)
      try { localStorage.setItem(ORDER_STORAGE_KEY, JSON.stringify(next)) } catch { /* ignore */ }
      return next
    })
    setDraggedId(null)
    setDragOverId(null)
  }
  const handleDragEnd = () => { setDraggedId(null); setDragOverId(null) }

  useEffect(() => {
    if (!expandedStream) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (cycleActive) stopCycle()
      else setExpandedStream(null)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [expandedStream, cycleActive])

  const layoutDef = layoutButtons.find(l => l.value === layout)!
  const visible = displayStreams.slice(0, layout)
  // Suppress empty slot tiles when hiding offline cameras — no point showing blank slots.
  const placeholders = hideOffline ? [] : Array.from({ length: Math.max(0, layout - visible.length) })

  if (loading) return <LoadingState message="Loading streams…" />

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100dvh - 92px)', gap: 12 }}>
      {/* Controls bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', flexShrink: 0 }}>
        <span style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 500 }}>LAYOUT</span>

        {layoutButtons.map(btn => (
          <button
            key={btn.value}
            onClick={() => setLayoutAndSave(btn.value)}
            title={`${btn.value}-camera grid`}
            style={{
              display: 'flex', alignItems: 'center', gap: 5,
              padding: '5px 10px',
              borderRadius: 'var(--radius)',
              border: layout === btn.value ? '1px solid var(--accent)' : '1px solid var(--border)',
              background: layout === btn.value ? 'var(--accent-dim)' : 'var(--bg-surface)',
              color: layout === btn.value ? 'var(--text-accent)' : 'var(--text-muted)',
              cursor: 'pointer',
              fontSize: 12,
              fontWeight: 600,
              transition: 'all var(--transition)',
              minWidth: 44,
              minHeight: 32,
            }}
          >
            <LayoutIcon cols={btn.cols} rows={btn.rows} active={layout === btn.value} />
            {btn.label}
          </button>
        ))}

        <div style={{ flex: 1 }} />

        {/* Hide offline toggle */}
        {streams.length > 0 && (
          <button
            onClick={() => setHideOffline(v => {
              const next = !v
              try { localStorage.setItem(HIDE_OFFLINE_STORAGE_KEY, next ? '1' : '0') } catch { /* ignore */ }
              return next
            })}
            title={hideOffline ? 'Show all cameras' : 'Hide cameras that are not streaming'}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '5px 10px',
              borderRadius: 'var(--radius)',
              border: hideOffline ? '1px solid var(--accent)' : '1px solid var(--border)',
              background: hideOffline ? 'var(--accent-dim)' : 'var(--bg-surface)',
              color: hideOffline ? 'var(--text-accent)' : 'var(--text-muted)',
              cursor: 'pointer', fontSize: 12, fontWeight: 600,
              transition: 'all var(--transition)',
              minHeight: 32,
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {hideOffline
                ? <><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></>
                : <><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></>
              }
            </svg>
            {hideOffline
              ? `Live only${offlineCount > 0 ? ` (${offlineCount} hidden)` : ''}`
              : 'Hide offline'}
          </button>
        )}

        {displayStreams.length > 0 && (
          <button
            onClick={startCycle}
            title="Cycle cameras fullscreen (configure interval in Settings)"
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: '5px 10px',
              borderRadius: 'var(--radius)',
              border: '1px solid var(--border)',
              background: 'var(--bg-surface)',
              color: 'var(--text-muted)',
              cursor: 'pointer', fontSize: 12, fontWeight: 600,
              transition: 'all var(--transition)',
              minHeight: 32,
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="5 12 3 12 12 3 21 12 19 12"/>
              <polyline points="5 12 5 20 19 20 19 12"/>
            </svg>
            Cycle
          </button>
        )}

        {streams.length > 0 && (
          <div className="live-indicator" style={{ marginRight: 4 }}>
            <span className="live-dot" />
            <span>{visible.length} camera{visible.length !== 1 ? 's' : ''}</span>
          </div>
        )}

        {error && <div style={{ fontSize: 12, color: 'var(--danger)' }}>⚠ {error}</div>}
      </div>

      {/* Group filter tabs */}
      {groups.length > 1 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
          {groups.map(g => (
            <button
              key={g}
              onClick={() => setSelectedGroup(g)}
              style={{
                padding: '4px 14px', borderRadius: 'var(--radius-full)',
                border: selectedGroup === g ? '1px solid var(--accent)' : '1px solid var(--border)',
                background: selectedGroup === g ? 'var(--accent-dim)' : 'transparent',
                color: selectedGroup === g ? 'var(--text-accent)' : 'var(--text-muted)',
                cursor: 'pointer', fontSize: 12, fontWeight: 600,
                transition: 'all var(--transition)',
              }}
            >
              {g}
            </button>
          ))}
        </div>
      )}

      {/* Grid */}
      <style>{`
        @media (max-width: 640px) {
          .live-grid { grid-template-columns: 1fr !important; grid-template-rows: auto !important; height: auto !important; }
          .live-grid > * { min-height: 200px; }
        }
      `}</style>

      {visibleStreams.length === 0 && !error ? (
        <div style={{
          flex: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'var(--bg-surface)',
          borderRadius: 'var(--radius-lg)',
          border: '1px solid var(--border)',
        }}>
          <EmptyState
            icon="📷"
            title="No cameras configured"
            body="Add cameras on the Cameras page to see live streams here. Streams appear automatically once cameras are active."
            action={
              <a href="/cameras" className="btn btn-primary btn-sm" style={{ textDecoration: 'none' }}>
                Go to Cameras
              </a>
            }
          />
        </div>
      ) : (
        <div
          className="live-grid"
          style={{
            flex: 1,
            display: 'grid',
            gridTemplateColumns: `repeat(${(hideOffline ? bestGrid(visible.length) : layoutDef).cols}, 1fr)`,
            gridTemplateRows: `repeat(${(hideOffline ? bestGrid(visible.length) : layoutDef).rows}, 1fr)`,
            gap: 6,
          }}
        >
          {visible.map(s => {
            const isSingle = layout === 1
            const isDragged = draggedId === s.camera_id
            const isDropTarget = dragOverId === s.camera_id && draggedId !== s.camera_id
            // When this camera is open in the expanded overlay a second WebRTCPlayer
            // would create a duplicate WebRTC connection to the same go2rtc stream,
            // causing both to fight for the connection and trigger reconnect loops.
            // Suspend the grid tile's player while the overlay is open for this camera.
            const isExpanded = expandedStream?.camera_id === s.camera_id
            const recBorder = s.is_recording
              ? '2px solid rgba(220,38,38,0.85)'
              : isDropTarget ? '2px solid var(--accent)' : '2px solid transparent'
            // Promote this tile to fullscreen for the normal double-click expand
            // (NOT kiosk cycling, which switches cameras and uses the overlay's
            // own player). The player element stays mounted at its stable tree
            // position, so the live WebRTC connection survives the transition and
            // there is no 3-5s black reconnect gap entering or leaving fullscreen.
            // This only holds while both branches below keep the identical
            // div > div > ZoomableWrapper > player element nesting.
            const isPromoted = isExpanded && !cycleActive
            const player = (
              <WebRTCPlayer
                stream={s}
                style={{ height: '100%', borderRadius: isPromoted ? 'var(--radius-lg)' : undefined }}
                isRecording={s.is_recording}
                cameraId={s.camera_id}
                ptzCapable={cameras.find(c => c.id === s.camera_id)?.ptz_capable}
                onConnectionChange={connected => handleConnectionChange(s.camera_id, connected)}
              />
            )

            if (isPromoted) {
              return (
                <div
                  key={s.camera_id}
                  onClick={e => { if (e.target === e.currentTarget) setExpandedStream(null) }}
                  style={{
                    position: 'fixed', inset: 0, zIndex: 790,
                    background: '#000',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}
                >
                  {/* Explicit 16:9 box — inner height:100% needs a definite height
                      (see CLAUDE.md gotcha #22). */}
                  <div style={{
                    height: 'min(90vh, calc(95vw * 9 / 16))',
                    width: 'min(95vw, calc(90vh * 16 / 9))',
                  }}>
                    <ZoomableWrapper active onDoubleClickWhenReset={() => setExpandedStream(null)}>
                      {player}
                    </ZoomableWrapper>
                  </div>
                </div>
              )
            }

            return (
              <div
                key={s.camera_id}
                draggable={!isSingle}
                onDragStart={!isSingle ? e => handleDragStart(e, s.camera_id) : undefined}
                onDragOver={!isSingle ? e => handleDragOver(e, s.camera_id) : undefined}
                onDrop={!isSingle ? e => handleDrop(e, s.camera_id) : undefined}
                onDragEnd={!isSingle ? handleDragEnd : undefined}
                onDoubleClick={isSingle ? undefined : () => setExpandedStream(s)}
                title={isSingle
                  ? 'Scroll to zoom · Drag to pan · Double-click to expand'
                  : 'Drag to rearrange · Double-click to expand'}
                style={{
                  height: '100%',
                  cursor: isSingle ? undefined : 'grab',
                  opacity: isDragged ? 0.45 : 1,
                  outline: recBorder,
                  outlineOffset: '-2px',
                  borderRadius: 'var(--radius)',
                  transition: 'opacity 0.15s, outline 0.1s',
                  position: 'relative',
                }}
              >
                {/* Same div > ZoomableWrapper > player nesting as the promoted
                    branch above. If the element types along this path differ
                    between the two branches, React unmounts the player on every
                    fullscreen enter/exit and the WebRTC session drops to black. */}
                <div style={{ height: '100%' }}>
                  <ZoomableWrapper
                    active={isSingle}
                    onDoubleClickWhenReset={() => setExpandedStream(s)}
                  >
                    {/* Suspend the grid tile's player only while kiosk cycling has
                        this camera in the overlay (its own switching player owns the
                        connection then). */}
                    {isExpanded ? (
                      <div style={{
                        height: '100%', background: '#000',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                      }}>
                        <span style={{ color: 'rgba(255,255,255,0.25)', fontSize: 12 }}>
                          {s.camera_name}
                        </span>
                      </div>
                    ) : player}
                  </ZoomableWrapper>
                </div>
              </div>
            )
          })}
          {placeholders.map((_, i) => (
            <EmptyTile key={`ph-${i}`} index={visible.length + i} />
          ))}
        </div>
      )}

      {/* Expanded camera overlay (also used by cycle/kiosk mode) */}
      {expandedStream && (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 800,
            // When not cycling the fullscreen picture comes from the promoted grid
            // tile beneath this layer, so the overlay is transparent and lets
            // pointer events (zoom/pan, background-click-to-close) reach the tile;
            // only the chrome (close button) re-enables pointer events.
            background: cycleActive ? '#000' : 'transparent',
            pointerEvents: cycleActive ? 'auto' : 'none',
            display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center',
          }}
          onClick={e => { if (!cycleActive && e.target === e.currentTarget) setExpandedStream(null) }}
        >
          {/* CSS for progress bar depletion animation */}
          <style>{`
            @keyframes cycle-deplete { from { width: 100% } to { width: 0% } }
          `}</style>

          {/* Close / Stop button */}
          <button
            onClick={e => { e.stopPropagation(); cycleActive ? stopCycle() : setExpandedStream(null) }}
            aria-label={cycleActive ? 'Stop cycle' : 'Close expanded view'}
            style={{
              position: 'absolute', top: 16, right: 16,
              background: 'rgba(0,0,0,0.6)',
              border: '1px solid rgba(255,255,255,0.2)',
              borderRadius: 'var(--radius-full)',
              color: '#fff',
              width: 36, height: 36,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 18, cursor: 'pointer', zIndex: 801,
              backdropFilter: 'blur(4px)',
              transition: 'background var(--transition)',
              pointerEvents: 'auto',
            }}
            onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.15)')}
            onMouseLeave={e => (e.currentTarget.style.background = 'rgba(0,0,0,0.6)')}
          >
            &#x2715;
          </button>

          {/* Cycle controls — visible only in kiosk mode */}
          {cycleActive && (
            <div style={{
              position: 'absolute', top: 16, left: 16,
              display: 'flex', alignItems: 'center', gap: 6,
              zIndex: 801,
            }}>
              {/* Prev */}
              <button
                onClick={() => setCycleIndex(prev => (prev - 1 + cycleStreams.length) % cycleStreams.length)}
                title="Previous camera"
                style={cycleBtn}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="15 18 9 12 15 6"/>
                </svg>
              </button>

              {/* Pause / Resume */}
              <button
                onClick={() => setCyclePaused(p => !p)}
                title={cyclePaused ? 'Resume' : 'Pause'}
                style={cycleBtn}
              >
                {cyclePaused
                  ? <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                  : <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
                }
              </button>

              {/* Next */}
              <button
                onClick={() => setCycleIndex(prev => (prev + 1) % cycleStreams.length)}
                title="Next camera"
                style={cycleBtn}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="9 18 15 12 9 6"/>
                </svg>
              </button>

              {/* Counter */}
              <span style={{ fontSize: 12, color: 'rgba(255,255,255,0.7)', fontWeight: 600, marginLeft: 4 }}>
                {cycleIndex + 1} / {cycleStreams.length}
              </span>
            </div>
          )}

          {/* Video player — only in kiosk cycle mode, which switches cameras and
              needs its own player. The normal double-click expand keeps the grid
              tile's player mounted and promotes it to fullscreen (see the grid
              map above), avoiding the reconnect black-out. */}
          {cycleActive && (
            <div
              style={{
                height: 'min(90vh, calc(95vw * 9 / 16))',
                width: 'min(95vw, calc(90vh * 16 / 9))',
                pointerEvents: 'auto',
              }}
            >
              <ZoomableWrapper active={false} onDoubleClickWhenReset={() => {}}>
                <WebRTCPlayer
                  stream={expandedStream}
                  isRecording={expandedStream.is_recording}
                  cameraId={expandedStream.camera_id}
                  ptzCapable={cameras.find(c => c.id === expandedStream.camera_id)?.ptz_capable}
                  style={{ height: '100%', borderRadius: 0 }}
                />
              </ZoomableWrapper>
            </div>
          )}

          {/* Camera dots — shown when cycling */}
          {cycleActive && cycleStreams.length > 1 && (
            <div style={{
              display: 'flex', gap: 6, marginTop: 14,
              flexWrap: 'wrap', justifyContent: 'center', maxWidth: '80vw',
            }}>
              {cycleStreams.map((s, i) => (
                <button
                  key={s.camera_id}
                  onClick={() => setCycleIndex(i)}
                  title={s.camera_name}
                  style={{
                    width: i === cycleIndex ? 20 : 8,
                    height: 8,
                    borderRadius: 4,
                    border: 'none',
                    padding: 0,
                    background: i === cycleIndex ? 'var(--danger)' : 'rgba(255,255,255,0.3)',
                    cursor: 'pointer',
                    transition: 'all 0.3s',
                    flexShrink: 0,
                  }}
                />
              ))}
            </div>
          )}

          {/* Progress bar — depletes over cycle interval */}
          {cycleActive && !cyclePaused && (
            <div style={{
              position: 'absolute', bottom: 0, left: 0, right: 0,
              height: 3, background: 'rgba(255,255,255,0.1)',
            }}>
              <div
                key={`progress-${cycleIndex}`}
                style={{
                  height: '100%',
                  background: 'var(--danger)',
                  animation: `cycle-deplete ${cycleIntervalMs}ms linear forwards`,
                }}
              />
            </div>
          )}

          {!cycleActive && (
            <div style={{ marginTop: 12, fontSize: 12, color: 'rgba(255,255,255,0.5)', letterSpacing: '0.04em' }}>
              Scroll to zoom · Drag to pan · Double-click or Esc to close
            </div>
          )}
        </div>
      )}
    </div>
  )
}
