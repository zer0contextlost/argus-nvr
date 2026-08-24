/**
 * PlaybackPage — incident-first NVR review UI.
 *
 * Layout (desktop):
 *   ┌─────────────────────────────────────────┬──────────────┐
 *   │ camera select + date chip + loading     │              │
 *   ├─────────────────────────────────────────┤              │
 *   │ ContinuousVideoPlayer (seamless segs)   │ EventsRail   │
 *   │ transport bar (⏮ ▶ ⏭ · speed · clip)   │ (thumbnails) │
 *   │ Scrubber (single bar + zoom chips)      │              │
 *   └─────────────────────────────────────────┴──────────────┘
 *
 * Mobile: same order but stacked vertically, events rail below scrubber.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Camera, TimelineEvent, TimelineResult,
  camerasApi, timelineApi,
} from '../api/client'
import { useToast } from '../components/ui/Toast'
import {
  ContinuousVideoPlayer,
  ContinuousVideoPlayerHandle,
} from '../components/ContinuousVideoPlayer'
import { Scrubber } from '../components/Scrubber'
import { EventsRail, EventFilter } from '../components/EventsRail'
import { SaveClipModal } from '../components/SaveClipModal'
import { DateChip } from '../components/DateChip'

// ── Constants ─────────────────────────────────────────────────────────────────

const SPEEDS = [1, 2, 4, 8]
const NARROW = 768
/** Slide viewport when playhead passes 80% of viewport width during playback */
const FOLLOW_THRESHOLD = 0.80
// Seconds of lead-up to include when jumping to an event, to cover detection
// latency + the recorder pre-buffer so the start of the action is visible.
const EVENT_PREROLL_MS = 10_000

// ── Helpers ───────────────────────────────────────────────────────────────────

