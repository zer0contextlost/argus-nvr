/**
 * WebRTCPlayer — WebRTC-only live stream player using trickle ICE.
 *
 * Uses go2rtc's /api/ws WebSocket for signaling instead of the single-shot
 * HTTP POST, so ICE candidates are exchanged properly via trickle ICE.
 * This fixes remote viewing: the browser's STUN-discovered candidates reach
 * go2rtc, and go2rtc's candidates (including its server-reflexive address)
 * reach the browser.
 *
 * MSE fallback was removed because MSE connections being torn down triggered
 * a nil-pointer panic in go2rtc 1.9.14's mp4/consumer.go:82.
 */

import React, { useEffect, useRef, useState, useCallback } from 'react'
import type { StreamInfo } from '../api/client'
import { ptzApi, withToken } from '../api/client'

interface Props {
  stream: StreamInfo
  style?: React.CSSProperties
  onConnectionChange?: (connected: boolean) => void
  isRecording?: boolean
  cameraId?: number
  ptzCapable?: boolean
}

const STALL_TIMEOUT_MS = 8000
const WATCHDOG_INTERVAL_MS = 4000
const RETRY_BASE_MS = 1000
const RETRY_MAX_MS = 15000
const ICE_TIMEOUT_MS = 15000
// After this many consecutive failed attempts, show "Camera offline" and drop
// to a slow background retry instead of the fast reconnect loop.
const OFFLINE_AFTER_FAILURES = 4
const OFFLINE_RETRY_MS = 60000
// Only forgive past failures once the stream has played this long without
// dropping, so a camera that connects then dies immediately keeps backing off.
const STABLE_PLAY_MS = 30000

