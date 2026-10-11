/**
 * Typed API client for Argus NVR backend.
 * All requests hit /api/* — the Vite proxy or FastAPI static serving handles routing.
 *
 * Auth: token is stored in localStorage under STORAGE_KEY.
 * Every request automatically attaches "Authorization: Bearer <token>".
 * On 401 responses the token is cleared and a redirect to /login is triggered.
 */

const BASE = '/api'
const STORAGE_KEY = 'sentinel_token'

// ---------------------------------------------------------------------------
// Token management
// ---------------------------------------------------------------------------

export function getToken(): string | null {
  return localStorage.getItem(STORAGE_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(STORAGE_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(STORAGE_KEY)
}

export function isAuthenticated(): boolean {
  return Boolean(getToken())
}

export function withToken(url: string): string {
  const token = getToken()
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}

// ---------------------------------------------------------------------------
// Core request helper
// ---------------------------------------------------------------------------

async function requestBlob(method: string, path: string, body?: unknown): Promise<Blob> {
  const token = getToken()
  const res = await fetch(`/api${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text}`)
  }
  return res.blob()
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  skipAuth = false,
): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (!skipAuth) {
    const token = getToken()
    if (token) headers['Authorization'] = `Bearer ${token}`
  }

  const opts: RequestInit = { method, headers }
  if (body !== undefined) {
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`${BASE}${path}`, opts)

  if (res.status === 401 && !skipAuth) {
    // Token expired or invalid — clear and redirect to login.
    clearToken()
    window.location.href = '/login'
    throw new Error('Unauthenticated — redirecting to login')
  }

  if (!res.ok) {
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Camera {
  id: number
  name: string
  host: string
  rtsp_port: number
  rtsp_user: string
  main_path: string
  sub_path: string
  channel: number
  record_mode: string
  enabled: boolean
  detect_config_json: string
  cam_type: string
  cam_make: string
  cam_group: string
  created_at: string
  retention_days: number | null
  ptz_capable: boolean
  ptz_config_json: string
  privacy_masks_json: string
}

export interface CameraCreate {
  name: string
  host: string
  rtsp_port?: number
  rtsp_user?: string
  rtsp_pass?: string
  main_path?: string
  sub_path?: string
  channel?: number
  record_mode?: string
  enabled?: boolean
  cam_type?: string
  cam_make?: string
  detect_config_json?: string
  cam_group?: string
  retention_days?: number | null
  ptz_capable?: boolean
  ptz_config_json?: string
  privacy_masks_json?: string
}

export interface StreamInfo {
  camera_id: number
  camera_name: string
  stream_name: string
  sub_stream_name: string
  webrtc_url: string
  mse_url: string
  hls_url: string
  rtsp_url: string
  go2rtc_api: string
  is_recording: boolean
  /** True whenever the recorder service is running (armed), even if the motion gate is closed. */
  is_armed?: boolean
}

export interface CameraDay {
  date: string       // "YYYY-MM-DD"
  recordings: number
  events: number
}

export interface DiscoveredDevice {
  ip: string
  mac: string
  model: string
  name: string
  onvif_url: string
  open_ports: number[]
  source: string
}

export interface DiscoverResult {
  count: number
  devices: DiscoveredDevice[]
}

export interface SupervisorStatus {
  name: string
  status: string
  pid: number | null
  restarts: number
}

export interface StorageStatus {
  disk_total_bytes: number
  disk_used_bytes: number
  disk_free_bytes: number
  free_pct: number
  recordings_total_bytes: number
  segment_count: number
  gb_per_day: number
  projected_retention_days: number | null
  retention_max_age_days: number
  free_space_floor_pct: number
  error?: string
}

export interface HealthResponse {
  status: string
  uptime_s: number
  platform: string
  python: string
  db_tables: string[]
  go2rtc: SupervisorStatus
  recorders: SupervisorStatus[]
  detectors: SupervisorStatus[]
  supervisors: SupervisorStatus[]
  storage: StorageStatus
}

export interface TestResult {
  ok: boolean
  stream_count?: number
  streams?: Array<{
    index: number
    codec_name: string
    codec_type: string
    width?: number
    height?: number
    r_frame_rate?: string
  }>
  error?: string
}

// ---------------------------------------------------------------------------
// Camera API
// ---------------------------------------------------------------------------

export const camerasApi = {
  list: () => request<Camera[]>('GET', '/cameras'),
  get: (id: number) => request<Camera>('GET', `/cameras/${id}`),
  create: (data: CameraCreate) => request<Camera>('POST', '/cameras', data),
  update: (id: number, data: Partial<CameraCreate>) =>
    request<Camera>('PUT', `/cameras/${id}`, data),
  delete: (id: number) => request<void>('DELETE', `/cameras/${id}`),
  test: (id: number) => request<TestResult>('POST', `/cameras/${id}/test`),
  discover: () => request<DiscoverResult>('POST', '/cameras/discover'),
  days: (id: number, month: string) =>
    request<CameraDay[]>('GET', `/cameras/${id}/days?month=${encodeURIComponent(month)}`),
  syncTime: (id: number) =>
    request<{ ok: boolean; method: string; utc_time: string }>('POST', `/cameras/${id}/sync-time`),
}

// ---------------------------------------------------------------------------
// Live API
// ---------------------------------------------------------------------------

export const liveApi = {
  list: () => request<StreamInfo[]>('GET', '/live'),
  get: (id: number) => request<StreamInfo>('GET', `/live/${id}`),
}

// ---------------------------------------------------------------------------
// Events types + API
// ---------------------------------------------------------------------------

export interface ArgusEvent {
  id: number
  camera_id: number
  type: string
  label: string | null
  score: number | null
  started_at: string
  ended_at: string | null
  thumb_path: string | null
  clip_path: string | null
  recording_id: number | null
  meta_json: string
  reviewed_at: string | null
  person_id: number | null
}

export interface EventsResult {
  total: number
  limit: number
  offset: number
  events: ArgusEvent[]
}

export const eventsApi = {
  list: (params?: {
    camera_id?: number
    type?: string
    label?: string
    person_id?: number
    started_after?: string
    started_before?: string
    limit?: number
    offset?: number
  }) => {
    const qs = new URLSearchParams()
    if (params?.camera_id != null) qs.set('camera_id', String(params.camera_id))
    if (params?.type) qs.set('type', params.type)
    if (params?.label) qs.set('label', params.label)
    if (params?.person_id != null) qs.set('person_id', String(params.person_id))
    if (params?.started_after) qs.set('started_after', params.started_after)
    if (params?.started_before) qs.set('started_before', params.started_before)
    if (params?.limit != null) qs.set('limit', String(params.limit))
    if (params?.offset != null) qs.set('offset', String(params.offset))
    const q = qs.toString()
    return request<EventsResult>('GET', `/events${q ? '?' + q : ''}`)
  },
  get: (id: number) => request<ArgusEvent>('GET', `/events/${id}`),
  thumbUrl: (id: number) => `/api/events/${id}/thumb`,
  clipUrl: (id: number) => `/api/events/${id}/clip`,
  reviewEvent: (id: number, reviewed: boolean) =>
    request<{ id: number; reviewed_at: string | null }>('PATCH', `/events/${id}/reviewed`, { reviewed }),
  unreviewedCount: () => request<{ count: number }>('GET', '/events/unreviewed-count'),
  updateDetectZone: (cameraId: number, detectConfigJson: string) =>
    request<{ camera_id: number; detect_config_json: string }>(
      'PUT',
      `/events/cameras/${cameraId}/detect-zone`,
      { detect_config_json: detectConfigJson },
    ),
}

// ---------------------------------------------------------------------------
// Recordings types + API
// ---------------------------------------------------------------------------

export interface Recording {
  id: number
  camera_id: number
  path: string
  started_at: string
  ended_at: string | null
  duration_s: number | null
  size_bytes: number | null
  has_gap: boolean
}

export interface RecordingsResult {
  total: number
  limit: number
  offset: number
  recordings: Recording[]
}

export const recordingsApi = {
  list: (params?: {
    camera_id?: number
    started_after?: string
    started_before?: string
    limit?: number
    offset?: number
  }) => {
    const qs = new URLSearchParams()
    if (params?.camera_id != null) qs.set('camera_id', String(params.camera_id))
    if (params?.started_after) qs.set('started_after', params.started_after)
    if (params?.started_before) qs.set('started_before', params.started_before)
    if (params?.limit != null) qs.set('limit', String(params.limit))
    if (params?.offset != null) qs.set('offset', String(params.offset))
    const q = qs.toString()
    return request<RecordingsResult>('GET', `/recordings${q ? '?' + q : ''}`)
  },
  get: (id: number) => request<Recording>('GET', `/recordings/${id}`),
  videoUrl: (id: number) => `/api/recordings/${id}/video`,
}

// ---------------------------------------------------------------------------
// Timeline + Export API
// ---------------------------------------------------------------------------

export interface TimelineSegment {
  id: number
  path: string
  url: string
  started_at: string
  ended_at: string | null
  duration_s: number | null
  has_gap: boolean
}

export interface TimelineEvent {
  id: number
  type: string
  label: string | null
  score: number | null
  started_at: string
  ended_at: string | null
  thumb_url: string | null
}

export interface TimelineResult {
  camera_id: number
  start: string
  end: string
  segments: TimelineSegment[]
  events: TimelineEvent[]
}

export const timelineApi = {
  get: (params: { camera_id: number; start: string; end: string }) => {
    const qs = new URLSearchParams({
      camera_id: String(params.camera_id),
      start: params.start,
      end: params.end,
    })
    return request<TimelineResult>('GET', `/timeline?${qs}`)
  },

  exportUrl: () => `/api/export`,

  timelapse: (params: { camera_id: number; start: string; end: string; speed: number }) =>
    requestBlob('POST', '/timeline/timelapse', params),

  exportClip: async (params: {
    camera_id: number
    start: string
    end: string
  }): Promise<void> => {
    const token = getToken()
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (token) headers['Authorization'] = `Bearer ${token}`
    const res = await fetch('/api/export', {
      method: 'POST',
      headers,
      body: JSON.stringify(params),
    })
    if (!res.ok) {
      const text = await res.text()
      throw new Error(`HTTP ${res.status}: ${text}`)
    }
    // Trigger browser download from blob.
    const blob = await res.blob()
    const disposition = res.headers.get('Content-Disposition') ?? ''
    const nameMatch = disposition.match(/filename="([^"]+)"/)
    const filename = nameMatch ? nameMatch[1] : 'clip.mp4'
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },
}

// ---------------------------------------------------------------------------
// System API
// ---------------------------------------------------------------------------

export const systemApi = {
  health: () => request<HealthResponse>('GET', '/health', undefined, true),
  storage: () => request<StorageStatus>('GET', '/system/storage'),
  purge: (password: string) => request<{ ok: boolean; deleted_files: number; deleted_bytes: number }>('POST', '/system/purge', { password }),
}

// ---------------------------------------------------------------------------
// Auth API (Phase 7)
// ---------------------------------------------------------------------------

export interface LoginResponse {
  access_token: string
  token_type: string
  username: string
  role: string
  must_change_password: boolean
}

export interface CurrentUser {
  id: string
  username: string
  role: string
  must_change_password: boolean
}

const MUST_CHANGE_KEY = 'sentinel_must_change_password'

export function setMustChangePassword(value: boolean): void {
  if (value) {
    localStorage.setItem(MUST_CHANGE_KEY, '1')
  } else {
    localStorage.removeItem(MUST_CHANGE_KEY)
  }
}

export function mustChangePassword(): boolean {
  return localStorage.getItem(MUST_CHANGE_KEY) === '1'
}

export const authApi = {
  login: (username: string, password: string) =>
    request<LoginResponse>('POST', '/auth/login', { username, password }, true),

  logout: () => request<{ message: string }>('POST', '/auth/logout'),

  me: () => request<CurrentUser>('GET', '/auth/me'),

  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ message: string; access_token?: string }>('PUT', '/auth/password', {
      current_password: currentPassword,
      new_password: newPassword,
    }),
}

