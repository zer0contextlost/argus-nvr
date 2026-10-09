/**
 * ContinuousVideoPlayer — seamlessly advances across 2-minute MP4 segments
 * without re-keying the <video> element by swapping src imperatively.
 *
 * Exposes seek / play / pause / setSpeed via forwardRef handle.
 * Two stacked <video> elements alternate roles: the visible "primary" plays
 * the current segment while the hidden "standby" buffers the next one (from
 * 15s before the end). On `ended`, if the standby has its first frame decoded,
 * the roles swap and it starts playing immediately; otherwise we fall back to
 * loading the next segment into the primary element.
 *
 * Event handlers are attached to both elements but ignore any event whose
 * target is not the current primary.
 *
 * Error-handling philosophy
 * -------------------------
 * The <video> element fires `error` events in many spurious situations:
 *  - on mount before any src is set
 *  - when src is detached / replaced
 *  - lingering errors from the previous src that fire after a new src has been
 *    assigned (race between MediaElement state machine and React state)
 *
 * To avoid surfacing those as user-visible "Failed to load recording" toasts,
 * we only forward an error when ALL of these hold:
 *  1. We currently expect a real source to be loaded (`expectedSrc !== null`)
 *  2. The element's resolved `currentSrc` matches our expected URL
 *  3. The element has a real `MediaError` (code 2/3/4 — network/decode/src)
 */

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  type SyntheticEvent,
  useState,
} from 'react'
import { TimelineSegment, withToken } from '../api/client'

// ---------------------------------------------------------------------------
// Public handle type
// ---------------------------------------------------------------------------