function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function localDateStr(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function fmtClock(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}

function centeredViewport(
  centerMs: number,
  spanMs: number,
  dayStartMs: number,
  dayEndMs: number,
): { start: number; end: number } {
  let start = centerMs - spanMs / 2
  let end = start + spanMs
  if (start < dayStartMs) { start = dayStartMs; end = start + spanMs }
  if (end > dayEndMs) { end = dayEndMs; start = end - spanMs }
  return { start, end }
}

// ── Component ─────────────────────────────────────────────────────────────────

export function PlaybackPage() {
  const toast = useToast()
  const [searchParams] = useSearchParams()

  // Deep-link: ?t=ISO ?camera_id=N ?date=YYYY-MM-DD
  const initialTMs = useMemo(() => {
    const t = searchParams.get('t')
    if (!t) return null
    const ms = new Date(t).getTime()
    return isNaN(ms) ? null : ms
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const didInitialSeek = useRef(false)
  const timelineGenRef = useRef(0)

  // Core state
  const [cameras, setCameras] = useState<Camera[]>([])
  const [camId, setCamId] = useState(() => searchParams.get('camera_id') ?? '')
  const [dateStr, setDateStr] = useState<string>(() => {
    const d = searchParams.get('date')
    if (d) return d
    const t = searchParams.get('t')
    if (t) {
      const dt = new Date(t)
      if (!isNaN(dt.getTime())) return localDateStr(dt)
    }
    return todayStr()
  })

  const [timeline, setTimeline] = useState<TimelineResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Playback state — driven by player callbacks, not by us
  const [playWallMs, setPlayWallMs] = useState<number | null>(null)
  const [isPlaying, setIsPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)

  // Scrubber viewport: which slice of the day is visible
  const [viewport, setViewport] = useState<{ start: number; end: number } | null>(null)

  // UI
  const [filter, setFilter] = useState<EventFilter>('all')
  const [saveClipOpen, setSaveClipOpen] = useState(false)
  const [isNarrow, setIsNarrow] = useState(
    typeof window !== 'undefined' ? window.innerWidth < NARROW : false,
  )

  const playerRef = useRef<ContinuousVideoPlayerHandle>(null)

  // Day boundaries (local midnight)
  const dayStartMs = useMemo(() => {
    const d = new Date(dateStr + 'T00:00:00')
    return isNaN(d.getTime()) ? Date.now() : d.getTime()
  }, [dateStr])
  const dayEndMs = useMemo(() => dayStartMs + 86_400_000, [dayStartMs])

  // ── Effects ────────────────────────────────────────────────────────────────

  useEffect(() => {
    camerasApi.list().then(cams => {
      setCameras(cams)
      if (cams.length > 0 && !camId) setCamId(String(cams[0].id))
    }).catch(() => {})
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    function onResize() { setIsNarrow(window.innerWidth < NARROW) }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Load timeline whenever camera or day changes
  useEffect(() => {
    if (!camId) return
    const startIso = new Date(dayStartMs).toISOString().replace(/\.\d+Z$/, 'Z')
    const endIso   = new Date(dayEndMs).toISOString().replace(/\.\d+Z$/, 'Z')

    playerRef.current?.pause()
    setPlayWallMs(null)
    setIsPlaying(false)
    setTimeline(null)
    setError(null)
    setLoading(true)

    // Guard against out-of-order responses: rapidly switching camera/day can
    // leave an older request in flight that resolves AFTER the current one and
    // overwrites it with the wrong camera's segments. Stamp each run and bail
    // if a newer run has started by the time this response lands.
    const gen = ++timelineGenRef.current

    const handle = window.setTimeout(() => {
      timelineApi
        .get({ camera_id: parseInt(camId), start: startIso, end: endIso })
        .then(result => {
          if (gen !== timelineGenRef.current) return
          setTimeline(result)

          // Deep-link: seek to ?t= once. Apply the same pre-roll as
          // handleEventClick below — ?t= is always an event's started_at
          // (raw motion-confirm timestamp) coming from EventsPage/PersonsPage's
          // "View in Playback" links, so without this the deep link lands
          // exactly at confirm time and skips the pre-buffered lead-up where
          // the triggering object is still entering frame.
          if (initialTMs != null && !didInitialSeek.current) {
            didInitialSeek.current = true
            setViewport(centeredViewport(initialTMs, 3 * 3_600_000, dayStartMs, dayEndMs))
            const seekMs = Math.max(dayStartMs, initialTMs - EVENT_PREROLL_MS)
            setTimeout(() => {
              playerRef.current?.seek(seekMs)
              playerRef.current?.play()
            }, 120)
            return
          }

          // Default: 3-hour viewport centered on first segment
          if (result.segments.length > 0) {
            const firstMs = new Date(result.segments[0].started_at).getTime()
            setViewport(centeredViewport(firstMs, 3 * 3_600_000, dayStartMs, dayEndMs))
          } else {
            setViewport({ start: dayStartMs, end: dayEndMs })
          }
        })
        .catch(e => { if (gen === timelineGenRef.current) setError(String(e)) })
        .finally(() => { if (gen === timelineGenRef.current) setLoading(false) })
    }, 150)

    return () => clearTimeout(handle)
  }, [camId, dayStartMs, dayEndMs]) // eslint-disable-line react-hooks/exhaustive-deps

  // Propagate speed changes to the player
  useEffect(() => {
    playerRef.current?.setSpeed(speed)
  }, [speed])

  // ── Derived ────────────────────────────────────────────────────────────────

  const segments = useMemo(() => timeline?.segments ?? [], [timeline])

  const filteredEvents = useMemo((): TimelineEvent[] => {
    const evts = timeline?.events ?? []
    const sorted = [...evts].sort(
      (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
    )
    if (filter === 'motion') return sorted.filter(e => e.type === 'motion')
    if (filter === 'object') return sorted.filter(e =>
      e.type === 'object' || e.type === 'person' || e.type === 'face' || e.type === 'vehicle',
    )
    return sorted
  }, [timeline, filter])

  const activeEventId = useMemo(() => {
    if (playWallMs == null) return null
    for (const ev of filteredEvents) {
      const s = new Date(ev.started_at).getTime()
      const e = ev.ended_at ? new Date(ev.ended_at).getTime() : s + 1000
      if (playWallMs >= s && playWallMs <= e) return ev.id
    }
    return null
  }, [filteredEvents, playWallMs])

  const hasSegments = segments.length > 0
  const camName = cameras.find(c => String(c.id) === camId)?.name ?? ''

  // ── Callbacks ──────────────────────────────────────────────────────────────

  const handleWallTimeChange = useCallback((ms: number) => {
    setPlayWallMs(ms)
    // Auto-slide viewport right when playhead approaches the right edge
    setViewport(prev => {
      if (!prev) return prev
      const span = prev.end - prev.start
      const pos = (ms - prev.start) / span
      if (pos > FOLLOW_THRESHOLD) {
        const newStart = ms - span * 0.2
        return { start: newStart, end: newStart + span }
      }
      return prev
    })
  }, [])

  const handleSeek = useCallback((wallMs: number) => {
    playerRef.current?.seek(wallMs)
    setPlayWallMs(wallMs)
    setViewport(prev =>
      prev ? centeredViewport(wallMs, prev.end - prev.start, dayStartMs, dayEndMs) : prev,
    )
  }, [dayStartMs, dayEndMs])

  const handleEventClick = useCallback((evt: TimelineEvent) => {
    // Seek a few seconds BEFORE the event timestamp. The event is stamped at
    // motion-confirmation time (a couple seconds after the action actually
    // starts) and the recorder's pre-buffer captures the lead-up, so starting
    // playback at exactly started_at skips the approach — the car is already
    // half out of frame. Pre-roll surfaces that footage. If the pre-roll lands
    // in a recording gap the player snaps forward to the next segment anyway,
    // so this is safe to apply unconditionally.
    const startMs = new Date(evt.started_at).getTime()
    handleSeek(Math.max(dayStartMs, startMs - EVENT_PREROLL_MS))
    // seek() loads the segment; play() sets the "playing" intent and will
    // kick off playback as soon as the element fires loadedmetadata.
    playerRef.current?.play()
  }, [handleSeek, dayStartMs])

  const handleGapSkipped = useCallback((prevEndMs: number, nextStartMs: number) => {
    const mins = Math.round((nextStartMs - prevEndMs) / 60_000)
    toast.info(`Skipped ${mins}m recording gap`)
  }, [toast])

  const togglePlay = useCallback(() => {
    if (isPlaying) playerRef.current?.pause()
    else playerRef.current?.play()
  }, [isPlaying])

  const skip = useCallback((seconds: number) => {
    if (playWallMs == null) return
    handleSeek(playWallMs + seconds * 1000)
  }, [playWallMs, handleSeek])

  // ── Keyboard shortcuts ─────────────────────────────────────────────────────

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      switch (e.key) {
        case ' ':          e.preventDefault(); togglePlay();  break
        case 'ArrowLeft':  e.preventDefault(); skip(-10);     break
        case 'ArrowRight': e.preventDefault(); skip(10);      break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [togglePlay, skip])

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div style={{ maxWidth: 1600 }}>
      {/* ── Top bar ───────────────────────────────────────────────────────── */}
      <div style={{
        display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap',
        marginBottom: 12, padding: '8px 12px',
        background: 'var(--bg-surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-md)',
      }}>
        <select
          value={camId}
          onChange={e => setCamId(e.target.value)}
          style={{ minWidth: 160, fontSize: 13, minHeight: 32 }}
        >
          <option value="">Select camera…</option>
          {cameras.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>

        <DateChip camId={camId} dateStr={dateStr} onDateChange={setDateStr} />

        {loading && (
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-muted)', fontSize: 12 }}>
            <span className="spinner spinner-sm" />
            Loading…
          </span>
        )}

        <div style={{ flex: 1 }} />

        {camName && (
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{camName}</span>
        )}
      </div>

      {error && (
        <div className="alert alert-error" style={{ marginBottom: 12 }} role="alert">
          {error}
        </div>
      )}

      {/* ── Two-column layout ─────────────────────────────────────────────── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: isNarrow ? '1fr' : 'minmax(0, 1fr) 340px',
        gap: 12,
        alignItems: 'start',
      }}>

        {/* ── Main column ─────────────────────────────────────────────────── */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0 }}>

          {/* Video */}
          <div style={{
            background: '#000',
            borderRadius: 'var(--radius-md)',
            border: '1px solid var(--border)',
            overflow: 'hidden',
          }}>
            <ContinuousVideoPlayer
              ref={playerRef}
              segments={segments}
              onWallTimeChange={handleWallTimeChange}
              onPlayingChange={setIsPlaying}
              onGapSkipped={handleGapSkipped}
              onError={() => toast.error('Failed to load recording')}
            />
          </div>

          {/* Transport bar */}
          <div style={{
            display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap',
            padding: '8px 12px',
            background: 'var(--bg-surface)', border: '1px solid var(--border)',
            borderRadius: 'var(--radius-md)',
          }}>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => skip(-10)}
              disabled={!hasSegments}
              title="Back 10s (←)"
            >⏮ 10s</button>

            <button
              onClick={togglePlay}
              disabled={!hasSegments}
              title={isPlaying ? 'Pause (Space)' : 'Play (Space)'}
              style={{
                width: 40, height: 32, borderRadius: 'var(--radius)',
                border: '1px solid var(--accent)',
                background: 'var(--accent-dim)',
                color: 'var(--text-accent)',
                cursor: hasSegments ? 'pointer' : 'not-allowed',
                opacity: hasSegments ? 1 : 0.4,
                fontSize: 16, display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              {isPlaying ? '⏸' : '▶'}
            </button>

            <button
              className="btn btn-secondary btn-sm"
              onClick={() => skip(10)}
              disabled={!hasSegments}
              title="Forward 10s (→)"
            >10s ⏭</button>

            <span style={{
              fontFamily: 'var(--font-mono)', fontSize: 14,
              color: 'var(--text-primary)', fontWeight: 600,
              minWidth: 80, letterSpacing: '0.04em',
            }}>
              {playWallMs != null ? fmtClock(playWallMs) : '--:--:--'}
            </span>

            <div style={{ flex: 1 }} />

            {/* Speed chips */}
            <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
              {SPEEDS.map(s => (
                <button
                  key={s}
                  onClick={() => setSpeed(s)}
                  disabled={!hasSegments}
                  style={{
                    padding: '3px 8px', borderRadius: 'var(--radius)',
                    border: speed === s ? '1px solid var(--accent)' : '1px solid var(--border)',
                    background: speed === s ? 'var(--accent-dim)' : 'transparent',
                    color: speed === s ? 'var(--text-accent)' : 'var(--text-muted)',
                    cursor: hasSegments ? 'pointer' : 'not-allowed',
                    fontSize: 11, fontWeight: 600,
                    opacity: hasSegments ? 1 : 0.4,
                    fontFamily: 'var(--font-mono)',
                    transition: 'all var(--transition)',
                  }}
                >{s}×</button>
              ))}
            </div>

            <button
              className="btn btn-secondary btn-sm"
              disabled={!hasSegments}
              onClick={() => setSaveClipOpen(true)}
            >
              Save Clip
            </button>
          </div>

          {/* Scrubber */}
          {viewport && (
            <div style={{
              background: 'var(--bg-surface)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-md)', padding: '10px 12px',
            }}>
              <Scrubber
                segments={segments}
                events={timeline?.events ?? []}
                dayStartMs={dayStartMs}
                dayEndMs={dayEndMs}
                playWallMs={playWallMs}
                viewport={viewport}
                onSeek={handleSeek}
                onViewportChange={setViewport}
                cameraId={camId ? parseInt(camId) : undefined}
              />
            </div>
          )}

          {/* Empty states */}
          {!loading && !camId && (
            <div style={{
              padding: '40px 16px', textAlign: 'center',
              color: 'var(--text-muted)', fontSize: 13,
              background: 'var(--bg-surface)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-md)',
            }}>
              Select a camera to start reviewing footage.
            </div>
          )}

          {!loading && camId && timeline && !hasSegments && (
            <div style={{
              padding: '40px 16px', textAlign: 'center',
              color: 'var(--text-muted)', fontSize: 13,
              background: 'var(--bg-surface)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-md)',
            }}>
              No recordings for this day — the camera may have been offline.
              <br />
              <span style={{ fontSize: 12, marginTop: 6, display: 'block' }}>
                Use the date picker to try another day.
              </span>
            </div>
          )}
        </div>

        {/* ── Events rail ─────────────────────────────────────────────────── */}
        <EventsRail
          events={filteredEvents}
          activeEventId={activeEventId}
          filter={filter}
          onFilterChange={setFilter}
          onEventClick={handleEventClick}
          isNarrow={isNarrow}
        />
      </div>

      {/* Save clip modal */}
      <SaveClipModal
        open={saveClipOpen}
        onClose={() => setSaveClipOpen(false)}
        camId={camId ? parseInt(camId) : null}
        segments={segments}
        initialMs={playWallMs}
      />
    </div>
  )
}
