# SPDX-License-Identifier: BSD-3-Clause
"""Pure curve validation and asymmetric control; no hardware or Decky imports."""
import copy
import math
from collections import deque

MIN_RPM, MAX_RPM, RPM_STEP = 1500, 4800, 1
STOP_SETTLE_S = 20  # A rotor coasting down from 4800 RPM can exceed 12 seconds.
# Nonzero sub-threshold override selects a real stop; override zero releases it.
STOP_OVERRIDE = 256
HARD_MAX_C = 100.0
LEASE_S, SAMPLE_S = 3.0, 0.25
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
    return _validate_curve(value, -1, True)


def _validate_curve(value, minimum, monotonic):
    if not isinstance(value, list) or len(value) != 8:
        raise ValueError('A curve must contain exactly eight points.')
    last_t, last_r = minimum, 0
    result = []
    for i, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError('Invalid curve point.')
        t, rpm = point
        if type(t) is not int or type(rpm) is not int:
            raise ValueError('Temperature and RPM must be integers.')
        if not last_t < t <= 100 or not valid_rpm(rpm) or monotonic and rpm < last_r:
            raise ValueError('Curve points must be ordered and inside the validated operating range.')
        if i == 7 and (t != 100 or rpm != MAX_RPM):
            raise ValueError('The last point must be 100°C at full speed.')
        result.append([t, rpm]); last_t, last_r = t, rpm
    return result


def validate_settings(value):
    return _validate_settings(value, 4)


def _validate_settings(value, schema):
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != schema:
        raise ValueError('Unsupported fan settings schema; review before enabling.')
    if type(value.get('enabled')) is not bool or type(value.get('revision')) is not int or value['revision'] < 0:
        raise ValueError('Invalid fan settings intent.')
    mappings = value.get('presets')
    if not isinstance(mappings, dict) or set(mappings) - set(PRESETS):
        raise ValueError('Unknown Companion preset assignment.')
    unit = value.get('display_unit', 'rpm')
    if unit not in ('rpm', 'percent'):
        raise ValueError('Unknown fan speed display unit.')
    check = validate_curve if schema >= 3 else lambda curve: _validate_curve(curve, 49, False)
    result = dict(schema=schema, enabled=value['enabled'], revision=value['revision'], display_unit=unit,
                global_curve=check(value.get('global_curve')),
                presets={k: check(v) for k, v in mappings.items()})
    if schema == 4:
        assignment_mode = value.get('assign_tdp_presets', False)
        if type(assignment_mode) is not bool:
            raise ValueError('TDP curve assignments must be a boolean.')
        result['assign_tdp_presets'] = assignment_mode
    if 'curve_archive_v2' in value:
        archive = value['curve_archive_v2']
        if schema < 3 or not isinstance(archive, dict) or set(archive) != {'global', 'presets'}:
            raise ValueError('Invalid eight-point curve archive.')
        _validate_curve(archive['global'], 49, False)
        if not isinstance(archive['presets'], dict) or set(archive['presets']) - set(PRESETS):
            raise ValueError('Invalid eight-point preset archive.')
        for curve in archive['presets'].values():
            _validate_curve(curve, 49, False)
        result['curve_archive_v2'] = copy.deepcopy(archive)
    if 'legacy_curves' in value:
        legacy = value['legacy_curves']
        if not isinstance(legacy, dict) or set(legacy) != {'global', 'presets'}:
            raise ValueError('Invalid legacy curve archive.')
        validate_legacy_curve(legacy['global'])
        if not isinstance(legacy['presets'], dict) or set(legacy['presets']) - set(PRESETS):
            raise ValueError('Invalid legacy preset archive.')
        for curve in legacy['presets'].values():
            validate_legacy_curve(curve)
        result['legacy_curves'] = copy.deepcopy(legacy)
    return result


