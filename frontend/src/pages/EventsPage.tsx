/**
 * EventsPage — split-panel event browser with auto-filtering and live WebSocket updates.
 *
 *   +----------------------------------------+--------------------+
 *   | filters / event list                   | detail panel       |
 *   +----------------------------------------+--------------------+
 *
 * Features:
 *   - Two-column at >= 900px, single-column below
 *   - Compact horizontal event rows (80x52 thumbs), 100 per page
 *   - Auto-refresh on filter change (300ms debounce, no Search button)
 *   - Type filter chips: All / Motion / Face
 *   - Camera + datetime range + unreviewed-only filters
 *   - Live WebSocket: motion_start prepends with NEW flash; motion_end updates ended_at
 *   - Selected event populates sticky detail panel with large thumb + actions
 *   - "View in Playback" deep-links to /playback?camera_id=X&date=YYYY-MM-DD
 */

import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { eventsApi, camerasApi, Camera, ArgusEvent, getToken } from '../api/client'
import { PageHeader } from '../components/ui/Primitives'
import { useToast } from '../components/ui/Toast'

// ── Utilities ────────────────────────────────────────────────────────────────

function withToken(url: string): string {
  const token = getToken()
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}

function fmtClockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function fmtDateTime(iso: string): string {
  return new Date(iso).toLocaleString()
}

function fmtDuration(sec: number): string {
  if (sec < 1) return `${Math.round(sec * 1000)}ms`
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
}

function durationSec(e: ArgusEvent): number | null {
  if (!e.ended_at) return null
  return (new Date(e.ended_at).getTime() - new Date(e.started_at).getTime()) / 1000
}

// ── Live WebSocket hook ──────────────────────────────────────────────────────

function useLiveEvents(onEvent: (e: Record<string, unknown>) => void) {
  const wsRef = useRef<WebSocket | null>(null)
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent

  useEffect(() => {
    let ws: WebSocket | null = null
    let retryTimeout: ReturnType<typeof setTimeout> | null = null
    let retryDelay = 1000
    let destroyed = false

    const connect = () => {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
      const token = getToken()
      const tokenQs = token ? `?token=${encodeURIComponent(token)}` : ''
      ws = new WebSocket(`${protocol}://${location.host}/api/events/ws${tokenQs}`)
      wsRef.current = ws
      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data)
          if (data.type !== 'ping') onEventRef.current(data)
        } catch { /* ignore */ }
      }
      ws.onclose = () => {
        if (destroyed) return
        retryTimeout = setTimeout(() => {
          retryDelay = Math.min(retryDelay * 2, 30000)
          connect()
        }, retryDelay)
      }
      ws.onerror = () => {
        ws?.close()
      }
    }

    connect()

    return () => {
      destroyed = true
      if (retryTimeout) clearTimeout(retryTimeout)
      ws?.close()
    }
  }, [])
}

// ── Type badge ───────────────────────────────────────────────────────────────

function typeBadgeStyle(type: string, label?: string | null): React.CSSProperties {
  // Face events are object/face — show as "face" colored
  const t = (type === 'object' && label === 'face') ? 'face' : type.toLowerCase()
  const colors: Record<string, { bg: string; color: string; border: string }> = {
    motion:  { bg: 'rgba(245,158,11,0.12)', color: '#fcd34d', border: 'rgba(245,158,11,0.25)' },
    object:  { bg: 'rgba(59,130,246,0.12)', color: '#93c5fd', border: 'rgba(59,130,246,0.25)' },
    face:    { bg: 'rgba(16,185,129,0.12)', color: '#6ee7b7', border: 'rgba(16,185,129,0.25)' },
    person:  { bg: 'rgba(16,185,129,0.12)', color: '#6ee7b7', border: 'rgba(16,185,129,0.25)' },
    vehicle: { bg: 'rgba(139,92,246,0.12)', color: '#c4b5fd', border: 'rgba(139,92,246,0.25)' },
  }
  const c = colors[t] ?? { bg: 'var(--bg-elevated)', color: 'var(--text-muted)', border: 'var(--border)' }
  return {
    display: 'inline-flex', alignItems: 'center',
    padding: '1px 7px', borderRadius: 'var(--radius-full)',
    fontSize: 9, fontWeight: 700, letterSpacing: '0.06em',
    textTransform: 'uppercase',
    background: c.bg, color: c.color, border: `1px solid ${c.border}`,
  }
}

