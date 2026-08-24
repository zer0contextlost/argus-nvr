import React, { useEffect, useState } from 'react'
import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { authApi, clearToken, eventsApi, CurrentUser } from '../api/client'

// ── Nav items with icons ──────────────────────────────────────────────────────

const navItems = [
  { to: '/dashboard', label: 'Dashboard', icon: '◈' },
  { to: '/live',     label: 'Live',     icon: '⬤' },
  { to: '/cameras',  label: 'Cameras',  icon: '⬡' },
  { to: '/playback', label: 'Playback', icon: '▶' },
  { to: '/search',   label: 'Search',   icon: '⌕' },
  { to: '/events',   label: 'Events',   icon: '⊡' },
  { to: '/persons',  label: 'Persons',  icon: '◉' },
  { to: '/users',    label: 'Users',    icon: '⊕' },
  { to: '/audit',    label: 'Audit',    icon: '≡' },
  { to: '/settings', label: 'Settings', icon: '⚙' },
  { to: '/health',   label: 'Health',   icon: '◎' },
]

// ── SVG icons (inline, no deps) ───────────────────────────────────────────────

function IconDashboard() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
      <rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>
    </svg>
  )
}

function IconLive() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M6.3 6.3a8 8 0 0 0 0 11.4"/>
      <path d="M17.7 6.3a8 8 0 0 1 0 11.4"/>
      <path d="M3.7 3.7a12 12 0 0 0 0 16.6"/>
      <path d="M20.3 3.7a12 12 0 0 1 0 16.6"/>
    </svg>
  )
}

function IconCamera() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M23 7 16 12 23 17V7z"/>
      <rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
    </svg>
  )
}

function IconPlayback() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="5 3 19 12 5 21 5 3"/>
    </svg>
  )
}

function IconEvents() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
      <polyline points="9 22 9 12 15 12 15 22"/>
    </svg>
  )
}

function IconSettings() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </svg>
  )
}

function IconHealth() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
    </svg>
  )
}

function IconPersons() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
      <circle cx="12" cy="7" r="4"/>
    </svg>
  )
}

function IconLogout() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
      <polyline points="16 17 21 12 16 7"/>
      <line x1="21" y1="12" x2="9" y2="12"/>
    </svg>
  )
}

function IconSearch() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  )
}

function IconUsers() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>
      <path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
    </svg>
  )
}

function IconAudit() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
      <polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>
      <line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>
    </svg>
  )
}

const navIcons: Record<string, React.ReactNode> = {
  '/dashboard': <IconDashboard />,
  '/live':     <IconLive />,
  '/cameras':  <IconCamera />,
  '/playback': <IconPlayback />,
  '/search':   <IconSearch />,
  '/events':   <IconEvents />,
  '/persons':  <IconPersons />,
  '/users':    <IconUsers />,
  '/audit':    <IconAudit />,
  '/settings': <IconSettings />,
  '/health':   <IconHealth />,
}

// ── Argus shield logo SVG ──────────────────────────────────────────────────

function ArgusLogo() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 2L4 6v6c0 5.25 3.5 10.15 8 11.35C16.5 22.15 20 17.25 20 12V6L12 2z"
        fill="var(--accent)"
        opacity="0.9"
      />
      <path
        d="M9 12l2 2 4-4"
        stroke="white"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

// ── Hamburger icon ────────────────────────────────────────────────────────────

function HamburgerIcon({ open }: { open: boolean }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="var(--text-secondary)" strokeWidth="1.8" strokeLinecap="round">
      {open ? (
        <>
          <line x1="3" y1="3" x2="15" y2="15" />
          <line x1="15" y1="3" x2="3" y2="15" />
        </>
      ) : (
        <>
          <line x1="2" y1="5" x2="16" y2="5" />
          <line x1="2" y1="9" x2="16" y2="9" />
          <line x1="2" y1="13" x2="16" y2="13" />
        </>
      )}
    </svg>
  )
}

// ── Shell ─────────────────────────────────────────────────────────────────────

