// SPDX-License-Identifier: BSD-3-Clause
.pragma library
// The Custom slider model of the Decky panel (src/tdp.tsx), kept identical; tests/frontend_desktop_parity.cjs
// compares the two. SPL is the dial, slow and fast boost are headroom above it, so no slider position
// breaks FPPT >= SPPT >= SPL or the ceilings. A tuning is {spl, spptOff, fpptOff} in watts; caps are {spl, sppt, fppt}.
var OFFSET_MAX = {sppt: 15, fppt: 15};
var FALLBACK_STD = {spl: 35, sppt: 37, fppt: 45};

function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }

/** Headroom still available above the current SPL, per parameter. */
function offsetMax(spl, caps) {
    return {sppt: Math.max(0, Math.min(OFFSET_MAX.sppt, caps.sppt - spl)),
            fppt: Math.max(0, Math.min(OFFSET_MAX.fppt, caps.fppt - spl))};
}

/** Force a tuning back inside the ceilings, keeping SPPT <= FPPT. */
function normalise(t, caps, minW) {
    var spl = clamp(t.spl, minW, caps.spl), max = offsetMax(spl, caps);
    var spptOff = clamp(t.spptOff, 0, max.sppt);
    return {spl: spl, spptOff: spptOff, fpptOff: Math.max(clamp(t.fpptOff, 0, max.fppt), spptOff)};
}

function absolute(t) { return {spl: t.spl, sppt: t.spl + t.spptOff, fppt: t.spl + t.fpptOff}; }

function fromAbsolute(spl, sppt, fppt) {
    return {spl: spl, spptOff: Math.max(0, sppt - spl), fpptOff: Math.max(0, fppt - spl)};
}

/** Standard single-dial Custom mode uses the same caps as the Go 2 firmware. */
function simpleCaps(caps, unlocked) {
    return unlocked ? caps : {spl: Math.min(caps.spl, FALLBACK_STD.spl), sppt: Math.min(caps.sppt, FALLBACK_STD.sppt),
                              fppt: Math.min(caps.fppt, FALLBACK_STD.fppt)};
}

function simpleTuning(spl, caps, minW) {
    var bounded = clamp(spl, minW, caps.spl);
    return fromAbsolute(bounded, Math.min(bounded + 10, caps.sppt), Math.min(bounded + 15, caps.fppt));
}

// The three slider handlers (makeTuningHandlers in src/tdp.tsx).
/** Moving SPL re-clamps both offsets: at the ceiling there is no headroom left. */
function withSpl(t, v, caps, minW) { return normalise({spl: v, spptOff: t.spptOff, fpptOff: t.fpptOff}, caps, minW); }

/** Pushing SPPT up drags FPPT along so SPPT never overtakes it. */
function withSppt(t, v, caps) {
    var spptOff = clamp(v, 0, offsetMax(t.spl, caps).sppt);
    return {spl: t.spl, spptOff: spptOff, fpptOff: Math.max(t.fpptOff, spptOff)};
}

/** Pulling FPPT below SPPT drags SPPT down to meet it. */
function withFppt(t, v, caps) {
    var fpptOff = clamp(v, 0, offsetMax(t.spl, caps).fppt);
    return {spl: t.spl, spptOff: Math.min(t.spptOff, fpptOff), fpptOff: fpptOff};
}