function typeLabel(type: string, label?: string | null): string {
  if (type === 'object' && label === 'face') return 'face'
  return type
}

// ── Constants ────────────────────────────────────────────────────────────────

const LIMIT = 100
const NARROW_BREAKPOINT = 900
type TypeFilter = 'all' | 'motion' | 'face'

// ── EventsPage ───────────────────────────────────────────────────────────────

export function EventsPage() {
  const toast = useToast()
  const navigate = useNavigate()

  const [cameras, setCameras] = useState<Camera[]>([])
  const [events, setEvents] = useState<ArgusEvent[]>([])
  const [newEventIds, setNewEventIds] = useState<Set<number>>(new Set())
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)

  // Filters
  const [filterCamId, setFilterCamId] = useState('')
  const [filterType, setFilterType] = useState<TypeFilter>('all')
  const [filterAfter, setFilterAfter] = useState('')
  const [filterBefore, setFilterBefore] = useState('')
  const [unreviewedOnly, setUnreviewedOnly] = useState(false)

  const [isNarrow, setIsNarrow] = useState(
    typeof window !== 'undefined' ? window.innerWidth < NARROW_BREAKPOINT : false
  )

  // Cameras
  useEffect(() => {
    camerasApi.list().then(setCameras).catch(() => {})
  }, [])

  // Resize listener
  useEffect(() => {
    function onResize() { setIsNarrow(window.innerWidth < NARROW_BREAKPOINT) }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const camNameById = useMemo(
    () => Object.fromEntries(cameras.map(c => [c.id, c.name])) as Record<number, string>,
    [cameras]
  )

  // ── Loader ──────────────────────────────────────────────────────────────

  const loadEvents = useCallback(async (newOffset: number) => {
    setLoading(true)
    setError(null)
    try {
      const params: Parameters<typeof eventsApi.list>[0] = { limit: LIMIT, offset: newOffset }
      if (filterCamId) params.camera_id = parseInt(filterCamId)
      if (filterAfter) params.started_after = filterAfter
      if (filterBefore) params.started_before = filterBefore
      if (filterType === 'motion') params.type = 'motion'
      else if (filterType === 'face') { params.type = 'object'; params.label = 'face' }
      const result = await eventsApi.list(params)
      setEvents(result.events)
      setTotal(result.total)
      setOffset(newOffset)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [filterCamId, filterAfter, filterBefore, filterType])

  // Auto-refresh on filter change with 300ms debounce (resets to page 0)
  useEffect(() => {
    const handle = window.setTimeout(() => { loadEvents(0) }, 300)
    return () => window.clearTimeout(handle)
  }, [loadEvents])

  // ── Live updates ────────────────────────────────────────────────────────

  useLiveEvents((wsEvent) => {
    if (wsEvent.type === 'motion_start') {
      const e: ArgusEvent = {
        id: wsEvent.event_id as number,
        camera_id: wsEvent.camera_id as number,
        type: 'motion', label: null, score: null,
        started_at: wsEvent.started_at as string,
        ended_at: null,
        thumb_path: wsEvent.thumb_path as string | null,
        clip_path: null,
        recording_id: wsEvent.recording_id as number | null,
        meta_json: '{}',
        reviewed_at: null,
        person_id: null,
      }
      // Only prepend if we're on page 0 (avoid mixing pages)
      if (offset === 0) {
        setEvents(prev => {
          // dedupe by id
          if (prev.some(x => x.id === e.id)) return prev
          return [e, ...prev.slice(0, LIMIT - 1)]
        })
      }
      setNewEventIds(prev => { const s = new Set(prev); s.add(e.id); return s })
      setTotal(t => t + 1)
      setTimeout(() => {
        setNewEventIds(prev => { const s = new Set(prev); s.delete(e.id); return s })
      }, 5000)
    }
    if (wsEvent.type === 'motion_end') {
      const eventId = wsEvent.event_id as number
      setEvents(prev => prev.map(e => e.id === eventId ? { ...e, ended_at: wsEvent.ended_at as string } : e))
    }
  })

  // ── Actions ─────────────────────────────────────────────────────────────

  const hasFilters = !!(filterCamId || filterAfter || filterBefore || filterType !== 'all' || unreviewedOnly)

  function clearFilters() {
    setFilterCamId('')
    setFilterType('all')
    setFilterAfter('')
    setFilterBefore('')
    setUnreviewedOnly(false)
  }

  async function handleReview(id: number, reviewed: boolean) {
    try {
      const res = await eventsApi.reviewEvent(id, reviewed)
      setEvents(prev => prev.map(e => e.id === id ? { ...e, reviewed_at: res.reviewed_at } : e))
      toast.success(reviewed ? 'Event marked reviewed' : 'Reviewed mark removed')
    } catch (e) {
      toast.error(`Failed to update review: ${e}`)
    }
  }

  function viewInPlayback(e: ArgusEvent) {
    navigate(`/playback?camera_id=${e.camera_id}&t=${encodeURIComponent(e.started_at)}`)
  }

  // ── Derived data ────────────────────────────────────────────────────────

  const displayedEvents = useMemo(
    () => unreviewedOnly ? events.filter(e => !e.reviewed_at) : events,
    [events, unreviewedOnly]
  )

  const selectedEvent = useMemo(
    () => displayedEvents.find(e => e.id === selectedId) ?? null,
    [displayedEvents, selectedId]
  )

  // Clear selection if it falls off the visible page
  useEffect(() => {
    if (selectedId != null && !displayedEvents.some(e => e.id === selectedId)) {
      setSelectedId(null)
    }
  }, [displayedEvents, selectedId])

  const totalPages = Math.max(1, Math.ceil(total / LIMIT))
  const currentPage = Math.floor(offset / LIMIT) + 1

  // For narrow layout, optionally show detail panel above list when selected
  const showDetailAbove = isNarrow && selectedEvent != null

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div style={{ maxWidth: 1600 }}>
      <PageHeader title="Events">
        <div className="live-indicator">
          <span className="live-dot" />
          <span>Live</span>
        </div>
        <span style={{ fontSize: 13, color: 'var(--text-muted)', marginLeft: 'auto', fontFamily: 'var(--font-mono)' }}>
          {total.toLocaleString()} event{total !== 1 ? 's' : ''}
        </span>
      </PageHeader>

      {error && (
        <div className="alert alert-error" style={{ marginBottom: 12 }} role="alert">
          <span style={{ fontWeight: 700 }}>✕</span>
          {error}
        </div>
      )}

      {/* ── Two-column layout ───────────────────────────────────────────── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: isNarrow ? '1fr' : 'minmax(0, 1fr) 320px',
        gap: 12,
        alignItems: 'start',
      }}>
        {/* ── Main column: filters + list ─────────────────────────────── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>

          {/* Detail panel above list on narrow layouts when an event is selected */}
          {showDetailAbove && selectedEvent && (
            <DetailPanel
              event={selectedEvent}
              cameraName={camNameById[selectedEvent.camera_id] ?? `Camera ${selectedEvent.camera_id}`}
              isNew={newEventIds.has(selectedEvent.id)}
              onReview={handleReview}
              onViewInPlayback={viewInPlayback}
              onClose={() => setSelectedId(null)}
              sticky={false}
            />
          )}

          {/* Filter bar */}
          <div style={{
            display: 'flex', flexDirection: 'column', gap: 8,
            padding: '10px 12px',
            background: 'var(--bg-surface)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius-md)',
          }}>
            {/* Row 1: camera + type chips + unreviewed */}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <select
                value={filterCamId}
                onChange={e => setFilterCamId(e.target.value)}
                style={{ minWidth: 140, minHeight: 32, fontSize: 13 }}
                aria-label="Filter by camera"
              >
                <option value="">All cameras</option>
                {cameras.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>

              <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                {(['all', 'motion', 'face'] as TypeFilter[]).map(t => (
                  <button
                    key={t}
                    onClick={() => setFilterType(t)}
                    style={{
                      padding: '4px 12px', borderRadius: 'var(--radius-full)',
                      border: filterType === t ? '1px solid var(--accent)' : '1px solid var(--border)',
                      background: filterType === t ? 'var(--accent-dim)' : 'transparent',
                      color: filterType === t ? 'var(--text-accent)' : 'var(--text-muted)',
                      cursor: 'pointer', fontSize: 11, fontWeight: 600,
                      textTransform: 'capitalize',
                      transition: 'all var(--transition)',
                    }}
                  >
                    {t}
                  </button>
                ))}
              </div>

              <label style={{
                display: 'flex', alignItems: 'center', gap: 6,
                fontSize: 12, color: 'var(--text-secondary)', cursor: 'pointer',
                marginLeft: 4,
              }}>
                <input
                  type="checkbox"
                  checked={unreviewedOnly}
                  onChange={e => setUnreviewedOnly(e.target.checked)}
                />
                Unreviewed only
              </label>

              <div style={{ flex: 1 }} />

              {loading && (
                <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-muted)', fontSize: 12 }}>
                  <span className="spinner spinner-sm" />
                  Loading…
                </span>
              )}
            </div>

            {/* Row 2: date range + clear */}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text-muted)' }}>
                From
                <input
                  type="datetime-local"
                  value={filterAfter ? localFromIso(filterAfter) : ''}
                  onChange={e => setFilterAfter(e.target.value ? new Date(e.target.value).toISOString() : '')}
                  style={{ minHeight: 30, fontSize: 12 }}
                />
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text-muted)' }}>
                To
                <input
                  type="datetime-local"
                  value={filterBefore ? localFromIso(filterBefore) : ''}
                  onChange={e => setFilterBefore(e.target.value ? new Date(e.target.value).toISOString() : '')}
                  style={{ minHeight: 30, fontSize: 12 }}
                />
              </label>

              {hasFilters && (
                <button className="btn btn-ghost btn-sm" onClick={clearFilters}>
                  Clear filters
                </button>
              )}

              <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 'auto', fontStyle: 'italic' }}>
                Auto-refreshes
              </span>
            </div>
          </div>

          {/* Event list */}
          <div style={{
            background: 'var(--bg-surface)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius-md)',
            overflow: 'hidden',
          }}>
            {loading && events.length === 0 ? (
              <div style={{
                padding: '60px 16px', textAlign: 'center',
                color: 'var(--text-muted)', fontSize: 13,
                display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 10,
              }}>
                <span className="spinner spinner-sm" />
                Loading events…
              </div>
            ) : displayedEvents.length === 0 ? (
              <div style={{
                padding: '60px 16px', textAlign: 'center',
                color: 'var(--text-muted)', fontSize: 13,
              }}>
                <div style={{ fontSize: 22, marginBottom: 8, opacity: 0.6 }}>No events found</div>
                <div style={{ fontSize: 12, marginBottom: 12 }}>
                  Motion events will appear here automatically when cameras detect activity.
                </div>
                {hasFilters && (
                  <button className="btn btn-secondary btn-sm" onClick={clearFilters}>
                    Clear filters
                  </button>
                )}
              </div>
            ) : (
              displayedEvents.map((e, idx) => (
                <EventRow
                  key={e.id}
                  event={e}
                  cameraName={camNameById[e.camera_id] ?? `Camera ${e.camera_id}`}
                  isNew={newEventIds.has(e.id)}
                  selected={e.id === selectedId}
                  isLast={idx === displayedEvents.length - 1}
                  onSelect={() => setSelectedId(e.id)}
                  onReview={handleReview}
                />
              ))
            )}
          </div>

          {/* Pagination */}
          {total > LIMIT && (
            <div style={{
              display: 'flex', gap: 8, justifyContent: 'center', alignItems: 'center',
              padding: '4px 0',
            }}>
              <button
                className="btn btn-secondary btn-sm"
                disabled={offset === 0 || loading}
                onClick={() => loadEvents(Math.max(0, offset - LIMIT))}
              >
                ← Prev
              </button>
              <span style={{ color: 'var(--text-secondary)', fontSize: 12, padding: '0 8px', fontFamily: 'var(--font-mono)' }}>
                page {currentPage} of {totalPages}
                {' '}
                <span style={{ color: 'var(--text-muted)' }}>
                  ({offset + 1}–{Math.min(offset + LIMIT, total)})
                </span>
              </span>
              <button
                className="btn btn-secondary btn-sm"
                disabled={offset + LIMIT >= total || loading}
                onClick={() => loadEvents(offset + LIMIT)}
              >
                Next →
              </button>
            </div>
          )}
        </div>

        {/* ── Detail panel (right column on wide; hidden on narrow when not selected) */}
        {!isNarrow && (
          selectedEvent ? (
            <DetailPanel
              event={selectedEvent}
              cameraName={camNameById[selectedEvent.camera_id] ?? `Camera ${selectedEvent.camera_id}`}
              isNew={newEventIds.has(selectedEvent.id)}
              onReview={handleReview}
              onViewInPlayback={viewInPlayback}
              onClose={() => setSelectedId(null)}
              sticky={true}
            />
          ) : (
            <div style={{
              background: 'var(--bg-surface)',
              border: '1px solid var(--border)',
              borderRadius: 'var(--radius-md)',
              padding: '40px 20px',
              color: 'var(--text-muted)',
              fontSize: 12,
              textAlign: 'center',
              position: 'sticky',
              top: 12,
              display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10,
            }}>
              <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.4">
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <path d="M3 8h18M8 3v18" />
              </svg>
              <span>Select an event to view details</span>
            </div>
          )
        )}
      </div>
    </div>
  )
}

