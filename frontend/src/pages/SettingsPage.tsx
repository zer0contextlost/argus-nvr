/**
 * SettingsPage — Notification channel config + rules. Polished v2 UI.
 */

import { useEffect, useState, useCallback } from 'react'
import { settingsApi, systemApi, NotifyHistory, cloudBackupApi, webpushApi, withToken } from '../api/client'
import { LoadingState, PageHeader } from '../components/ui/Primitives'
import { useToast } from '../components/ui/Toast'

// ── Web Push helpers ────────────────────────────────────────────────────────

// Converts a URL-safe base64 VAPID public key into the Uint8Array format
// PushManager.subscribe() requires.
function urlBase64ToUint8Array(base64String: string): Uint8Array {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const rawData = atob(base64)
  const outputArray = new Uint8Array(rawData.length)
  for (let i = 0; i < rawData.length; i++) {
    outputArray[i] = rawData.charCodeAt(i)
  }
  return outputArray
}

// ── Field component ───────────────────────────────────────────────────────────

function Field({
  label, value, onChange, placeholder, type = 'text', help, required,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  type?: string
  help?: string
  required?: boolean
}) {
  return (
    <div>
      <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 5, marginTop: 12 }}>
        {label}
        {required && <span style={{ color: 'var(--danger)', marginLeft: 3 }}>*</span>}
      </label>
      <input
        style={{ width: '100%' }}
        type={type}
        value={value}
        placeholder={placeholder ?? ''}
        onChange={e => onChange(e.target.value)}
        autoComplete="off"
      />
      {help && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4, lineHeight: 1.4 }}>
          {help}
        </div>
      )}
    </div>
  )
}

// ── Test send button ──────────────────────────────────────────────────────────

function TestButton({ channel, disabled }: { channel: string; disabled?: boolean }) {
  const [state, setState] = useState<'idle' | 'sending' | 'ok' | 'err'>('idle')
  const [msg, setMsg] = useState('')

  const handleTest = async () => {
    setState('sending'); setMsg('')
    try {
      const r = await settingsApi.testNotify(channel)
      if (r.ok) { setState('ok'); setMsg('Test sent successfully') }
      else { setState('err'); setMsg(r.error ?? 'Unknown error') }
    } catch (e) {
      setState('err'); setMsg(String(e))
    }
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 16 }}>
      <button
        className="btn btn-secondary btn-sm"
        onClick={handleTest}
        disabled={disabled || state === 'sending'}
      >
        {state === 'sending' ? (
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span className="spinner spinner-sm" />
            Sending…
          </span>
        ) : `Send test ${channel}`}
      </button>
      {state === 'ok' && (
        <span style={{ fontSize: 12, color: 'var(--success)', display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{ fontWeight: 700 }}>✓</span> {msg}
        </span>
      )}
      {state === 'err' && (
        <span style={{ fontSize: 12, color: 'var(--danger)', display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{ fontWeight: 700 }}>✕</span> {msg}
        </span>
      )}
    </div>
  )
}

// ── Channel section wrapper ───────────────────────────────────────────────────

function ChannelSection({ icon, title, children }: { icon: string; title: string; children: React.ReactNode }) {
  return (
    <div style={{
      background: 'var(--bg-surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-md)',
      padding: '20px 20px 16px',
      marginBottom: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <span style={{
          width: 32, height: 32, borderRadius: 'var(--radius)',
          background: 'var(--bg-elevated)', border: '1px solid var(--border)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 16, flexShrink: 0,
        }}>
          {icon}
        </span>
        <span style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)' }}>
          {title}
        </span>
      </div>
      {children}
    </div>
  )
}

// ── History status cell ───────────────────────────────────────────────────────

function StatusCell({ status }: { status: string }) {
  const color = status === 'sent' ? 'var(--success)' : status === 'failed' ? 'var(--danger)' : 'var(--text-muted)'
  const bg = status === 'sent' ? 'var(--success-bg)' : status === 'failed' ? 'var(--danger-bg)' : 'var(--bg-elevated)'
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: '2px 8px', borderRadius: 'var(--radius-full)',
      fontSize: 11, fontWeight: 600,
      background: bg, color,
      textTransform: 'uppercase', letterSpacing: '0.04em',
    }}>
      {status === 'sent' ? '✓' : status === 'failed' ? '✕' : '·'} {status}
    </span>
  )
}