export interface ContinuousVideoPlayerHandle {
  seek: (wallMs: number) => void
  play: () => void
  pause: () => void
  setSpeed: (s: number) => void
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface Props {
  segments: TimelineSegment[]
  onWallTimeChange: (ms: number) => void
  onPlayingChange: (playing: boolean) => void
  onGapSkipped?: (prevEndMs: number, nextStartMs: number) => void
  onError?: () => void
}

// ---------------------------------------------------------------------------
// Internal mutable state (avoids stale-closure issues in event handlers)
// ---------------------------------------------------------------------------

interface InternalState {
  segIdx: number | null
  pendingOffset: number | null
  /** Desired playback rate; applied each time metadata loads. */
  speed: number
  /** User intent: should we be playing as soon as metadata is ready? */
  playing: boolean
  /**
   * The src URL we have most recently asked the primary video element to load.
   * Used to ignore stale `error` events from previous srcs and to avoid
   * spurious reloads.  `null` means "no source currently expected".
   */
  expectedSrc: string | null
  /** Which of the two video elements is currently the visible primary. */
  active: 0 | 1
  /** Segment index the standby element is buffering, or null. */
  standbySegIdx: number | null
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export const ContinuousVideoPlayer = forwardRef<ContinuousVideoPlayerHandle, Props>(
  function ContinuousVideoPlayer(
    { segments, onWallTimeChange, onPlayingChange, onGapSkipped, onError },
    ref,
  ) {
    const videoARef = useRef<HTMLVideoElement>(null)
    const videoBRef = useRef<HTMLVideoElement>(null)
    const stateRef = useRef<InternalState>({
      segIdx: null,
      pendingOffset: null,
      speed: 1,
      playing: false,
      expectedSrc: null,
      active: 0,
      standbySegIdx: null,
    })

    const getPrimary = () =>
      (stateRef.current.active === 0 ? videoARef : videoBRef).current
    const getStandby = () =>
      (stateRef.current.active === 0 ? videoBRef : videoARef).current
    /** Wrap a media event handler so it only runs for the current primary. */
    const forPrimary = (fn: () => void) =>
      (e: SyntheticEvent<HTMLVideoElement>) => {
        if (e.currentTarget === getPrimary()) fn()
      }

    // Initial visibility is set imperatively (not via the style prop) because
    // swapStandbyIn() flips it imperatively and React must not fight it.
    useLayoutEffect(() => {
      if (videoBRef.current) videoBRef.current.style.visibility = 'hidden'
    }, [])

    // Keep latest segments in a ref so imperative handlers don't close over
    // a stale snapshot.
    const segmentsRef = useRef<TimelineSegment[]>(segments)
    useEffect(() => {
      segmentsRef.current = segments
    }, [segments])

    // Track whether the primary video has a source so we can show/hide the placeholder.
    const [hasSource, setHasSource] = useState(false)

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /**
     * Find the segment index whose [start, end) window contains wallMs.
     * If wallMs falls in a gap, returns the index of the NEXT segment that
     * starts after wallMs (so clicking an event timestamp that sits a few ms
     * before its containing segment still snaps to that segment).
     */
    function findSegIdx(wallMs: number): number | null {
      const segs = segmentsRef.current
      if (segs.length === 0) return null

      // Direct hit
      for (let i = 0; i < segs.length; i++) {
        const seg = segs[i]
        const start = new Date(seg.started_at).getTime()
        const end = seg.ended_at ? new Date(seg.ended_at).getTime() : Infinity
        if (wallMs >= start && wallMs < end) return i
      }

      // Snap to the next segment starting after wallMs
      for (let i = 0; i < segs.length; i++) {
        const start = new Date(segs[i].started_at).getTime()
        if (start >= wallMs) return i
      }

      // wallMs is after every segment — snap to last segment's end
      return segs.length - 1
    }

    function loadSegIntoElement(el: HTMLVideoElement, segId: number): string {
      const url = withToken(`/api/recordings/${segId}/video`)
      // Setting src and calling load() kicks off a fresh resource fetch and
      // resets the element's error / readyState.
      el.src = url
      el.load()
      return url
    }

    function clearPrimary(el: HTMLVideoElement): void {
      // Canonical way to detach a video source without triggering
      // MEDIA_ERR_SRC_NOT_SUPPORTED: remove the attribute then call load().
      // Assigning `el.src = ''` is interpreted as the resolved document URL,
      // which DOES fire an error event in Chromium.
      el.removeAttribute('src')
      try {
        el.load()
      } catch {
        /* no-op */
      }
    }

    /** Drop whatever the standby element is buffering. */
    function resetStandby(): void {
      stateRef.current.standbySegIdx = null
      const standby = getStandby()
      if (standby && standby.getAttribute('src') !== null) clearPrimary(standby)
    }

    /**
     * Promote the standby element (already buffering segment `nextIdx`) to
     * primary and start it playing from `startAt` seconds. Returns false if it isn't ready, in which
     * case the caller should load the segment the slow way.
     */
    function swapStandbyIn(nextIdx: number, startAt: number): boolean {
      const oldPrimary = getPrimary()
      const standby = getStandby()
      if (!oldPrimary || !standby) return false
      if (stateRef.current.standbySegIdx !== nextIdx) return false
      if (standby.readyState < 2 /* HAVE_CURRENT_DATA */) return false

      stateRef.current.active = stateRef.current.active === 0 ? 1 : 0
      stateRef.current.standbySegIdx = null
      stateRef.current.segIdx = nextIdx
      stateRef.current.pendingOffset = null
      stateRef.current.expectedSrc = standby.getAttribute('src')

      if (startAt > 0) {
        try { standby.currentTime = startAt } catch { /* no-op */ }
      }
      standby.playbackRate = stateRef.current.speed
      standby.muted = false
      standby.style.visibility = 'visible'
      oldPrimary.style.visibility = 'hidden'
      oldPrimary.muted = true
      try { oldPrimary.pause() } catch { /* no-op */ }

      if (stateRef.current.playing) {
        const result = standby.play()
        if (result && typeof result.catch === 'function') result.catch(() => {})
      }
      return true
    }

    // ------------------------------------------------------------------
    // Imperative handle
    // ------------------------------------------------------------------

    useImperativeHandle(ref, () => ({
      seek(wallMs: number) {
        const primary = getPrimary()
        if (!primary) return

        const segs = segmentsRef.current
        const segIdx = findSegIdx(wallMs)
        if (segIdx === null) return

        const seg = segs[segIdx]
        const segStart = new Date(seg.started_at).getTime()
        // If the click landed before the segment actually starts (gap snap),
        // start from the beginning of that segment.
        const offset = Math.max(0, (wallMs - segStart) / 1000)

        if (segIdx === stateRef.current.segIdx && stateRef.current.expectedSrc !== null) {
          // Same segment already loaded — just jump the playhead.
          if (primary.readyState >= 1 /* HAVE_METADATA */) {
            primary.currentTime = offset
          } else {
            // Metadata not yet here; queue the offset.
            stateRef.current.pendingOffset = offset
          }
        } else {
          resetStandby()
          stateRef.current.segIdx = segIdx
          stateRef.current.pendingOffset = offset
          stateRef.current.expectedSrc = loadSegIntoElement(primary, seg.id)
          setHasSource(true)
        }
      },

      play() {
        const primary = getPrimary()
        if (!primary) return
        stateRef.current.playing = true

        const segs = segmentsRef.current
        // No segment loaded yet but we have segments available — start at the first one.
        if (stateRef.current.segIdx === null && segs.length > 0) {
          const seg = segs[0]
          stateRef.current.segIdx = 0
          stateRef.current.pendingOffset = 0
          stateRef.current.expectedSrc = loadSegIntoElement(primary, seg.id)
          setHasSource(true)
          // Actual play() will fire from handleLoadedMetadata when ready.
          return
        }

        // Segment loaded — kick off playback. If metadata is still loading,
        // the `playing` flag will cause handleLoadedMetadata to start playback
        // once it arrives.
        if (stateRef.current.expectedSrc !== null) {
          const result = primary.play()
          if (result && typeof result.catch === 'function') {
            result.catch(() => {
              // Autoplay rejections, AbortError from a fresh src swap, etc.
              // Don't surface as a load error — those have their own path.
            })
          }
        }
      },

      pause() {
        stateRef.current.playing = false
        const primary = getPrimary()
        if (primary) {
          try {
            primary.pause()
          } catch {
            /* no-op */
          }
        }
      },

      setSpeed(s: number) {
        stateRef.current.speed = s
        for (const el of [videoARef.current, videoBRef.current]) {
          if (el) el.playbackRate = s
        }
      },
    }))

    // ------------------------------------------------------------------
    // Video event handlers
    // ------------------------------------------------------------------

    function handleLoadedMetadata() {
      const primary = getPrimary()
      if (!primary) return

      primary.playbackRate = stateRef.current.speed

      if (stateRef.current.pendingOffset !== null) {
        try {
          primary.currentTime = stateRef.current.pendingOffset
        } catch {
          /* readyState race — ignore */
        }
        stateRef.current.pendingOffset = null
      }

      if (stateRef.current.playing) {
        const result = primary.play()
        if (result && typeof result.catch === 'function') {
          result.catch(() => {})
        }
      }
    }

    function handleTimeUpdate() {
      const primary = getPrimary()
      const standby = getStandby()
      const segs = segmentsRef.current
      if (!primary || stateRef.current.segIdx === null) return

      const seg = segs[stateRef.current.segIdx]
      if (!seg) return

      const wallMs =
        new Date(seg.started_at).getTime() + primary.currentTime * 1000
      onWallTimeChange(wallMs)

      // Preload next segment when within 15s of the end.
      const remaining = primary.duration - primary.currentTime
      if (remaining < 15 && !isNaN(remaining) && standby) {
        const nextIdx = stateRef.current.segIdx + 1
        if (nextIdx < segs.length && stateRef.current.standbySegIdx !== nextIdx) {
          standby.muted = true
          standby.playbackRate = stateRef.current.speed
          loadSegIntoElement(standby, segs[nextIdx].id)
          stateRef.current.standbySegIdx = nextIdx
        }
      }
    }

    function handleEnded() {
      const primary = getPrimary()
      const segs = segmentsRef.current
      if (stateRef.current.segIdx === null || !primary) return

      const currentSeg = segs[stateRef.current.segIdx]
      const nextIdx = stateRef.current.segIdx + 1

      if (nextIdx >= segs.length) {
        stateRef.current.playing = false
        onPlayingChange(false)
        return
      }

      const nextSeg = segs[nextIdx]
      const prevEndMs = currentSeg.ended_at
        ? new Date(currentSeg.ended_at).getTime()
        : Date.now()
      const nextStartMs = new Date(nextSeg.started_at).getTime()
      const gapMs = nextStartMs - prevEndMs

      if (gapMs > 2000) {
        onGapSkipped?.(prevEndMs, nextStartMs)
      }

      // Pre-roll harvested at a motion trigger overlaps the first seconds of
      // the main recording; start the next segment past the overlap instead
      // of replaying it. Sub-0.5s overlaps are naming jitter, not real.
      const overlapS = gapMs < -500 ? Math.min(-gapMs / 1000, 10) : 0

      if (swapStandbyIn(nextIdx, overlapS)) return

      // Standby not ready (or buffering something else) — load the slow way.
      resetStandby()
      stateRef.current.segIdx = nextIdx
      stateRef.current.pendingOffset = overlapS
      stateRef.current.expectedSrc = loadSegIntoElement(primary, nextSeg.id)
    }

    function handleError() {
      const primary = getPrimary()
      if (!primary) return

      // No source expected at all — spurious error from mount / src detach.
      if (stateRef.current.expectedSrc === null) return

      // No real MediaError, or it's MEDIA_ERR_ABORTED (code 1).
      // Chrome fires code-1 abort events when a new load() cancels an in-flight
      // fetch; by the time the event fires, currentSrc may already reflect the
      // new URL so the src-match check below passes — filter it here instead.
      if (!primary.error || primary.error.code === MediaError.MEDIA_ERR_ABORTED) return

      // Only surface the error if currentSrc is set AND matches what we last
      // asked for.  An empty currentSrc means the element hasn't resolved the
      // new URL yet (transitional state); a mismatched currentSrc means this
      // error belongs to a previous src we've already abandoned.
      if (!primary.currentSrc || !primary.currentSrc.endsWith(stateRef.current.expectedSrc)) {
        return
      }

      // Real, current-src error (code 2/3/4) — surface it.
      onError?.()
    }

    function handlePlay() {
      stateRef.current.playing = true
      onPlayingChange(true)
    }

    function handlePause() {
      // Distinguish a real pause from an end-of-stream pause (handleEnded
      // already reports !playing).  If we're at the end, let handleEnded own it.
      const primary = getPrimary()
      if (primary && primary.ended) return
      stateRef.current.playing = false
      onPlayingChange(false)
    }

    // ------------------------------------------------------------------
    // Effect: reset on segments change (e.g. user picks a different day)
    // ------------------------------------------------------------------

    useEffect(() => {
      if (segments.length === 0) {
        const primary = getPrimary()
        // Order matters: clear expectedSrc FIRST so handleError treats any
        // resulting error event as spurious.
        stateRef.current.expectedSrc = null
        stateRef.current.segIdx = null
        stateRef.current.pendingOffset = null
        stateRef.current.playing = false
        if (primary) clearPrimary(primary)
        resetStandby()
        setHasSource(false)
      }
    }, [segments])

    // ------------------------------------------------------------------
    // Render
    // ------------------------------------------------------------------

    return (
      <div style={{ position: 'relative', width: '100%', background: '#000' }}>
        {/* Two stacked elements; roles alternate (see header comment). A sits
            in normal flow and sizes the box, B overlays it exactly. */}
        {[videoARef, videoBRef].map((r, i) => (
          <video
            key={i}
            ref={r}
            controls={false}
            preload="auto"
            playsInline
            onLoadedMetadata={forPrimary(handleLoadedMetadata)}
            onTimeUpdate={forPrimary(handleTimeUpdate)}
            onEnded={forPrimary(handleEnded)}
            onError={forPrimary(handleError)}
            onPlay={forPrimary(handlePlay)}
            onPause={forPrimary(handlePause)}
            style={{
              display: 'block',
              width: '100%',
              aspectRatio: '16 / 9',
              background: '#000',
              ...(i === 1 ? { position: 'absolute', top: 0, left: 0 } : {}),
            }}
          />
        ))}

        {/* Placeholder overlay — shown when no segment is loaded */}
        {!hasSource && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 12,
              background: '#000',
              pointerEvents: 'none',
              userSelect: 'none',
            }}
          >
            {/* Play icon (triangle) */}
            <div
              style={{
                width: 0,
                height: 0,
                borderTop: '24px solid transparent',
                borderBottom: '24px solid transparent',
                borderLeft: '40px solid rgba(255,255,255,0.18)',
              }}
            />
            <span
              style={{
                fontSize: 13,
                color: 'var(--text-muted)',
                letterSpacing: '0.02em',
              }}
            >
              Click an event or scrub to a time
            </span>
          </div>
        )}
      </div>
    )
  },
)

export default ContinuousVideoPlayer
