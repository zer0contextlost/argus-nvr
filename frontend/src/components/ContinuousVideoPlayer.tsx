/**
 * ContinuousVideoPlayer — seamlessly advances across 2-minute MP4 segments
 * without re-keying the <video> element by swapping src imperatively.
 *
 * Exposes seek / play / pause / setSpeed via forwardRef handle.
 * Uses a hidden preload video to buffer the next segment 15s before the
 * current one ends, then swaps into the primary element on handleEnded.
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
  useRef,
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
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export const ContinuousVideoPlayer = forwardRef<ContinuousVideoPlayerHandle, Props>(
  function ContinuousVideoPlayer(
    { segments, onWallTimeChange, onPlayingChange, onGapSkipped, onError },
    ref,
  ) {
    const primaryRef = useRef<HTMLVideoElement>(null)
    const preloadRef = useRef<HTMLVideoElement>(null)
    const stateRef = useRef<InternalState>({
      segIdx: null,
      pendingOffset: null,
      speed: 1,
      playing: false,
      expectedSrc: null,
    })

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

    // ------------------------------------------------------------------
    // Imperative handle
    // ------------------------------------------------------------------

    useImperativeHandle(ref, () => ({
      seek(wallMs: number) {
        const primary = primaryRef.current
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
          stateRef.current.segIdx = segIdx
          stateRef.current.pendingOffset = offset
          stateRef.current.expectedSrc = loadSegIntoElement(primary, seg.id)
          setHasSource(true)
        }
      },

      play() {
        const primary = primaryRef.current
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
        const primary = primaryRef.current
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
        if (primaryRef.current) {
          primaryRef.current.playbackRate = s
        }
      },
    }))

    // ------------------------------------------------------------------
    // Video event handlers
    // ------------------------------------------------------------------

    function handleLoadedMetadata() {
      const primary = primaryRef.current
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
      const primary = primaryRef.current
      const preload = preloadRef.current
      const segs = segmentsRef.current
      if (!primary || stateRef.current.segIdx === null) return

      const seg = segs[stateRef.current.segIdx]
      if (!seg) return

      const wallMs =
        new Date(seg.started_at).getTime() + primary.currentTime * 1000
      onWallTimeChange(wallMs)

      // Preload next segment when within 15s of the end.
      const remaining = primary.duration - primary.currentTime
      if (remaining < 15 && !isNaN(remaining) && preload) {
        const nextIdx = stateRef.current.segIdx + 1
        if (nextIdx < segs.length) {
          const nextSeg = segs[nextIdx]
          const expectedSrc = withToken(`/api/recordings/${nextSeg.id}/video`)
          // Compare against the raw assigned src (browser may normalize
          // currentSrc to an absolute URL, but src returns what we set).
          if (preload.getAttribute('src') !== expectedSrc) {
            preload.src = expectedSrc
            preload.load()
          }
        }
      }
    }

    function handleEnded() {
      const primary = primaryRef.current
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

      stateRef.current.segIdx = nextIdx
      stateRef.current.pendingOffset = 0
      stateRef.current.expectedSrc = loadSegIntoElement(primary, nextSeg.id)
    }

    function handleError() {
      const primary = primaryRef.current
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
      const primary = primaryRef.current
      if (primary && primary.ended) return
      stateRef.current.playing = false
      onPlayingChange(false)
    }

    // ------------------------------------------------------------------
    // Effect: reset on segments change (e.g. user picks a different day)
    // ------------------------------------------------------------------

    useEffect(() => {
      if (segments.length === 0) {
        const primary = primaryRef.current
        // Order matters: clear expectedSrc FIRST so handleError treats any
        // resulting error event as spurious.
        stateRef.current.expectedSrc = null
        stateRef.current.segIdx = null
        stateRef.current.pendingOffset = null
        stateRef.current.playing = false
        if (primary) clearPrimary(primary)
        setHasSource(false)
      }
    }, [segments])

    // ------------------------------------------------------------------
    // Render
    // ------------------------------------------------------------------

    return (
      <div style={{ position: 'relative', width: '100%', background: '#000' }}>
        <video
          ref={primaryRef}
          controls={false}
          preload="auto"
          playsInline
          onLoadedMetadata={handleLoadedMetadata}
          onTimeUpdate={handleTimeUpdate}
          onEnded={handleEnded}
          onError={handleError}
          onPlay={handlePlay}
          onPause={handlePause}
          style={{
            display: 'block',
            width: '100%',
            aspectRatio: '16 / 9',
            background: '#000',
          }}
        />

        {/* Hidden preload buffer */}
        <video
          ref={preloadRef}
          preload="auto"
          muted
          playsInline
          style={{ display: 'none' }}
          aria-hidden="true"
        />

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
