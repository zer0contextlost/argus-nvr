/**
 * CamerasPage — camera management with polished UI.
 */

import { useEffect, useState } from 'react'
import { camerasApi, eventsApi, withToken, Camera, CameraCreate, DiscoveredDevice } from '../api/client'
import { ZoneEditor } from '../components/ZoneEditor'
import {
  EmptyState, LoadingState, PageHeader, SectionCard,
  FormField, StatusDot, ErrorAlert,
} from '../components/ui/Primitives'
import { useToast } from '../components/ui/Toast'

// ── Default form ──────────────────────────────────────────────────────────────

const DEFAULT_FORM: CameraCreate = {
  name: '',
  host: '',
  rtsp_port: 554,
  rtsp_user: 'admin',
  rtsp_pass: '',
  main_path: '/Streaming/Channels/101',
  sub_path: '/Streaming/Channels/102',
  channel: 1,
  record_mode: 'motion-only',
  enabled: true,
  cam_type: 'rtsp',
  cam_make: 'hikvision',
  cam_group: '',
}

// ── Camera make presets (cam_make) ────────────────────────────────────────────

type CamMakePreset = {
  value: string
  label: string
  main_path: string
  sub_path: string
  showChannel: boolean
  singleStream: boolean
}

const CAM_MAKE_PRESETS: CamMakePreset[] = [
  {
    value: 'hikvision',
    label: 'Hikvision',
    main_path: '/Streaming/Channels/101',
    sub_path: '/Streaming/Channels/102',
    showChannel: true,
    singleStream: false,
  },
  {
    value: 'dahua',
    label: 'Dahua',
    main_path: '/cam/realmonitor?channel=1&subtype=0',
    sub_path: '/cam/realmonitor?channel=1&subtype=1',
    showChannel: false,
    singleStream: false,
  },
  {
    value: 'reolink',
    label: 'Reolink',
    main_path: '/h264Preview_01_main',
    sub_path: '/h264Preview_01_sub',
    showChannel: false,
    singleStream: false,
  },
  {
    value: 'axis',
    label: 'Axis',
    main_path: '/axis-media/media.amp',
    sub_path: '/axis-media/media.amp?videocodec=h264&resolution=640x360',
    showChannel: false,
    singleStream: false,
  },
  {
    value: 'wyze',
    label: 'Wyze (RTSP firmware)',
    main_path: '/live',
    sub_path: '/live',
    showChannel: false,
    singleStream: true,
  },
  {
    value: 'generic',
    label: 'Generic RTSP',
    main_path: '',
    sub_path: '',
    showChannel: false,
    singleStream: true,
  },
]

// ── Record mode labels ─────────────────────────────────────────────────────────

const MODE_LABELS: Record<string, string> = {
  'continuous':      'Continuous',
  'motion-only':     'Motion only',
  'continuous+events': 'Continuous + events',
}

// ── Detection settings modal ──────────────────────────────────────────────────

interface DetectConfig {
  face_detection?: boolean
  pixel_threshold?: number
  min_changed_pct?: number
  max_changed_pct?: number
  min_blob_pct?: number
  motion_confirm_frames?: number
  post_motion_seconds?: number
}

function parseDetectConfig(json: string): DetectConfig {
  try { return JSON.parse(json || '{}') } catch { return {} }
}

function SliderRow({
  label, help, value, min, max, step, unit, onChange,
}: {
  label: string; help: string; value: number; min: number; max: number
  step: number; unit: string; onChange: (v: number) => void
}) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)' }}>{label}</span>
        <span style={{ fontSize: 13, fontFamily: 'var(--font-mono)', color: 'var(--text-accent)' }}>
          {value}{unit}
        </span>
      </div>
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(Number(e.target.value))}
        style={{ width: '100%', accentColor: 'var(--accent)' }}
      />
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>{help}</div>
    </div>
  )
}