// ---------------------------------------------------------------------------
// Settings + Notifications API (Phase 6)
// ---------------------------------------------------------------------------

export interface SettingsMap {
  [key: string]: string
}

export interface TestNotifyResult {
  ok: boolean
  error?: string
}

export interface NotifyHistory {
  total: number
  limit: number
  offset: number
  notifications: Array<{
    id: number
    event_id: number
    channel: string
    status: string
    sent_at: string | null
    camera_id: number | null
    started_at: string | null
    event_type: string | null
  }>
}

export const settingsApi = {
  getSettings: () => request<SettingsMap>('GET', '/settings'),

  updateSettings: (s: SettingsMap) =>
    request<{ updated: number; keys: string[] }>('PUT', '/settings', { settings: s }),

  testNotify: (channel: string) =>
    request<TestNotifyResult>('POST', `/settings/notify/test/${channel}`),

  getNotifyHistory: (params?: { limit?: number; offset?: number; channel?: string; status?: string }) => {
    const qs = new URLSearchParams()
    if (params?.limit != null) qs.set('limit', String(params.limit))
    if (params?.offset != null) qs.set('offset', String(params.offset))
    if (params?.channel) qs.set('channel', params.channel)
    if (params?.status) qs.set('status', params.status)
    const q = qs.toString()
    return request<NotifyHistory>('GET', `/settings/notify/history${q ? '?' + q : ''}`)
  },
}

