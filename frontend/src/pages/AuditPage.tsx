/**
 * AuditPage — admin-only audit log viewer.
 */

import { useEffect, useState } from 'react'
import { auditApi, AuditEntry } from '../api/client'
import { useToast } from '../components/ui/Toast'
import { PageHeader, SectionCard, LoadingState, ErrorAlert } from '../components/ui/Primitives'

export function AuditPage() {
  const toast = useToast()
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [filterAction, setFilterAction] = useState('')
  const [filterUser, setFilterUser] = useState('')
  const [page, setPage] = useState(0)
  const PAGE_SIZE = 100

  async function load(p = page) {
    setLoading(true)
    try {
      const res = await auditApi.list({
        limit: PAGE_SIZE,
        offset: p * PAGE_SIZE,
        action: filterAction || undefined,
        username: filterUser || undefined,
      })
      setEntries(res.entries)
      setTotal(res.total)
    } catch (e: any) {
      setError(e.message || 'Failed to load audit log')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(0); setPage(0) }, [filterAction, filterUser])

  async function handlePurge() {
    if (!confirm('Purge all audit log entries? This cannot be undone.')) return
    try {
      await auditApi.purge()
      toast.success('Audit log purged')
      load(0)
    } catch (e: any) {
      toast.error(e.message || 'Purge failed')
    }
  }

  function actionColor(action: string) {
    if (action.includes('delete')) return 'var(--danger)'
    if (action.includes('login')) return 'var(--accent)'
    if (action.includes('create')) return 'var(--success, #16a34a)'
    return 'var(--text-secondary)'
  }

  return (
    <div style={{ maxWidth: 1000, margin: '0 auto' }}>
      <PageHeader title="Audit Log">
        <button className="btn btn-ghost btn-sm" style={{ color: 'var(--danger)' }} onClick={handlePurge}>
          Purge All
        </button>
      </PageHeader>

      <SectionCard title="Filters">
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <input className="input" placeholder="Filter by action…" value={filterAction} onChange={e => setFilterAction(e.target.value)} style={{ maxWidth: 220 }} />
          <input className="input" placeholder="Filter by username…" value={filterUser} onChange={e => setFilterUser(e.target.value)} style={{ maxWidth: 200 }} />
        </div>
      </SectionCard>

      {loading ? <LoadingState /> : error ? <ErrorAlert message={error} /> : (
        <SectionCard title={`Entries (${entries.length} of ${total})`}>
          {entries.length === 0 ? (
            <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 40 }}>No audit entries found</div>
          ) : (
            <>
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)' }}>
                      {['Time', 'User', 'Action', 'Target', 'Detail', 'IP'].map(h => (
                        <th key={h} style={{ padding: '6px 10px', textAlign: 'left', color: 'var(--text-muted)', fontWeight: 500 }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map(e => (
                      <tr key={e.id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                        <td style={{ padding: '7px 10px', whiteSpace: 'nowrap', color: 'var(--text-muted)' }}>{e.created_at.slice(0, 19).replace('T', ' ')}</td>
                        <td style={{ padding: '7px 10px', fontWeight: 500 }}>{e.username || '—'}</td>
                        <td style={{ padding: '7px 10px' }}>
                          <span style={{ color: actionColor(e.action), fontWeight: 500 }}>{e.action}</span>
                        </td>
                        <td style={{ padding: '7px 10px', color: 'var(--text-muted)' }}>
                          {e.target_type ? `${e.target_type}#${e.target_id}` : '—'}
                        </td>
                        <td style={{ padding: '7px 10px', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {e.detail || '—'}
                        </td>
                        <td style={{ padding: '7px 10px', color: 'var(--text-muted)', fontFamily: 'monospace', fontSize: 11 }}>{e.ip || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 12, fontSize: 12, color: 'var(--text-muted)' }}>
                <span>Page {page + 1} of {Math.ceil(total / PAGE_SIZE) || 1}</span>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button className="btn btn-ghost btn-sm" disabled={page === 0} onClick={() => { setPage(p => p - 1); load(page - 1) }}>← Prev</button>
                  <button className="btn btn-ghost btn-sm" disabled={(page + 1) * PAGE_SIZE >= total} onClick={() => { setPage(p => p + 1); load(page + 1) }}>Next →</button>
                </div>
              </div>
            </>
          )}
        </SectionCard>
      )}
    </div>
  )
}
