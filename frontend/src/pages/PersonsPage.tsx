/**
 * PersonsPage — split-panel browser for face detections.
 *
 *   +----------------------------------------+--------------------+
 *   | filter bar                             |                    |
 *   +----------------------------------------+ detail panel       |
 *   | face card grid                         |                    |
 *   +----------------------------------------+--------------------+
 *
 * Features:
 *   - Two-column at >= 900px, single-column below (detail above grid when narrow)
 *   - Compact face cards (~170px), click to select, accent border on selected
 *   - Filter bar: camera dropdown, person-status chips (All / Named / Unnamed)
 *   - Auto-refresh on filter change (300ms debounce, no Search button)
 *   - "View in Playback" deep-links to /playback?camera_id=X&date=YYYY-MM-DD&t=ISO
 *   - NameDialog (unchanged) for naming / re-enrolling a face
 *   - Pagination at the bottom for > LIMIT results
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  eventsApi, camerasApi, personsApi,
  Camera, ArgusEvent, Person, getToken,
} from '../api/client'
import { EmptyState, LoadingState, PageHeader } from '../components/ui/Primitives'

// ── Utilities ────────────────────────────────────────────────────────────────

function withToken(url: string): string {
  const token = getToken()
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
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

function dateOnly(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// ── Constants ────────────────────────────────────────────────────────────────

const LIMIT = 60
const NARROW_BREAKPOINT = 900
type PersonFilter = 'all' | 'named' | 'unnamed'

// ── Name-this-person dialog (kept intact from the previous implementation) ──

interface NameDialogProps {
  event: ArgusEvent
  persons: Person[]
  onClose: () => void
  onSaved: (personName: string) => void
}

function NameDialog({ event, persons, onClose, onSaved }: NameDialogProps) {
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  async function handleSave() {
    const trimmed = name.trim()
    if (!trimmed) return
    setSaving(true)
    setError(null)
    try {
      await personsApi.assignPerson(event.id, trimmed)
      onSaved(trimmed)
    } catch (e) {
      const msg = String(e)
      if (msg.includes('no stored face embedding')) {
        setError('This face event has no recognition data. Only new face events (captured after enabling face recognition) can be named.')
      } else {
        setError(msg)
      }
      setSaving(false)
    }
  }

  const thumb = event.thumb_path
    ? withToken(eventsApi.thumbUrl(event.id))
    : null

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 1000,
        background: 'rgba(0,0,0,0.7)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: 'var(--bg-surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          padding: 24,
          width: 380,
          maxWidth: '90vw',
        }}
      >
        <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 16 }}>
          Name this person
        </div>

        {thumb && (
          <img
            src={thumb}
            alt="Face"
            style={{
              width: '100%', height: 160, objectFit: 'cover',
              borderRadius: 'var(--radius-md)', marginBottom: 16,
            }}
          />
        )}

        {/* Quick-select from existing persons */}
        {persons.length > 0 && (
          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>
              Existing persons
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {persons.map(p => (
                <button
                  key={p.id}
                  className="btn btn-secondary btn-sm"
                  onClick={() => setName(p.name)}
                  style={name === p.name ? { borderColor: 'var(--accent)', color: 'var(--accent)' } : {}}
                >
                  {p.name}
                </button>
              ))}
            </div>
          </div>
        )}

        <input
          ref={inputRef}
          type="text"
          className="input"
          placeholder="Name (e.g. Crissy)"
          value={name}
          onChange={e => setName(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') handleSave() }}
          style={{ width: '100%', marginBottom: 12 }}
        />

        {error && (
          <div style={{ color: 'var(--danger)', fontSize: 12, marginBottom: 10 }}>{error}</div>
        )}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
          <button
            className="btn btn-primary btn-sm"
            disabled={!name.trim() || saving}
            onClick={handleSave}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── PersonsPage ──────────────────────────────────────────────────────────────

export function PersonsPage() {
  const navigate = useNavigate()

  const [cameras, setCameras] = useState<Camera[]>([])
  const [persons, setPersons] = useState<Person[]>([])
  const [events, setEvents] = useState<ArgusEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)

  // Filters
  const [filterCamId, setFilterCamId] = useState('')
  const [filterPerson, setFilterPerson] = useState<PersonFilter>('all')

  // Selection / dialog
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [namingEvent, setNamingEvent] = useState<ArgusEvent | null>(null)

  // person_id → name (kept in sync with persons list)
  const [personNames, setPersonNames] = useState<Record<number, string>>({})
  // event_id → optimistic person name while the API response is in flight
  const [pendingNames, setPendingNames] = useState<Record<number, string>>({})

  // Responsive
  const [isNarrow, setIsNarrow] = useState(
    typeof window !== 'undefined' ? window.innerWidth < NARROW_BREAKPOINT : false
  )

  // ── Side effects ────────────────────────────────────────────────────────

  useEffect(() => {
    camerasApi.list().then(setCameras).catch(() => {})
    personsApi.list().then(setPersons).catch(() => {})
  }, [])

  useEffect(() => {
    function onResize() { setIsNarrow(window.innerWidth < NARROW_BREAKPOINT) }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Keep personNames map in sync with persons list
  useEffect(() => {
    const m: Record<number, string> = {}
    persons.forEach(p => { m[p.id] = p.name })
    setPersonNames(m)
  }, [persons])

  const camNameById = useMemo(
    () => Object.fromEntries(cameras.map(c => [c.id, c.name])) as Record<number, string>,
    [cameras]
  )

  // ── Loader ──────────────────────────────────────────────────────────────

  const loadFaces = useCallback(async (newOffset = 0) => {
    setLoading(true)
    setError(null)
    try {
      const params: Parameters<typeof eventsApi.list>[0] = {
        type: 'object',
        label: 'face',
        limit: LIMIT,
        offset: newOffset,
      }
      if (filterCamId) params.camera_id = parseInt(filterCamId)
      if (filterPerson === 'unnamed') params.person_id = -1
      const result = await eventsApi.list(params)
      setEvents(result.events)
      setTotal(result.total)
      setOffset(newOffset)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [filterCamId, filterPerson])

  // Auto-refresh on filter change with 300ms debounce (resets to page 0)
  useEffect(() => {
    const handle = window.setTimeout(() => { loadFaces(0) }, 300)
    return () => window.clearTimeout(handle)
  }, [loadFaces])

  // ── Helpers ─────────────────────────────────────────────────────────────

  function personNameFor(e: ArgusEvent): string | null {
    if (pendingNames[e.id]) return pendingNames[e.id]
    if (e.person_id != null) return personNames[e.person_id] ?? null
    return null
  }

  function handleNamed(event: ArgusEvent, personName: string) {
    setNamingEvent(null)
    // Optimistically show the name on the card without mutating person_id.
    setPendingNames(prev => ({ ...prev, [event.id]: personName }))
    // Reload persons list so filter dropdown and personNames map update.
    personsApi.list().then(ps => {
      setPersons(ps)
      // Once we have the real person_id, update the event row and clear pending.
      const pid = ps.find(p => p.name === personName)?.id
      if (pid != null) {
        setEvents(prev => prev.map(e => e.id === event.id ? { ...e, person_id: pid } : e))
        setPendingNames(prev => { const n = { ...prev }; delete n[event.id]; return n })
      }
    }).catch(() => {
      // On failure, clear the optimistic name so we don't show stale data.
      setPendingNames(prev => { const n = { ...prev }; delete n[event.id]; return n })
    })
  }

  function viewInPlayback(e: ArgusEvent) {
    const d = dateOnly(e.started_at)
    navigate(`/playback?camera_id=${e.camera_id}&date=${d}&t=${encodeURIComponent(e.started_at)}`)
  }

  // ── Derived ─────────────────────────────────────────────────────────────

  // Client-side filter for "named" — the API doesn't support a positive
  // "person_id IS NOT NULL" filter, so we filter after fetch. (Unnamed is
  // handled server-side via person_id=-1.)
  const displayedEvents = useMemo(() => {
    if (filterPerson === 'named') return events.filter(e => e.person_id != null || pendingNames[e.id])
    return events
  }, [events, filterPerson, pendingNames])

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

  const hasFilters = !!(filterCamId || filterPerson !== 'all')
  function clearFilters() {
    setFilterCamId('')
    setFilterPerson('all')
  }

  const totalPages = Math.max(1, Math.ceil(total / LIMIT))
  const currentPage = Math.floor(offset / LIMIT) + 1
  const showDetailAbove = isNarrow && selectedEvent != null

  // ── Render ──────────────────────────────────────────────────────────────

  return (
    <div style={{ maxWidth: 1600 }}>
      <PageHeader title="Persons">
        <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
          Captured face detections · name them for smart alerts
        </span>
        <span style={{ fontSize: 13, color: 'var(--text-muted)', marginLeft: 'auto', fontFamily: 'var(--font-mono)' }}>
          {total.toLocaleString()} detection{total !== 1 ? 's' : ''}
        </span>
      </PageHeader>

      {error && (
        <div className="alert alert-error" style={{ marginBottom: 12 }} role="alert">
          <span style={{ fontWeight: 700 }}>✕</span> {error}
        </div>
      )}

      {/* ── Two-column layout ────────────────────────────────────────── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: isNarrow ? '1fr' : 'minmax(0, 1fr) 320px',
        gap: 12,
        alignItems: 'start',
      }}>
        {/* ── Main column ──────────────────────────────────────────── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>

          {/* Detail panel above grid on narrow layouts when an event is selected */}
          {showDetailAbove && selectedEvent && (
            <DetailPanel
              event={selectedEvent}
              cameraName={camNameById[selectedEvent.camera_id] ?? `Camera ${selectedEvent.camera_id}`}
              personName={personNameFor(selectedEvent)}
              onName={() => setNamingEvent(selectedEvent)}
              onViewInPlayback={viewInPlayback}
              onClose={() => setSelectedId(null)}
              sticky={false}
            />
          )}

          {/* Filter bar */}
          <div style={{
            display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap',
            padding: '10px 12px',
            background: 'var(--bg-surface)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius-md)',
          }}>
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
              {(['all', 'named', 'unnamed'] as PersonFilter[]).map(t => (
                <button
                  key={t}
                  onClick={() => setFilterPerson(t)}
                  style={{
                    padding: '4px 12px', borderRadius: 'var(--radius-full)',
                    border: filterPerson === t ? '1px solid var(--accent)' : '1px solid var(--border)',
                    background: filterPerson === t ? 'var(--accent-dim)' : 'transparent',
                    color: filterPerson === t ? 'var(--text-accent)' : 'var(--text-muted)',
                    cursor: 'pointer', fontSize: 11, fontWeight: 600,
                    textTransform: 'capitalize',
                    transition: 'all var(--transition)',
                  }}
                >
                  {t}
                </button>
              ))}
            </div>

            {hasFilters && (
              <button className="btn btn-ghost btn-sm" onClick={clearFilters}>
                Clear
              </button>
            )}

            <div style={{ flex: 1 }} />

            {loading && (
              <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-muted)', fontSize: 12 }}>
                <span className="spinner spinner-sm" />
                Loading…
              </span>
            )}

            <span style={{
              fontSize: 11, padding: '3px 9px', borderRadius: 'var(--radius-full)',
              background: 'var(--bg-elevated)', color: 'var(--text-secondary)',
              border: '1px solid var(--border)', fontWeight: 600,
              fontFamily: 'var(--font-mono)',
            }}>
              {displayedEvents.length} detection{displayedEvents.length !== 1 ? 's' : ''}
            </span>

            {persons.length > 0 && (
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                {persons.length} named
              </span>
            )}
          </div>

          {/* Card grid */}
          {loading && events.length === 0 ? (
            <LoadingState message="Loading face detections…" />
          ) : displayedEvents.length === 0 ? (
            <EmptyState
              icon="👤"
              title="No face detections yet"
              body="Faces appear here when cameras with face detection enabled capture a person. Enable face detection per-camera in the Cameras → Detection settings."
            />
          ) : (
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))',
              gap: 10,
            }}>
              {displayedEvents.map(e => {
                const pName = personNameFor(e)
                const isSelected = e.id === selectedId
                const conf = e.score != null ? `${Math.round(e.score * 100)}%` : null
                const started = new Date(e.started_at).toLocaleString()

                return (
                  <div
                    key={e.id}
                    onClick={() => setSelectedId(e.id)}
                    style={{
                      background: 'var(--bg-surface)',
                      border: isSelected ? '2px solid var(--accent)' : '1px solid var(--border)',
                      borderRadius: 'var(--radius-md)',
                      overflow: 'hidden',
                      cursor: 'pointer',
                      boxShadow: isSelected ? 'var(--accent-glow)' : 'none',
                      transition: 'border-color 150ms ease, box-shadow 150ms ease',
                      // Compensate for thicker border so cards don't jump in size
                      margin: isSelected ? 0 : 1,
                    }}
                    onMouseEnter={ev => {
                      if (isSelected) return
                      const el = ev.currentTarget as HTMLDivElement
                      el.style.borderColor = 'var(--accent)'
                    }}
                    onMouseLeave={ev => {
                      if (isSelected) return
                      const el = ev.currentTarget as HTMLDivElement
                      el.style.borderColor = 'var(--border)'
                    }}
                  >
                    {/* Thumbnail */}
                    <div style={{
                      position: 'relative',
                      aspectRatio: '4 / 3',
                      background: 'var(--bg-base)',
                    }}>
                      {e.thumb_path ? (
                        <img
                          src={withToken(eventsApi.thumbUrl(e.id))}
                          alt={`Face ${e.id}`}
                          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                          onError={ev => { (ev.target as HTMLImageElement).style.display = 'none' }}
                        />
                      ) : (
                        <div style={{
                          width: '100%', height: '100%',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          color: 'var(--text-muted)', fontSize: 24,
                        }}>
                          👤
                        </div>
                      )}

                      {/* Top-left badge: person name or "FACE" */}
                      {pName ? (
                        <span style={{
                          position: 'absolute', top: 6, left: 6,
                          background: 'var(--accent)',
                          color: '#fff', fontSize: 10, fontWeight: 700,
                          padding: '2px 8px', borderRadius: 4,
                          letterSpacing: '0.04em',
                          maxWidth: 'calc(100% - 12px)',
                          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        }}>
                          {pName}
                        </span>
                      ) : (
                        <span style={{
                          position: 'absolute', top: 6, left: 6,
                          background: 'rgba(16,185,129,0.9)',
                          color: '#fff', fontSize: 9, fontWeight: 700,
                          padding: '2px 6px', borderRadius: 4,
                          letterSpacing: '0.06em', textTransform: 'uppercase',
                        }}>
                          Face
                        </span>
                      )}

                      {/* Bottom-right confidence */}
                      {conf && (
                        <span style={{
                          position: 'absolute', bottom: 6, right: 6,
                          background: 'rgba(0,0,0,0.72)',
                          color: '#fff', fontSize: 10, fontWeight: 700,
                          padding: '2px 6px', borderRadius: 4,
                          fontFamily: 'var(--font-mono)',
                        }}>
                          {conf}
                        </span>
                      )}
                    </div>

                    {/* Body */}
                    <div style={{ padding: '8px 10px' }}>
                      <div style={{
                        fontWeight: 600, fontSize: 12, color: 'var(--text-primary)',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>
                        {camNameById[e.camera_id] ?? `Camera ${e.camera_id}`}
                      </div>
                      <div style={{
                        fontSize: 10, color: 'var(--text-muted)',
                        fontFamily: 'var(--font-mono)', marginTop: 1,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>
                        {started}
                      </div>
                      <div style={{
                        fontSize: 11, color: pName ? 'var(--text-secondary)' : 'var(--text-muted)',
                        marginTop: 4, fontStyle: pName ? 'normal' : 'italic',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      }}>
                        {pName ?? 'Unnamed'}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* Pagination */}
          {total > LIMIT && (
            <div style={{
              display: 'flex', gap: 8, justifyContent: 'center', alignItems: 'center',
              padding: '4px 0',
            }}>
              <button
                className="btn btn-secondary btn-sm"
                disabled={offset === 0 || loading}
                onClick={() => loadFaces(Math.max(0, offset - LIMIT))}
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
                onClick={() => loadFaces(offset + LIMIT)}
              >
                Next →
              </button>
            </div>
          )}
        </div>

        {/* ── Detail panel (right column on wide) ──────────────────── */}
        {!isNarrow && (
          selectedEvent ? (
            <DetailPanel
              event={selectedEvent}
              cameraName={camNameById[selectedEvent.camera_id] ?? `Camera ${selectedEvent.camera_id}`}
              personName={personNameFor(selectedEvent)}
              onName={() => setNamingEvent(selectedEvent)}
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
              <div style={{ fontSize: 32, opacity: 0.4 }}>👤</div>
              <span>Select a face to see details</span>
            </div>
          )
        )}
      </div>

      {namingEvent && (
        <NameDialog
          event={namingEvent}
          persons={persons}
          onClose={() => setNamingEvent(null)}
          onSaved={(name) => handleNamed(namingEvent, name)}
        />
      )}
    </div>
  )
}

// ── DetailPanel ──────────────────────────────────────────────────────────────

interface DetailPanelProps {
  event: ArgusEvent
  cameraName: string
  personName: string | null
  sticky: boolean
  onName: () => void
  onViewInPlayback: (e: ArgusEvent) => void
  onClose: () => void
}

function DetailPanel({
  event, cameraName, personName, sticky,
  onName, onViewInPlayback, onClose,
}: DetailPanelProps) {
  const dur = durationSec(event)
  const conf = event.score != null ? `${Math.round(event.score * 100)}%` : null

  return (
    <div style={{
      background: 'var(--bg-surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-md)',
      overflow: 'hidden',
      position: sticky ? 'sticky' : 'static',
      top: sticky ? 0 : undefined,
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
          Face Detail
        </span>
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

      {/* Large thumbnail */}
      <div style={{
        background: '#000',
        borderBottom: '1px solid var(--border)',
        aspectRatio: '16 / 9',
        position: 'relative',
      }}>
        {event.thumb_path ? (
          <img
            src={withToken(eventsApi.thumbUrl(event.id))}
            alt={`Face ${event.id}`}
            style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
        ) : (
          <div style={{
            width: '100%', height: '100%',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'var(--text-muted)', fontSize: 12,
          }}>
            No thumbnail
          </div>
        )}
        {conf && (
          <span style={{
            position: 'absolute', bottom: 8, right: 8,
            background: 'rgba(0,0,0,0.72)',
            color: '#fff', fontSize: 11, fontWeight: 700,
            padding: '3px 8px', borderRadius: 4,
            fontFamily: 'var(--font-mono)',
          }}>
            {conf}
          </span>
        )}
      </div>

      {/* Body */}
      <div style={{ padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div>
          <div style={{
            fontWeight: 700, color: 'var(--text-primary)', fontSize: 15,
            marginBottom: 4,
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>
            {cameraName}
          </div>
          <div style={{
            fontSize: 12, color: 'var(--text-secondary)',
            fontFamily: 'var(--font-mono)',
          }}>
            {fmtDateTime(event.started_at)}
          </div>
          {dur != null && (
            <div style={{
              fontSize: 11, color: 'var(--text-muted)',
              fontFamily: 'var(--font-mono)', marginTop: 2,
            }}>
              Duration {fmtDuration(dur)}
            </div>
          )}
        </div>

        {/* Confidence */}
        {conf && (
          <DetailRow label="Confidence" value={conf} mono />
        )}

        {/* Person section */}
        <div style={{
          padding: '10px 12px',
          background: 'var(--bg-base)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-md)',
        }}>
          <div style={{
            fontSize: 10, color: 'var(--text-muted)', fontWeight: 700,
            letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: 8,
          }}>
            Person
          </div>
          {personName ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
              <span style={{
                background: 'rgba(16,185,129,0.9)',
                color: '#fff', fontSize: 12, fontWeight: 700,
                padding: '3px 10px', borderRadius: 4,
                letterSpacing: '0.04em',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                maxWidth: '100%',
              }}>
                {personName}
              </span>
              <button
                className="btn btn-secondary btn-sm"
                onClick={onName}
                style={{ fontSize: 11 }}
              >
                Rename
              </button>
            </div>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 12, color: 'var(--text-muted)', fontStyle: 'italic' }}>
                Unnamed
              </span>
              <button
                className="btn btn-primary btn-sm"
                onClick={onName}
                style={{ fontSize: 11 }}
              >
                + Name this person
              </button>
            </div>
          )}
        </div>

        {/* View in Playback */}
        <button
          className="btn btn-secondary btn-sm"
          onClick={() => onViewInPlayback(event)}
          style={{ width: '100%' }}
        >
          View in Playback
        </button>

        {/* Recording info */}
        {event.recording_id != null && (
          <>
            <div style={{ height: 1, background: 'var(--border)' }} />
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <DetailRow label="Recording" value={`#${event.recording_id}`} mono />
              <DetailRow label="Event ID" value={`#${event.id}`} mono />
            </div>
          </>
        )}
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
