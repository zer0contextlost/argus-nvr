/**
 * HealthPage — System health dashboard. Polished v2.
 * Auto-refreshes every 10 seconds.
 */

import { useEffect, useRef, useState } from 'react'
import { systemApi, HealthResponse, SupervisorStatus, StorageStatus, cameraHealthApi, CameraHealth } from '../api/client'
import { LoadingState, StatusBadge, MeterBar, PageHeader } from '../components/ui/Primitives'

// ── Utility ───────────────────────────────────────────────────────────────────

function fmtUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (d > 0) return `${d}d ${h}h ${m}m`
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

function fmtBytes(bytes: number): string {
  if (bytes >= 1e12) return `${(bytes / 1e12).toFixed(1)} TB`
  if (bytes >= 1e9)  return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6)  return `${(bytes / 1e6).toFixed(1)} MB`
  return `${bytes} B`
}

// ── Stat card ─────────────────────────────────────────────────────────────────

function StatCard({
  label, value, sub, valueColor, icon,
}: {
  label: string
  value: string | React.ReactNode
  sub?: string
  valueColor?: string
  icon?: React.ReactNode
}) {
  return (
    <div className="stat-box">
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8, marginBottom: 8 }}>
        <div className="stat-box-label">{label}</div>
        {icon && <span style={{ opacity: 0.5, fontSize: 16 }}>{icon}</span>}
      </div>
      <div className="stat-box-value" style={valueColor ? { color: valueColor } : {}}>
        {value}
      </div>
      {sub && <div className="stat-box-sub">{sub}</div>}
    </div>
  )
}

// ── App info card ─────────────────────────────────────────────────────────────

function AppInfoCard({ health }: { health: HealthResponse }) {
  return (
    <div style={{
      background: 'var(--bg-surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-md)', padding: '20px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
        <span style={{
          width: 36, height: 36, borderRadius: 'var(--radius)',
          background: health.status === 'ok' ? 'var(--success-bg)' : 'var(--danger-bg)',
          border: `1px solid ${health.status === 'ok' ? 'rgba(16,185,129,0.2)' : 'rgba(239,68,68,0.2)'}`,
          display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 18,
        }}>
          {health.status === 'ok' ? '✓' : '✕'}
        </span>
        <div>
          <div style={{ fontWeight: 700, fontSize: 14, color: 'var(--text-primary)' }}>
            Argus NVR
          </div>
          <StatusBadge status={health.status} />
        </div>
        <div style={{ flex: 1 }} />
        <div style={{
          fontFamily: 'var(--font-mono)', fontSize: 11,
          color: 'var(--text-muted)', textAlign: 'right',
        }}>
          <div>Python {health.python}</div>
          <div>{health.platform}</div>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 8 }}>
        <StatCard
          label="Uptime"
          value={fmtUptime(health.uptime_s)}
          icon="⏱"
        />
        <StatCard
          label="go2rtc"
          value={<StatusBadge status={health.go2rtc.status} />}
          sub={`PID ${health.go2rtc.pid ?? '—'} · ${health.go2rtc.restarts} restart${health.go2rtc.restarts !== 1 ? 's' : ''}`}
          icon="📡"
        />
        <StatCard
          label="Recorders"
          value={String(health.supervisors?.filter(s => s.name.startsWith('recorder:') && s.status === 'running').length ?? 0)}
          sub={`of ${health.supervisors?.filter(s => s.name.startsWith('recorder:')).length ?? 0} active`}
          icon="⏺"
        />
        <StatCard
          label="Detectors"
          value={String(health.supervisors?.filter(s => s.name.startsWith('detector:') && s.status === 'running').length ?? 0)}
          sub={`of ${health.supervisors?.filter(s => s.name.startsWith('detector:')).length ?? 0} active`}
          icon="👁"
        />
      </div>
    </div>
  )
}

// ── Camera process table ──────────────────────────────────────────────────────