// ---------------------------------------------------------------------------
// Persons (face recognition) API
// ---------------------------------------------------------------------------

export interface Person {
  id: number
  name: string
  created_at: string
  encoding_count: number
  event_count: number
}

export interface AssignPersonResult {
  person: Person
  encoding_id: number
  event_id: number
}

export const personsApi = {
  list: () => request<Person[]>('GET', '/persons'),
  create: (name: string) => request<Person>('POST', '/persons', { name }),
  rename: (id: number, name: string) => request<Person>('PATCH', `/persons/${id}`, { name }),
  delete: (id: number) => request<void>('DELETE', `/persons/${id}`),
  enroll: (personId: number, eventId: number) =>
    request<{ encoding_id: number; person_id: number; event_id: number }>(
      'POST', `/persons/${personId}/enroll`, { event_id: eventId }
    ),
  removeEncoding: (personId: number, encId: number) =>
    request<void>('DELETE', `/persons/${personId}/encodings/${encId}`),
  assignPerson: (eventId: number, name: string) =>
    request<AssignPersonResult>('POST', `/events/${eventId}/assign-person`, { name }),
}

// ---------------------------------------------------------------------------
// Users API (admin-only)
// ---------------------------------------------------------------------------

export interface User {
  id: number
  username: string
  role: 'admin' | 'operator' | 'viewer'
  created_at: string
  must_change_password: boolean
}

