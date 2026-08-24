import React, { useEffect, useRef, useState } from 'react'
import { TimelineSegment, timelineApi } from '../api/client'
import { useToast } from '../components/ui/Toast'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ClipMode = 'realtime' | 'timelapse'

interface SaveClipModalProps {
  open: boolean
  onClose: () => void
  camId: number | null
  segments: TimelineSegment[]
  initialMs: number | null
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function msToTimeInput(ms: number): string {
  const totalSecs = Math.floor(ms / 1000)
  const hh = Math.floor(totalSecs / 3600)
  const mm = Math.floor((totalSecs % 3600) / 60)
  const ss = totalSecs % 60
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
}

/** Parse a local HH:MM:SS time input against the wall-clock date portion of a reference ms. */
function timeInputToMs(timeStr: string, refMs: number): number {
  const refDate = new Date(refMs)
  const [hh, mm, ss] = timeStr.split(':').map(Number)
  const d = new Date(refDate)
  d.setHours(hh, mm, ss, 0)
  return d.getTime()
}

function formatDuration(ms: number): string {
  const totalSecs = Math.max(0, Math.round(ms / 1000))
  const m = Math.floor(totalSecs / 60)
  const s = totalSecs % 60
  if (m === 0) return `${s}s`
  return `${m}m ${s}s`
}

function coverageRange(segments: TimelineSegment[]): { minMs: number; maxMs: number } | null {
  if (segments.length === 0) return null
  const starts = segments.map((s) => new Date(s.started_at).getTime())
  const ends = segments
    .filter((s) => s.ended_at)
    .map((s) => new Date(s.ended_at!).getTime())
  if (ends.length === 0) return null
  return { minMs: Math.min(...starts), maxMs: Math.max(...ends) }
}

// ---------------------------------------------------------------------------
// SaveClipModal
// ---------------------------------------------------------------------------

export function SaveClipModal({
  open,
  onClose,
  camId,
  segments,
  initialMs,
}: SaveClipModalProps) {
  const toast = useToast()

  const [startMs, setStartMs] = useState(0)
  const [endMs, setEndMs] = useState(0)
  const [mode, setMode] = useState<ClipMode>('realtime')
  const [exporting, setExporting] = useState(false)

  const overlayRef = useRef<HTMLDivElement>(null)

  // Initialize range when modal opens
  useEffect(() => {
    if (!open) return

    const coverage = coverageRange(segments)

    if (coverage == null) {
      setStartMs(0)
      setEndMs(0)
      return
    }

    if (initialMs != null) {
      const HALF = 2.5 * 60 * 1000
      const rawStart = initialMs - HALF
      const rawEnd = initialMs + HALF
      setStartMs(Math.max(coverage.minMs, rawStart))
      setEndMs(Math.min(coverage.maxMs, rawEnd))
    } else {
      setStartMs(coverage.minMs)
      setEndMs(Math.min(coverage.maxMs, coverage.minMs + 5 * 60 * 1000))
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  // Escape key to close
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !exporting) onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open, exporting, onClose])

  if (!open) return null

  const durationMs = Math.max(0, endMs - startMs)

  function handleOverlayClick(e: React.MouseEvent<HTMLDivElement>) {
    if (e.target === overlayRef.current && !exporting) {
      onClose()
    }
  }

  function handleStartChange(e: React.ChangeEvent<HTMLInputElement>) {
    const parsed = timeInputToMs(e.target.value, startMs || Date.now())
    if (parsed < endMs) setStartMs(parsed)
  }

  function handleEndChange(e: React.ChangeEvent<HTMLInputElement>) {
    const parsed = timeInputToMs(e.target.value, endMs || Date.now())
    if (parsed > startMs) setEndMs(parsed)
  }

  async function handleDownload() {
    if (camId == null) return
    setExporting(true)
    try {
      const start = new Date(startMs).toISOString()
      const end = new Date(endMs).toISOString()

      if (mode === 'realtime') {
        await timelineApi.exportClip({ camera_id: camId, start, end })
        toast.success('Clip download started')
      } else {
        const blob = await timelineApi.timelapse({
          camera_id: camId,
          start,
          end,
          speed: 60,
        })
        // Derive a filename from start time in HH-MM-SS
        const startDate = new Date(startMs)
        const hh = String(startDate.getHours()).padStart(2, '0')
        const mm = String(startDate.getMinutes()).padStart(2, '0')
        const ss = String(startDate.getSeconds()).padStart(2, '0')
        const filename = `timelapse_cam${camId}_${hh}${mm}${ss}.mp4`
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = filename
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
        toast.success('Time-lapse download started')
      }

      onClose()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Export failed')
    } finally {
      setExporting(false)
    }
  }

  return (
    <div
      ref={overlayRef}
      onClick={handleOverlayClick}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0, 0, 0, 0.6)',
        zIndex: 1000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <div
        style={{
          background: 'var(--bg-surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-md)',
          padding: 20,
          width: 'min(480px, 92vw)',
          display: 'flex',
          flexDirection: 'column',
          gap: 16,
        }}
      >
        {/* Title */}
        <h2
          style={{
            margin: 0,
            fontSize: 16,
            fontWeight: 700,
            color: 'var(--text-primary)',
          }}
        >
          Save Clip
        </h2>

        {/* Time range */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {/* Start */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <label
              style={{
                width: 44,
                fontSize: 13,
                color: 'var(--text-secondary)',
                flexShrink: 0,
              }}
            >
              Start
            </label>
            <input
              type="time"
              step="1"
              value={msToTimeInput(startMs)}
              onChange={handleStartChange}
              disabled={exporting}
              style={{
                flex: 1,
                fontFamily: 'var(--font-mono)',
                fontSize: 13,
                padding: '5px 8px',
                background: 'var(--bg-base)',
                border: '1px solid var(--border)',
                borderRadius: 'var(--radius-sm)',
                color: 'var(--text-primary)',
              }}
            />
          </div>

          {/* Duration */}
          <div
            style={{
              textAlign: 'center',
              fontSize: 12,
              color: 'var(--text-muted)',
            }}
          >
            Duration: {formatDuration(durationMs)}
          </div>

          {/* End */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <label
              style={{
                width: 44,
                fontSize: 13,
                color: 'var(--text-secondary)',
                flexShrink: 0,
              }}
            >
              End
            </label>
            <input
              type="time"
              step="1"
              value={msToTimeInput(endMs)}
              onChange={handleEndChange}
              disabled={exporting}
              style={{
                flex: 1,
                fontFamily: 'var(--font-mono)',
                fontSize: 13,
                padding: '5px 8px',
                background: 'var(--bg-base)',
                border: '1px solid var(--border)',
                borderRadius: 'var(--radius-sm)',
                color: 'var(--text-primary)',
              }}
            />
          </div>
        </div>

        {/* Mode */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Format
          </span>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {(
              [
                { value: 'realtime', label: 'Real-time MP4' },
                { value: 'timelapse', label: 'Time-lapse (60×)' },
              ] as { value: ClipMode; label: string }[]
            ).map(({ value, label }) => (
              <label
                key={value}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  cursor: 'pointer',
                  fontSize: 13,
                  color: 'var(--text-primary)',
                }}
              >
                <input
                  type="radio"
                  name="clip-mode"
                  value={value}
                  checked={mode === value}
                  onChange={() => setMode(value)}
                  disabled={exporting}
                  style={{ accentColor: 'var(--accent)', cursor: 'pointer' }}
                />
                {label}
              </label>
            ))}
          </div>
        </div>

        {/* Actions */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button
            className="btn btn-ghost btn-sm"
            onClick={onClose}
            disabled={exporting}
          >
            Cancel
          </button>
          <button
            className="btn btn-primary btn-sm"
            onClick={handleDownload}
            disabled={exporting || camId == null || durationMs === 0}
            style={{ display: 'flex', alignItems: 'center', gap: 6 }}
          >
            {exporting && <span className="spinner spinner-sm" />}
            {exporting ? 'Exporting…' : 'Download'}
          </button>
        </div>
      </div>
    </div>
  )
}
