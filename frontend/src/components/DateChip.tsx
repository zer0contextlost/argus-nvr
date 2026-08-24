import React, { useEffect, useRef, useState } from 'react'
import { CameraDay, camerasApi } from '../api/client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface DateChipProps {
  camId: string
  dateStr: string
  onDateChange: (d: string) => void
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Parse "YYYY-MM-DD" as a local-time date (noon to dodge DST) */
function parseLocalDate(dateStr: string): Date {
  return new Date(dateStr + 'T12:00:00')
}

/** Format a local Date as "YYYY-MM-DD" */
function toDateStr(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** Format month as "YYYY-MM" for API */
function toMonthStr(year: number, month: number): string {
  return `${year}-${String(month + 1).padStart(2, '0')}`
}

const WEEKDAY_HEADERS = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su']

/** Return ISO weekday index 0=Mon … 6=Sun for a Date */
function isoWeekday(d: Date): number {
  return (d.getDay() + 6) % 7
}

interface CalendarGrid {
  weeks: (Date | null)[][]
}

function buildCalendarGrid(year: number, month: number): CalendarGrid {
  const firstDay = new Date(year, month, 1)
  const lastDay = new Date(year, month + 1, 0)
  const startOffset = isoWeekday(firstDay)

  const cells: (Date | null)[] = []
  for (let i = 0; i < startOffset; i++) cells.push(null)
  for (let d = 1; d <= lastDay.getDate(); d++) cells.push(new Date(year, month, d))

  const weeks: (Date | null)[][] = []
  for (let i = 0; i < cells.length; i += 7) {
    const week = cells.slice(i, i + 7)
    while (week.length < 7) week.push(null)
    weeks.push(week)
  }
  return { weeks }
}

function today(): string {
  return toDateStr(new Date())
}

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

// ---------------------------------------------------------------------------
// DateChip
// ---------------------------------------------------------------------------

export function DateChip({ camId, dateStr, onDateChange }: DateChipProps): React.ReactElement {
  const [open, setOpen] = useState(false)

  // Displayed month in popover (may differ from selected date)
  const selectedDate = parseLocalDate(dateStr)
  const [viewYear, setViewYear] = useState(selectedDate.getFullYear())
  const [viewMonth, setViewMonth] = useState(selectedDate.getMonth())

  const [days, setDays] = useState<CameraDay[]>([])
  const [loadingDays, setLoadingDays] = useState(false)

  const containerRef = useRef<HTMLDivElement>(null)
  const todayStr = today()

  // Sync view month to selected date when dateStr changes externally
  useEffect(() => {
    const d = parseLocalDate(dateStr)
    setViewYear(d.getFullYear())
    setViewMonth(d.getMonth())
  }, [dateStr])

  // Load camera days when popover opens or month changes
  useEffect(() => {
    if (!open || !camId) {
      setDays([])
      return
    }
    const monthStr = toMonthStr(viewYear, viewMonth)
    setLoadingDays(true)
    camerasApi
      .days(parseInt(camId, 10), monthStr)
      .then((result) => setDays(result))
      .catch(() => setDays([]))
      .finally(() => setLoadingDays(false))
  }, [open, camId, viewYear, viewMonth])

  // Close on outside click
  useEffect(() => {
    if (!open) return
    function handleMouseDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [open])

  // Build a lookup map for the loaded days
  const dayMap = new Map<string, CameraDay>()
  for (const d of days) dayMap.set(d.date, d)

  function prevDay() {
    const d = parseLocalDate(dateStr)
    d.setDate(d.getDate() - 1)
    onDateChange(toDateStr(d))
  }

  function nextDay() {
    const d = parseLocalDate(dateStr)
    d.setDate(d.getDate() + 1)
    onDateChange(toDateStr(d))
  }

  function goToday() {
    onDateChange(todayStr)
  }

  function prevMonth() {
    if (viewMonth === 0) {
      setViewYear((y) => y - 1)
      setViewMonth(11)
    } else {
      setViewMonth((m) => m - 1)
    }
  }

  function nextMonth() {
    if (viewMonth === 11) {
      setViewYear((y) => y + 1)
      setViewMonth(0)
    } else {
      setViewMonth((m) => m + 1)
    }
  }

  function handleDayClick(d: Date) {
    onDateChange(toDateStr(d))
    setOpen(false)
  }

  const chipLabel = parseLocalDate(dateStr).toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  })

  const grid = buildCalendarGrid(viewYear, viewMonth)

  const navBtnStyle: React.CSSProperties = {
    background: 'none',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-sm)',
    color: 'var(--text-secondary)',
    cursor: 'pointer',
    fontSize: 14,
    lineHeight: 1,
    padding: '4px 8px',
    transition: 'var(--transition)',
  }

  return (
    <div ref={containerRef} style={{ position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      {/* Prev day */}
      <button
        onClick={prevDay}
        style={navBtnStyle}
        aria-label="Previous day"
        title="Previous day"
      >
        ‹
      </button>

      {/* Chip button */}
      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          background: open ? 'var(--accent-dim)' : 'var(--bg-surface)',
          border: open ? '1px solid var(--accent)' : '1px solid var(--border)',
          borderRadius: 'var(--radius-sm)',
          color: open ? 'var(--text-accent)' : 'var(--text-primary)',
          cursor: 'pointer',
          fontSize: 13,
          fontWeight: 600,
          padding: '5px 12px',
          transition: 'var(--transition)',
          whiteSpace: 'nowrap',
        }}
      >
        {chipLabel}
      </button>

      {/* Next day */}
      <button
        onClick={nextDay}
        style={navBtnStyle}
        aria-label="Next day"
        title="Next day"
      >
        ›
      </button>

      {/* Today shortcut */}
      {dateStr !== todayStr && (
        <button
          onClick={goToday}
          style={{
            ...navBtnStyle,
            fontSize: 12,
            padding: '4px 10px',
          }}
        >
          Today
        </button>
      )}

      {/* Calendar popover */}
      {open && (
        <div
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            left: 0,
            zIndex: 200,
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius-md)',
            padding: 12,
            minWidth: 260,
            boxShadow: '0 4px 16px rgba(0,0,0,0.3)',
          }}
        >
          {/* Month navigation */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: 10,
            }}
          >
            <button
              onClick={prevMonth}
              style={navBtnStyle}
              aria-label="Previous month"
            >
              ‹
            </button>
            <span style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-primary)' }}>
              {MONTH_NAMES[viewMonth]} {viewYear}
            </span>
            <button
              onClick={nextMonth}
              style={navBtnStyle}
              aria-label="Next month"
            >
              ›
            </button>
          </div>

          {/* Loading */}
          {loadingDays ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '16px 0' }}>
              <span className="spinner spinner-sm" />
            </div>
          ) : (
            <table style={{ borderCollapse: 'collapse', width: '100%' }}>
              <thead>
                <tr>
                  {WEEKDAY_HEADERS.map((h) => (
                    <th
                      key={h}
                      style={{
                        fontSize: 11,
                        fontWeight: 600,
                        color: 'var(--text-muted)',
                        textAlign: 'center',
                        paddingBottom: 6,
                        width: '14.28%',
                      }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {grid.weeks.map((week, wi) => (
                  <tr key={wi}>
                    {week.map((day, di) => {
                      if (!day) {
                        return <td key={di} />
                      }
                      const ds = toDateStr(day)
                      const isSelected = ds === dateStr
                      const isToday = ds === todayStr
                      const isCurrentMonth = day.getMonth() === viewMonth
                      const info = dayMap.get(ds)
                      const hasEvents = info ? info.events > 0 : false
                      const hasRecordingsOnly = info ? info.recordings > 0 && info.events === 0 : false

                      return (
                        <td
                          key={di}
                          style={{ textAlign: 'center', padding: '2px 0' }}
                        >
                          <button
                            onClick={() => handleDayClick(day)}
                            style={{
                              position: 'relative',
                              width: 32,
                              height: 32,
                              borderRadius: '50%',
                              border: isToday && !isSelected ? '1px solid var(--accent)' : '1px solid transparent',
                              background: isSelected ? 'var(--accent)' : 'transparent',
                              color: isSelected
                                ? '#fff'
                                : isCurrentMonth
                                ? 'var(--text-primary)'
                                : 'var(--text-muted)',
                              opacity: isCurrentMonth ? 1 : 0.4,
                              cursor: 'pointer',
                              fontSize: 12,
                              fontWeight: isSelected || isToday ? 700 : 400,
                              display: 'flex',
                              flexDirection: 'column',
                              alignItems: 'center',
                              justifyContent: 'center',
                              padding: 0,
                              transition: 'var(--transition)',
                              lineHeight: 1,
                            }}
                          >
                            <span style={{ lineHeight: 1 }}>{day.getDate()}</span>
                            {/* Activity dot */}
                            {(hasEvents || hasRecordingsOnly) && (
                              <span
                                style={{
                                  position: 'absolute',
                                  bottom: 3,
                                  left: '50%',
                                  transform: 'translateX(-50%)',
                                  width: 4,
                                  height: 4,
                                  borderRadius: '50%',
                                  background: hasEvents ? '#f59e0b' : '#3b82f6',
                                  flexShrink: 0,
                                }}
                              />
                            )}
                          </button>
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