function ProcessRow({ sup }: { sup: SupervisorStatus }) {
  const isRunning = sup.status === 'running'
  const camName = sup.name.replace(/^(recorder|detector):/, '')
  const isRecorder = sup.name.startsWith('recorder:')

  return (
    <tr>
      <td>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{
            width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
            background: isRunning ? 'var(--success)' : 'var(--danger)',
            boxShadow: isRunning ? '0 0 5px rgba(16,185,129,0.5)' : '0 0 5px rgba(239,68,68,0.4)',
          }} />
          <span style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{camName}</span>
        </div>
      </td>
      <td>
        <span style={{
          fontSize: 10, padding: '2px 7px', borderRadius: 'var(--radius-full)',
          background: isRecorder ? 'var(--info-bg)' : 'var(--accent-dim)',
          color: isRecorder ? 'var(--info)' : 'var(--text-accent)',
          fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em',
        }}>
          {isRecorder ? 'FFmpeg' : 'Detector'}
        </span>
      </td>
      <td>
        <span style={{
          fontSize: 10, padding: '2px 7px', borderRadius: 'var(--radius-full)',
          fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em',
          background: isRunning ? 'var(--success-bg)' : 'var(--danger-bg)',
          color: isRunning ? 'var(--success)' : 'var(--danger)',
        }}>
          {sup.status}
        </span>
      </td>
      <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>
        {sup.pid ?? '—'}
      </td>
      <td>
        {sup.restarts > 0 ? (
          <span style={{ color: 'var(--warning)', fontSize: 12, fontWeight: 500 }}>
            {sup.restarts} restart{sup.restarts !== 1 ? 's' : ''}
          </span>
        ) : (
          <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
        )}
      </td>
    </tr>
  )
}

