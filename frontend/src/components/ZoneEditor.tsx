/**
 * ZoneEditor — draw detection polygons over a live-refreshing camera snapshot.
 *
 * UX:
 *   - Click on the image to drop polygon vertices. A rubber-band line follows
 *     the cursor so you can see what you're drawing.
 *   - After 3+ points, the in-progress polygon fills semi-transparently and the
 *     first vertex highlights when you hover near it — click it to close.
 *     (Or use the "Finish Zone" button.)
 *   - Backspace removes the last placed vertex; Escape cancels drawing.
 *   - Existing zones show their vertices. The active zone's vertices can be
 *     dragged to reshape it, or right-clicked to delete (min 3 points).
 *   - The background snapshot auto-refreshes every 3 seconds (toggleable) so
 *     you can see scene changes — useful for framing the zone around what
 *     actually matters (driveway, doorway, etc.).
 *
 * Props:
 *   cameraId      — camera to edit zones for
 *   snapshotUrl   — URL for the background snapshot image. Any ?token= the caller
 *                   appends is stripped — we provide auth via the Authorization
 *                   header so the JWT stays out of the URL string.
 *   initialJson   — existing detect_config_json string (or '{}')
 *   onSave        — called with updated detect_config_json string
 *   onClose       — called when user dismisses
 */

import React, { useRef, useEffect, useState, useCallback, useMemo } from 'react'
import { getToken } from '../api/client'

interface Zone {
  name: string
  polygon: [number, number][]  // normalised [0..1]
  enabled: boolean
}

interface ArmWindow {
  days: number[]
  start: string
  end: string
}

interface DetectConfig {
  zones: Zone[]
  min_changed_pct?: number
  motion_confirm_frames?: number
  post_motion_seconds?: number
  pixel_threshold?: number
  arm_schedule?: ArmWindow[]
}

interface ZoneEditorProps {
  cameraId: number
  snapshotUrl?: string
  initialJson: string
  onSave: (json: string) => void
  onClose: () => void
}

const BTN: React.CSSProperties = {
  padding: '6px 14px',
  borderRadius: '6px',
  border: 'none',
  cursor: 'pointer',
  fontSize: '13px',
  fontWeight: 500,
}

// Snap distance in canvas pixels for "click first vertex to close"
const SNAP_PX = 12
// Hit-test radius for grabbing existing vertices to drag
const DRAG_HIT_PX = 10
// How often to refresh the background snapshot (ms)
const REFRESH_INTERVAL_MS = 3000