// ── EventRow ─────────────────────────────────────────────────────────────────

interface EventRowProps {
  event: ArgusEvent
  cameraName: string
  isNew: boolean
  selected: boolean
  isLast: boolean
  onSelect: () => void
  onReview: (id: number, reviewed: boolean) => void
}

function EventRow({ event, cameraName, isNew, selected, isLast, onSelect, onReview }: EventRowProps) {
  const dur = durationSec(event)
  const isReviewed = !!event.reviewed_at
  const isLive = !event.ended_at

  return (
    <div
      onClick={onSelect}
      style={{
        display: 'flex', gap: 10, alignItems: 'flex-start',
        padding: '10px 12px',
        borderBottom: isLast ? 'none' : '1px solid var(--border-subtle)',
        cursor: 'pointer',
        background: selected ? 'var(--bg-elevated)' : 'transparent',
        borderLeft: selected ? '3px solid var(--accent)' : '3px solid transparent',
        opacity: isReviewed && !selected ? 0.65 : 1,
        transition: 'background var(--transition), opacity var(--transition)',
      }}
      onMouseEnter={e => {
        if (!selected) (e.currentTarget as HTMLDivElement).style.background = 'var(--bg-elevated)'
      }}
      onMouseLeave={e => {
        if (!selected) (e.currentTarget as HTMLDivElement).style.background = 'transparent'
      }}
    >
      {/* Thumbnail */}
      {event.thumb_path ? (
        <img
          src={withToken(eventsApi.thumbUrl(event.id))}
          alt=""
          style={{
            width: 80, height: 52, objectFit: 'cover',
            borderRadius: 'var(--radius-sm)',
            background: '#000',
            flexShrink: 0, border: '1px solid var(--border)',
          }}
          onError={e => { (e.target as HTMLImageElement).style.visibility = 'hidden' }}
        />
      ) : (
        <div style={{
          width: 80, height: 52, flexShrink: 0,
          background: 'var(--bg-base)',
          borderRadius: 'var(--radius-sm)',
          border: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--text-muted)', fontSize: 9,
        }}>
          no img
        </div>
      )}

      {/* Content */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3, flexWrap: 'wrap' }}>
          <span style={{
            fontWeight: 600, color: 'var(--text-primary)', fontSize: 13,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>
            {cameraName}
          </span>
          <span style={typeBadgeStyle(event.type, event.label)}>{typeLabel(event.type, event.label)}</span>
          {isNew && (
            <span style={{
              fontSize: 9, padding: '1px 6px', borderRadius: 999,
              background: 'rgba(59,130,246,0.15)', color: 'var(--text-accent)',
              border: '1px solid rgba(59,130,246,0.3)',
              fontWeight: 700, letterSpacing: '0.06em',
              animation: 'pulse-live 2s ease-in-out infinite',
            }}>NEW</span>
          )}
          {isLive && (
            <div className="live-indicator" style={{ fontSize: 10 }}>
              <span className="live-dot" />
              <span>LIVE</span>
            </div>
          )}
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginLeft: 'auto' }}>
            #{event.id}
          </span>
        </div>

        <div style={{
          display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap',
          fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)',
        }}>
          <span style={{ color: 'var(--text-secondary)' }}>{fmtClockTime(event.started_at)}</span>
          {dur != null && <span>{fmtDuration(dur)}</span>}
          {event.label && event.label !== 'face' && (
            <span style={{ color: 'var(--text-secondary)' }}>
              {event.label}
              {event.score != null && ` ${Math.round(event.score * 100)}%`}
            </span>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
          {isReviewed ? (
            <span style={{
              fontSize: 10, display: 'inline-flex', alignItems: 'center', gap: 4,
              color: 'var(--success)', fontWeight: 600,
            }}>
              <span style={{ fontWeight: 700 }}>✓</span> Reviewed
            </span>
          ) : (
            <button
              className="btn btn-ghost btn-sm"
              style={{ fontSize: 10, padding: '2px 8px', minHeight: 22 }}
              onClick={(ev) => { ev.stopPropagation(); onReview(event.id, true) }}
            >
              Mark reviewed
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── DetailPanel ──────────────────────────────────────────────────────────────

interface DetailPanelProps {
  event: ArgusEvent
  cameraName: string
  isNew: boolean
  sticky: boolean
  onReview: (id: number, reviewed: boolean) => void
  onViewInPlayback: (e: ArgusEvent) => void
  onClose: () => void
}

function DetailPanel({ event, cameraName, isNew, sticky, onReview, onViewInPlayback, onClose }: DetailPanelProps) {
  const dur = durationSec(event)
  const isReviewed = !!event.reviewed_at
  const isFace = event.type === 'object' && event.label === 'face'

  return (
    <div style={{
      background: 'var(--bg-surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-md)',
      overflow: 'hidden',
      position: sticky ? 'sticky' : 'static',
      top: sticky ? 12 : undefined,
      display: 'flex', flexDirection: 'column',
    }}>
      {/* Header */}
      <div style={{
        padding: '10px 12px',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
      }}>
        <span style={{
          fontSize: 11, fontWeight: 700, color: 'var(--text-muted)',
          letterSpacing: '0.07em', textTransform: 'uppercase',
        }}>
          Event Detail
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {isNew && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              fontSize: 9, padding: '2px 7px', borderRadius: 999,
              background: 'rgba(59,130,246,0.15)', color: 'var(--text-accent)',
              border: '1px solid rgba(59,130,246,0.3)',
              fontWeight: 700, letterSpacing: '0.06em',
              animation: 'pulse-live 2s ease-in-out infinite',
            }}>
              <span className="live-dot" /> Live
            </span>
          )}
          <button
            className="btn btn-ghost btn-sm"
            onClick={onClose}
            title="Close detail panel"
            style={{ padding: '2px 8px', minHeight: 22, fontSize: 14, lineHeight: 1 }}
            aria-label="Close"
          >
            ×
          </button>
        </div>
      </div>

      {/* Thumbnail */}
      <div style={{
        background: '#000',
        borderBottom: '1px solid var(--border)',
      }}>
        {event.thumb_path ? (
          <img
            src={withToken(eventsApi.thumbUrl(event.id))}
            alt={`Event ${event.id}`}
            style={{
              width: '100%', maxHeight: 200,
              objectFit: 'cover', display: 'block',
            }}
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
        ) : (
          <div style={{
            width: '100%', height: 160,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'var(--text-muted)', fontSize: 12,
          }}>
            No thumbnail
          </div>
        )}
      </div>

      {/* Metadata */}
      <div style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div>
          <div style={{
            fontWeight: 700, color: 'var(--text-primary)', fontSize: 15,
            marginBottom: 4,
          }}>
            {cameraName}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            <span style={typeBadgeStyle(event.type, event.label)}>{typeLabel(event.type, event.label)}</span>
            {event.label && event.label !== 'face' && (
              <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
                {event.label}
              </span>
            )}
            {event.score != null && (
              <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                {Math.round(event.score * 100)}%
              </span>
            )}
            {!event.ended_at && (
              <div className="live-indicator" style={{ fontSize: 10 }}>
                <span className="live-dot" />
                <span>LIVE</span>
              </div>
            )}
          </div>
        </div>

        <DetailRow label="Started" value={fmtDateTime(event.started_at)} mono />
        <DetailRow
          label="Duration"
          value={dur != null ? fmtDuration(dur) : <span style={{ color: 'var(--warning)' }}>recording…</span>}
          mono
        />
        <DetailRow label="Event ID" value={`#${event.id}`} mono />
        {event.recording_id != null && (
          <DetailRow label="Recording" value={`#${event.recording_id}`} mono />
        )}
        {isFace && (
          <DetailRow
            label="Person"
            value={event.person_id != null ? `#${event.person_id}` : <span style={{ color: 'var(--text-muted)', fontStyle: 'italic' }}>Unknown</span>}
            mono
          />
        )}
        <DetailRow
          label="Reviewed"
          value={
            isReviewed
              ? <span style={{ color: 'var(--success)' }}>✓ {fmtDateTime(event.reviewed_at!)}</span>
              : <span style={{ color: 'var(--text-muted)' }}>Not reviewed</span>
          }
        />

        {/* Actions */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 4 }}>
          {isReviewed ? (
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => onReview(event.id, false)}
              style={{ width: '100%' }}
            >
              Unmark reviewed
            </button>
          ) : (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => onReview(event.id, true)}
              style={{ width: '100%' }}
            >
              Mark reviewed
            </button>
          )}
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => onViewInPlayback(event)}
            style={{ width: '100%' }}
          >
            View in Playback
          </button>
          {event.clip_path && (
            <a
              href={withToken(eventsApi.clipUrl(event.id))}
              download={`event_${event.id}.mp4`}
              className="btn btn-secondary btn-sm"
              style={{ width: '100%', textAlign: 'center', textDecoration: 'none', display: 'block' }}
            >
              Download Clip
            </a>
          )}
        </div>
      </div>
    </div>
  )
}

function DetailRow({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
      <span style={{
        fontSize: 10, color: 'var(--text-muted)', fontWeight: 700,
        letterSpacing: '0.06em', textTransform: 'uppercase',
        flexShrink: 0,
      }}>
        {label}
      </span>
      <span style={{
        fontSize: 12, color: 'var(--text-secondary)',
        fontFamily: mono ? 'var(--font-mono)' : undefined,
        textAlign: 'right',
        overflow: 'hidden', textOverflow: 'ellipsis',
      }}>
        {value}
      </span>
    </div>
  )
}

// ── datetime-local helper ────────────────────────────────────────────────────

function localFromIso(iso: string): string {
  // Convert an ISO string to the value form expected by <input type="datetime-local">
  // (local YYYY-MM-DDTHH:mm with no timezone).
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