def validate_legacy_curve(value):
    if not isinstance(value, list) or len(value) != 10:
        raise ValueError('Invalid legacy ten-point curve.')
    last_t, last_r = 49, 0
    for i, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError('Invalid legacy point.')
        t, r = point
        if (type(t) is not int or type(r) is not int or not last_t < t <= 100 or t % 5
                or not (r == 0 or MIN_RPM <= r <= MAX_RPM and r % 100 == 0) or r < last_r
                or i == 0 and t != 50 or i == 9 and t != 100):
            raise ValueError('Invalid legacy operating range.')
        last_t, last_r = t, r
    return copy.deepcopy(value)


def migrate_settings(value):
    """Preserve every old point and mapping; minimize resampling error, never arm."""
    if isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 3:
        result = _validate_settings(value, 3)
        result.update(schema=4, enabled=False, assign_tdp_presets=False, revision=result['revision']+1)
        return validate_settings(result)
    if isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 2:
        result = _validate_settings(value, 2)
        result['curve_archive_v2'] = copy.deepcopy({'global': result['global_curve'], 'presets': result['presets']})
        for curve in [result['global_curve'], *result['presets'].values()]:
            # Cumulative maximum never reduces cooling at any temperature.
            running = 0
            for point in curve:
                running = max(running, point[1])
                point[1] = running
        result.update(schema=4, enabled=False, assign_tdp_presets=False, revision=result['revision']+1)
        return validate_settings(result)
    if not isinstance(value, dict) or value.get('schema') != 1:
        return validate_settings(value)
    if type(value['schema']) is not int:
        raise ValueError('Invalid legacy schema.')
    from itertools import combinations
    if type(value.get('enabled')) is not bool or type(value.get('revision')) is not int or value['revision'] < 0:
        raise ValueError('Invalid legacy intent.')
    mappings = value.get('presets')
    if not isinstance(mappings, dict) or set(mappings) - set(PRESETS):
        raise ValueError('Invalid legacy preset assignments.')
    original = dict(global_curve=validate_legacy_curve(value.get('global_curve')),
                    presets={k: validate_legacy_curve(v) for k, v in mappings.items()})
    def reduce(curve):
        candidates = [copy.deepcopy([curve[0]]+[curve[i] for i in indices]+[[100, MAX_RPM]])
                      for indices in combinations(range(1, 9), 6)]
        # Compare raw interpolation below the newly fixed full-speed endpoint.
        def raw(points, t):
            if t <= points[0][0]: return points[0][1]
            for (a, r1), (b, r2) in zip(points, points[1:]):
                if t <= b: return r1+(r2-r1)*(t-a)/(b-a)
            return points[-1][1]
        def error(points):
            errors = [abs(raw(points, t)-raw(curve, t)) for t in range(50, 100)]
            return max(errors), sum(errors)
        return validate_curve(min(candidates, key=error))
    result = dict(schema=4, enabled=False, assign_tdp_presets=False, revision=value['revision']+1, display_unit='rpm',
                  global_curve=reduce(original['global_curve']),
                  presets={k: reduce(v) for k, v in original['presets'].items()},
                  legacy_curves={'global':original['global_curve'], 'presets':original['presets']})
    return validate_settings(result)


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

    def step(self, curve, temperature, now):
        desired = target_for(curve, temperature)
        if temperature >= HARD_MAX_C:
            self.target = MAX_RPM
            self.peak = temperature
            self.cool_since = None
            self.settling = False
            self.history.clear(); self.history.append((now, MAX_RPM))
            return MAX_RPM
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
        if self.history[-1][1] != self.target:
            self.history.append((now, self.target))
        while len(self.history)>1 and self.history[1][0] <= now-12:
            self.history.popleft()
        return self.target

    def is_stalled(self, rpm, now):
        # A higher target needs ramp time on every curve/preset change, not
        # only at initial startup. The rolling minimum remains bounded and
        # still catches a stopped/underperforming fan during repeated edits.
        required = max(MIN_RPM, min(target for _, target in self.history))
        return self.target > 0 and now-self.started > 12 and rpm < .6*required