export function ZoneEditor({ cameraId, snapshotUrl, initialJson, onSave, onClose }: ZoneEditorProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const [config, setConfig] = useState<DetectConfig>(() => {
    try { return JSON.parse(initialJson || '{}') } catch { return {} as DetectConfig }
  })
  const [zones, setZones] = useState<Zone[]>(() => {
    try {
      const c = JSON.parse(initialJson || '{}')
      return c.zones || []
    } catch { return [] }
  })
  const [armSchedule, setArmSchedule] = useState<ArmWindow[]>(() => {
    try {
      const c = JSON.parse(initialJson || '{}')
      return c.arm_schedule || []
    } catch { return [] }
  })

  const [activeZoneIdx, setActiveZoneIdx] = useState<number>(0)
  const [drawing, setDrawing] = useState(false)
  const [currentPoly, setCurrentPoly] = useState<[number, number][]>([])
  const [bgImage, setBgImage] = useState<HTMLImageElement | null>(null)
  // Mirror of bgImage as a ref so the snapshot-load effect can read the current
  // value without re-running on every change (and without subtle stale-closure
  // bugs in onerror handlers that fire long after the effect set up).
  const bgImageRef = useRef<HTMLImageElement | null>(null)
  useEffect(() => { bgImageRef.current = bgImage }, [bgImage])
  const [imgAspect, setImgAspect] = useState<number>(16 / 9)
  const [canvasSize, setCanvasSize] = useState({ w: 800, h: 450 })
  const [status, setStatus] = useState<string>('')

  // Cursor position in normalised (0..1) coords. null when cursor is off-canvas.
  const [mousePos, setMousePos] = useState<[number, number] | null>(null)
  // Are we currently dragging a vertex of an existing zone? { zoneIdx, ptIdx }
  const [dragging, setDragging] = useState<{ zoneIdx: number; ptIdx: number } | null>(null)

  // Live snapshot refresh
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [refreshTick, setRefreshTick] = useState(0)
  const [snapshotFailed, setSnapshotFailed] = useState(false)

  // ── Snapshot loading + auto-refresh ────────────────────────────────────────
  // Fetch via XHR with the Authorization header so the JWT stays out of the URL
  // (browser history, devtools network tab, and server access logs would otherwise
  // capture it). The /snapshot route accepts both Bearer and ?token= query auth;
  // we prefer Bearer for hygiene. The response blob is wrapped in an object URL
  // and fed to an off-DOM <Image> so existing canvas rendering keeps working.
  useEffect(() => {
    if (!snapshotUrl) return
    let cancelled = false
    let objectUrl: string | null = null

    // Strip any ?token=/&token= so the URL is clean; we provide auth via header.
    const cleanUrl = snapshotUrl
      .replace(/([?&])token=[^&]*&?/, '$1')
      .replace(/[?&]$/, '')

    const cacheBust = cleanUrl.includes('?') ? `&t=${refreshTick}` : `?t=${refreshTick}`
    const url = cleanUrl + cacheBust
    const token = getToken()

    fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then(async (resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
        return resp.blob()
      })
      .then((blob) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        const img = new Image()
        img.onload = () => {
          if (cancelled) { URL.revokeObjectURL(objectUrl!); return }
          setBgImage(img)
          setSnapshotFailed(false)
          setImgAspect(img.naturalWidth / Math.max(1, img.naturalHeight))
        }
        img.onerror = () => {
          // Existing image stays put; only mark failed if we never had one.
          setSnapshotFailed((prev) => prev || bgImageRef.current === null)
        }
        img.src = objectUrl
      })
      .catch(() => {
        if (cancelled) return
        setSnapshotFailed((prev) => prev || bgImageRef.current === null)
      })

    return () => {
      cancelled = true
      // Revoke after a tick so the <Image> finishes any pending decode.
      if (objectUrl) {
        const u = objectUrl
        setTimeout(() => URL.revokeObjectURL(u), 1000)
      }
    }
  }, [snapshotUrl, refreshTick])

  useEffect(() => {
    if (!autoRefresh) return
    const id = window.setInterval(() => setRefreshTick((n) => n + 1), REFRESH_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [autoRefresh])

  // ── Responsive canvas size — fits container width, preserves image aspect ─
  useEffect(() => {
    const ro = new ResizeObserver(() => {
      const cont = containerRef.current
      if (!cont) return
      // Account for the modal padding — use the container's content width.
      const availW = Math.min(cont.clientWidth, 1080)
      // Leave some headroom for the rest of the modal vertically.
      const maxH = Math.max(280, window.innerHeight * 0.55)
      let w = availW
      let h = Math.round(w / imgAspect)
      if (h > maxH) {
        h = Math.round(maxH)
        w = Math.round(h * imgAspect)
      }
      setCanvasSize({ w, h })
    })
    if (containerRef.current) ro.observe(containerRef.current)
    return () => ro.disconnect()
  }, [imgAspect])

  // ── Canvas rendering ──────────────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const W = canvas.width, H = canvas.height

    ctx.clearRect(0, 0, W, H)

    // Background
    if (bgImage) {
      ctx.drawImage(bgImage, 0, 0, W, H)
    } else {
      ctx.fillStyle = '#0f1117'
      ctx.fillRect(0, 0, W, H)
      ctx.fillStyle = '#4b6488'
      ctx.font = '14px sans-serif'
      ctx.textAlign = 'center'
      ctx.fillText(
        snapshotFailed ? 'Snapshot unavailable — camera may be offline' : 'Loading snapshot…',
        W / 2,
        H / 2,
      )
    }

    // Existing zones (filled + stroked + labelled)
    zones.forEach((z, i) => {
      if (z.polygon.length < 2) return
      const isActive = i === activeZoneIdx
      const disabled = !z.enabled

      ctx.beginPath()
      z.polygon.forEach(([nx, ny], pi) => {
        const x = nx * W, y = ny * H
        if (pi === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      })
      ctx.closePath()

      if (disabled) {
        ctx.fillStyle = 'rgba(148,163,184,0.05)'
        ctx.strokeStyle = '#475569'
        ctx.lineWidth = 1
        ctx.setLineDash([4, 4])
      } else if (isActive) {
        ctx.fillStyle = 'rgba(59,130,246,0.18)'
        ctx.strokeStyle = '#3b82f6'
        ctx.lineWidth = 2.5
        ctx.setLineDash([])
      } else {
        ctx.fillStyle = 'rgba(96,165,250,0.08)'
        ctx.strokeStyle = '#60a5fa'
        ctx.lineWidth = 1.5
        ctx.setLineDash([])
      }
      ctx.fill()
      ctx.stroke()
      ctx.setLineDash([])

      // Label at centroid
      const cx = z.polygon.reduce((s, p) => s + p[0], 0) / z.polygon.length * W
      const cy = z.polygon.reduce((s, p) => s + p[1], 0) / z.polygon.length * H
      ctx.fillStyle = isActive ? '#bfdbfe' : '#cbd5e1'
      ctx.font = '600 12px sans-serif'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      // Label backdrop for readability
      const labelText = (z.name || `Zone ${i + 1}`) + (disabled ? ' (off)' : '')
      const tw = ctx.measureText(labelText).width
      ctx.fillStyle = 'rgba(0,0,0,0.55)'
      ctx.fillRect(cx - tw / 2 - 5, cy - 9, tw + 10, 18)
      ctx.fillStyle = isActive ? '#bfdbfe' : '#cbd5e1'
      ctx.fillText(labelText, cx, cy)

      // Vertex dots — show on all zones; active zone gets bigger handles
      const dotR = isActive ? 5 : 3
      z.polygon.forEach(([nx, ny]) => {
        const x = nx * W, y = ny * H
        ctx.beginPath()
        ctx.arc(x, y, dotR, 0, Math.PI * 2)
        ctx.fillStyle = isActive ? '#3b82f6' : '#60a5fa'
        ctx.fill()
        if (isActive) {
          ctx.lineWidth = 1.5
          ctx.strokeStyle = '#0f1117'
          ctx.stroke()
        }
      })
    })

    // In-progress polygon
    if (currentPoly.length > 0) {
      // Filled body once 3+ points so the user sees the zone forming
      if (currentPoly.length >= 3) {
        ctx.beginPath()
        currentPoly.forEach(([nx, ny], pi) => {
          const x = nx * W, y = ny * H
          if (pi === 0) ctx.moveTo(x, y)
          else ctx.lineTo(x, y)
        })
        ctx.closePath()
        ctx.fillStyle = 'rgba(245,158,11,0.18)'
        ctx.fill()
      }

      // Solid edges between placed vertices
      ctx.beginPath()
      currentPoly.forEach(([nx, ny], pi) => {
        const x = nx * W, y = ny * H
        if (pi === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      })
      ctx.strokeStyle = '#f59e0b'
      ctx.lineWidth = 2
      ctx.stroke()

      // Rubber-band: from last vertex → cursor (dashed)
      if (mousePos && drawing) {
        const [lastX, lastY] = currentPoly[currentPoly.length - 1]
        ctx.beginPath()
        ctx.moveTo(lastX * W, lastY * H)
        ctx.lineTo(mousePos[0] * W, mousePos[1] * H)
        ctx.strokeStyle = 'rgba(245,158,11,0.7)'
        ctx.lineWidth = 1.5
        ctx.setLineDash([6, 4])
        ctx.stroke()
        ctx.setLineDash([])

        // Closing hint line (cursor → first vertex) when we have 2+ points
        if (currentPoly.length >= 2) {
          const [firstX, firstY] = currentPoly[0]
          ctx.beginPath()
          ctx.moveTo(mousePos[0] * W, mousePos[1] * H)
          ctx.lineTo(firstX * W, firstY * H)
          ctx.strokeStyle = 'rgba(245,158,11,0.25)'
          ctx.lineWidth = 1
          ctx.setLineDash([3, 5])
          ctx.stroke()
          ctx.setLineDash([])
        }
      }

      // Placed vertex dots
      currentPoly.forEach(([nx, ny], pi) => {
        const x = nx * W, y = ny * H
        ctx.beginPath()
        ctx.arc(x, y, pi === 0 && currentPoly.length >= 3 ? 7 : 5, 0, Math.PI * 2)
        ctx.fillStyle = '#f59e0b'
        ctx.fill()
        ctx.lineWidth = 1.5
        ctx.strokeStyle = '#0f1117'
        ctx.stroke()
      })

      // Snap-to-close highlight on first vertex
      if (mousePos && drawing && currentPoly.length >= 3) {
        const [firstX, firstY] = currentPoly[0]
        const dx = (mousePos[0] - firstX) * W
        const dy = (mousePos[1] - firstY) * H
        if (Math.hypot(dx, dy) <= SNAP_PX) {
          ctx.beginPath()
          ctx.arc(firstX * W, firstY * H, 10, 0, Math.PI * 2)
          ctx.strokeStyle = '#22c55e'
          ctx.lineWidth = 2
          ctx.stroke()
        }
      }

      // Ghost cursor dot (where the next vertex would land)
      if (mousePos && drawing) {
        ctx.beginPath()
        ctx.arc(mousePos[0] * W, mousePos[1] * H, 4, 0, Math.PI * 2)
        ctx.fillStyle = 'rgba(245,158,11,0.55)'
        ctx.fill()
      }
    }
  }, [bgImage, snapshotFailed, zones, currentPoly, activeZoneIdx, mousePos, drawing, canvasSize])

  // ── Coordinate helpers ────────────────────────────────────────────────────
  const eventToNorm = useCallback((e: React.MouseEvent<HTMLCanvasElement>): [number, number] | null => {
    const canvas = canvasRef.current
    if (!canvas) return null
    const rect = canvas.getBoundingClientRect()
    const x = (e.clientX - rect.left) / rect.width
    const y = (e.clientY - rect.top) / rect.height
    return [Math.max(0, Math.min(1, x)), Math.max(0, Math.min(1, y))]
  }, [])

  const vertexHit = useCallback((nx: number, ny: number): { zoneIdx: number; ptIdx: number } | null => {
    // Only vertices of the active zone are draggable.
    const z = zones[activeZoneIdx]
    if (!z) return null
    const W = canvasSize.w, H = canvasSize.h
    for (let i = 0; i < z.polygon.length; i++) {
      const [px, py] = z.polygon[i]
      if (Math.hypot((px - nx) * W, (py - ny) * H) <= DRAG_HIT_PX) {
        return { zoneIdx: activeZoneIdx, ptIdx: i }
      }
    }
    return null
  }, [zones, activeZoneIdx, canvasSize])

  // ── Zone actions used by mouse + keyboard handlers ────────────────────────
  // Memoised so the handlers below capture a fresh closure when state changes —
  // without this, deleting a zone while drawing could make a cached handler call
  // an old finishPoly that sees a stale zones array.
  const finishPoly = useCallback(() => {
    if (currentPoly.length < 3) {
      setStatus('Need at least 3 points to define a zone.')
      return
    }
    const zoneName = `Zone ${zones.length + 1}`
    const newZone: Zone = { name: zoneName, polygon: currentPoly, enabled: true }
    setZones((prev) => [...prev, newZone])
    setActiveZoneIdx(zones.length)  // index of the newly-pushed zone
    setCurrentPoly([])
    setDrawing(false)
    setStatus(`Zone "${zoneName}" added. Click Save to persist.`)
  }, [currentPoly, zones])

  // ── Mouse handlers ────────────────────────────────────────────────────────
  const handleMouseDown = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    if (e.button !== 0) return  // left button only
    const pt = eventToNorm(e)
    if (!pt) return

    if (drawing) {
      // Snap-to-close on first vertex
      if (currentPoly.length >= 3) {
        const [fx, fy] = currentPoly[0]
        if (Math.hypot((fx - pt[0]) * canvasSize.w, (fy - pt[1]) * canvasSize.h) <= SNAP_PX) {
          finishPoly()
          return
        }
      }
      setCurrentPoly((prev) => [...prev, pt])
      return
    }

    // Not drawing — try to start a vertex drag on the active zone
    const hit = vertexHit(pt[0], pt[1])
    if (hit) {
      setDragging(hit)
      e.preventDefault()
    }
  }, [drawing, currentPoly, canvasSize, eventToNorm, vertexHit, finishPoly])

  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const pt = eventToNorm(e)
    setMousePos(pt)
    if (!pt) return
    if (dragging) {
      setZones((prev) => prev.map((z, zi) => {
        if (zi !== dragging.zoneIdx) return z
        const poly = z.polygon.map((p, pi) => pi === dragging.ptIdx ? pt : p) as [number, number][]
        return { ...z, polygon: poly }
      }))
    }
  }, [dragging, eventToNorm])

  const handleMouseUp = useCallback(() => {
    if (dragging) setDragging(null)
  }, [dragging])

  const handleMouseLeave = useCallback(() => {
    setMousePos(null)
    if (dragging) setDragging(null)
  }, [dragging])

  const handleContextMenu = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    e.preventDefault()
    if (drawing) return  // right-click while drawing does nothing
    const pt = eventToNorm(e)
    if (!pt) return
    const hit = vertexHit(pt[0], pt[1])
    if (!hit) return
    const z = zones[hit.zoneIdx]
    if (z.polygon.length <= 3) {
      setStatus('A zone needs at least 3 vertices — delete the zone if you want it gone.')
      return
    }
    setZones((prev) => prev.map((zz, zi) => {
      if (zi !== hit.zoneIdx) return zz
      return { ...zz, polygon: zz.polygon.filter((_, pi) => pi !== hit.ptIdx) }
    }))
  }, [drawing, eventToNorm, vertexHit, zones])

  // ── Keyboard: backspace to undo, escape to cancel drawing ────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      if (target && ['INPUT', 'TEXTAREA'].includes(target.tagName)) return
      if (!drawing) return
      if (e.key === 'Backspace' || e.key === 'Delete') {
        e.preventDefault()
        setCurrentPoly((prev) => prev.slice(0, -1))
      } else if (e.key === 'Escape') {
        e.preventDefault()
        setCurrentPoly([])
        setDrawing(false)
      } else if (e.key === 'Enter') {
        e.preventDefault()
        finishPoly()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawing, finishPoly])

  // ── Zone actions ──────────────────────────────────────────────────────────
  const removeZone = (idx: number) => {
    setZones((prev) => prev.filter((_, i) => i !== idx))
    setActiveZoneIdx((cur) => Math.min(cur, Math.max(0, zones.length - 2)))
  }

  const toggleZone = (idx: number) => {
    setZones((prev) =>
      prev.map((z, i) => (i === idx ? { ...z, enabled: !z.enabled } : z)),
    )
  }

  const renameZone = (idx: number, name: string) => {
    setZones((prev) => prev.map((z, i) => i === idx ? { ...z, name } : z))
  }

  const handleSave = () => {
    const updated: DetectConfig = {
      ...config,
      zones,
      arm_schedule: armSchedule,
    }
    // onSave triggers the parent to unmount this modal, so do NOT setState after.
    onSave(JSON.stringify(updated))
  }

  const startDrawing = () => {
    setDrawing(true)
    setCurrentPoly([])
    setStatus('')
  }

  const cancelDrawing = () => {
    setCurrentPoly([])
    setDrawing(false)
  }

  const undoLast = () => {
    setCurrentPoly((prev) => prev.slice(0, -1))
  }

  const useFullFrame = () => {
    const fullZone: Zone = {
      name: 'Full Frame',
      polygon: [[0.01, 0.01], [0.99, 0.01], [0.99, 0.99], [0.01, 0.99]],
      enabled: true,
    }
    setZones((prev) => [...prev, fullZone])
    setActiveZoneIdx(zones.length)
    setStatus('Full-frame zone added. Click Save to persist.')
  }

  // ── Dynamic instruction banner text ──────────────────────────────────────
  const instruction = useMemo(() => {
    if (drawing) {
      if (currentPoly.length === 0) return 'Click on the image to place the first vertex.'
      if (currentPoly.length < 3) return `Placed ${currentPoly.length} of 3 minimum. Keep clicking to add vertices.`
      return 'Click more points, or click the first vertex (green halo) to close. Backspace = undo · Esc = cancel.'
    }
    if (zones.length === 0) {
      return 'No zones yet. Click "+ Draw Zone" to outline a detection area, or "Use Full Frame" for whole-image motion.'
    }
    return 'Click a zone to make it active. Drag its dots to reshape. Right-click a dot to remove it.'
  }, [drawing, currentPoly.length, zones.length])

  const cursorStyle = drawing ? 'crosshair' : (dragging ? 'grabbing' : 'default')

  // Token-coloured shortcut for inline kbd-style chips
  const kbdStyle: React.CSSProperties = {
    fontFamily: 'var(--font-mono, monospace)',
    fontSize: 11,
    padding: '1px 5px',
    background: '#0f1117',
    border: '1px solid #2d3148',
    borderRadius: 4,
    color: '#cbd5e1',
  }

  return (
    <div
      style={{
        position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
        background: 'rgba(0,0,0,0.78)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={containerRef}
        style={{
          background: '#1a1d27', border: '1px solid #2d3148',
          borderRadius: '12px', padding: '20px',
          maxWidth: '1120px', width: 'calc(100% - 40px)',
          maxHeight: '92vh', overflowY: 'auto',
          boxShadow: '0 16px 48px rgba(0,0,0,0.7)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
          <div>
            <h3 style={{ color: '#e2e8f0', margin: 0, fontSize: '16px', fontWeight: 600 }}>
              Detection Zones — Camera {cameraId}
            </h3>
            <p style={{ color: '#94a3b8', fontSize: '12px', margin: '4px 0 0 0' }}>
              Motion is only detected inside enabled zones. Use one big zone for the whole scene, or several to focus on specific areas.
            </p>
          </div>
          <button style={{ ...BTN, background: 'transparent', color: '#94a3b8' }} onClick={onClose}>
            Close
          </button>
        </div>

        {/* Instruction banner */}
        <div
          style={{
            background: drawing ? 'rgba(245,158,11,0.10)' : 'rgba(59,130,246,0.08)',
            border: `1px solid ${drawing ? 'rgba(245,158,11,0.35)' : 'rgba(59,130,246,0.25)'}`,
            borderRadius: 8,
            padding: '8px 12px',
            color: '#e2e8f0',
            fontSize: 13,
            marginBottom: 10,
            display: 'flex',
            alignItems: 'center',
            gap: 10,
          }}
        >
          <span style={{ flex: 1 }}>{instruction}</span>
          {mousePos && (
            <span style={{ color: '#94a3b8', fontFamily: 'var(--font-mono, monospace)', fontSize: 11 }}>
              {(mousePos[0] * 100).toFixed(1)}%, {(mousePos[1] * 100).toFixed(1)}%
            </span>
          )}
        </div>

        {/* Canvas */}
        <div style={{ position: 'relative', display: 'flex', justifyContent: 'center' }}>
          <canvas
            ref={canvasRef}
            width={canvasSize.w}
            height={canvasSize.h}
            style={{
              display: 'block',
              cursor: cursorStyle,
              border: '1px solid #2d3148',
              borderRadius: '6px',
              background: '#0f1117',
              maxWidth: '100%',
              touchAction: 'none',
              userSelect: 'none',
            }}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseLeave}
            onContextMenu={handleContextMenu}
          />
          {/* Live / paused badge */}
          <div
            style={{
              position: 'absolute', top: 8, right: 8,
              padding: '3px 8px', borderRadius: 12,
              background: 'rgba(0,0,0,0.65)',
              color: autoRefresh ? '#22c55e' : '#94a3b8',
              fontSize: 11, fontWeight: 600,
              display: 'flex', alignItems: 'center', gap: 5,
              pointerEvents: 'none',
            }}
          >
            <span style={{
              width: 7, height: 7, borderRadius: '50%',
              background: autoRefresh ? '#22c55e' : '#64748b',
              boxShadow: autoRefresh ? '0 0 6px rgba(34,197,94,0.7)' : 'none',
            }} />
            {autoRefresh ? 'LIVE' : 'PAUSED'}
          </div>
        </div>

        {/* Primary controls */}
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '12px', alignItems: 'center' }}>
          {!drawing ? (
            <>
              <button
                style={{ ...BTN, background: '#f59e0b', color: '#000' }}
                onClick={startDrawing}
              >
                + Draw Zone
              </button>
              <button
                style={{ ...BTN, background: '#2d3148', color: '#cbd5e1' }}
                onClick={useFullFrame}
              >
                Use Full Frame
              </button>
            </>
          ) : (
            <>
              <button
                style={{
                  ...BTN,
                  background: currentPoly.length >= 3 ? '#22c55e' : '#1f2a3e',
                  color: currentPoly.length >= 3 ? '#fff' : '#64748b',
                  cursor: currentPoly.length >= 3 ? 'pointer' : 'not-allowed',
                }}
                onClick={finishPoly}
                disabled={currentPoly.length < 3}
              >
                Finish Zone ({currentPoly.length} pt{currentPoly.length === 1 ? '' : 's'})
              </button>
              <button
                style={{ ...BTN, background: '#2d3148', color: '#cbd5e1' }}
                onClick={undoLast}
                disabled={currentPoly.length === 0}
              >
                Undo last
              </button>
              <button style={{ ...BTN, background: 'transparent', color: '#94a3b8' }} onClick={cancelDrawing}>
                Cancel
              </button>
            </>
          )}

          <div style={{ flex: 1 }} />

          {/* Snapshot controls */}
          <label style={{
            display: 'flex', alignItems: 'center', gap: 6,
            color: '#94a3b8', fontSize: 12, cursor: 'pointer', userSelect: 'none',
          }}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            Auto-refresh preview
          </label>
          <button
            style={{ ...BTN, background: '#2d3148', color: '#cbd5e1', padding: '5px 10px', fontSize: 12 }}
            onClick={() => setRefreshTick((n) => n + 1)}
            title="Fetch a fresh snapshot from the camera"
          >
            ↻ Refresh
          </button>

          <button style={{ ...BTN, background: '#3b82f6', color: '#fff' }} onClick={handleSave}>
            Save
          </button>
        </div>

        {/* Keyboard hints (only shown while drawing) */}
        {drawing && (
          <div style={{ marginTop: 8, display: 'flex', gap: 12, color: '#94a3b8', fontSize: 11, flexWrap: 'wrap' }}>
            <span><span style={kbdStyle}>Click</span> add vertex</span>
            <span><span style={kbdStyle}>Backspace</span> undo</span>
            <span><span style={kbdStyle}>Enter</span> finish</span>
            <span><span style={kbdStyle}>Esc</span> cancel</span>
            <span><span style={kbdStyle}>Click 1st vertex</span> close polygon</span>
          </div>
        )}

        {status && (
          <p style={{ color: '#94a3b8', fontSize: '12px', marginTop: '8px' }}>{status}</p>
        )}

        {/* Zone list */}
        {zones.length > 0 && (
          <div style={{ marginTop: '16px' }}>
            <div style={{ color: '#64748b', fontSize: '11px', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Zones ({zones.length})
            </div>
            {zones.map((z, i) => (
              <div
                key={i}
                style={{
                  display: 'flex', gap: '8px', alignItems: 'center',
                  padding: '6px 8px', borderRadius: '6px', marginBottom: '4px',
                  background: i === activeZoneIdx ? '#243049' : 'transparent',
                  border: `1px solid ${i === activeZoneIdx ? '#3b82f6' : 'transparent'}`,
                  cursor: 'pointer',
                }}
                onClick={() => setActiveZoneIdx(i)}
              >
                <span
                  style={{
                    width: 10, height: 10, borderRadius: 2,
                    background: z.enabled ? '#3b82f6' : '#475569',
                    flexShrink: 0,
                  }}
                />
                <input
                  value={z.name}
                  onChange={(e) => renameZone(i, e.target.value)}
                  onClick={(e) => e.stopPropagation()}
                  style={{
                    flex: 1, fontSize: 13,
                    background: 'transparent', border: 'none', color: '#e2e8f0',
                    outline: 'none', padding: 0,
                  }}
                />
                <span style={{ color: '#64748b', fontSize: '11px' }}>{z.polygon.length} pts</span>
                <button
                  style={{ ...BTN, padding: '2px 8px', background: 'transparent', color: z.enabled ? '#22c55e' : '#64748b' }}
                  onClick={(e) => { e.stopPropagation(); toggleZone(i) }}
                >
                  {z.enabled ? 'Enabled' : 'Disabled'}
                </button>
                <button
                  style={{ ...BTN, padding: '2px 8px', background: 'transparent', color: '#ef4444' }}
                  onClick={(e) => { e.stopPropagation(); removeZone(i) }}
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Arm schedule */}
        <details style={{ marginTop: '14px' }}>
          <summary style={{ color: '#64748b', fontSize: '12px', cursor: 'pointer', userSelect: 'none' }}>
            Arm schedule ({armSchedule.length === 0 ? 'always armed' : `${armSchedule.length} window${armSchedule.length !== 1 ? 's' : ''}`})
          </summary>
          <div style={{ marginTop: '10px' }}>
            <p style={{ fontSize: '11px', color: '#64748b', marginBottom: '8px' }}>
              Detection only runs during these windows. Leave empty to always arm.
            </p>
            {armSchedule.map((w, i) => (
              <div key={i} style={{ display: 'flex', gap: '6px', alignItems: 'center', marginBottom: '6px', flexWrap: 'wrap' }}>
                <div style={{ display: 'flex', gap: '2px' }}>
                  {['M','T','W','T','F','S','S'].map((d, di) => (
                    <button
                      key={di}
                      type="button"
                      style={{
                        ...BTN, padding: '3px 7px', fontSize: '11px',
                        background: w.days.includes(di) ? '#3b82f6' : '#2d3148',
                        color: w.days.includes(di) ? '#fff' : '#94a3b8',
                      }}
                      onClick={() => {
                        const days = w.days.includes(di) ? w.days.filter(x => x !== di) : [...w.days, di].sort((a,b)=>a-b)
                        setArmSchedule(prev => prev.map((x, xi) => xi === i ? { ...x, days } : x))
                      }}
                    >{d}</button>
                  ))}
                </div>
                <input
                  type="time"
                  value={w.start}
                  style={{ padding: '3px 6px', borderRadius: '5px', border: '1px solid #2d3148', background: '#0f1117', color: '#e2e8f0', fontSize: '12px' }}
                  onChange={e => setArmSchedule(prev => prev.map((x, xi) => xi === i ? { ...x, start: e.target.value } : x))}
                />
                <span style={{ color: '#64748b', fontSize: '11px' }}>–</span>
                <input
                  type="time"
                  value={w.end}
                  style={{ padding: '3px 6px', borderRadius: '5px', border: '1px solid #2d3148', background: '#0f1117', color: '#e2e8f0', fontSize: '12px' }}
                  onChange={e => setArmSchedule(prev => prev.map((x, xi) => xi === i ? { ...x, end: e.target.value } : x))}
                />
                <button
                  type="button"
                  style={{ ...BTN, padding: '2px 8px', background: 'transparent', color: '#ef4444', fontSize: '12px' }}
                  onClick={() => setArmSchedule(prev => prev.filter((_, xi) => xi !== i))}
                >×</button>
              </div>
            ))}
            <button
              type="button"
              style={{ ...BTN, background: '#2d3148', color: '#94a3b8', fontSize: '12px', marginTop: '4px' }}
              onClick={() => setArmSchedule(prev => [...prev, { days: [0,1,2,3,4], start: '08:00', end: '22:00' }])}
            >+ Add window</button>
          </div>
        </details>

        {/* Advanced config */}
        <details style={{ marginTop: '14px' }}>
          <summary style={{ color: '#64748b', fontSize: '12px', cursor: 'pointer' }}>
            Advanced settings
          </summary>
          <div style={{ marginTop: '10px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
            {[
              { key: 'min_changed_pct', label: 'Min changed % (0.1–10)', def: 0.5 },
              { key: 'motion_confirm_frames', label: 'Confirm frames (1–10)', def: 2 },
              { key: 'post_motion_seconds', label: 'Post-motion seconds (1–60)', def: 10 },
              { key: 'pixel_threshold', label: 'Pixel threshold (5–100)', def: 25 },
            ].map(({ key, label, def }) => (
              <div key={key}>
                <label style={{ color: '#94a3b8', fontSize: '11px', display: 'block', marginBottom: '3px' }}>
                  {label}
                </label>
                <input
                  type="number"
                  style={{
                    width: '100%', padding: '5px 8px', borderRadius: '5px',
                    border: '1px solid #2d3148', background: '#0f1117', color: '#e2e8f0', fontSize: '13px',
                  }}
                  value={(config as unknown as Record<string, unknown>)[key] as number ?? def}
                  onChange={(e) => setConfig((prev) => ({ ...prev, [key]: parseFloat(e.target.value) }))}
                />
              </div>
            ))}
          </div>
        </details>
      </div>
    </div>
  )
}