interface DetectionModalProps {
  cam: Camera
  onSave: (camId: number, cfg: DetectConfig) => Promise<void>
  onClose: () => void
}

function DetectionModal({ cam, onSave, onClose }: DetectionModalProps) {
  const [cfg, setCfg] = useState<DetectConfig>(() => parseDetectConfig(cam.detect_config_json || '{}'))
  const [saving, setSaving] = useState(false)

  const set = (k: keyof DetectConfig, v: unknown) => setCfg(c => ({ ...c, [k]: v }))

  const pt  = cfg.pixel_threshold      ?? 25
  const mcp = cfg.min_changed_pct      ?? 0.5
  const xcp = cfg.max_changed_pct      ?? 25
  const mbp = cfg.min_blob_pct         ?? 0.3
  const mcf = cfg.motion_confirm_frames ?? 2
  const pms = cfg.post_motion_seconds  ?? 10
  const fd  = cfg.face_detection       ?? true

  const handleSave = async () => {
    setSaving(true)
    try { await onSave(cam.id, cfg) } finally { setSaving(false) }
  }

  return (
    <div
      style={{
        position: 'fixed', inset: 0,
        background: 'var(--bg-overlay)', backdropFilter: 'blur(4px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 500, padding: 16,
      }}
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div style={{
        background: 'var(--bg-surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', padding: '24px 24px 20px',
        width: '100%', maxWidth: 480, maxHeight: '90vh', overflowY: 'auto',
        boxShadow: 'var(--shadow-xl)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-primary)' }}>
            Detection — {cam.name}
          </h2>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>&#x2715;</button>
        </div>
        <p style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 20, lineHeight: 1.5 }}>
          Settings are applied immediately when saved. Changes restart the detector for this camera only.
        </p>

        {/* Face detection toggle */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '12px 14px', marginBottom: 20,
          background: 'var(--bg-elevated)', borderRadius: 'var(--radius)',
          border: '1px solid var(--border)',
        }}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-secondary)' }}>Face detection</div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
              Runs MediaPipe on confirmed motion frames. Disable for cameras too far away to see faces.
            </div>
          </div>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', marginLeft: 12, flexShrink: 0 }}>
            <input type="checkbox" checked={fd} onChange={e => set('face_detection', e.target.checked)} />
            <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{fd ? 'On' : 'Off'}</span>
          </label>
        </div>

        {/* Motion sensitivity */}
        <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--text-muted)', marginBottom: 12 }}>
          Motion sensitivity
        </div>

        <SliderRow
          label="Pixel threshold"
          help="Brightness change (0–255) a pixel must have to count as moved. Higher = less sensitive."
          value={pt} min={10} max={60} step={1} unit=""
          onChange={v => set('pixel_threshold', v)}
        />
        <SliderRow
          label="Zone coverage % (min)"
          help="% of zone pixels that must be changed before motion is considered. Raise to ignore minor movements."
          value={mcp} min={0.1} max={5} step={0.1} unit="%"
          onChange={v => set('min_changed_pct', v)}
        />
        <SliderRow
          label="Zone coverage % (max)"
          help="If more than this % of zone pixels change in one frame, the frame is rejected — insects on the lens or sudden obstructions flood the zone but people don't."
          value={xcp} min={5} max={60} step={1} unit="%"
          onChange={v => set('max_changed_pct', v)}
        />
        <SliderRow
          label="Blob size %"
          help="Largest contiguous changed region must be at least this % of zone. Set to 0 to disable. Increase to filter insects and leaves."
          value={mbp} min={0} max={2} step={0.05} unit="%"
          onChange={v => set('min_blob_pct', v)}
        />

        {/* Timing */}
        <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--text-muted)', margin: '20px 0 12px' }}>
          Timing
        </div>

        <SliderRow
          label="Confirm frames"
          help="Consecutive motion frames required before an event fires. Higher = fewer false triggers."
          value={mcf} min={1} max={8} step={1} unit=" frames"
          onChange={v => set('motion_confirm_frames', v)}
        />
        <SliderRow
          label="Post-motion delay"
          help="Seconds of recording kept after motion stops. Ensures the end of an event isn't cut off."
          value={pms} min={3} max={120} step={1} unit="s"
          onChange={v => set('post_motion_seconds', v)}
        />

        <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
          <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
            {saving ? (
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span className="spinner spinner-sm" style={{ borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3)' }} />
                Saving…
              </span>
            ) : 'Save'}
          </button>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  )
}