export const usersApi = {
  list: () => request<User[]>('GET', '/users'),
  get: (id: number) => request<User>('GET', `/users/${id}`),
  create: (data: { username: string; password: string; role: string; must_change_password?: boolean }) =>
    request<User>('POST', '/users', data),
  update: (id: number, data: { role?: string; must_change_password?: boolean }) =>
    request<User>('PUT', `/users/${id}`, data),
  delete: (id: number) => request<void>('DELETE', `/users/${id}`),
  resetPassword: (id: number, new_password: string, must_change_password = true) =>
    request<{ ok: boolean }>('POST', `/users/${id}/reset-password`, { new_password, must_change_password }),
}

// ---------------------------------------------------------------------------
// Audit log API (admin-only)
// ---------------------------------------------------------------------------

export interface AuditEntry {
  id: number
  user_id: number | null
  username: string
  action: string
  target_type: string | null
  target_id: number | null
  detail: string | null
  ip: string | null
  created_at: string
}

export interface AuditResult {
  total: number
  limit: number
  offset: number
  entries: AuditEntry[]
}

export const auditApi = {
  list: (params?: { limit?: number; offset?: number; action?: string; username?: string }) => {
    const qs = new URLSearchParams()
    if (params?.limit != null) qs.set('limit', String(params.limit))
    if (params?.offset != null) qs.set('offset', String(params.offset))
    if (params?.action) qs.set('action', params.action)
    if (params?.username) qs.set('username', params.username)
    const q = qs.toString()
    return request<AuditResult>('GET', `/audit${q ? '?' + q : ''}`)
  },
  purge: () => request<{ ok: boolean }>('DELETE', '/audit'),
}

// ---------------------------------------------------------------------------
// PTZ API
// ---------------------------------------------------------------------------

export interface PTZPreset { token: string; name: string }

