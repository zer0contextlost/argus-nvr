/**
 * UsersPage — admin-only user management.
 * Create, edit role, reset password, delete users.
 */

import { useEffect, useState } from 'react'
import { usersApi, User } from '../api/client'
import { useToast } from '../components/ui/Toast'
import { PageHeader, SectionCard, LoadingState, ErrorAlert } from '../components/ui/Primitives'

const ROLES = ['admin', 'operator', 'viewer'] as const

export function UsersPage() {
  const toast = useToast()
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState<string>('viewer')
  const [creating, setCreating] = useState(false)

  // Edit/reset state keyed by user id
  const [editRole, setEditRole] = useState<Record<number, string>>({})
  const [resetPwd, setResetPwd] = useState<Record<number, string>>({})
  const [showReset, setShowReset] = useState<Record<number, boolean>>({})

  async function load() {
    setLoading(true)
    try {
      const list = await usersApi.list()
      setUsers(list)
      const roles: Record<number, string> = {}
      list.forEach(u => { roles[u.id] = u.role })
      setEditRole(roles)
    } catch (e: any) {
      setError(e.message || 'Failed to load users')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  async function handleCreate() {
    if (!newUsername.trim() || !newPassword) return
    setCreating(true)
    try {
      await usersApi.create({ username: newUsername.trim(), password: newPassword, role: newRole, must_change_password: true })
      toast.success('User created')
      setNewUsername(''); setNewPassword(''); setNewRole('viewer'); setShowCreate(false)
      load()
    } catch (e: any) {
      toast.error(e.message || 'Create failed')
    } finally {
      setCreating(false)
    }
  }

  async function handleUpdateRole(userId: number) {
    try {
      await usersApi.update(userId, { role: editRole[userId] })
      toast.success('Role updated')
      load()
    } catch (e: any) {
      toast.error(e.message || 'Update failed')
    }
  }

  async function handleResetPassword(userId: number) {
    const pwd = resetPwd[userId] || ''
    if (pwd.length < 8) { toast.error('Password must be ≥ 8 characters'); return }
    try {
      await usersApi.resetPassword(userId, pwd)
      toast.success('Password reset')
      setShowReset(s => ({ ...s, [userId]: false }))
      setResetPwd(s => ({ ...s, [userId]: '' }))
    } catch (e: any) {
      toast.error(e.message || 'Reset failed')
    }
  }

  async function handleDelete(userId: number, username: string) {
    if (!confirm(`Delete user "${username}"?`)) return
    try {
      await usersApi.delete(userId)
      toast.success('User deleted')
      load()
    } catch (e: any) {
      toast.error(e.message || 'Delete failed')
    }
  }

  if (loading) return <LoadingState />
  if (error) return <ErrorAlert message={error} />

  return (
    <div style={{ maxWidth: 800, margin: '0 auto' }}>
      <PageHeader title="Users">
        <button className="btn btn-primary btn-sm" onClick={() => setShowCreate(s => !s)}>
          {showCreate ? 'Cancel' : '+ Add User'}
        </button>
      </PageHeader>

      {showCreate && (
        <div style={{ marginBottom: 20 }}><SectionCard title="New User">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr auto', gap: 12, alignItems: 'end' }}>
            <div>
              <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Username</label>
              <input className="input" value={newUsername} onChange={e => setNewUsername(e.target.value)} placeholder="username" />
            </div>
            <div>
              <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Password</label>
              <input className="input" type="password" value={newPassword} onChange={e => setNewPassword(e.target.value)} placeholder="min 8 chars" />
            </div>
            <div>
              <label style={{ fontSize: 12, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>Role</label>
              <select className="input" value={newRole} onChange={e => setNewRole(e.target.value)}>
                {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
          </div>
          <button className="btn btn-primary btn-sm" style={{ marginTop: 12 }} onClick={handleCreate} disabled={creating}>
            {creating ? 'Creating…' : 'Create User'}
          </button>
        </SectionCard></div>
      )}

      <SectionCard title={`Users (${users.length})`}>
        {users.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 32 }}>No users found</div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)' }}>
                {['ID', 'Username', 'Role', 'Created', 'Must Change', 'Actions'].map(h => (
                  <th key={h} style={{ padding: '8px 12px', textAlign: 'left', color: 'var(--text-muted)', fontWeight: 500, fontSize: 11 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {users.map(user => (
                <>
                  <tr key={user.id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                    <td style={{ padding: '10px 12px', color: 'var(--text-muted)' }}>{user.id}</td>
                    <td style={{ padding: '10px 12px', fontWeight: 500 }}>{user.username}</td>
                    <td style={{ padding: '10px 12px' }}>
                      <select
                        className="input"
                        style={{ fontSize: 12, padding: '2px 6px', height: 'auto' }}
                        value={editRole[user.id] ?? user.role}
                        onChange={e => setEditRole(s => ({ ...s, [user.id]: e.target.value }))}
                      >
                        {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
                      </select>
                      {editRole[user.id] !== user.role && (
                        <button className="btn btn-primary btn-sm" style={{ marginLeft: 6, fontSize: 11 }} onClick={() => handleUpdateRole(user.id)}>Save</button>
                      )}
                    </td>
                    <td style={{ padding: '10px 12px', color: 'var(--text-muted)', fontSize: 12 }}>{user.created_at.slice(0, 10)}</td>
                    <td style={{ padding: '10px 12px' }}>
                      <span style={{
                        fontSize: 11, padding: '2px 7px', borderRadius: 12,
                        background: user.must_change_password ? 'var(--warning-bg, rgba(234,179,8,0.15))' : 'var(--success-bg, rgba(34,197,94,0.15))',
                        color: user.must_change_password ? 'var(--warning, #ca8a04)' : 'var(--success, #16a34a)',
                      }}>
                        {user.must_change_password ? 'Yes' : 'No'}
                      </span>
                    </td>
                    <td style={{ padding: '10px 12px' }}>
                      <button className="btn btn-ghost btn-sm" style={{ fontSize: 11 }} onClick={() => setShowReset(s => ({ ...s, [user.id]: !s[user.id] }))}>
                        Reset Pwd
                      </button>
                      <button className="btn btn-ghost btn-sm" style={{ fontSize: 11, color: 'var(--danger)', marginLeft: 4 }} onClick={() => handleDelete(user.id, user.username)}>
                        Delete
                      </button>
                    </td>
                  </tr>
                  {showReset[user.id] && (
                    <tr key={`reset-${user.id}`} style={{ borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-elevated)' }}>
                      <td colSpan={6} style={{ padding: '8px 12px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <input
                            className="input"
                            type="password"
                            placeholder="New password (min 8 chars)"
                            value={resetPwd[user.id] || ''}
                            onChange={e => setResetPwd(s => ({ ...s, [user.id]: e.target.value }))}
                            style={{ maxWidth: 240 }}
                          />
                          <button className="btn btn-primary btn-sm" onClick={() => handleResetPassword(user.id)}>Set Password</button>
                          <button className="btn btn-ghost btn-sm" onClick={() => setShowReset(s => ({ ...s, [user.id]: false }))}>Cancel</button>
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        )}
      </SectionCard>
    </div>
  )
}
