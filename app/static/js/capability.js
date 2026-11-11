/*
 * capability.js — dashboard.pdhc input-capability detection.
 *
 * Plain script. No modules, no build step, no dependencies. Load with:
 *     <script src="{{ url_for('static', filename='js/capability.js') }}"></script>
 * and read it as the global `DashboardCapability`.
 *
 * WHY THIS EXISTS
 * ---------------
 * The dashboard's interaction model is SELECTION. Hover is an accelerator, and
 * only where hover genuinely exists. Two facts make "does hover exist?" harder
 * than it looks, and both are about the SAME iPad:
 *
 *   1. iPadOS Safari SYNTHESISES a hover on the first tap, for backwards
 *      compatibility with mouse-era pages. A UI that reveals an affordance on
 *      hover and activates it on click therefore becomes a two-tap
 *      reveal-then-activate on touch — the user taps once and nothing appears
 *      to happen.
 *   2. Attaching a Magic Keyboard or a trackpad to that same iPad restores
 *      REAL hover, mid-session, with no reload.
 *
 * So the capability is not a property of the device, it is a property of the
 * moment, and IT CHANGES AT RUNTIME. Hence: a media query, plus a subscription
 * so callers are told when it flips. Never sniff the user agent — a user agent
 * string cannot tell you whether a trackpad is attached right now.
 *
 * DEGRADATION
 * -----------
 * If `matchMedia` is missing, or throws, or the query is unsupported, this
 * module assumes NO HOVER and the touch row height. That direction is chosen
 * deliberately: assuming hover exists is the failure that breaks the iPad
 * (invisible affordances, two-tap activation, 30px targets), whereas assuming
 * it does not merely costs a desktop user some density.
 */