export function WebRTCPlayer({ stream, style, onConnectionChange, isRecording, cameraId, ptzCapable }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const pcRef = useRef<RTCPeerConnection | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const [mode, setMode] = useState<'webrtc' | 'offline'>('webrtc')
  const [reconnecting, setReconnecting] = useState(false)
  // False until the first track arrives for the current stream, so the initial
  // connect shows a spinner instead of a bare black box.
  const [connected, setConnected] = useState(false)
  const [useSubStream, setUseSubStream] = useState(false)
  const [showHeatmap, setShowHeatmap] = useState(false)
  const [showPtz, setShowPtz] = useState(false)
  const [snapping, setSnapping] = useState(false)

  const iceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastProgressRef = useRef<{ t: number; at: number }>({ t: 0, at: 0 })
  const stableTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Set when we need to (re)start once the <video> is mounted (leaving offline mode).
  const pendingStartRef = useRef(false)
  const retryCountRef = useRef(0)
  const destroyedRef = useRef(false)
  const genRef = useRef(0)
  const onConnChangeRef = useRef(onConnectionChange)
  useEffect(() => { onConnChangeRef.current = onConnectionChange }, [onConnectionChange])

  const getToken = () =>
    (typeof window !== 'undefined' && window.localStorage.getItem('sentinel_token')) || ''

  function clearTimers() {
    if (iceTimerRef.current) { clearTimeout(iceTimerRef.current); iceTimerRef.current = null }
    if (watchdogRef.current) { clearInterval(watchdogRef.current); watchdogRef.current = null }
    if (retryTimerRef.current) { clearTimeout(retryTimerRef.current); retryTimerRef.current = null }
    if (stableTimerRef.current) { clearTimeout(stableTimerRef.current); stableTimerRef.current = null }
  }

  // Called whenever playback is seen progressing; resets the backoff only after
  // the stream has stayed up for STABLE_PLAY_MS (timer is cleared on any restart).
  function markPlaying() {
    if (stableTimerRef.current) return
    stableTimerRef.current = setTimeout(() => {
      stableTimerRef.current = null
      retryCountRef.current = 0
    }, STABLE_PLAY_MS)
  }

  // The offline view has no <video>, so leaving it must wait for the re-render.
  function resumeFromOffline() {
    pendingStartRef.current = true
    setMode('webrtc')
  }

  function teardownPc() {
    if (wsRef.current) {
      try { wsRef.current.close() } catch { /* ignore */ }
      wsRef.current = null
    }
    if (pcRef.current) {
      try { pcRef.current.close() } catch { /* ignore */ }
      pcRef.current = null
    }
  }

  function scheduleRestart(reason: string) {
    if (destroyedRef.current) return
    genRef.current += 1
    clearTimers()
    teardownPc()
    onConnChangeRef.current?.(false)
    const delay = Math.min(RETRY_BASE_MS * 2 ** retryCountRef.current, RETRY_MAX_MS)
    retryCountRef.current += 1
    if (retryCountRef.current >= OFFLINE_AFTER_FAILURES) {
      console.warn(`stream offline (${reason}) for ${stream.stream_name}, retrying in ${OFFLINE_RETRY_MS}ms`)
      setReconnecting(false)
      setConnected(false)
      setMode('offline')
      retryTimerRef.current = setTimeout(() => {
        if (destroyedRef.current) return
        resumeFromOffline()
      }, OFFLINE_RETRY_MS)
      return
    }
    setReconnecting(true)
    console.warn(`stream restart (${reason}) for ${stream.stream_name} in ${delay}ms`)
    retryTimerRef.current = setTimeout(() => {
      if (destroyedRef.current) return
      setMode('webrtc')
      startWebRTC()
    }, delay)
  }

  function startWatchdog() {
    if (watchdogRef.current) clearInterval(watchdogRef.current)
    lastProgressRef.current = { t: videoRef.current?.currentTime ?? 0, at: Date.now() }
    watchdogRef.current = setInterval(() => {
      const v = videoRef.current
      if (!v || destroyedRef.current) return
      const now = Date.now()
      const cur = v.currentTime
      if (cur > lastProgressRef.current.t + 0.01) {
        lastProgressRef.current = { t: cur, at: now }
        markPlaying()
      } else if (now - lastProgressRef.current.at > STALL_TIMEOUT_MS) {
        scheduleRestart('watchdog-stall')
      }
    }, WATCHDOG_INTERVAL_MS)
  }

  function attachVideoHandlers() {
    const v = videoRef.current
    if (!v) return
    v.onerror = () => scheduleRestart('video-error')
    v.onended = () => scheduleRestart('video-ended')
    v.onstalled = () => { console.debug('video stalled', stream.stream_name) }
    v.onplaying = () => { markPlaying(); startWatchdog() }
  }

  async function startWebRTC() {
    const myGen = genRef.current
    attachVideoHandlers()

    try {
      const pc = new RTCPeerConnection({
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
      })
      pcRef.current = pc

      pc.oniceconnectionstatechange = () => {
        if (genRef.current !== myGen) return
        const s = pc.iceConnectionState
        // 'disconnected' is transient — the ICE agent may recover on its own.
        // Only hard-restart on 'failed' or 'closed'; let the watchdog catch true stalls.
        if (s === 'failed' || s === 'closed') {
          console.warn(`WebRTC ICE ${s}, retrying`)
          scheduleRestart(`ice-${s}`)
        } else if (s === 'disconnected') {
          console.debug('WebRTC ICE disconnected (transient, waiting for watchdog)')
        }
      }
      pc.onconnectionstatechange = () => {
        if (genRef.current !== myGen) return
        if (pc.connectionState === 'failed') {
          console.warn('WebRTC connection failed, retrying')
          scheduleRestart('connection-failed')
        }
      }

      pc.ontrack = (event) => {
        if (genRef.current !== myGen) return
        const v = videoRef.current
        if (!v) return
        const incoming = event.streams[0] ?? new MediaStream([event.track])
        const existing = v.srcObject as MediaStream | null
        if (existing && !event.streams[0]) {
          if (!existing.getTracks().some(t => t.id === event.track.id)) {
            existing.addTrack(event.track)
          }
        } else {
          v.srcObject = incoming
        }
        v.play().catch(() => { /* autoplay policy; watchdog will catch stall */ })
        setMode('webrtc')
        setReconnecting(false)
        setConnected(true)
        onConnChangeRef.current?.(true)
        if (iceTimerRef.current) { clearTimeout(iceTimerRef.current); iceTimerRef.current = null }
        startWatchdog()
      }

      pc.addTransceiver('video', { direction: 'recvonly' })
      pc.addTransceiver('audio', { direction: 'recvonly' })

      // Build WebSocket URL for trickle ICE signaling.
      const token = getToken()
      const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const wsHost = window.location.host
      const activeName = useSubStream ? stream.sub_stream_name : stream.stream_name
      const wsUrl = `${wsProto}//${wsHost}/api/go2rtc/api/ws` +
        `?src=${encodeURIComponent(activeName)}` +
        (token ? `&token=${encodeURIComponent(token)}` : '')

      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = async () => {
        if (genRef.current !== myGen) { ws.close(); return }
        const offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        if (genRef.current !== myGen) { ws.close(); return }
        ws.send(JSON.stringify({ type: 'webrtc/offer', value: offer.sdp }))
      }

      ws.onmessage = async (evt) => {
        if (genRef.current !== myGen) return
        let msg: { type: string; value?: string }
        try { msg = JSON.parse(evt.data) } catch { return }

        if (msg.type === 'webrtc/answer' && msg.value) {
          await pc.setRemoteDescription({ type: 'answer', sdp: msg.value })
        } else if (msg.type === 'webrtc/candidate') {
          if (msg.value) {
            // go2rtc sends the candidate as a raw SDP candidate string
            // (e.g. "candidate:1 1 udp ... typ host ..."), NOT a JSON object.
            // Wrap it in an RTCIceCandidateInit. go2rtc bundles everything onto
            // a single m-line, so sdpMid/sdpMLineIndex 0 is correct.
            try {
              await pc.addIceCandidate({
                candidate: msg.value,
                sdpMid: '0',
                sdpMLineIndex: 0,
              })
            } catch { /* ignore malformed candidate */ }
          }
          // An empty value is go2rtc's end-of-candidates marker. Browsers
          // accept end-of-candidates via addIceCandidate(undefined|null), but
          // it's optional and some engines reject it — so we simply ignore it.
        }
      }

      // Send trickle ICE candidates to go2rtc as they're gathered.
      // go2rtc expects `value` to be the raw SDP candidate string (the same
      // format it sends back), NOT a JSON-encoded RTCIceCandidate object.
      pc.onicecandidate = (evt) => {
        if (genRef.current !== myGen) return
        if (ws.readyState !== WebSocket.OPEN) return
        ws.send(JSON.stringify({
          type: 'webrtc/candidate',
          value: evt.candidate ? evt.candidate.candidate : '',
        }))
      }

      ws.onerror = () => {
        if (genRef.current === myGen) scheduleRestart('ws-error')
      }
      ws.onclose = (evt) => {
        // Normal close (code 1000/1001) after teardown — don't restart.
        if (genRef.current === myGen && !destroyedRef.current && evt.code !== 1000 && evt.code !== 1001) {
          scheduleRestart('ws-closed')
        }
      }

      // ICE timeout: if no track arrives within 15s, retry.
      iceTimerRef.current = setTimeout(() => {
        if (genRef.current === myGen && pcRef.current === pc) {
          console.warn('WebRTC track timeout, retrying')
          scheduleRestart('ice-timeout')
        }
      }, ICE_TIMEOUT_MS)

    } catch (err) {
      if (genRef.current !== myGen) return
      console.warn('WebRTC failed, retrying:', err)
      scheduleRestart('webrtc-error')
    }
  }

  useEffect(() => {
    destroyedRef.current = false
    retryCountRef.current = 0
    genRef.current += 1
    setConnected(false)
    if (mode === 'offline') resumeFromOffline()
    else startWebRTC()
    return () => {
      destroyedRef.current = true
      genRef.current += 1
      clearTimers()
      teardownPc()
      setReconnecting(false)
      if (videoRef.current) {
        const v = videoRef.current
        v.onerror = null; v.onended = null; v.onstalled = null; v.onplaying = null
        v.srcObject = null
        v.src = ''
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stream.stream_name, useSubStream])

  useEffect(() => {
    if (mode === 'webrtc' && pendingStartRef.current) {
      pendingStartRef.current = false
      if (!destroyedRef.current) startWebRTC()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode])

  const handleSnapshot = useCallback(async () => {
    if (!cameraId || snapping) return
    setSnapping(true)
    try {
      // Use Bearer rather than ?token= in the URL so the JWT doesn't leak into
      // browser history / devtools / server access logs. The /snapshot route
      // accepts both, but Bearer is the hygienic default.
      const token = getToken()
      const res = await fetch(`/api/cameras/${cameraId}/snapshot`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `${stream.camera_name}_${Date.now()}.jpg`
      a.click()
      URL.revokeObjectURL(a.href)
    } catch (e) {
      console.warn('snapshot failed', e)
    } finally {
      setSnapping(false)
    }
  }, [cameraId, snapping, stream.camera_name])

  const handlePtzMove = useCallback(async (pan: number, tilt: number) => {
    if (!cameraId) return
    try { await ptzApi.move(cameraId, pan, tilt) } catch { /* ignore */ }
  }, [cameraId])

  const container: React.CSSProperties = {
    position: 'relative',
    background: '#000',
    overflow: 'hidden',
    borderRadius: 'var(--radius)',
    minHeight: 180,
    ...(style ?? {}),
  }

  if (mode === 'offline') {
    return (
      <div style={container}>
        <div style={{
          display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center',
          height: '100%', gap: 10,
          color: 'var(--text-muted)',
        }}>
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" opacity="0.5">
            <path d="M23 7 16 12 23 17V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>
            <line x1="1" y1="1" x2="23" y2="23" stroke="var(--danger)" strokeWidth="2"/>
          </svg>
          <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)' }}>
            {stream.camera_name}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Camera offline</div>
          <button
            onClick={() => {
              if (retryTimerRef.current) { clearTimeout(retryTimerRef.current); retryTimerRef.current = null }
              retryCountRef.current = 0
              resumeFromOffline()
            }}
            className="btn btn-secondary btn-sm"
            style={{ marginTop: 4 }}
          >
            Retry
          </button>
        </div>
      </div>
    )
  }

  return (
    <div style={container}>
      <video
        ref={videoRef}
        autoPlay
        muted
        playsInline
        style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
      />

      {/* Connecting/reconnecting overlay — shown until video is flowing */}
      {(reconnecting || !connected) && (
        <div style={{
          position: 'absolute', inset: 0,
          background: 'rgba(0,0,0,0.55)',
          display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center',
          gap: 8,
          pointerEvents: 'none',
        }}>
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.8)" strokeWidth="2" strokeLinecap="round">
            <polyline points="23 4 23 10 17 10"/>
            <polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
            <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="1s" repeatCount="indefinite"/>
          </svg>
          <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.7)', fontWeight: 600, letterSpacing: '0.06em' }}>
            {reconnecting ? 'RECONNECTING' : 'CONNECTING'}
          </span>
        </div>
      )}

      {/* Heatmap overlay */}
      {showHeatmap && cameraId && (
        <img
          src={withToken(`/api/cameras/${cameraId}/heatmap?width=640&height=360`)}
          alt="heatmap"
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', pointerEvents: 'none', opacity: 0.65 }}
        />
      )}

      {/* PTZ directional pad */}
      {showPtz && cameraId && (
        <div style={{
          position: 'absolute', bottom: 36, right: 8,
          display: 'grid', gridTemplateColumns: 'repeat(3, 28px)', gap: 2,
          pointerEvents: 'all',
        }}>
          {[
            { label: '↖', pan: -0.2, tilt: 0.2 }, { label: '↑', pan: 0, tilt: 0.3 }, { label: '↗', pan: 0.2, tilt: 0.2 },
            { label: '←', pan: -0.3, tilt: 0 },   { label: '⏹', pan: 0, tilt: 0 },   { label: '→', pan: 0.3, tilt: 0 },
            { label: '↙', pan: -0.2, tilt: -0.2 },{ label: '↓', pan: 0, tilt: -0.3 },{ label: '↘', pan: 0.2, tilt: -0.2 },
          ].map(b => (
            <button
              key={b.label}
              onClick={() => b.label === '⏹' ? ptzApi.stop(cameraId).catch(() => {}) : handlePtzMove(b.pan, b.tilt)}
              style={{
                width: 28, height: 28, fontSize: 12, cursor: 'pointer',
                background: 'rgba(0,0,0,0.6)', color: '#fff', border: '1px solid rgba(255,255,255,0.2)',
                borderRadius: 4, display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >{b.label}</button>
          ))}
        </div>
      )}

      <div style={{
        position: 'absolute', bottom: 0, left: 0, right: 0,
        padding: '20px 10px 6px 10px',
        background: 'linear-gradient(to top, rgba(0,0,0,0.7) 0%, transparent 100%)',
        display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between',
        pointerEvents: 'none',
      }}>
        <span style={{
          color: '#fff', fontSize: 11, fontWeight: 500,
          textShadow: '0 1px 3px rgba(0,0,0,0.8)',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        }}>
          {stream.camera_name}
        </span>
        {/* Control buttons */}
        <div style={{ display: 'flex', gap: 4, pointerEvents: 'all' }}>
          {cameraId && (
            <button
              onClick={handleSnapshot}
              title="Download snapshot"
              style={{
                width: 22, height: 22, borderRadius: 4, border: 'none', cursor: 'pointer',
                background: snapping ? 'rgba(255,255,255,0.5)' : 'rgba(255,255,255,0.2)',
                color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12,
              }}
            >📷</button>
          )}
          <button
            onClick={() => setUseSubStream(s => !s)}
            title={useSubStream ? 'Switch to main stream' : 'Switch to sub-stream'}
            style={{
              padding: '0 5px', height: 22, borderRadius: 4, border: 'none', cursor: 'pointer',
              background: useSubStream ? 'rgba(59,130,246,0.7)' : 'rgba(255,255,255,0.2)',
              color: '#fff', fontSize: 9, fontWeight: 700, letterSpacing: '0.05em',
            }}
          >{useSubStream ? 'SUB' : 'MAIN'}</button>
          {cameraId && (
            <button
              onClick={() => setShowHeatmap(s => !s)}
              title={showHeatmap ? 'Hide heatmap' : 'Show motion heatmap'}
              style={{
                padding: '0 5px', height: 22, borderRadius: 4, border: 'none', cursor: 'pointer',
                background: showHeatmap ? 'rgba(239,68,68,0.7)' : 'rgba(255,255,255,0.2)',
                color: '#fff', fontSize: 9, fontWeight: 700,
              }}
            >HEAT</button>
          )}
          {ptzCapable && cameraId && (
            <button
              onClick={() => setShowPtz(s => !s)}
              title="PTZ controls"
              style={{
                padding: '0 5px', height: 22, borderRadius: 4, border: 'none', cursor: 'pointer',
                background: showPtz ? 'rgba(139,92,246,0.7)' : 'rgba(255,255,255,0.2)',
                color: '#fff', fontSize: 9, fontWeight: 700,
              }}
            >PTZ</button>
          )}
        </div>
      </div>

      <div style={{
        position: 'absolute', top: 8, right: 8,
        display: 'flex', alignItems: 'center', gap: 5,
        pointerEvents: 'none',
      }}>
        {isRecording && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 4,
            padding: '2px 7px',
            background: 'rgba(200,0,0,0.75)',
            borderRadius: 'var(--radius-full)',
            backdropFilter: 'blur(4px)',
          }}>
            <span style={{
              width: 5, height: 5, borderRadius: '50%',
              background: '#fff',
              animation: 'live-pulse 1s ease-in-out infinite',
            }} />
            <span style={{ fontSize: 9, color: '#fff', fontWeight: 700, letterSpacing: '0.07em' }}>
              REC
            </span>
          </div>
        )}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 4,
          padding: '2px 7px',
          background: 'rgba(0,0,0,0.55)',
          borderRadius: 'var(--radius-full)',
          backdropFilter: 'blur(4px)',
        }}>
          <span style={{
            width: 5, height: 5, borderRadius: '50%',
            background: 'var(--danger)',
            animation: 'live-pulse 1.5s ease-in-out infinite',
          }} />
          <span style={{ fontSize: 9, color: '#fff', fontWeight: 700, letterSpacing: '0.07em' }}>
            LIVE
          </span>
        </div>
      </div>
    </div>
  )
}