export const ptzApi = {
  presets: (cameraId: number) =>
    request<{ presets: PTZPreset[] }>('GET', `/cameras/${cameraId}/ptz/presets`),
  move: (cameraId: number, pan: number, tilt: number, zoom = 0, speed = 0.5) =>
    request<{ ok: boolean }>('POST', `/cameras/${cameraId}/ptz/move`, { pan, tilt, zoom, speed_pan: speed, speed_tilt: speed }),
  goto: (cameraId: number, preset_token: string) =>
    request<{ ok: boolean }>('POST', `/cameras/${cameraId}/ptz/goto`, { preset_token }),
  stop: (cameraId: number) =>
    request<{ ok: boolean }>('POST', `/cameras/${cameraId}/ptz/stop`),
}

// ---------------------------------------------------------------------------
// Heatmap API
// ---------------------------------------------------------------------------

export const heatmapApi = {
  url: (cameraId: number, width = 640, height = 360) =>
    withToken(`/api/cameras/${cameraId}/heatmap?width=${width}&height=${height}`),
  reset: (cameraId: number) => request<void>('DELETE', `/cameras/${cameraId}/heatmap`),
}

// ---------------------------------------------------------------------------
// Web Push API
// ---------------------------------------------------------------------------

export const webpushApi = {
  getVapidKey: () => request<{ public_key: string; available: boolean }>('GET', '/webpush/vapid-public-key'),
  subscribe: (data: { endpoint: string; p256dh: string; auth: string; user_agent?: string }) =>
    request<{ ok: boolean }>('POST', '/webpush/subscribe', data),
  unsubscribe: (data: { endpoint: string; p256dh: string; auth: string }) =>
    request<{ ok: boolean }>('DELETE', '/webpush/subscribe', data),
  testPush: () => request<{ ok: boolean; sent?: number; errors?: string[] }>('POST', '/webpush/test'),
}

// ---------------------------------------------------------------------------
// Cloud Backup API
// ---------------------------------------------------------------------------

export interface BackupRun {
  id: number
  started_at: string
  ended_at: string | null
  status: string
  bytes_transferred: number | null
  files_transferred: number | null
  error_msg: string | null
}

export const cloudBackupApi = {
  trigger: () => request<{ ok: boolean; run_id?: number; started_at?: string; error?: string }>('POST', '/cloud-backup/trigger'),
  history: (limit = 50) => request<{ runs: BackupRun[] }>('GET', `/cloud-backup/history?limit=${limit}`),
  status: () => request<{ has_run: boolean; running: boolean; status?: string; started_at?: string; error_msg?: string }>('GET', '/cloud-backup/status'),
}

// ---------------------------------------------------------------------------
// Smart Search API
// ---------------------------------------------------------------------------

export interface SearchResult {
  id: number
  camera_id: number
  camera_name: string
  type: string
  label: string | null
  score: number | null
  started_at: string
  ended_at: string | null
  thumb_url: string | null
  recording_id: number | null
  rec_started_at: string | null
  reviewed_at: string | null
}

export interface SearchResponse {
  total: number
  limit: number
  offset: number
  results: SearchResult[]
}

export const searchApi = {
  search: (params: {
    q?: string
    camera_id?: number
    event_type?: string
    started_after?: string
    started_before?: string
    min_score?: number
    has_recording?: boolean
    limit?: number
    offset?: number
  }) => {
    const qs = new URLSearchParams()
    if (params.q) qs.set('q', params.q)
    if (params.camera_id != null) qs.set('camera_id', String(params.camera_id))
    if (params.event_type) qs.set('event_type', params.event_type)
    if (params.started_after) qs.set('started_after', params.started_after)
    if (params.started_before) qs.set('started_before', params.started_before)
    if (params.min_score != null) qs.set('min_score', String(params.min_score))
    if (params.has_recording != null) qs.set('has_recording', String(params.has_recording))
    if (params.limit != null) qs.set('limit', String(params.limit))
    if (params.offset != null) qs.set('offset', String(params.offset))
    return request<SearchResponse>('GET', `/recordings/search?${qs}`)
  },
}

// ---------------------------------------------------------------------------
// Camera Health API
// ---------------------------------------------------------------------------

export interface CameraHealth {
  id: number
  name: string
  enabled: boolean
  record_mode: string
  recorder_status: string
  detector_status: string
  last_recording_at: string | null
  last_recording_ended: string | null
  events_last_hour: number
}

export const cameraHealthApi = {
  list: () => request<{ cameras: CameraHealth[] }>('GET', '/system/camera-health'),
}