// ── Camera card ───────────────────────────────────────────────────────────────

interface CameraCardProps {
  cam: Camera
  testResult: string | undefined
  snapshot: string | undefined
  onTest: (id: number) => void
  onSnapshot: (id: number) => void
  onEdit: (cam: Camera) => void
  onDelete: (id: number) => void
  onZones: (cam: Camera) => void
  onDetection: (cam: Camera) => void
  onSyncTime: (id: number) => void
}

function CameraCard({ cam, testResult, snapshot, onTest, onSnapshot, onEdit, onDelete, onZones, onDetection, onSyncTime }: CameraCardProps) {
  const toast = useToast()
  const isOk = testResult?.startsWith('OK')
  const isFail = testResult?.startsWith('FAIL') || testResult?.startsWith('error')
  const statusDotStatus = cam.enabled ? 'running' : 'stopped'

  return (
    <div style={{
      background: 'var(--bg-surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-md)',
      overflow: 'hidden',
      transition: 'border-color var(--transition)',
    }}
    onMouseEnter={e => (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--border-focus)'}
    onMouseLeave={e => (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--border)'}
    >
      {/* Card header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '14px 16px',
        borderBottom: '1px solid var(--border-subtle)',
        flexWrap: 'wrap',
      }}>
        {/* Status dot + name */}
        <StatusDot status={statusDotStatus} />
        <span style={{ fontWeight: 600, fontSize: 15, color: 'var(--text-primary)', flex: 1, minWidth: 0 }}>
          {cam.name}
        </span>

        {/* Enabled badge */}
        <span style={{
          fontSize: 10, padding: '2px 8px', borderRadius: '999px', fontWeight: 600,
          background: cam.enabled ? 'rgba(16,185,129,0.1)' : 'var(--bg-elevated)',
          color: cam.enabled ? 'var(--success)' : 'var(--text-muted)',
          border: cam.enabled ? '1px solid rgba(16,185,129,0.2)' : '1px solid var(--border)',
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
        }}>
          {cam.enabled ? 'enabled' : 'disabled'}
        </span>

        {/* Record mode badge */}
        <span style={{
          fontSize: 10, padding: '2px 8px', borderRadius: '999px', fontWeight: 500,
          background: 'var(--bg-elevated)', color: 'var(--text-muted)',
          border: '1px solid var(--border)',
        }}>
          {MODE_LABELS[cam.record_mode] ?? cam.record_mode}
        </span>

        {/* Group badge */}
        {cam.cam_group && (
          <span style={{
            fontSize: 10, padding: '2px 8px', borderRadius: '999px', fontWeight: 500,
            background: 'var(--bg-elevated)', color: 'var(--text-accent)',
            border: '1px solid var(--border)',
          }}>
            {cam.cam_group}
          </span>
        )}
      </div>

      {/* Card body */}
      <div style={{ padding: '12px 16px' }}>
        {/* IP + port */}
        <div style={{ fontSize: 12, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginBottom: 12 }}>
          {cam.host}:{cam.rtsp_port} &nbsp;·&nbsp; ch {cam.channel}
        </div>

        {/* Action buttons */}
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <button className="btn btn-secondary btn-sm" onClick={() => onTest(cam.id)}>
            Test connection
          </button>
          <button className="btn btn-secondary btn-sm" onClick={() => onSnapshot(cam.id)}>
            Snapshot
          </button>
          <button className="btn btn-secondary btn-sm" onClick={() => onZones(cam)}>
            Zones
          </button>
          <button className="btn btn-secondary btn-sm" onClick={() => onDetection(cam)}>
            Detection
          </button>
          <button
            className="btn btn-ghost btn-sm"
            title="Copy RTSP URL to clipboard"
            onClick={() => {
              const slug = cam.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '')
              const port = 8554  // go2rtc RTSP port
              const host = window.location.hostname
              const url = `rtsp://${host}:${port}/${slug}`
              navigator.clipboard.writeText(url).then(() => toast.success('RTSP URL copied'))
            }}
          >
            RTSP
          </button>
          <button className="btn btn-secondary btn-sm" onClick={() => onSyncTime(cam.id)} title="Sync camera clock to server time">
            Sync Time
          </button>
          <div style={{ flex: 1 }} />
          <button className="btn btn-ghost btn-sm" onClick={() => onEdit(cam)}>
            Edit
          </button>
          <button className="btn btn-danger btn-sm" onClick={() => onDelete(cam.id)}>
            Delete
          </button>
        </div>

        {/* Test result */}
        {testResult && (
          <div style={{
            marginTop: 10, padding: '8px 12px',
            borderRadius: 'var(--radius)',
            background: isOk ? 'var(--success-bg)' : isFail ? 'var(--danger-bg)' : 'var(--bg-elevated)',
            border: `1px solid ${isOk ? 'rgba(16,185,129,0.2)' : isFail ? 'rgba(239,68,68,0.2)' : 'var(--border)'}`,
            color: isOk ? 'var(--success)' : isFail ? 'var(--danger)' : 'var(--text-muted)',
            fontSize: 12, fontFamily: 'var(--font-mono)',
          }}>
            {testResult}
          </div>
        )}

        {/* Snapshot */}
        {snapshot === 'loading' && (
          <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-muted)', fontSize: 12 }}>
            <span className="spinner spinner-sm" />
            Loading snapshot…
          </div>
        )}
        {snapshot && snapshot !== 'loading' && !snapshot.startsWith('error') && (
          <img
            src={snapshot}
            alt={`Snapshot — ${cam.name}`}
            style={{
              marginTop: 10, width: '100%', maxHeight: 220,
              objectFit: 'cover', borderRadius: 'var(--radius)',
              border: '1px solid var(--border)',
            }}
          />
        )}
        {snapshot?.startsWith('error') && (
          <div style={{ marginTop: 8, fontSize: 12, color: 'var(--danger)' }}>
            {snapshot}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Add/Edit form modal ───────────────────────────────────────────────────────

interface FormModalProps {
  form: CameraCreate
  editId: number | null
  saving: boolean
  error: string | null
  onField: (k: keyof CameraCreate, v: unknown) => void
  onSave: () => void
  onCancel: () => void
}

function FormModal({ form, editId, saving, error, onField, onSave, onCancel }: FormModalProps) {
  const preset = CAM_MAKE_PRESETS.find(p => p.value === (form.cam_make ?? 'generic')) ?? CAM_MAKE_PRESETS[0]

  const handleCamMakeChange = (value: string) => {
    const p = CAM_MAKE_PRESETS.find(x => x.value === value)
    if (!p) return
    onField('cam_make', p.value)
    onField('main_path', p.main_path)
    onField('sub_path', p.sub_path)
    if (!p.showChannel) onField('channel', 1)
  }

  return (
    <div style={{
      position: 'fixed', inset: 0,
      background: 'var(--bg-overlay)',
      backdropFilter: 'blur(4px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 500, padding: 16,
    }}
    onClick={e => { if (e.target === e.currentTarget) onCancel() }}
    >
      <div style={{
        background: 'var(--bg-surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
        padding: '24px 24px 20px',
        width: '100%', maxWidth: 640,
        maxHeight: '90vh', overflowY: 'auto',
        boxShadow: 'var(--shadow-xl)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
          <h2 style={{ fontSize: 16, fontWeight: 700, color: 'var(--text-primary)' }}>
            {editId !== null ? 'Edit Camera' : 'Add Camera'}
          </h2>
          <button className="btn btn-ghost btn-sm" onClick={onCancel} aria-label="Close">&#x2715;</button>
        </div>

        {error && <div style={{ marginBottom: 16 }}><ErrorAlert message={error} /></div>}

        {/* Make / model — full width, above the grid */}
        <div style={{ marginBottom: 12 }}>
          <FormField label="Make / Model">
            <select
              value={form.cam_make ?? 'generic'}
              onChange={e => handleCamMakeChange(e.target.value)}
            >
              {CAM_MAKE_PRESETS.map(p => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
          </FormField>
        </div>

        <div className="grid-2" style={{ gap: 12 }}>
          <FormField label="Name" required>
            <input
              value={form.name}
              onChange={e => onField('name', e.target.value)}
              placeholder="Front Door"
            />
          </FormField>
          <FormField label="Host / IP" required>
            <input
              value={form.host}
              onChange={e => onField('host', e.target.value)}
              placeholder="192.168.1.64"
            />
          </FormField>
          <FormField label="RTSP Port">
            <input
              type="number"
              value={form.rtsp_port}
              onChange={e => onField('rtsp_port', Number(e.target.value))}
            />
          </FormField>
          {preset.showChannel && (
            <FormField label="Channel">
              <input
                type="number"
                value={form.channel}
                onChange={e => onField('channel', Number(e.target.value))}
              />
            </FormField>
          )}
          <FormField label="Username">
            <input
              value={form.rtsp_user}
              onChange={e => onField('rtsp_user', e.target.value)}
            />
          </FormField>
          <FormField label={editId !== null ? 'Password (leave blank to keep)' : 'Password'}>
            <input
              type="password"
              value={form.rtsp_pass}
              onChange={e => onField('rtsp_pass', e.target.value)}
              placeholder="&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;"
            />
          </FormField>
          <FormField label="Main stream path">
            <input
              value={form.main_path}
              onChange={e => onField('main_path', e.target.value)}
              placeholder={preset.value === 'generic' ? '/stream/main' : undefined}
            />
          </FormField>
          {!preset.singleStream && (
            <FormField label="Sub stream path">
              <input
                value={form.sub_path}
                onChange={e => onField('sub_path', e.target.value)}
              />
            </FormField>
          )}
          <FormField label="Record mode">
            <select value={form.record_mode} onChange={e => onField('record_mode', e.target.value)}>
              <option value="continuous">Continuous</option>
              <option value="motion-only">Motion only</option>
              <option value="continuous+events">Continuous + events</option>
            </select>
          </FormField>
          <FormField label="Group (optional)">
            <input
              value={form.cam_group ?? ''}
              onChange={e => onField('cam_group', e.target.value)}
              placeholder="Exterior, Interior, Front…"
            />
          </FormField>
          <FormField label="Retention (days)" help="Override global retention. Leave blank to use the system default.">
            <input
              type="number"
              min={1}
              max={365}
              value={form.retention_days ?? ''}
              onChange={e => onField('retention_days', e.target.value === '' ? null : parseInt(e.target.value))}
              placeholder="Global default"
            />
          </FormField>
          <div style={{ display: 'flex', alignItems: 'flex-end', paddingBottom: 2 }}>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer', fontSize: 13, color: 'var(--text-secondary)' }}>
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={e => onField('enabled', e.target.checked)}
              />
              Enabled
            </label>
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-end', paddingBottom: 2 }}>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer', fontSize: 13, color: 'var(--text-secondary)' }}>
              <input
                type="checkbox"
                checked={form.ptz_capable ?? false}
                onChange={e => onField('ptz_capable', e.target.checked)}
              />
              PTZ capable (ONVIF)
            </label>
          </div>
        </div>

        {/* Arm schedule */}
        <div style={{ marginTop: 16 }}>
          <FormField label="Arm schedule (JSON)" help='e.g. [{"start":"22:00","end":"06:00","days":[0,1,2,3,4]}] (days: 0=Mon … 6=Sun) — leave blank for always armed'>
            <input
              value={(() => { try { return JSON.parse(form.detect_config_json || '{}').arm_schedule ? JSON.stringify(JSON.parse(form.detect_config_json || '{}').arm_schedule) : '' } catch { return '' } })()}
              onChange={e => {
                try {
                  const cfg = JSON.parse(form.detect_config_json || '{}')
                  if (e.target.value) cfg.arm_schedule = JSON.parse(e.target.value)
                  else delete cfg.arm_schedule
                  onField('detect_config_json', JSON.stringify(cfg))
                } catch { /* invalid JSON — ignore */ }
              }}
              placeholder='Leave blank for always armed'
            />
          </FormField>
        </div>

        {/* Privacy masks */}
        <div style={{ marginTop: 8 }}>
          <FormField label="Privacy masks (JSON array of rectangles)" help='e.g. [{"x":0.1,"y":0.1,"w":0.3,"h":0.2}] — fractional coords, applied to detector feed'>
            <textarea
              rows={2}
              style={{ width: '100%', resize: 'vertical', fontFamily: 'monospace', fontSize: 12 }}
              value={form.privacy_masks_json ?? '[]'}
              onChange={e => onField('privacy_masks_json', e.target.value)}
              placeholder="[]"
            />
          </FormField>
        </div>

        <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
          <button
            className="btn btn-primary"
            onClick={onSave}
            disabled={saving || !form.name || !form.host}
          >
            {saving ? (
              <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span className="spinner spinner-sm" style={{ borderTopColor: '#fff', borderColor: 'rgba(255,255,255,0.3)' }} />
                {editId !== null ? 'Updating…' : 'Adding…'}
              </span>
            ) : (editId !== null ? 'Update Camera' : 'Add Camera')}
          </button>
          <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export function CamerasPage() {
  const toast = useToast()
  const [cameras, setCameras] = useState<Camera[]>([])
  const [form, setForm] = useState<CameraCreate>(DEFAULT_FORM)
  const [editId, setEditId] = useState<number | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [discovered, setDiscovered] = useState<DiscoveredDevice[]>([])
  const [scanning, setScanning] = useState(false)
  const [testResult, setTestResult] = useState<Record<number, string>>({})
  const [snapshot, setSnapshot] = useState<Record<number, string>>({})
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [pageError, setPageError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [zoneEditorCam, setZoneEditorCam] = useState<Camera | null>(null)
  const [detectionCam, setDetectionCam] = useState<Camera | null>(null)

  const loadCameras = async () => {
    try {
      const cams = await camerasApi.list()
      setCameras(cams)
      setPageError(null)
    } catch (e) {
      setPageError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadCameras() }, [])

  const setField = (k: keyof CameraCreate, v: unknown) => setForm(f => ({ ...f, [k]: v }))

  const openAdd = (prefill?: Partial<CameraCreate>) => {
    setForm({ ...DEFAULT_FORM, ...prefill })
    setEditId(null)
    setFormError(null)
    setShowForm(true)
  }

  const openEdit = (cam: Camera) => {
    setForm({
      name: cam.name, host: cam.host, rtsp_port: cam.rtsp_port,
      rtsp_user: cam.rtsp_user, rtsp_pass: '',
      main_path: cam.main_path, sub_path: cam.sub_path,
      channel: cam.channel, record_mode: cam.record_mode, enabled: cam.enabled,
      cam_type: cam.cam_type ?? 'rtsp',
      cam_make: cam.cam_make ?? 'generic',
      cam_group: cam.cam_group ?? '',
      retention_days: cam.retention_days ?? null,
      ptz_capable: cam.ptz_capable ?? false,
      ptz_config_json: cam.ptz_config_json ?? '{}',
      privacy_masks_json: cam.privacy_masks_json ?? '[]',
    })
    setEditId(cam.id)
    setFormError(null)
    setShowForm(true)
  }

  const cancelForm = () => { setShowForm(false); setEditId(null) }

  const saveCamera = async () => {
    setSaving(true)
    setFormError(null)
    try {
      if (editId !== null) {
        await camerasApi.update(editId, form)
        toast.success('Camera updated')
      } else {
        await camerasApi.create(form)
        toast.success('Camera added')
      }
      setShowForm(false)
      setEditId(null)
      await loadCameras()
    } catch (e) {
      setFormError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const deleteCamera = async (id: number) => {
    if (!window.confirm('Delete this camera?')) return
    try {
      await camerasApi.delete(id)
      toast.success('Camera deleted')
      await loadCameras()
    } catch (e) {
      toast.error(String(e))
    }
  }

  const testCamera = async (id: number) => {
    setTestResult(r => ({ ...r, [id]: 'Testing…' }))
    try {
      const res = await camerasApi.test(id)
      const msg = res.ok
        ? `OK — ${res.stream_count} stream(s): ${res.streams?.map(s => `${s.codec_name}(${s.codec_type})`).join(', ')}`
        : `FAIL: ${res.error}`
      setTestResult(r => ({ ...r, [id]: msg }))
      if (res.ok) toast.success('Connection test passed')
      else toast.error('Connection test failed')
    } catch (e) {
      setTestResult(r => ({ ...r, [id]: `FAIL: ${e}` }))
    }
  }

  const loadSnapshot = async (id: number) => {
    setSnapshot(s => ({ ...s, [id]: 'loading' }))
    try {
      const token = localStorage.getItem('sentinel_token')
      const resp = await fetch(`/api/cameras/${id}/snapshot`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      setSnapshot(s => ({ ...s, [id]: url }))
    } catch (e) {
      setSnapshot(s => ({ ...s, [id]: `error: ${e}` }))
    }
  }

  const scanNetwork = async () => {
    setScanning(true)
    setDiscovered([])
    setPageError(null)
    try {
      const res = await camerasApi.discover()
      setDiscovered(res.devices)
      if (res.devices.length === 0) toast.info('No devices discovered on the network')
      else toast.success(`Discovered ${res.devices.length} device(s)`)
    } catch (e) {
      setPageError(String(e))
      toast.error('Network scan failed')
    } finally {
      setScanning(false)
    }
  }

  const handleSyncTime = async (id: number) => {
    try {
      const result = await camerasApi.syncTime(id)
      toast.success(`Clock synced via ${result.method} — ${result.utc_time}`)
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : 'Time sync failed')
    }
  }

  const saveDetection = async (camId: number, cfg: DetectConfig) => {
    const existingCam = cameras.find(c => c.id === camId)
    const existing = parseDetectConfig(existingCam?.detect_config_json || '{}')
    const merged = { ...existing, ...cfg }
    await camerasApi.update(camId, { detect_config_json: JSON.stringify(merged) })
    toast.success('Detection settings saved')
    setDetectionCam(null)
    await loadCameras()
  }

  if (loading) return <LoadingState message="Loading cameras…" />

  return (
    <div style={{ maxWidth: 920 }}>
      <PageHeader title="Cameras">
        <button className="btn btn-primary" onClick={() => openAdd()}>
          + Add Camera
        </button>
        <button
          className="btn btn-secondary"
          onClick={scanNetwork}
          disabled={scanning}
        >
          {scanning ? (
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span className="spinner spinner-sm" />
              Scanning…
            </span>
          ) : (
            <>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
              Scan Network
            </>
          )}
        </button>
      </PageHeader>

      {pageError && <div style={{ marginBottom: 16 }}><ErrorAlert message={pageError} /></div>}

      {/* Discovery results */}
      {discovered.length > 0 && (
        <SectionCard title={`Discovered Devices (${discovered.length})`}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {discovered.map(d => (
              <div key={d.ip} style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '8px 4px',
                borderBottom: '1px solid var(--border-subtle)',
                flexWrap: 'wrap',
              }}>
                <code style={{ fontSize: 12, color: 'var(--text-accent)', fontFamily: 'var(--font-mono)', minWidth: 120 }}>
                  {d.ip}
                </code>
                <span style={{ color: 'var(--text-secondary)', fontSize: 12, flex: 1 }}>
                  {d.model || d.name || '—'}
                  {d.mac && <span style={{ color: 'var(--text-muted)' }}> ({d.mac})</span>}
                  {d.open_ports.length > 0 && <span style={{ color: 'var(--text-muted)' }}> · ports {d.open_ports.join(', ')}</span>}
                  <span style={{ marginLeft: 6, fontSize: 10, padding: '1px 6px', borderRadius: 999, background: 'var(--bg-elevated)', color: 'var(--text-muted)', border: '1px solid var(--border)' }}>
                    {d.source}
                  </span>
                </span>
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={() => openAdd({ host: d.ip, name: d.name || d.model || d.ip })}
                >
                  Add
                </button>
              </div>
            ))}
          </div>
        </SectionCard>
      )}

      {/* Camera list */}
      {cameras.length === 0 ? (
        <div style={{
          background: 'var(--bg-surface)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
        }}>
          <EmptyState
            icon="📷"
            title="No cameras yet"
            body="Scan your network to auto-discover Hikvision cameras, or add one manually with an RTSP address."
            action={
              <div style={{ display: 'flex', gap: 8, justifyContent: 'center', flexWrap: 'wrap' }}>
                <button className="btn btn-primary" onClick={() => openAdd()}>+ Add Camera</button>
                <button className="btn btn-secondary" onClick={scanNetwork} disabled={scanning}>
                  Scan Network
                </button>
              </div>
            }
          />
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {cameras.map(cam => (
            <CameraCard
              key={cam.id}
              cam={cam}
              testResult={testResult[cam.id]}
              snapshot={snapshot[cam.id]}
              onTest={testCamera}
              onSnapshot={loadSnapshot}
              onEdit={openEdit}
              onDelete={deleteCamera}
              onZones={setZoneEditorCam}
              onDetection={setDetectionCam}
              onSyncTime={handleSyncTime}
            />
          ))}
        </div>
      )}

      {/* Add/Edit form modal */}
      {showForm && (
        <FormModal
          form={form}
          editId={editId}
          saving={saving}
          error={formError}
          onField={setField}
          onSave={saveCamera}
          onCancel={cancelForm}
        />
      )}

      {/* Detection settings modal */}
      {detectionCam && (
        <DetectionModal
          cam={detectionCam}
          onSave={saveDetection}
          onClose={() => setDetectionCam(null)}
        />
      )}

      {/* Zone editor modal */}
      {zoneEditorCam && (
        <ZoneEditor
          cameraId={zoneEditorCam.id}
          snapshotUrl={withToken(`/api/cameras/${zoneEditorCam.id}/snapshot`)}
          initialJson={zoneEditorCam.detect_config_json || '{}'}
          onSave={async (json) => {
            try {
              await eventsApi.updateDetectZone(zoneEditorCam.id, json)
              await loadCameras()
              setZoneEditorCam(null)
              toast.success('Detection zones saved')
            } catch (e) {
              toast.error(String(e))
            }
          }}
          onClose={() => setZoneEditorCam(null)}
        />
      )}
    </div>
  )
}