// ── SettingsPage ──────────────────────────────────────────────────────────────

export function SettingsPage() {
  const toast = useToast()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [purging, setPurging] = useState(false)
  const [purgeConfirm, setPurgeConfirm] = useState(false)
  const [purgePassword, setPurgePassword] = useState('')

  // HTTPS
  const [httpsEnabled, setHttpsEnabled] = useState('0')
  const [sslCertPath, setSslCertPath]   = useState('')
  const [sslKeyPath, setSslKeyPath]     = useState('')

  // General
  const [enabled, setEnabled]       = useState('1')
  const [cooldown, setCooldown]     = useState('60')
  const [quietStart, setQuietStart] = useState('')
  const [quietEnd, setQuietEnd]     = useState('')
  const [cameras, setCameraIds]     = useState('')
  const [channels, setChannels]     = useState('telegram,ntfy,email')

  // Telegram
  const [tgToken, setTgToken] = useState('')
  const [tgChat, setTgChat]   = useState('')

  // ntfy
  const [ntfyServer, setNtfyServer] = useState('https://ntfy.sh')
  const [ntfyTopic, setNtfyTopic]   = useState('')

  // Webhook
  const [webhookUrl, setWebhookUrl]       = useState('')
  const [webhookSecret, setWebhookSecret] = useState('')

  // Camera Cycle
  const [cycleInterval, setCycleInterval] = useState('10')

  // Cloud Backup
  const [cloudBackupEnabled, setCloudBackupEnabled] = useState('0')
  const [cloudBackupRemote, setCloudBackupRemote] = useState('')
  const [cloudBackupSchedule, setCloudBackupSchedule] = useState('0 2 * * *')
  const [backupStatus, setBackupStatus] = useState<{ running?: boolean; status?: string; started_at?: string; error_msg?: string } | null>(null)
  const [triggeringBackup, setTriggeringBackup] = useState(false)

  // Web Push
  const [webpushEnabled, setWebpushEnabled] = useState('0')
  const [vapidPublicKey, setVapidPublicKey] = useState('')
  const [browserSubscribed, setBrowserSubscribed] = useState(false)
  const [subscribing, setSubscribing] = useState(false)

  // ONVIF Output
  const [onvifOutputEnabled, setOnvifOutputEnabled] = useState('0')

  // SMTP
  const [smtpHost, setSmtpHost] = useState('')
  const [smtpPort, setSmtpPort] = useState('587')
  const [smtpUser, setSmtpUser] = useState('')
  const [smtpPass, setSmtpPass] = useState('')
  const [smtpFrom, setSmtpFrom] = useState('')
  const [smtpTo, setSmtpTo]     = useState('')
  const [smtpTls, setSmtpTls]   = useState('1')

  // History
  const [history, setHistory]     = useState<NotifyHistory['notifications']>([])
  const [histTotal, setHistTotal] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const s = await settingsApi.getSettings()
      setHttpsEnabled(s.https_enabled ?? '0')
      setSslCertPath(s.ssl_cert_path ?? '')
      setSslKeyPath(s.ssl_key_path ?? '')
      setEnabled(s.notify_enabled ?? '1')
      setCooldown(s.notify_cooldown_seconds ?? '60')
      setQuietStart(s.notify_quiet_start ?? '')
      setQuietEnd(s.notify_quiet_end ?? '')
      setCameraIds(s.notify_cameras ?? '')
      setChannels(s.notify_channels ?? 'telegram,ntfy,email')
      setTgToken(s.notify_telegram_bot_token ?? '')
      setTgChat(s.notify_telegram_chat_id ?? '')
      setNtfyServer(s.notify_ntfy_server ?? 'https://ntfy.sh')
      setNtfyTopic(s.notify_ntfy_topic ?? '')
      setWebhookUrl(s.notify_webhook_url ?? '')
      setWebhookSecret(s.notify_webhook_secret ?? '')
      setCycleInterval(s.cycle_interval_seconds ?? '10')
      setCloudBackupEnabled(s.cloud_backup_enabled ?? '0')
      setCloudBackupRemote(s.cloud_backup_remote ?? '')
      setCloudBackupSchedule(s.cloud_backup_schedule ?? '0 2 * * *')
      setWebpushEnabled(s.webpush_enabled ?? '0')
      setOnvifOutputEnabled(s.onvif_output_enabled ?? '0')
      setSmtpHost(s.notify_smtp_host ?? '')
      setSmtpPort(s.notify_smtp_port ?? '587')
      setSmtpUser(s.notify_smtp_user ?? '')
      setSmtpPass(s.notify_smtp_pass ?? '')
      setSmtpFrom(s.notify_smtp_from ?? '')
      setSmtpTo(s.notify_smtp_to ?? '')
      setSmtpTls(s.notify_smtp_tls ?? '1')
      // Load supplemental data
      cloudBackupApi.status().then(setBackupStatus).catch(() => {})
      webpushApi.getVapidKey().then(r => setVapidPublicKey(r.public_key)).catch(() => {})
      if ('serviceWorker' in navigator && 'PushManager' in window) {
        navigator.serviceWorker.getRegistration().then(reg =>
          reg?.pushManager.getSubscription()
        ).then(sub => setBrowserSubscribed(!!sub)).catch(() => {})
      }
    } catch (e) {
      toast.error(`Failed to load settings: ${e}`)
    } finally {
      setLoading(false)
    }
  }, [])

  const loadHistory = useCallback(async () => {
    try {
      const h = await settingsApi.getNotifyHistory()
      setHistory(h.notifications)
      setHistTotal(h.total)
    } catch { /* non-fatal */ }
  }, [])

  useEffect(() => { load(); loadHistory() }, [load, loadHistory])

  const handleSave = async () => {
    setSaving(true)
    try {
      await settingsApi.updateSettings({
        https_enabled:             httpsEnabled,
        ssl_cert_path:             sslCertPath,
        ssl_key_path:              sslKeyPath,
        notify_enabled:            enabled,
        notify_cooldown_seconds:   cooldown,
        notify_quiet_start:        quietStart,
        notify_quiet_end:          quietEnd,
        notify_cameras:            cameras,
        notify_channels:           channels,
        notify_telegram_bot_token: tgToken,
        notify_telegram_chat_id:   tgChat,
        notify_ntfy_server:        ntfyServer,
        notify_ntfy_topic:         ntfyTopic,
        notify_webhook_url:        webhookUrl,
        notify_webhook_secret:     webhookSecret,
        cycle_interval_seconds:    cycleInterval,
        cloud_backup_enabled:      cloudBackupEnabled,
        cloud_backup_remote:       cloudBackupRemote,
        cloud_backup_schedule:     cloudBackupSchedule,
        webpush_enabled:           webpushEnabled,
        onvif_output_enabled:      onvifOutputEnabled,
        notify_smtp_host:          smtpHost,
        notify_smtp_port:          smtpPort,
        notify_smtp_user:          smtpUser,
        notify_smtp_pass:          smtpPass,
        notify_smtp_from:          smtpFrom,
        notify_smtp_to:            smtpTo,
        notify_smtp_tls:           smtpTls,
      })
      toast.success('Settings saved')
    } catch (e) {
      toast.error(`Save failed: ${e}`)
    } finally {
      setSaving(false)
    }
  }

  const handleSubscribeBrowser = async () => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      toast.error('Push notifications are not supported in this browser')
      return
    }
    if (!vapidPublicKey || vapidPublicKey === 'NOT_CONFIGURED') {
      toast.error('Web push is not configured on the server yet')
      return
    }
    setSubscribing(true)
    try {
      const permission = await Notification.requestPermission()
      if (permission !== 'granted') {
        toast.error('Notification permission denied')
        return
      }
      const reg = await navigator.serviceWorker.register('/sw.js')
      await navigator.serviceWorker.ready
      let sub = await reg.pushManager.getSubscription()
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapidPublicKey) as BufferSource,
        })
      }
      const json = sub.toJSON() as { endpoint: string; keys?: { p256dh?: string; auth?: string } }
      await webpushApi.subscribe({
        endpoint: json.endpoint,
        p256dh: json.keys?.p256dh ?? '',
        auth: json.keys?.auth ?? '',
        user_agent: navigator.userAgent,
      })
      setBrowserSubscribed(true)
      toast.success('This browser is now subscribed to push notifications')
    } catch (e) {
      toast.error(`Subscribe failed: ${e}`)
    } finally {
      setSubscribing(false)
    }
  }

  const handleUnsubscribeBrowser = async () => {
    setSubscribing(true)
    try {
      const reg = await navigator.serviceWorker.getRegistration()
      const sub = await reg?.pushManager.getSubscription()
      if (sub) {
        const json = sub.toJSON() as { endpoint: string; keys?: { p256dh?: string; auth?: string } }
        await sub.unsubscribe()
        await webpushApi.unsubscribe({
          endpoint: json.endpoint,
          p256dh: json.keys?.p256dh ?? '',
          auth: json.keys?.auth ?? '',
        })
      }
      setBrowserSubscribed(false)
      toast.success('Unsubscribed this browser')
    } catch (e) {
      toast.error(`Unsubscribe failed: ${e}`)
    } finally {
      setSubscribing(false)
    }
  }

  if (loading) return <LoadingState message="Loading settings…" />

  return (
    <div style={{ maxWidth: 700 }}>
      <PageHeader title="Notification Settings">
        <div style={{ flex: 1 }} />
        <button
          className="btn btn-primary"
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? (
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className="spinner spinner-sm" style={{ borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3)' }} />
              Saving…
            </span>
          ) : 'Save all settings'}
        </button>
      </PageHeader>

      {/* HTTPS */}
      <ChannelSection icon="🔒" title="HTTPS">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer', fontSize: 14, color: 'var(--text-secondary)' }}>
            <input
              type="checkbox"
              checked={httpsEnabled === '1'}
              onChange={e => setHttpsEnabled(e.target.checked ? '1' : '0')}
            />
            <span>Enable HTTPS</span>
          </label>
        </div>
        {httpsEnabled === '1' && (
          <>
            <Field
              label="Certificate path (optional)"
              value={sslCertPath}
              onChange={setSslCertPath}
              placeholder="/path/to/sentinel.crt"
              help="Leave blank to auto-generate a self-signed certificate."
            />
            <Field
              label="Key path (optional)"
              value={sslKeyPath}
              onChange={setSslKeyPath}
              placeholder="/path/to/sentinel.key"
              help="Leave blank to auto-generate alongside the certificate."
            />
          </>
        )}
        <div style={{
          marginTop: 12, padding: '10px 12px',
          background: 'rgba(234,179,8,0.1)', border: '1px solid rgba(234,179,8,0.4)',
          borderRadius: 'var(--radius)', fontSize: 12, color: 'var(--warning)',
          lineHeight: 1.5,
        }}>
          A server restart is required for HTTPS changes to take effect.
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6, lineHeight: 1.4 }}>
          If no cert path is specified, a self-signed certificate will be auto-generated.
        </div>
      </ChannelSection>

      {/* General rules */}
      <ChannelSection icon="⚙" title="General rules">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer', fontSize: 14, color: 'var(--text-secondary)' }}>
            <input
              type="checkbox"
              checked={enabled === '1'}
              onChange={e => setEnabled(e.target.checked ? '1' : '0')}
            />
            <span>Notifications enabled</span>
          </label>
        </div>

        <Field
          label="Cooldown (seconds)"
          value={cooldown}
          onChange={setCooldown}
          placeholder="60"
          type="number"
          help="Minimum seconds between repeat notifications per camera + channel"
        />

        <div className="grid-2" style={{ gap: 12 }}>
          <Field
            label="Quiet hours start"
            value={quietStart}
            onChange={setQuietStart}
            placeholder="22:00"
            help="24h local time"
          />
          <Field
            label="Quiet hours end"
            value={quietEnd}
            onChange={setQuietEnd}
            placeholder="07:00"
            help="24h local time"
          />
        </div>

        <Field
          label="Camera IDs (comma-separated, blank = all)"
          value={cameras}
          onChange={setCameraIds}
          placeholder="1,2,3"
        />

        <Field
          label="Active channels (comma-separated)"
          value={channels}
          onChange={setChannels}
          placeholder="telegram,ntfy,email"
          help="Values: telegram, ntfy, email, webhook"
        />
      </ChannelSection>

      {/* Telegram */}
      <ChannelSection icon="✈" title="Telegram">
        <Field
          label="Bot token"
          value={tgToken}
          onChange={setTgToken}
          placeholder="123456:ABC-DEF..."
          type="password"
          help="From @BotFather — keep this private"
        />
        <Field
          label="Chat ID"
          value={tgChat}
          onChange={setTgChat}
          placeholder="-1001234567890"
          help="Your user, group or channel chat ID"
        />
        <TestButton channel="telegram" />
      </ChannelSection>

      {/* ntfy */}
      <ChannelSection icon="🔔" title="ntfy">
        <Field
          label="Server URL"
          value={ntfyServer}
          onChange={setNtfyServer}
          placeholder="https://ntfy.sh"
        />
        <Field
          label="Topic"
          value={ntfyTopic}
          onChange={setNtfyTopic}
          placeholder="my-sentinel-alerts"
          help="Subscribe to this topic in the ntfy app on your phone"
        />
        <TestButton channel="ntfy" />
      </ChannelSection>

      {/* SMTP */}
      <ChannelSection icon="📧" title="Email (SMTP)">
        <div className="grid-2" style={{ gap: 12 }}>
          <Field
            label="SMTP host"
            value={smtpHost}
            onChange={setSmtpHost}
            placeholder="smtp.gmail.com"
          />
          <Field
            label="Port"
            value={smtpPort}
            onChange={setSmtpPort}
            placeholder="587"
            type="number"
          />
        </div>
        <Field label="Username" value={smtpUser} onChange={setSmtpUser} placeholder="you@gmail.com" />
        <Field label="Password / app password" value={smtpPass} onChange={setSmtpPass} type="password" placeholder="••••••••" />
        <Field label="From address" value={smtpFrom} onChange={setSmtpFrom} placeholder="sentinel@yourdomain.com" />
        <Field
          label="To address(es)"
          value={smtpTo}
          onChange={setSmtpTo}
          placeholder="you@example.com, other@example.com"
          help="Comma-separate multiple recipients"
        />
        <div style={{ marginTop: 12 }}>
          <label style={{ display: 'block', fontSize: 12, fontWeight: 500, color: 'var(--text-secondary)', marginBottom: 5 }}>
            TLS mode
          </label>
          <select
            value={smtpTls}
            onChange={e => setSmtpTls(e.target.value)}
            style={{ width: 'auto', minWidth: 220 }}
          >
            <option value="1">STARTTLS (port 587)</option>
            <option value="ssl">SSL/TLS (port 465)</option>
            <option value="0">Plain — no encryption</option>
          </select>
        </div>
        <TestButton channel="email" />
      </ChannelSection>

      {/* Webhook */}
      <ChannelSection icon="🔗" title="Webhook (HTTP POST)">
        <Field
          label="Webhook URL"
          value={webhookUrl}
          onChange={setWebhookUrl}
          placeholder="https://your-server.com/hook"
          help="Argus will POST JSON to this URL on every motion/face event"
        />
        <Field
          label="Secret (optional)"
          value={webhookSecret}
          onChange={setWebhookSecret}
          placeholder="mysecret"
          help="If set, a X-Argus-Signature: sha256=... HMAC header is added"
        />
      </ChannelSection>

      {/* Camera Cycle */}
      <ChannelSection icon="⟳" title="Camera Cycle">
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: '0 0 4px', lineHeight: 1.5 }}>
          Fullscreen kiosk mode — cycles through all live cameras one at a time.
          Start it from the Live page using the Cycle button.
        </p>
        <Field
          label="Seconds per camera"
          value={cycleInterval}
          onChange={v => setCycleInterval(v.replace(/\D/g, ''))}
          type="number"
          placeholder="10"
          help="How long each camera is shown before advancing to the next. Minimum 3 s."
        />
      </ChannelSection>

      {/* Cloud Backup */}
      <ChannelSection icon="☁" title="Cloud Backup (rclone)">
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: '0 0 4px', lineHeight: 1.5 }}>
          Sync recordings to a cloud provider using rclone. Requires rclone installed and configured.
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '12px 0' }}>
          <label style={{ fontSize: 13, fontWeight: 500 }}>Enable cloud backup</label>
          <input type="checkbox" checked={cloudBackupEnabled === '1'} onChange={e => setCloudBackupEnabled(e.target.checked ? '1' : '0')} />
        </div>
        <Field label="rclone remote destination" value={cloudBackupRemote} onChange={setCloudBackupRemote} placeholder="gdrive:argus-backups" help="Format: remote:path (configure rclone separately)" />
        <Field label="Schedule (cron)" value={cloudBackupSchedule} onChange={setCloudBackupSchedule} placeholder="0 2 * * *" help="Standard cron expression for auto-backup time" />
        {backupStatus && (
          <div style={{ marginTop: 10, fontSize: 12, color: 'var(--text-muted)' }}>
            Last backup: <strong>{backupStatus.status || 'none'}</strong>
            {backupStatus.started_at ? ` at ${backupStatus.started_at.slice(0, 16).replace('T', ' ')}` : ''}
            {backupStatus.error_msg && <span style={{ color: 'var(--danger)' }}> — {backupStatus.error_msg}</span>}
          </div>
        )}
        <div style={{ marginTop: 12 }}>
          <button
            className="btn btn-secondary btn-sm"
            disabled={triggeringBackup}
            onClick={async () => {
              setTriggeringBackup(true)
              try {
                const r = await cloudBackupApi.trigger()
                if (r.ok) toast.success('Backup started')
                else toast.error(r.error || 'Failed to start backup')
                cloudBackupApi.status().then(setBackupStatus).catch(() => {})
              } catch (e) { toast.error(String(e)) }
              finally { setTriggeringBackup(false) }
            }}
          >
            {triggeringBackup ? 'Starting…' : 'Backup Now'}
          </button>
        </div>
      </ChannelSection>

      {/* Web Push */}
      <ChannelSection icon="🔔" title="Web Push / PWA Notifications">
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: '0 0 4px', lineHeight: 1.5 }}>
          Send push notifications to browsers and PWA installs. Requires pywebpush installed on the server.
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '12px 0' }}>
          <label style={{ fontSize: 13, fontWeight: 500 }}>Enable Web Push</label>
          <input type="checkbox" checked={webpushEnabled === '1'} onChange={e => setWebpushEnabled(e.target.checked ? '1' : '0')} />
        </div>
        {vapidPublicKey && vapidPublicKey !== 'NOT_CONFIGURED' && (
          <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'monospace', wordBreak: 'break-all', background: 'var(--bg-elevated)', padding: '6px 10px', borderRadius: 4, marginTop: 8 }}>
            VAPID public key: {vapidPublicKey.slice(0, 32)}…
          </div>
        )}
        <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: '10px 0 0', lineHeight: 1.5 }}>
          The toggle above just enables the channel server-side — each browser/device that should
          receive pushes still needs to subscribe itself below. Requires HTTPS or localhost.
        </p>
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
          {browserSubscribed ? (
            <button className="btn btn-secondary btn-sm" disabled={subscribing} onClick={handleUnsubscribeBrowser}>
              {subscribing ? 'Working…' : 'Unsubscribe this browser'}
            </button>
          ) : (
            <button className="btn btn-primary btn-sm" disabled={subscribing} onClick={handleSubscribeBrowser}>
              {subscribing ? 'Working…' : 'Subscribe this browser'}
            </button>
          )}
          <button className="btn btn-secondary btn-sm" onClick={async () => {
            try {
              const r = await webpushApi.testPush()
              if (r.ok) toast.success(`Sent ${r.sent} push(es)`)
              else toast.error(r.errors?.join('; ') || 'No subscriptions')
            } catch (e) { toast.error(String(e)) }
          }}>Send test push</button>
        </div>
      </ChannelSection>

      {/* Two-way audio */}
      <ChannelSection icon="🎙" title="Two-Way Audio">
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: '0 0 4px', lineHeight: 1.5 }}>
          Speak back to a camera from the live view player. <strong>Not implemented yet</strong> —
          the live player only opens a receive-only audio channel today. A real implementation
          needs a manufacturer-specific back-channel (e.g. Hikvision's ISAPI TwoWayAudio endpoints);
          it won't work uniformly across mixed camera brands like Wyze, which uses its own
          proprietary protocol for talk-back rather than RTSP/ONVIF.
        </p>
      </ChannelSection>

      {/* ONVIF Output */}
      <ChannelSection icon="📡" title="ONVIF Output">
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: '0 0 4px', lineHeight: 1.5 }}>
          Re-broadcast streams as ONVIF-compatible output so other NVR software can discover them.
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '12px 0' }}>
          <label style={{ fontSize: 13, fontWeight: 500 }}>Enable ONVIF output</label>
          <input type="checkbox" checked={onvifOutputEnabled === '1'} onChange={e => setOnvifOutputEnabled(e.target.checked ? '1' : '0')} />
        </div>
      </ChannelSection>

      {/* QR Code */}
      <ChannelSection icon="⬛" title="Mobile Access QR Code">
        <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: '0 0 8px', lineHeight: 1.5 }}>
          Scan this code on your phone to open the Argus NVR web interface. Works on your local network.
        </p>
        <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <img
            src={withToken(`/api/system/qr?url=${encodeURIComponent(window.location.origin)}`)}
            alt="QR code"
            style={{ width: 140, height: 140, background: '#fff', borderRadius: 4, border: '1px solid var(--border)' }}
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <div>
            <div style={{ fontSize: 12, fontWeight: 500, marginBottom: 6 }}>App URL</div>
            <code style={{ fontSize: 11, color: 'var(--text-secondary)', wordBreak: 'break-all' }}>
              {window.location.origin}
            </code>
            <button
              className="btn btn-ghost btn-sm"
              style={{ marginTop: 10, display: 'block' }}
              onClick={() => navigator.clipboard?.writeText(window.location.origin).then(() => toast.success('URL copied'))}
            >
              Copy URL
            </button>
          </div>
        </div>
      </ChannelSection>

      {/* Danger Zone */}
      <div style={{
        background: 'var(--bg-surface)',
        border: '1px solid var(--danger)',
        borderRadius: 'var(--radius-md)',
        padding: '20px 20px 16px',
        marginBottom: 12,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
          <span style={{
            width: 32, height: 32, borderRadius: 'var(--radius)',
            background: 'var(--danger-bg)', border: '1px solid var(--danger)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 16, flexShrink: 0,
          }}>
            ⚠
          </span>
          <span style={{ fontWeight: 600, fontSize: 14, color: 'var(--danger)' }}>
            Danger Zone
          </span>
        </div>

        <p style={{ fontSize: 13, color: 'var(--text-secondary)', margin: '0 0 14px', lineHeight: 1.5 }}>
          Permanently deletes all recordings, events, and notifications — both from disk and from the database.
          Cameras will immediately restart recording fresh segments. This cannot be undone.
        </p>

        {!purgeConfirm ? (
          <button
            className="btn btn-sm"
            style={{ background: 'var(--danger-bg)', color: 'var(--danger)', border: '1px solid var(--danger)' }}
            onClick={() => { setPurgeConfirm(true); setPurgePassword('') }}
          >
            Purge all recordings &amp; events…
          </button>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 420 }}>
            <span style={{ fontSize: 13, color: 'var(--danger)', fontWeight: 600 }}>
              This permanently deletes everything. Enter your password to confirm.
            </span>
            <input
              type="password"
              className="input input-sm"
              placeholder="Your password"
              value={purgePassword}
              onChange={e => setPurgePassword(e.target.value)}
              onKeyDown={e => e.key === 'Escape' && setPurgeConfirm(false)}
              autoFocus
            />
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                className="btn btn-sm"
                style={{ background: 'var(--danger)', color: '#fff', border: 'none' }}
                disabled={purging || !purgePassword}
                onClick={async () => {
                  setPurging(true)
                  try {
                    const r = await systemApi.purge(purgePassword)
                    const mb = (r.deleted_bytes / 1_048_576).toFixed(1)
                    toast.success(`Purge complete — ${r.deleted_files} items deleted (${mb} MB freed)`)
                    setPurgeConfirm(false)
                    setPurgePassword('')
                  } catch (e: any) {
                    const msg = e?.detail || String(e)
                    toast.error(msg.includes('403') || msg.toLowerCase().includes('incorrect') ? 'Incorrect password' : `Purge failed: ${e}`)
                  } finally {
                    setPurging(false)
                  }
                }}
              >
                {purging ? (
                  <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span className="spinner spinner-sm" style={{ borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3)' }} />
                    Purging…
                  </span>
                ) : 'Yes, delete everything'}
              </button>
              <button
                className="btn btn-ghost btn-sm"
                disabled={purging}
                onClick={() => { setPurgeConfirm(false); setPurgePassword('') }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Notification history */}
      <div style={{
        background: 'var(--bg-surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-md)', overflow: 'hidden',
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '14px 18px',
          borderBottom: '1px solid var(--border)',
        }}>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.07em', flex: 1 }}>
            Recent notifications ({histTotal})
          </span>
          <button className="btn btn-ghost btn-sm" onClick={loadHistory}>
            Refresh
          </button>
        </div>

        {history.length === 0 ? (
          <div style={{ padding: '32px 20px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
            No notifications sent yet.
          </div>
        ) : (
          <table className="table" style={{ margin: 0 }}>
            <thead>
              <tr>
                <th>Time</th>
                <th>Camera</th>
                <th>Channel</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {history.slice(0, 50).map(n => (
                <tr key={n.id}>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                    {n.sent_at ? n.sent_at.replace('T', ' ').slice(0, 19) : '—'}
                  </td>
                  <td style={{ color: 'var(--text-secondary)' }}>
                    {n.camera_id != null ? `cam ${n.camera_id}` : '—'}
                  </td>
                  <td style={{ color: 'var(--text-secondary)' }}>
                    {n.channel}
                  </td>
                  <td>
                    <StatusCell status={n.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
