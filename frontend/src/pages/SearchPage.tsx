/**
 * SearchPage — smart search across recordings + events.
 */

import { useState, useEffect } from 'react'
import { searchApi, SearchResult, camerasApi, Camera } from '../api/client'
import { PageHeader, SectionCard, LoadingState } from '../components/ui/Primitives'

function fmtDate(s: string) {
  return s ? s.slice(0, 19).replace('T', ' ') : '—'
}

function ScoreBadge({ score }: { score: number | null }) {
  if (score == null) return null
  const pct = Math.round(score * 100)
  return (
    <span style={{
      fontSize: 10, padding: '1px 5px', borderRadius: 10,
      background: 'var(--bg-elevated)',
      color: pct > 80 ? 'var(--danger)' : pct > 50 ? 'var(--accent)' : 'var(--text-muted)',
      fontWeight: 600,
    }}>
      {pct}%
    </span>
  )
}

export function SearchPage() {
  const [cameras, setCameras] = useState<Camera[]>([])
  const [results, setResults] = useState<SearchResult[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [searched, setSearched] = useState(false)

  // Filters
  const [q, setQ] = useState('')
  const [cameraId, setCameraId] = useState<number | ''>('')
  const [eventType, setEventType] = useState('')
  const [startedAfter, setStartedAfter] = useState('')
  const [startedBefore, setStartedBefore] = useState('')
  const [minScore, setMinScore] = useState('')
  const [hasRecording, setHasRecording] = useState<string>('')
  const [page, setPage] = useState(0)
  const PAGE_SIZE = 50

  useEffect(() => {
    camerasApi.list().then(setCameras).catch(() => {})
  }, [])

  async function doSearch(p = 0) {
    setLoading(true)
    setPage(p)
    try {
      const res = await searchApi.search({
        q: q || undefined,
        camera_id: cameraId !== '' ? Number(cameraId) : undefined,
        event_type: eventType || undefined,
        started_after: startedAfter || undefined,
        started_before: startedBefore || undefined,
        min_score: minScore ? parseFloat(minScore) : undefined,
        has_recording: hasRecording === 'yes' ? true : hasRecording === 'no' ? false : undefined,
        limit: PAGE_SIZE,
        offset: p * PAGE_SIZE,
      })
      setResults(res.results)
      setTotal(res.total)
      setSearched(true)
    } catch (e: any) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto' }}>
      <PageHeader title="Smart Search" />

      <SectionCard title="Filters">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12 }}>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Keyword</label>
            <input className="input" placeholder="Label or camera name…" value={q} onChange={e => setQ(e.target.value)} onKeyDown={e => e.key === 'Enter' && doSearch(0)} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Camera</label>
            <select className="input" value={cameraId} onChange={e => setCameraId(e.target.value === '' ? '' : Number(e.target.value))}>
              <option value="">All cameras</option>
              {cameras.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Event type</label>
            <select className="input" value={eventType} onChange={e => setEventType(e.target.value)}>
              <option value="">All types</option>
              <option value="motion">Motion</option>
              <option value="object">Object</option>
            </select>
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>After</label>
            <input className="input" type="datetime-local" value={startedAfter} onChange={e => setStartedAfter(e.target.value ? new Date(e.target.value).toISOString() : '')} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Before</label>
            <input className="input" type="datetime-local" value={startedBefore} onChange={e => setStartedBefore(e.target.value ? new Date(e.target.value).toISOString() : '')} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Min score (0–1)</label>
            <input className="input" type="number" min={0} max={1} step={0.05} placeholder="0.5" value={minScore} onChange={e => setMinScore(e.target.value)} />
          </div>
          <div>
            <label style={{ fontSize: 11, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Has recording?</label>
            <select className="input" value={hasRecording} onChange={e => setHasRecording(e.target.value)}>
              <option value="">Either</option>
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
          </div>
        </div>
        <button className="btn btn-primary" style={{ marginTop: 14 }} onClick={() => doSearch(0)} disabled={loading}>
          {loading ? 'Searching…' : 'Search'}
        </button>
      </SectionCard>

      {loading ? <LoadingState /> : searched && (
        <SectionCard title={`Results (${total} total)`}>
          {results.length === 0 ? (
            <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 40 }}>No results found</div>
          ) : (
            <>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {results.map(r => (
                  <div key={r.id} style={{
                    display: 'flex', alignItems: 'center', gap: 12,
                    padding: '10px 14px', borderRadius: 8,
                    background: 'var(--bg-elevated)',
                    border: '1px solid var(--border-subtle)',
                  }}>
                    {r.thumb_url ? (
                      <img src={r.thumb_url} alt="" style={{ width: 80, height: 56, objectFit: 'cover', borderRadius: 4, flexShrink: 0 }} />
                    ) : (
                      <div style={{ width: 80, height: 56, background: 'var(--bg-surface)', borderRadius: 4, flexShrink: 0 }} />
                    )}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                        <span style={{ fontWeight: 600, fontSize: 13 }}>{r.camera_name}</span>
                        <span style={{
                          fontSize: 10, padding: '1px 6px', borderRadius: 10,
                          background: r.type === 'motion' ? 'rgba(59,130,246,0.15)' : 'rgba(239,68,68,0.15)',
                          color: r.type === 'motion' ? '#3b82f6' : '#ef4444',
                          fontWeight: 600,
                        }}>{r.type}</span>
                        {r.label && <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{r.label}</span>}
                        <ScoreBadge score={r.score} />
                        {r.reviewed_at && <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>✓ reviewed</span>}
                      </div>
                      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                        {fmtDate(r.started_at)}
                        {r.rec_started_at && <span style={{ marginLeft: 8 }}>Recording: {fmtDate(r.rec_started_at)}</span>}
                      </div>
                    </div>
                    {r.recording_id && (
                      <a
                        href={`/playback?camera_id=${r.camera_id}&t=${encodeURIComponent(r.started_at)}`}
                        className="btn btn-ghost btn-sm"
                        style={{ flexShrink: 0, fontSize: 11 }}
                      >
                        View ▶
                      </a>
                    )}
                  </div>
                ))}
              </div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 16, fontSize: 12, color: 'var(--text-muted)' }}>
                <span>Page {page + 1} of {Math.ceil(total / PAGE_SIZE) || 1}</span>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button className="btn btn-ghost btn-sm" disabled={page === 0} onClick={() => doSearch(page - 1)}>← Prev</button>
                  <button className="btn btn-ghost btn-sm" disabled={(page + 1) * PAGE_SIZE >= total} onClick={() => doSearch(page + 1)}>Next →</button>
                </div>
              </div>
            </>
          )}
        </SectionCard>
      )}
    </div>
  )
}
