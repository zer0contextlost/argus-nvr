import { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { eventsApi, getToken } from '../api/client'
import { PageHeader } from '../components/ui/Primitives'

interface DashStats {
  cameras: { total: number; enabled: number; recording: number }
  events: { last_24h: number; last_7d: number; unreviewed: number }
  recent_events: Array<{
    id: number; camera_id: number; camera_name: string
    type: string; label: string | null
    started_at: string; ended_at: string | null; has_thumb: boolean
  }>
  storage: {
    disk_total_bytes: number; disk_used_bytes: number; disk_free_bytes: number
    free_pct: number; recordings_total_bytes: number; segment_count: number
    gb_per_day: number; projected_retention_days: number | null
    retention_max_age_days: number
  }
}

function withToken(url: string): string {
  const token = getToken()
  return token ? `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : url
}

function fmtBytes(b: number): string {
  if (b >= 1e12) return `${(b/1e12).toFixed(1)} TB`
  if (b >= 1e9)  return `${(b/1e9).toFixed(1)} GB`
  if (b >= 1e6)  return `${(b/1e6).toFixed(1)} MB`
  return `${b} B`
}

function fmtAgo(iso: string): string {
  const d = (Date.now() - new Date(iso).getTime()) / 1000
  if (d < 60)    return `${Math.round(d)}s ago`
  if (d < 3600)  return `${Math.round(d/60)}m ago`
  if (d < 86400) return `${Math.round(d/3600)}h ago`
  return `${Math.round(d/86400)}d ago`
}

function Stat({ label, value, sub, warn }: { label: string; value: string | number; sub?: string; warn?: boolean }) {
  return (
    <div style={{
      background: 'var(--bg-surface)',
      border: `1px solid ${warn ? 'rgba(239,68,68,0.4)' : 'var(--border)'}`,
      borderRadius: 'var(--radius-md)', padding: '14px 18px',
    }}>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 700, fontFamily: 'var(--font-mono)', color: warn ? 'var(--danger)' : 'var(--text-primary)', lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>{sub}</div>}
    </div>
  )
}

export function DashboardPage() {
  const navigate = useNavigate()
  const [data, setData] = useState<DashStats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/system/dashboard', {
        headers: { Authorization: `Bearer ${getToken() ?? ''}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
      setError(null)
    } catch (e) { setError(String(e)) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  if (loading && !data) return (
    <div style={{ display: 'flex', gap: 10, color: 'var(--text-muted)', padding: 40 }}>
      <span className="spinner spinner-sm" /> Loading…
    </div>
  )
  if (error && !data) return <div className="alert alert-error">{error}</div>
  if (!data) return null

  const s = data.storage
  const usedPct = s.disk_total_bytes > 0 ? s.disk_used_bytes / s.disk_total_bytes * 100 : 0
  const recPct  = s.disk_total_bytes > 0 ? s.recordings_total_bytes / s.disk_total_bytes * 100 : 0

  return (
    <div style={{ maxWidth: 1200 }}>
      <PageHeader title="Dashboard">
        <span style={{ fontSize: 12, color: 'var(--text-muted)', marginLeft: 'auto' }}>
          Updates every 30s
        </span>
      </PageHeader>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10, marginBottom: 16 }}>
        <Stat label="Cameras" value={data.cameras.total} sub={`${data.cameras.enabled} enabled`} />
        <Stat label="Recording" value={data.cameras.recording} sub="active" warn={data.cameras.recording === 0 && data.cameras.enabled > 0} />
        <Stat label="Events 24h" value={data.events.last_24h} />
        <Stat label="Events 7d" value={data.events.last_7d} />
        <Stat label="Unreviewed" value={data.events.unreviewed} warn={data.events.unreviewed > 0} sub="pending" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 280px', gap: 14, alignItems: 'start' }}>
        {/* Recent events list */}
        <div style={{ background: 'var(--bg-surface)', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)', overflow: 'hidden' }}>
          <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontWeight: 700, fontSize: 13 }}>Recent Events</span>
            <button className="btn btn-ghost btn-sm" onClick={() => navigate('/events')}>All events →</button>
          </div>
          {data.recent_events.length === 0 ? (
            <div style={{ padding: '32px 16px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>No events yet</div>
          ) : data.recent_events.map((e, i) => (
            <div
              key={e.id}
              onClick={() => navigate('/events')}
              style={{
                display: 'flex', gap: 10, alignItems: 'center', padding: '9px 14px',
                borderBottom: i < data.recent_events.length - 1 ? '1px solid var(--border-subtle)' : 'none',
                cursor: 'pointer',
              }}
              onMouseEnter={ev => { (ev.currentTarget as HTMLDivElement).style.background = 'var(--bg-elevated)' }}
              onMouseLeave={ev => { (ev.currentTarget as HTMLDivElement).style.background = 'transparent' }}
            >
              {e.has_thumb ? (
                <img src={withToken(eventsApi.thumbUrl(e.id))} alt="" style={{ width: 60, height: 40, objectFit: 'cover', borderRadius: 4, background: '#000', flexShrink: 0 }} onError={ev => { (ev.target as HTMLImageElement).style.visibility = 'hidden' }} />
              ) : (
                <div style={{ width: 60, height: 40, flexShrink: 0, background: 'var(--bg-base)', borderRadius: 4, border: '1px solid var(--border)' }} />
              )}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.camera_name}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{e.type}{e.label ? ` · ${e.label}` : ''}</div>
              </div>
              <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', flexShrink: 0 }}>{fmtAgo(e.started_at)}</span>
            </div>
          ))}
        </div>

        {/* Storage card */}
        <div style={{ background: 'var(--bg-surface)', border: '1px solid var(--border)', borderRadius: 'var(--radius-md)', padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 12 }}>
          <span style={{ fontWeight: 700, fontSize: 13 }}>Storage</span>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>
              <span>Disk used</span>
              <span style={{ fontFamily: 'var(--font-mono)' }}>{fmtBytes(s.disk_used_bytes)} / {fmtBytes(s.disk_total_bytes)}</span>
            </div>
            <div style={{ height: 6, background: 'var(--bg-elevated)', borderRadius: 3, overflow: 'hidden' }}>
              <div style={{ width: `${Math.min(usedPct, 100)}%`, height: '100%', background: usedPct > 85 ? 'var(--danger)' : usedPct > 70 ? 'var(--warning)' : 'var(--accent)', borderRadius: 3, transition: 'width 0.3s' }} />
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 3 }}>{s.free_pct.toFixed(1)}% free</div>
          </div>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-muted)', marginBottom: 4 }}>
              <span>Recordings</span>
              <span style={{ fontFamily: 'var(--font-mono)' }}>{fmtBytes(s.recordings_total_bytes)}</span>
            </div>
            <div style={{ height: 6, background: 'var(--bg-elevated)', borderRadius: 3, overflow: 'hidden' }}>
              <div style={{ width: `${Math.min(recPct, 100)}%`, height: '100%', background: 'var(--accent)', borderRadius: 3, transition: 'width 0.3s' }} />
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 3 }}>{s.segment_count.toLocaleString()} segments · {s.gb_per_day.toFixed(2)} GB/day</div>
          </div>
          <div style={{ borderTop: '1px solid var(--border)', paddingTop: 10, fontSize: 12, display: 'flex', flexDirection: 'column', gap: 5 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span style={{ color: 'var(--text-muted)' }}>Retention</span>
              <span style={{ fontFamily: 'var(--font-mono)' }}>{s.retention_max_age_days}d max</span>
            </div>
            {s.projected_retention_days != null && (
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <span style={{ color: 'var(--text-muted)' }}>Est. full in</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600, color: s.projected_retention_days < 3 ? 'var(--danger)' : s.projected_retention_days < 7 ? 'var(--warning)' : 'var(--success)' }}>
                  {s.projected_retention_days.toFixed(1)}d
                </span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
