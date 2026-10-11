/**
 * ChangePasswordPage — forced first-login password change.
 * Shown when the backend returns must_change_password=true (fresh install default).
 */

import { useState, FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { authApi, setMustChangePassword, setToken } from '../api/client'

function ShieldLogo() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 2L4 6v6c0 5.25 3.5 10.15 8 11.35C16.5 22.15 20 17.25 20 12V6L12 2z"
        fill="var(--accent)"
      />
      <path
        d="M9 12l2 2 4-4"
        stroke="white"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function ChangePasswordPage() {
  const navigate = useNavigate()

  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const mismatch = next.length > 0 && confirm.length > 0 && next !== confirm
  const tooShort = next.length > 0 && next.length < 8
  const canSubmit = current && next && confirm && !mismatch && !tooShort

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!canSubmit) return
    setError('')
    setLoading(true)
    try {
      const res = await authApi.changePassword(current, next)
      // The pre-change token is only accepted by /auth/*; swap in the new one.
      if (res.access_token) setToken(res.access_token)
      setMustChangePassword(false)
      navigate('/live', { replace: true })
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      if (msg.includes('400') || msg.includes('incorrect')) {
        setError('Current password is incorrect.')
      } else {
        setError('Password change failed. Please try again.')
      }
    } finally {
      setLoading(false)
    }
  }

  const inputStyle = (hasError: boolean): React.CSSProperties => ({
    width: '100%',
    padding: '10px 14px',
    background: 'var(--bg-elevated)',
    border: `1px solid ${hasError ? 'var(--danger)' : 'var(--border)'}`,
    borderRadius: 'var(--radius)',
    color: 'var(--text-primary)',
    fontSize: 15,
    transition: 'border-color var(--transition), box-shadow var(--transition)',
  })

  return (
    <div style={{
      minHeight: '100dvh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'var(--bg-base)',
      padding: '16px',
      backgroundImage: `
        linear-gradient(var(--bg-base) 1px, transparent 1px),
        linear-gradient(90deg, var(--bg-base) 1px, transparent 1px)
      `,
      backgroundSize: '40px 40px',
      backgroundPosition: '-0.5px -0.5px',
    }}>
      <div style={{
        position: 'fixed', top: '30%', left: '50%',
        transform: 'translate(-50%, -50%)',
        width: 400, height: 400,
        background: 'radial-gradient(circle, rgba(59,130,246,0.08) 0%, transparent 70%)',
        pointerEvents: 'none',
      }} />

      <div style={{
        background: 'var(--bg-surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-xl)',
        padding: '40px 36px 36px',
        width: '100%',
        maxWidth: '380px',
        boxShadow: 'var(--shadow-xl)',
        position: 'relative',
        zIndex: 1,
      }}>
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 14 }}>
            <div style={{
              width: 72, height: 72,
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border)',
              borderRadius: 'var(--radius-xl)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <ShieldLogo />
            </div>
          </div>
          <h1 style={{
            fontSize: 22, fontWeight: 700,
            color: 'var(--text-primary)',
            letterSpacing: '-0.02em',
            marginBottom: 6,
          }}>
            Set Your Password
          </h1>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.5 }}>
            You're using the default password. Set a new password before continuing.
          </p>
        </div>

        <form onSubmit={handleSubmit} autoComplete="off" noValidate>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 6 }}>
                Current password
              </label>
              <input
                type="password"
                value={current}
                onChange={e => setCurrent(e.target.value)}
                required
                placeholder="Argus123!"
                style={inputStyle(!!error)}
                onFocus={e => { e.target.style.borderColor = 'var(--border-focus)'; e.target.style.boxShadow = '0 0 0 3px var(--accent-dim)' }}
                onBlur={e => { e.target.style.borderColor = error ? 'var(--danger)' : 'var(--border)'; e.target.style.boxShadow = 'none' }}
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 6 }}>
                New password
              </label>
              <input
                type="password"
                value={next}
                onChange={e => setNext(e.target.value)}
                required
                placeholder="At least 8 characters"
                style={inputStyle(tooShort)}
                onFocus={e => { e.target.style.borderColor = 'var(--border-focus)'; e.target.style.boxShadow = '0 0 0 3px var(--accent-dim)' }}
                onBlur={e => { e.target.style.borderColor = tooShort ? 'var(--danger)' : 'var(--border)'; e.target.style.boxShadow = 'none' }}
              />
              {tooShort && (
                <p style={{ fontSize: 11, color: 'var(--danger)', marginTop: 4 }}>
                  Must be at least 8 characters.
                </p>
              )}
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 6 }}>
                Confirm new password
              </label>
              <input
                type="password"
                value={confirm}
                onChange={e => setConfirm(e.target.value)}
                required
                placeholder="••••••••"
                style={inputStyle(mismatch)}
                onFocus={e => { e.target.style.borderColor = 'var(--border-focus)'; e.target.style.boxShadow = '0 0 0 3px var(--accent-dim)' }}
                onBlur={e => { e.target.style.borderColor = mismatch ? 'var(--danger)' : 'var(--border)'; e.target.style.boxShadow = 'none' }}
              />
              {mismatch && (
                <p style={{ fontSize: 11, color: 'var(--danger)', marginTop: 4 }}>
                  Passwords do not match.
                </p>
              )}
            </div>
          </div>

          {error && (
            <div style={{
              marginTop: 14,
              padding: '10px 14px',
              background: 'var(--danger-bg)',
              border: '1px solid rgba(239,68,68,0.25)',
              borderRadius: 'var(--radius)',
              color: '#fca5a5',
              fontSize: 13,
              display: 'flex', alignItems: 'center', gap: 8,
            }} role="alert">
              <span style={{ fontWeight: 700 }}>✕</span>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading || !canSubmit}
            className="btn btn-primary btn-lg"
            style={{
              marginTop: 20,
              width: '100%',
              borderRadius: 'var(--radius)',
              fontSize: 15,
              letterSpacing: '0.01em',
            }}
          >
            {loading ? (
              <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span className="spinner spinner-sm" style={{ borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3)' }} />
                Saving…
              </span>
            ) : 'Set password and continue'}
          </button>
        </form>

        <p style={{ textAlign: 'center', marginTop: 20, fontSize: 11, color: 'var(--text-muted)' }}>
          Argus NVR · Self-hosted surveillance
        </p>
      </div>
    </div>
  )
}
