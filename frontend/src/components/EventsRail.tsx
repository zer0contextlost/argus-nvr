import { useEffect, useRef } from 'react'
import { TimelineEvent, withToken } from '../api/client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EventFilter = 'all' | 'motion' | 'object' | 'unreviewed'

interface EventsRailProps {
  events: TimelineEvent[]
  activeEventId: number | null
  filter: EventFilter
  onFilterChange: (f: EventFilter) => void
  onEventClick: (evt: TimelineEvent) => void
  isNarrow: boolean
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTime(iso: string): string {
  const d = new Date(iso)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

function formatDuration(started_at: string, ended_at: string | null): string | null {
  if (!ended_at) return null
  const secs = Math.round((new Date(ended_at).getTime() - new Date(started_at).getTime()) / 1000)
  if (secs <= 0) return null
  return `${secs}s`
}

const FILTER_LABELS: { key: EventFilter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'motion', label: 'Motion' },
  { key: 'object', label: 'Object' },
  { key: 'unreviewed', label: 'Unreviewed' },
]

// ---------------------------------------------------------------------------
// EventsRail
// ---------------------------------------------------------------------------

export function EventsRail({
  events,
  activeEventId,
  filter,
  onFilterChange,
  onEventClick,
  isNarrow,
}: EventsRailProps) {
  const rowRefs = useRef<Map<number, HTMLDivElement>>(new Map())

  // Scroll active event into view whenever activeEventId changes
  useEffect(() => {
    if (activeEventId == null) return
    const el = rowRefs.current.get(activeEventId)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [activeEventId])

  // Sort reverse-chronological (newest first)
  const sorted = [...events].sort(
    (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
  )

  const maxHeight = isNarrow ? '320px' : 'calc(100vh - 180px)'

  return (
    <div
      style={{
        background: 'var(--bg-surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-md)',
        display: 'flex',
        flexDirection: 'column',
        position: 'sticky',
        top: 12,
        maxHeight,
        minHeight: 0,
      }}
    >
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '10px 12px 0',
          flexShrink: 0,
        }}
      >
        <span
          style={{
            fontSize: 11,
            fontWeight: 700,
            letterSpacing: '0.08em',
            color: 'var(--text-muted)',
            textTransform: 'uppercase',
          }}
        >
          Events
        </span>
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: 'var(--text-secondary)',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border-subtle)',
            borderRadius: 'var(--radius-full)',
            padding: '1px 7px',
          }}
        >
          {events.length}
        </span>
      </div>

      {/* Filter chips */}
      <div
        style={{
          display: 'flex',
          gap: 6,
          padding: '8px 12px',
          flexWrap: 'wrap',
          flexShrink: 0,
          borderBottom: '1px solid var(--border-subtle)',
        }}
      >
        {FILTER_LABELS.map(({ key, label }) => {
          const active = filter === key
          return (
            <button
              key={key}
              onClick={() => onFilterChange(key)}
              style={{
                fontSize: 11,
                fontWeight: 600,
                padding: '3px 10px',
                borderRadius: 'var(--radius-full)',
                border: active ? '1px solid var(--accent)' : '1px solid var(--border)',
                background: active ? 'var(--accent-dim)' : 'transparent',
                color: active ? 'var(--text-accent)' : 'var(--text-secondary)',
                cursor: 'pointer',
                transition: 'var(--transition)',
                whiteSpace: 'nowrap',
              }}
            >
              {label}
            </button>
          )
        })}
      </div>

      {/* Events list */}
      <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
        {sorted.length === 0 ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              padding: '32px 16px',
              color: 'var(--text-muted)',
              fontSize: 13,
              textAlign: 'center',
            }}
          >
            {filter === 'all' ? 'No events' : 'No recordings for this filter'}
          </div>
        ) : (
          sorted.map((evt) => {
            const isActive = evt.id === activeEventId
            const duration = formatDuration(evt.started_at, evt.ended_at)
            const isMotion = evt.type === 'motion'

            return (
              <div
                key={evt.id}
                ref={(el) => {
                  if (el) rowRefs.current.set(evt.id, el)
                  else rowRefs.current.delete(evt.id)
                }}
                onClick={() => onEventClick(evt)}
                style={{
                  display: 'flex',
                  gap: 10,
                  padding: '10px 12px',
                  cursor: 'pointer',
                  borderLeft: isActive
                    ? '3px solid var(--accent)'
                    : '3px solid transparent',
                  background: isActive ? 'var(--accent-dim)' : 'transparent',
                  transition: 'background var(--transition)',
                }}
                onMouseEnter={(e) => {
                  if (!isActive) {
                    (e.currentTarget as HTMLDivElement).style.background = 'var(--bg-elevated)'
                  }
                }}
                onMouseLeave={(e) => {
                  if (!isActive) {
                    (e.currentTarget as HTMLDivElement).style.background = 'transparent'
                  }
                }}
              >
                {/* Thumbnail */}
                <div style={{ flexShrink: 0 }}>
                  {evt.thumb_url ? (
                    <img
                      src={withToken(evt.thumb_url)}
                      alt=""
                      width={96}
                      height={60}
                      style={{
                        display: 'block',
                        width: 96,
                        height: 60,
                        objectFit: 'cover',
                        border: '1px solid var(--border)',
                        borderRadius: 'var(--radius-sm)',
                        background: '#000',
                      }}
                      onError={(e) => {
                        (e.currentTarget as HTMLImageElement).style.display = 'none'
                      }}
                    />
                  ) : (
                    <div
                      style={{
                        width: 96,
                        height: 60,
                        background: 'var(--bg-base)',
                        border: '1px solid var(--border)',
                        borderRadius: 'var(--radius-sm)',
                      }}
                    />
                  )}
                </div>

                {/* Info */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
                  {/* Time */}
                  <span
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      color: 'var(--text-primary)',
                      fontFamily: 'var(--font-mono)',
                      lineHeight: 1.2,
                    }}
                  >
                    {formatTime(evt.started_at)}
                  </span>

                  {/* Type badge + label */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 5, flexWrap: 'wrap' }}>
                    <span
                      style={{
                        fontSize: 10,
                        fontWeight: 700,
                        padding: '1px 6px',
                        borderRadius: 'var(--radius-full)',
                        background: isMotion ? 'rgba(245, 158, 11, 0.25)' : 'rgba(34, 197, 94, 0.2)',
                        color: isMotion ? '#d97706' : '#16a34a',
                        textTransform: 'capitalize',
                        letterSpacing: '0.04em',
                        lineHeight: '16px',
                      }}
                    >
                      {evt.type}
                    </span>
                    {evt.label && (
                      <span
                        style={{
                          fontSize: 11,
                          color: 'var(--text-secondary)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {evt.label}
                      </span>
                    )}
                  </div>

                  {/* Duration */}
                  {duration && (
                    <span style={{ fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.2 }}>
                      {duration}
                    </span>
                  )}
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
