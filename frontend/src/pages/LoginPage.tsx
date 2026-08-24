/**
 * LoginPage — Argus NVR authentication screen.
 */

import { useState, FormEvent } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { authApi, setToken, setMustChangePassword } from '../api/client'

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

export function LoginPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string })?.from ?? '/live'

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await authApi.login(username, password)
      setToken(res.access_token)
      setMustChangePassword(res.must_change_password ?? false)
      navigate(res.must_change_password ? '/change-password' : from, { replace: true })
    } catch {
      setError('Invalid username or password. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100dvh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: 'var(--bg-base)',
      padding: '16px',
      /* Subtle grid pattern */
      backgroundImage: `
        linear-gradient(var(--bg-base) 1px, transparent 1px),
        linear-gradient(90deg, var(--bg-base) 1px, transparent 1px)
      `,
      backgroundSize: '40px 40px',
      backgroundPosition: '-0.5px -0.5px',
    }}>
      {/* Background accent glow */}
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
        {/* Brand header */}
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
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
            Argus NVR
          </h1>
          <p style={{ fontSize: 13, color: 'var(--text-muted)' }}>
            Sign in to access your cameras
          </p>
        </div>

        <form onSubmit={handleSubmit} autoComplete="on" noValidate>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 6 }}>
                Username
              </label>
              <input
                type="text"
                value={username}
                onChange={e => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
                required
                placeholder="admin"
                style={{
                  width: '100%', padding: '10px 14px',
                  background: 'var(--bg-elevated)',
                  border: `1px solid ${error ? 'var(--danger)' : 'var(--border)'}`,
                  borderRadius: 'var(--radius)',
                  color: 'var(--text-primary)',
                  fontSize: 15,
                  transition: 'border-color var(--transition), box-shadow var(--transition)',
                }}
                onFocus={e => {
                  e.target.style.borderColor = 'var(--border-focus)'
                  e.target.style.boxShadow = '0 0 0 3px var(--accent-dim)'
                }}
                onBlur={e => {
                  e.target.style.borderColor = error ? 'var(--danger)' : 'var(--border)'
                  e.target.style.boxShadow = 'none'
                }}
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 6 }}>
                Password
              </label>
              <input
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                autoComplete="current-password"
                required
                placeholder="••••••••"
                style={{
                  width: '100%', padding: '10px 14px',
                  background: 'var(--bg-elevated)',
                  border: `1px solid ${error ? 'var(--danger)' : 'var(--border)'}`,
                  borderRadius: 'var(--radius)',
                  color: 'var(--text-primary)',
                  fontSize: 15,
                  transition: 'border-color var(--transition), box-shadow var(--transition)',
                }}
                onFocus={e => {
                  e.target.style.borderColor = 'var(--border-focus)'
                  e.target.style.boxShadow = '0 0 0 3px var(--accent-dim)'
                }}
                onBlur={e => {
                  e.target.style.borderColor = error ? 'var(--danger)' : 'var(--border)'
                  e.target.style.boxShadow = 'none'
                }}
              />
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
            disabled={loading || !username || !password}
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
                Signing in…
              </span>
            ) : 'Sign in'}
          </button>
        </form>

        <p style={{
          textAlign: 'center',
          marginTop: 20,
          fontSize: 11,
          color: 'var(--text-muted)',
        }}>
          Argus NVR · Self-hosted surveillance
        </p>
      </div>
    </div>
  )
}
