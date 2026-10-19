# SPDX-License-Identifier: BSD-3-Clause
"""Pure curve validation and asymmetric control; no hardware or Decky imports."""
import copy
import math
from collections import deque

MIN_RPM, MAX_RPM, RPM_STEP = 1500, 4800, 1
LAST_MIN_RPM = 2000  # The 100°C point never runs slower than this.
STOP_SETTLE_S = 20  # A rotor coasting down from 4800 RPM can exceed 12 seconds.
# Nonzero sub-threshold override selects a real stop; override zero releases it.
STOP_OVERRIDE = 256
HARD_MAX_C = 100.0
LEASE_S, SAMPLE_S = 3.0, 0.25
TACH_GRACE_S = 12  # Spin-up time before a nonzero target with a slow tach counts as stalled (tests shorten it).
# One-time active check of a new (BIOS, EC) pair: the tach must follow a raised target within this time.
SELF_TEST_S, SELF_TEST_RPM = 15, 3000
PRESETS = ('silent', 'balanced', 'performance', 'max', 'custom')
# Starting points for editing, not firmware profiles or saved template identities.
TEMPLATES = {
    'silent': [[0, 0], [35, 0], [45, 1500], [55, 1800], [65, 2400], [75, 3300], [85, 4200], [100, 4800]],
    'balanced': [[0, 1500], [35, 1500], [45, 1800], [55, 2400], [65, 3000], [75, 3700], [85, 4400], [100, 4800]],
    'aggressive': [[0, 2000], [35, 2300], [45, 2800], [55, 3400], [65, 4000], [75, 4500], [85, 4800], [100, 4800]],
}
DEFAULT_CURVE = copy.deepcopy(TEMPLATES['balanced'])


def valid_rpm(rpm):
    return type(rpm) is int and (rpm == 0 or MIN_RPM <= rpm <= MAX_RPM and rpm % RPM_STEP == 0)


def stop_timed_out(rpm, effective, elapsed):
    return elapsed > STOP_SETTLE_S and (rpm != 0 or effective != 0)


def encode_target(rpm):
    if not valid_rpm(rpm):
        raise ValueError('Use stop or a supported positive fan target.')
    return STOP_OVERRIDE if rpm == 0 else rpm


def validate_curve(value):
    if not isinstance(value, list) or len(value) != 8:
        raise ValueError('A curve must contain exactly eight points.')
    last_t, last_r = -1, 0
    result = []
    for i, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError('Invalid curve point.')
        t, rpm = point
        if type(t) is not int or type(rpm) is not int:
            raise ValueError('Temperature and RPM must be integers.')
        if not last_t < t <= 100 or not valid_rpm(rpm) or rpm < last_r:
            raise ValueError('Curve points must be ordered and inside the validated operating range.')
        if i == 7 and (t != 100 or rpm < LAST_MIN_RPM):
            raise ValueError(f'The last point must be at 100°C and run at least {LAST_MIN_RPM} RPM.')
        result.append([t, rpm]); last_t, last_r = t, rpm
    return result


def _lift_last_point(curve):
    """Before 2.2.1 the 100°C point could go down to MIN_RPM; such a saved curve keeps its shape, lifted to the new floor."""
    if (isinstance(curve, list) and len(curve) == 8 and isinstance(curve[7], (list, tuple)) and len(curve[7]) == 2
            and type(curve[7][1]) is int and MIN_RPM <= curve[7][1] < LAST_MIN_RPM):
        return [*curve[:7], [curve[7][0], LAST_MIN_RPM]]
    return curve


def validate_settings(value):
    """Schema 4 only: Fan first shipped (2.1.0) at schema 4. Anything else fails
    closed for review. Unknown keys, such as dev-era curve archives, are dropped."""
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 4:
        raise ValueError('Unsupported fan settings schema; review before enabling.')
    if type(value.get('enabled')) is not bool or type(value.get('revision')) is not int or value['revision'] < 0:
        raise ValueError('Invalid fan settings intent.')
    mappings = value.get('presets')
    if not isinstance(mappings, dict) or set(mappings) - set(PRESETS):
        raise ValueError('Unknown Companion preset assignment.')
    unit = value.get('display_unit', 'rpm')
    if unit not in ('rpm', 'percent'):
        raise ValueError('Unknown fan speed display unit.')
    assignment_mode = value.get('assign_tdp_presets', False)
    if type(assignment_mode) is not bool:
        raise ValueError('TDP curve assignments must be a boolean.')
    return dict(schema=4, enabled=value['enabled'], revision=value['revision'], display_unit=unit,
                global_curve=validate_curve(_lift_last_point(value.get('global_curve'))),
                presets={k: validate_curve(_lift_last_point(v)) for k, v in mappings.items()},
                assign_tdp_presets=assignment_mode)


