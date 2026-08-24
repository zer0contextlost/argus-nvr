/**
 * Lightweight toast notification system.
 * Usage: import { toast } from './Toast'; toast.success('Saved!')
 */

import React, { createContext, useCallback, useContext, useEffect, useState } from 'react'
import ReactDOM from 'react-dom'

// ── Types ─────────────────────────────────────────────────────────────────────

type ToastVariant = 'success' | 'error' | 'warning' | 'info'

interface ToastItem {
  id: number
  message: string
  variant: ToastVariant
}

// ── Context ───────────────────────────────────────────────────────────────────

interface ToastCtx {
  show: (message: string, variant?: ToastVariant, duration?: number) => void
}

const Ctx = createContext<ToastCtx>({ show: () => {} })

// ── Icon map ─────────────────────────────────────────────────────────────────

const icons: Record<ToastVariant, string> = {
  success: '✓',
  error:   '✕',
  warning: '⚠',
  info:    'ℹ',
}

const variantColors: Record<ToastVariant, string> = {
  success: 'var(--success)',
  error:   'var(--danger)',
  warning: 'var(--warning)',
  info:    'var(--accent)',
}

// ── Single toast ─────────────────────────────────────────────────────────────

function ToastItem({ item, onRemove }: { item: ToastItem; onRemove: (id: number) => void }) {
  return (
    <div className={`toast toast-${item.variant}`} role="status" aria-live="polite">
      <span style={{
        width: 20, height: 20, borderRadius: '50%',
        background: variantColors[item.variant],
        color: '#fff',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 11, fontWeight: 700, flexShrink: 0,
      }}>
        {icons[item.variant]}
      </span>
      <span style={{ flex: 1 }}>{item.message}</span>
      <button
        onClick={() => onRemove(item.id)}
        style={{
          background: 'none', border: 'none', color: 'var(--text-muted)',
          cursor: 'pointer', padding: '2px 4px', fontSize: 14, lineHeight: 1,
        }}
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>
  )
}

// ── Provider ──────────────────────────────────────────────────────────────────

let counter = 0

// Global ref so toast() works outside React tree
let _show: ((msg: string, variant?: ToastVariant, duration?: number) => void) | null = null

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])

  const show = useCallback((message: string, variant: ToastVariant = 'info', duration = 3500) => {
    const id = ++counter
    setItems(prev => [...prev, { id, message, variant }])
    setTimeout(() => setItems(prev => prev.filter(t => t.id !== id)), duration)
  }, [])

  const remove = useCallback((id: number) => {
    setItems(prev => prev.filter(t => t.id !== id))
  }, [])

  // Register global
  useEffect(() => { _show = show; return () => { _show = null } }, [show])

  const toastRoot = document.getElementById('toast-root')

  return (
    <Ctx.Provider value={{ show }}>
      {children}
      {toastRoot && ReactDOM.createPortal(
        items.map(item => (
          <ToastItem key={item.id} item={item} onRemove={remove} />
        )),
        toastRoot,
      )}
    </Ctx.Provider>
  )
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useToast() {
  const ctx = useContext(Ctx)
  return {
    success: (msg: string, duration?: number) => ctx.show(msg, 'success', duration),
    error:   (msg: string, duration?: number) => ctx.show(msg, 'error',   duration),
    warning: (msg: string, duration?: number) => ctx.show(msg, 'warning', duration),
    info:    (msg: string, duration?: number) => ctx.show(msg, 'info',    duration),
  }
}

// ── Imperative API (usable anywhere) ─────────────────────────────────────────

export const toast = {
  success: (msg: string, duration?: number) => _show?.(msg, 'success', duration),
  error:   (msg: string, duration?: number) => _show?.(msg, 'error',   duration),
  warning: (msg: string, duration?: number) => _show?.(msg, 'warning', duration),
  info:    (msg: string, duration?: number) => _show?.(msg, 'info',    duration),
}