export function Shell() {
  const navigate = useNavigate()
  const [menuOpen, setMenuOpen] = useState(false)
  const [unreviewedCount, setUnreviewedCount] = useState(0)
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null)

  useEffect(() => {
    authApi.me().then(setCurrentUser).catch(() => {})
  }, [])

  useEffect(() => {
    const fetchCount = () => {
      eventsApi.unreviewedCount().then(r => setUnreviewedCount(r.count)).catch(() => {})
    }
    fetchCount()
    const interval = setInterval(fetchCount, 60000)
    return () => clearInterval(interval)
  }, [])

  const handleLogout = async () => {
    try { await authApi.logout() } catch { /* ignore */ }
    clearToken()
    navigate('/login', { replace: true })
  }

  const closeMenu = () => setMenuOpen(false)

  const isAdmin = currentUser?.role === 'admin'
  const visibleNavItems = navItems.filter(item =>
    (item.to !== '/users' && item.to !== '/audit') || isAdmin
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh', background: 'var(--bg-base)' }}>

      {/* ── Top nav bar ──────────────────────────────────────────── */}
      <nav style={{
        display: 'flex',
        alignItems: 'center',
        padding: '0 16px',
        background: 'var(--bg-surface)',
        borderBottom: '1px solid var(--border)',
        height: '52px',
        flexShrink: 0,
        position: 'relative',
        zIndex: 100,
        gap: 4,
      }}>

        {/* Brand */}
        <a
          href="/"
          style={{
            display: 'flex', alignItems: 'center', gap: 8,
            textDecoration: 'none',
            marginRight: 16,
            flexShrink: 0,
          }}
        >
          <ArgusLogo />
          <span style={{
            fontWeight: 700,
            fontSize: 15,
            color: 'var(--text-primary)',
            letterSpacing: '-0.01em',
            whiteSpace: 'nowrap',
          }}>
            Argus <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>NVR</span>
          </span>
        </a>

        {/* Desktop nav */}
        <div className="desktop-nav" style={{ display: 'flex', gap: 2, flex: 1 }}>
          {visibleNavItems.map(item => (
            <NavLink
              key={item.to}
              to={item.to}
              style={({ isActive }) => ({
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '6px 12px',
                borderRadius: 8,
                color: isActive ? 'var(--text-primary)' : 'var(--text-muted)',
                textDecoration: 'none',
                fontSize: 13,
                fontWeight: isActive ? 600 : 400,
                background: isActive ? 'var(--bg-elevated)' : 'transparent',
                borderBottom: isActive ? '2px solid var(--accent)' : '2px solid transparent',
                transition: 'all 150ms ease',
                whiteSpace: 'nowrap',
              })}
            >
              <span style={{ opacity: 0.8 }}>{navIcons[item.to]}</span>
              {item.label}
              {item.to === '/events' && unreviewedCount > 0 && (
                <span style={{
                  minWidth: 16, height: 16, borderRadius: 999,
                  background: 'var(--danger)',
                  color: '#fff',
                  fontSize: 10, fontWeight: 700,
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  padding: '0 4px',
                  lineHeight: 1,
                }}>
                  {unreviewedCount > 99 ? '99+' : unreviewedCount}
                </span>
              )}
            </NavLink>
          ))}
        </div>

        {/* Spacer */}
        <div style={{ flex: 1 }} className="desktop-spacer" />

        {/* Live indicator */}
        <div className="live-indicator desktop-nav" style={{ marginRight: 12 }}>
          <span className="live-dot" />
          <span>Live</span>
        </div>

        {/* Sign out */}
        <button
          onClick={handleLogout}
          className="desktop-signout btn btn-ghost btn-sm"
          style={{ gap: 5 }}
        >
          <IconLogout />
          Sign out
        </button>

        {/* Hamburger (mobile) */}
        <button
          className="mobile-menu-btn"
          onClick={() => setMenuOpen(o => !o)}
          aria-label="Toggle menu"
          aria-expanded={menuOpen}
          style={{
            background: 'none', border: 'none', padding: '8px',
            cursor: 'pointer', marginLeft: 'auto',
            display: 'none',
            borderRadius: 6,
          }}
        >
          <HamburgerIcon open={menuOpen} />
        </button>
      </nav>

      {/* ── Mobile slide-down menu ────────────────────────────────── */}
      {menuOpen && (
        <div
          className="mobile-menu"
          style={{
            background: 'var(--bg-surface)',
            borderBottom: '1px solid var(--border)',
            padding: '8px 12px 16px',
            display: 'flex',
            flexDirection: 'column',
            gap: 2,
            zIndex: 99,
            boxShadow: 'var(--shadow-md)',
          }}
        >
          {visibleNavItems.map(item => (
            <NavLink
              key={item.to}
              to={item.to}
              onClick={closeMenu}
              style={({ isActive }) => ({
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '11px 14px', borderRadius: 8,
                color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)',
                textDecoration: 'none',
                fontSize: 15,
                fontWeight: isActive ? 600 : 400,
                background: isActive ? 'var(--bg-elevated)' : 'transparent',
                transition: 'background 120ms ease',
              })}
            >
              <span style={{ opacity: 0.75 }}>{navIcons[item.to]}</span>
              {item.label}
              {item.to === '/events' && unreviewedCount > 0 && (
                <span style={{
                  minWidth: 18, height: 18, borderRadius: 999,
                  background: 'var(--danger)',
                  color: '#fff',
                  fontSize: 11, fontWeight: 700,
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  padding: '0 5px',
                }}>
                  {unreviewedCount > 99 ? '99+' : unreviewedCount}
                </span>
              )}
            </NavLink>
          ))}
          <div style={{ borderTop: '1px solid var(--border)', margin: '8px 0' }} />
          <button
            onClick={() => { closeMenu(); handleLogout() }}
            style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '11px 14px', borderRadius: 8,
              background: 'transparent', border: 'none',
              color: 'var(--text-muted)', cursor: 'pointer',
              fontSize: 15, fontWeight: 400, textAlign: 'left',
            }}
          >
            <IconLogout />
            Sign out
          </button>
        </div>
      )}

      {/* ── Main content ──────────────────────────────────────────── */}
      <main style={{
        flex: 1,
        overflow: 'auto',
        padding: '20px 20px',
        paddingLeft: 'max(20px, env(safe-area-inset-left))',
        paddingRight: 'max(20px, env(safe-area-inset-right))',
        paddingBottom: 'max(20px, env(safe-area-inset-bottom))',
      }}>
        <Outlet />
      </main>

      <style>{`
        @media (max-width: 640px) {
          .desktop-nav    { display: none !important; }
          .desktop-spacer { display: none !important; }
          .desktop-signout { display: none !important; }
          .mobile-menu-btn { display: flex !important; }
        }
        @media (min-width: 641px) {
          .mobile-menu { display: none !important; }
        }
      `}</style>
    </div>
  )
}