function CamerasCard({ supervisors }: { supervisors: SupervisorStatus[] }) {
  const allCam = supervisors.filter(s => s.name.startsWith('recorder') || s.name.startsWith('detector'))

  return (
    <div style={{
      background: 'var(--bg-surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-md)', overflow: 'hidden',
    }}>
      <div style={{
        padding: '14px 18px',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: 10,
      }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.07em', flex: 1 }}>
          Per-camera processes
        </span>
        {allCam.length > 0 && (
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            {allCam.filter(s => s.status === 'running').length} / {allCam.length} running
          </span>
        )}
      </div>

      {allCam.length === 0 ? (
        <div style={{ padding: '28px 20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
          No cameras configured — add cameras to see their process status here.
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead>
              <tr>
                <th>Camera</th>
                <th>Process</th>
                <th>Status</th>
                <th>PID</th>
                <th>Restarts</th>
              </tr>
            </thead>
            <tbody>
              {allCam.map(s => <ProcessRow key={s.name} sup={s} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Storage card ──────────────────────────────────────────────────────────────

function StorageCard({ storage }: { storage: StorageStatus }) {
  if (storage.error) {
    return (
      <div style={{
        background: 'var(--bg-surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-md)', padding: 20,
      }}>
        <div style={{ fontWeight: 600, marginBottom: 12, fontSize: 14, color: 'var(--text-primary)' }}>
          Storage (F:)
        </div>
        <div className="alert alert-error">{storage.error}</div>
      </div>
    )
  }

  const usedPct = 100 - storage.free_pct
  const meterColor = usedPct > 85 ? 'var(--danger)'
    : usedPct > 70 ? 'var(--warning)'
    : 'var(--success)'

  const retentionDays = storage.projected_retention_days
  const retentionColor = retentionDays != null && retentionDays < 3 ? 'var(--danger)'
    : retentionDays != null && retentionDays < 7 ? 'var(--warning)'
    : 'var(--text-primary)'

  return (
    <div style={{
      background: 'var(--bg-surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-md)', padding: 20,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.07em', flex: 1 }}>
          Storage (F:)
        </span>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 13, color: 'var(--text-secondary)' }}>
          {usedPct.toFixed(1)}% used
        </span>
      </div>

      {/* Disk usage bar */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6, fontSize: 12 }}>
          <span style={{ color: 'var(--text-secondary)' }}>
            {fmtBytes(storage.disk_used_bytes)} used
          </span>
          <span style={{ color: 'var(--text-muted)' }}>
            {fmtBytes(storage.disk_free_bytes)} free of {fmtBytes(storage.disk_total_bytes)}
          </span>
        </div>
        <MeterBar pct={usedPct} color={meterColor} />
      </div>

      {/* Stats grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 8 }}>
        <StatCard
          label="Recordings on disk"
          value={fmtBytes(storage.recordings_total_bytes)}
          sub={`${storage.segment_count} segment${storage.segment_count !== 1 ? 's' : ''}`}
          icon="💾"
        />
        <StatCard
          label="Write rate"
          value={storage.gb_per_day > 0 ? `${storage.gb_per_day.toFixed(1)} GB/day` : '—'}
          sub="24h rolling avg"
          icon="📈"
        />
        <StatCard
          label="Projected retention"
          value={retentionDays != null ? `${retentionDays.toFixed(1)} days` : 'Calculating…'}
          sub="until free-space floor"
          valueColor={retentionDays != null ? retentionColor : undefined}
          icon="📅"
        />
        <StatCard
          label="Retention config"
          value={`${storage.retention_max_age_days} days`}
          sub={`${storage.free_space_floor_pct}% free floor`}
          icon="⚙"
        />
      </div>
    </div>
  )
}

// ── HealthPage ────────────────────────────────────────────────────────────────

const REFRESH_INTERVAL = 10_000

function CameraHealthCard({ cameras }: { cameras: CameraHealth[] }) {
  function statusColor(s: string) {
    if (s === 'running') return 'var(--success, #16a34a)'
    if (s === 'stopped') return 'var(--text-muted)'
    return 'var(--danger)'
  }
  return (
    <div className="card" style={{ padding: 20 }}>
      <div style={{ fontWeight: 600, marginBottom: 14 }}>Camera Health</div>
      {cameras.length === 0 ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>No cameras configured</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                {['Camera', 'Mode', 'Recorder', 'Detector', 'Last Recording', 'Events/hr'].map(h => (
                  <th key={h} style={{ padding: '6px 10px', textAlign: 'left', color: 'var(--text-muted)', fontWeight: 500 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {cameras.map(cam => (
                <tr key={cam.id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                  <td style={{ padding: '8px 10px', fontWeight: 500 }}>
                    <span style={{ display: 'inline-block', width: 7, height: 7, borderRadius: '50%', background: cam.enabled ? 'var(--success, #16a34a)' : 'var(--text-muted)', marginRight: 6 }} />
                    {cam.name}
                  </td>
                  <td style={{ padding: '8px 10px', color: 'var(--text-muted)' }}>{cam.record_mode}</td>
                  <td style={{ padding: '8px 10px' }}>
                    <span style={{ color: statusColor(cam.recorder_status), fontWeight: 500 }}>{cam.recorder_status}</span>
                  </td>
                  <td style={{ padding: '8px 10px' }}>
                    <span style={{ color: statusColor(cam.detector_status), fontWeight: 500 }}>{cam.detector_status}</span>
                  </td>
                  <td style={{ padding: '8px 10px', color: 'var(--text-muted)' }}>
                    {cam.last_recording_at ? cam.last_recording_at.slice(0, 16).replace('T', ' ') : '—'}
                  </td>
                  <td style={{ padding: '8px 10px' }}>
                    <span style={{
                      padding: '1px 6px', borderRadius: 10, fontSize: 11, fontWeight: 600,
                      background: cam.events_last_hour > 0 ? 'rgba(239,68,68,0.15)' : 'var(--bg-elevated)',
                      color: cam.events_last_hour > 0 ? 'var(--danger)' : 'var(--text-muted)',
                    }}>{cam.events_last_hour}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export function HealthPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [cameraHealth, setCameraHealth] = useState<CameraHealth[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [countdown, setCountdown] = useState(10)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const countRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = () => {
    Promise.all([
      systemApi.health(),
      cameraHealthApi.list(),
    ]).then(([data, camHealth]) => {
      setHealth(data)
      setCameraHealth(camHealth.cameras)
      setError(null)
      setLastUpdated(new Date())
      setCountdown(10)
    })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    intervalRef.current = setInterval(load, REFRESH_INTERVAL)
    countRef.current = setInterval(() => setCountdown(c => Math.max(0, c - 1)), 1000)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
      if (countRef.current) clearInterval(countRef.current)
    }
  }, [])

  return (
    <div style={{ maxWidth: 900 }}>
      <PageHeader title="System Health">
        <button
          className="btn btn-secondary btn-sm"
          onClick={() => { setLoading(true); setCountdown(10); load() }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <polyline points="23 4 23 10 17 10"/>
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
          </svg>
          Refresh
        </button>
        <div style={{ flex: 1 }} />
        {lastUpdated && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            Updated {lastUpdated.toLocaleTimeString()} · next in {countdown}s
          </span>
        )}
      </PageHeader>

      {loading && !health && <LoadingState message="Loading health data…" />}

      {error && (
        <div className="alert alert-error" style={{ marginBottom: 12 }} role="alert">
          <span style={{ fontWeight: 700 }}>✕</span>
          {error}
        </div>
      )}

      {health && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <AppInfoCard health={health} />
          <CameraHealthCard cameras={cameraHealth} />
          <CamerasCard supervisors={health.supervisors ?? []} />
          <StorageCard storage={health.storage} />
        </div>
      )}
    </div>
  )
}