def defaults():
    return dict(schema=4, enabled=False, assign_tdp_presets=False, revision=0, display_unit='rpm', global_curve=copy.deepcopy(TEMPLATES['balanced']), presets={})


def target_for(curve, temperature):
    if not isinstance(temperature, (float, int)) or isinstance(temperature, bool) or not math.isfinite(temperature):
        raise ValueError('Invalid temperature sample.')
    if temperature >= HARD_MAX_C:
        return MAX_RPM
    if temperature <= curve[0][0]:
        return curve[0][1]
    for (a, r1), (b, r2) in zip(curve, curve[1:]):
        if temperature <= b:
            raw = r1 + (r2-r1)*(temperature-a)/(b-a)
            return max(MIN_RPM, int(math.ceil(raw/RPM_STEP)*RPM_STEP)) if raw > 0 else 0
    return curve[-1][1]


class CurveControl:
    """Rise ≤2 samples; descent needs 2°C hysteresis/5s dwell, then 300RPM/2s.

    Start at the requested curve speed; bounded tach verification detects failure.
    Zero is a real stop. At 100°C full speed bypasses every ramp/dwell.
    """
    def __init__(self, now, temperature, curve=DEFAULT_CURVE):
        self.started = now
        desired = target_for(curve, temperature)
        self.target = desired
        self.peak = temperature
        self.cool_since = None
        self.last_down = now
        self.settling = False
        self.history = deque([(now, self.target)])

    def retarget(self, temperature, now):
        # A user/preset curve change needs a bounded descent even at a stable
        # ambient temperature; ordinary thermal descent still uses hysteresis.
        self.settling = True
        self.peak = temperature
        self.cool_since = now
        self.last_down = now

    def _remember_target(self, now):
        if not self.history or self.history[-1][1] != self.target:
            self.history.append((now, self.target))
        while len(self.history)>1 and self.history[1][0] <= now-12:
            self.history.popleft()

    def force_max(self, now, temperature=HARD_MAX_C):
        # The target changes immediately; a healthy rotor still needs its
        # bounded acceleration window. Repeated hot samples cannot renew it.
        if self.target == 0:
            self.started = now
            self.history.clear()
        self.target = MAX_RPM
        self.peak = temperature
        self.cool_since = None
        self.settling = False
        self._remember_target(now)
        return self.target

    def step(self, curve, temperature, now):
        desired = target_for(curve, temperature)
        if temperature >= HARD_MAX_C:
            return self.force_max(now, temperature)
        if self.target == 0 and desired > 0:
            self.started = now
            self.target = desired
            self.peak = temperature
            self.settling = False
            self.history.clear()
            self.history.append((now, self.target))
        if desired > self.target:
            self.target = min(desired, self.target + 2000)
            self.peak = temperature
            self.cool_since = None
            self.settling = False
        elif desired < self.target:
            # Explicit curve changes may settle without a fictitious temperature fall.
            if temperature > self.peak+2 and desired > target_for(curve, self.peak):
                self.settling = False
            cooling = temperature <= self.peak-2 or self.settling
            if cooling:
                if self.cool_since is None:
                    self.cool_since = now
                if now-self.cool_since >= 5 and now-self.last_down >= 2:
                    next_target = max(desired, self.target-300)
                    self.target = 0 if next_target < MIN_RPM else next_target
                    self.last_down = now
                    if self.target == desired:
                        self.settling = False
                        self.peak = temperature
            else:
                self.cool_since = None
                self.peak = max(self.peak, temperature)
        else:
            self.cool_since = None
            self.peak = max(self.peak, temperature)
        self._remember_target(now)
        return self.target

    def is_stalled(self, rpm, now):
        # A higher target needs ramp time on every curve/preset change, not
        # only at initial startup. The rolling minimum remains bounded and
        # still catches a stopped/underperforming fan during repeated edits.
        required = max(MIN_RPM, min(target for _, target in self.history))
        return self.target > 0 and now-self.started > TACH_GRACE_S and rpm < .6*required
