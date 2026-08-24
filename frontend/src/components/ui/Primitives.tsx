/**
 * Shared UI primitives for Argus NVR.
 * All components are lightweight, styled via CSS classes from theme.css.
 */

import React from 'react'

// ── Spinner ────────────────────────────────────────────────────────────────

export function Spinner({ size = 'md', className = '' }: { size?: 'sm' | 'md' | 'lg'; className?: string }) {
  const cls = size === 'sm' ? 'spinner spinner-sm' : size === 'lg' ? 'spinner spinner-lg' : 'spinner'
  return <div className={`${cls} ${className}`} role="status" aria-label="Loading" />
}

// ── Loading overlay ────────────────────────────────────────────────────────

export function LoadingState({ message = 'Loading…' }: { message?: string }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      gap: 12, padding: '60px 20px', color: 'var(--text-muted)',
      fontSize: 14,
    }}>
      <Spinner />
      <span>{message}</span>
    </div>
  )
}

// ── Empty state ────────────────────────────────────────────────────────────

interface EmptyStateProps {
  icon?: string
  title: string
  body?: string
  action?: React.ReactNode
}

export function EmptyState({ icon = '📭', title, body, action }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <div className="empty-state-icon">{icon}</div>
      <div className="empty-state-title">{title}</div>
      {body && <div className="empty-state-body">{body}</div>}
      {action && <div style={{ marginTop: 8 }}>{action}</div>}
    </div>
  )
}

// ── Status dot ────────────────────────────────────────────────────────────

type StatusType = 'running' | 'ok' | 'stopped' | 'idle' | 'error' | string

function resolveStatus(status: StatusType): 'running' | 'stopped' | 'error' {
  if (status === 'running' || status === 'ok') return 'running'
  if (status === 'stopped' || status === 'idle') return 'stopped'
  return 'error'
}

export function StatusDot({ status }: { status: StatusType }) {
  const s = resolveStatus(status)
  const cls = s === 'running' ? 'status-dot status-dot-running'
    : s === 'stopped' ? 'status-dot status-dot-stopped'
    : 'status-dot status-dot-error'
  return <span className={cls} aria-hidden="true" />
}

export function StatusBadge({ status }: { status: StatusType }) {
  const s = resolveStatus(status)
  const color = s === 'running' ? 'var(--success)' : s === 'stopped' ? 'var(--text-muted)' : 'var(--danger)'
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, color, fontWeight: 600, fontSize: 13 }}>
      <StatusDot status={status} />
      {status}
    </span>
  )
}

// ── Badge ──────────────────────────────────────────────────────────────────

type BadgeVariant = 'success' | 'warning' | 'danger' | 'info' | 'muted' | 'accent' | 'live'

export function Badge({ children, variant = 'muted' }: { children: React.ReactNode; variant?: BadgeVariant }) {
  return <span className={`badge badge-${variant}`}>{children}</span>
}

// ── Error alert ────────────────────────────────────────────────────────────

export function ErrorAlert({ message }: { message: string }) {
  return (
    <div className="alert alert-error" role="alert">
      <span style={{ fontWeight: 700, flexShrink: 0 }}>✕</span>
      <span>{message}</span>
    </div>
  )
}

export function SuccessAlert({ message }: { message: string }) {
  return (
    <div className="alert alert-success" role="status">
      <span style={{ fontWeight: 700, flexShrink: 0 }}>✓</span>
      <span>{message}</span>
    </div>
  )
}

// ── Page header ────────────────────────────────────────────────────────────

export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="page-header">
      <h1 className="page-title">{title}</h1>
      {children}
    </div>
  )
}

// ── Section card ───────────────────────────────────────────────────────────

export function SectionCard({
  title,
  children,
  action,
  className = '',
}: {
  title?: string
  children: React.ReactNode
  action?: React.ReactNode
  className?: string
}) {
  return (
    <div className={`card ${className}`} style={{ marginBottom: 16 }}>
      {(title || action) && (
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          marginBottom: title ? 16 : 0,
        }}>
          {title && <div className="section-title">{title}</div>}
          {action}
        </div>
      )}
      {children}
    </div>
  )
}

// ── Form field ─────────────────────────────────────────────────────────────

export function FormField({
  label,
  help,
  children,
  required,
}: {
  label: string
  help?: string
  children: React.ReactNode
  required?: boolean
}) {
  return (
    <div>
      <label className="form-label">
        {label}
        {required && <span style={{ color: 'var(--danger)', marginLeft: 3 }}>*</span>}
      </label>
      {children}
      {help && <div className="form-help">{help}</div>}
    </div>
  )
}

// ── Storage meter ──────────────────────────────────────────────────────────

export function MeterBar({ pct, color }: { pct: number; color: string }) {
  return (
    <div className="meter-track" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <div className="meter-fill" style={{ width: `${Math.min(100, Math.max(0, pct))}%`, background: color }} />
    </div>
  )
}

// ── Skeleton block ─────────────────────────────────────────────────────────

export function Skeleton({ width = '100%', height = 16, style }: { width?: string | number; height?: number; style?: React.CSSProperties }) {
  return (
    <div
      className="skeleton"
      style={{ width, height, ...style }}
      aria-hidden="true"
    />
  )
}

// ── Section divider ────────────────────────────────────────────────────────

export function Divider() {
  return <hr className="divider" />
}