(function (global) {
  'use strict';

  /* ---- row heights -------------------------------------------------------
   * TOUCH_ROW_HEIGHT is 44 CSS px: the Apple HIG minimum touch target is
   * 44x44pt, and a list row is the full-width half of that target. Do not
   * lower it to win density on touch.
   * POINTER_ROW_HEIGHT is 30 CSS px: dense, for a real mouse or trackpad where
   * the pointer is precise and the target can be small.
   * Both are exported because the virtualised event list has to be TOLD which
   * mode it is in — it computes its own scroll geometry from the row height,
   * so it cannot read this off the DOM.
   */
  var TOUCH_ROW_HEIGHT = 44;
  var POINTER_ROW_HEIGHT = 30;

  /* Both halves matter. `(hover: hover)` alone is true for the iPad's
   * synthesised hover in some engines; `(pointer: fine)` alone is true for a
   * stylus, which has no hover. Requiring both is what distinguishes a real
   * mouse/trackpad from a finger or a pen. */
  var HOVER_QUERY = '(hover: hover) and (pointer: fine)';

  var mql = null;
  try {
    if (global && typeof global.matchMedia === 'function') {
      mql = global.matchMedia(HOVER_QUERY);
    }
  } catch (e) {
    mql = null; /* fall through to the no-hover assumption */
  }

  function readHover() {
    try {
      return !!(mql && mql.matches === true);
    } catch (e) {
      return false;
    }
  }

  var hasHover = readHover();
  var listeners = [];

  function currentRowHeight() {
    return hasHover ? POINTER_ROW_HEIGHT : TOUCH_ROW_HEIGHT;
  }

  function currentMode() {
    return hasHover ? 'pointer' : 'touch';
  }

  function snapshot() {
    return {
      hasHover: hasHover,
      mode: currentMode(),
      rowHeight: currentRowHeight(),
      touchRowHeight: TOUCH_ROW_HEIGHT,
      pointerRowHeight: POINTER_ROW_HEIGHT
    };
  }

  /* Re-read the query and notify subscribers if the answer changed.
   * Returns true when it changed. Safe to call at any time. */
  function refresh(nextValue) {
    var next = (typeof nextValue === 'boolean') ? nextValue : readHover();
    if (next === hasHover) {
      return false;
    }
    hasHover = next;
    var state = snapshot();
    /* Iterate a copy: a listener may unsubscribe itself. One throwing
     * listener must not stop the others from being told. */
    var current = listeners.slice();
    for (var i = 0; i < current.length; i++) {
      try {
        current[i](state);
      } catch (e) {
        if (global.console && global.console.error) {
          global.console.error('capability: subscriber threw', e);
        }
      }
    }
    return true;
  }

  function handleMediaChange(event) {
    refresh(event && typeof event.matches === 'boolean' ? event.matches : undefined);
  }

  if (mql) {
    try {
      if (typeof mql.addEventListener === 'function') {
        mql.addEventListener('change', handleMediaChange);
      } else if (typeof mql.addListener === 'function') {
        /* Safari < 14 — still shipped on iPads that are current enough to
         * matter here, so the deprecated form is kept as a fallback. */
        mql.addListener(handleMediaChange);
      }
    } catch (e) {
      /* No change notifications available. The initial value still stands,
       * and refresh() can be called manually. */
    }
  }

  /* subscribe(fn) -> unsubscribe()
   * fn is called with a snapshot ONLY when the capability changes, not on
   * subscribe. Read `DashboardCapability.hasHover` for the current value. */
  function subscribe(fn) {
    if (typeof fn !== 'function') {
      return function () {};
    }
    listeners.push(fn);
    var active = true;
    return function unsubscribe() {
      if (!active) {
        return;
      }
      active = false;
      var i = listeners.indexOf(fn);
      if (i !== -1) {
        listeners.splice(i, 1);
      }
    };
  }

  /* pointerTypeOf(event) -> 'mouse' | 'pen' | 'touch' | 'unknown'
   *
   * A single device delivers all three: an iPad with a trackpad and an Apple
   * Pencil produces mouse, touch and pen events from the same screen. So this
   * is a per-EVENT question and is deliberately separate from hasHover, which
   * is a per-MOMENT question about the device.
   *
   * Note the honest limit: on iPadOS a synthesised compatibility mouse event
   * reports 'mouse'. This function reports what the event says. Use
   * pointerType to branch on how THIS interaction arrived; use hasHover to
   * decide whether a hover affordance should exist at all.
   */
  function pointerTypeOf(event) {
    if (!event) {
      return 'unknown';
    }
    var t = event.pointerType;
    if (t === 'mouse' || t === 'pen' || t === 'touch') {
      return t;
    }
    /* Not a PointerEvent (or a PointerEvent with an empty pointerType, which
     * the spec permits). Fall back to the event family. */
    try {
      if (global.TouchEvent && event instanceof global.TouchEvent) {
        return 'touch';
      }
      if (event.touches || event.changedTouches) {
        return 'touch';
      }
      if (global.MouseEvent && event instanceof global.MouseEvent) {
        return 'mouse';
      }
    } catch (e) {
      /* instanceof across frames, or a plain object in a test — fall through */
    }
    if (typeof event.type === 'string' && event.type.indexOf('touch') === 0) {
      return 'touch';
    }
    return 'unknown';
  }

  var api = {
    HOVER_QUERY: HOVER_QUERY,
    TOUCH_ROW_HEIGHT: TOUCH_ROW_HEIGHT,
    POINTER_ROW_HEIGHT: POINTER_ROW_HEIGHT,
    subscribe: subscribe,
    onHoverChange: subscribe,
    refresh: refresh,
    snapshot: snapshot,
    rowHeight: currentRowHeight,
    mode: currentMode,
    isHoverCapable: readHover,
    pointerTypeOf: pointerTypeOf,
    /* Set when matchMedia is unavailable or threw, i.e. when hasHover is the
     * safe assumption rather than a measurement. Useful in a debug panel. */
    supported: mql !== null
  };

  /* Live accessors, so a caller that cached the object cannot read a stale
   * boolean after a trackpad is attached. Falls back to plain properties on
   * an engine without defineProperty getters. */
  try {
    Object.defineProperty(api, 'hasHover', {
      enumerable: true,
      get: function () { return hasHover; }
    });
    Object.defineProperty(api, 'currentRowHeight', {
      enumerable: true,
      get: function () { return currentRowHeight(); }
    });
  } catch (e) {
    api.hasHover = hasHover;
    api.currentRowHeight = currentRowHeight();
  }

  global.DashboardCapability = api;

  /* Harmless when absent; lets a future node-based test require this file
   * without introducing a bundler. */
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
