# SPDX-License-Identifier: BSD-3-Clause
"""A persistent firmware clamp is not an unbounded stream of new conflicts."""
import contextlib
import unittest
from unittest.mock import patch
import test_integration
import tdp_backend as tdp


class DriftBudgetTests(unittest.TestCase):
    def setUp(self):
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.want=(25000,30000,35000);self.observed={'spl_limit':25.,'sppt_limit':30.,'fppt_limit':35.}
        self.override=True;self.profile_lost=False
        for name,value in {'_last_source':'wmi','_drift_target':(),'_drift_settled':(),
                '_drift_attempts':0,'_drift_context':(False,()),'_wmi_live_limits':(15.,17.)}.items():
            self.stack.enter_context(patch.object(tdp,name,value,create=True))
        self.stack.enter_context(patch.object(tdp,'_panel_is_active',return_value=False))
        self.stack.enter_context(patch.object(tdp,'_read_limits',side_effect=lambda:dict(self.observed)))
        self.stack.enter_context(patch.object(tdp,'_wmi_profile_lost',side_effect=lambda:self.profile_lost))
        self.stack.enter_context(patch.object(tdp,'_wmi_limits_overridden',side_effect=lambda want:self.override))
        self.stack.enter_context(patch.object(tdp,'_load_settings',return_value={}))
        self.apply=self.stack.enter_context(patch.object(tdp,'_apply_limits_with_saved_cpu_power',return_value={'success':True}))

    def passes(self,n=12):
        for _ in range(n):tdp._enforce_target(self.want)

    def test_persistent_live_limit_disagreement_does_not_reset_retry_budget(self):
        self.passes()
        self.assertEqual(self.apply.call_count,tdp.DRIFT_MAX_ATTEMPTS)

    def test_not_due_crosschecks_do_not_turn_unverified_wmi_facade_into_recovery(self):
        for _ in range(8):
            self.override=True;self.passes(1)
            self.override=None;self.passes(4)
        self.assertEqual(self.apply.call_count,tdp.DRIFT_MAX_ATTEMPTS)

    def test_changed_real_limits_get_a_fresh_recovery_budget(self):
        self.passes();self.assertEqual(self.apply.call_count,tdp.DRIFT_MAX_ATTEMPTS)
        tdp._wmi_live_limits=(12.,14.)
        self.passes();self.assertEqual(self.apply.call_count,2*tdp.DRIFT_MAX_ATTEMPTS)

    def test_existing_tolerance_also_applies_to_an_unchanged_smu_clamp(self):
        self.passes();tdp._wmi_live_limits=(15.2,17.2)
        self.passes();self.assertEqual(self.apply.call_count,tdp.DRIFT_MAX_ATTEMPTS)

    def test_confirmed_live_recovery_resets_budget_for_a_later_independent_drift(self):
        self.passes();self.override=False;self.passes(1)
        self.assertEqual(tdp._drift_attempts,0)
        self.override=True;self.passes()
        self.assertEqual(self.apply.call_count,2*tdp.DRIFT_MAX_ATTEMPTS)

    def test_profile_that_never_accepts_custom_does_not_cause_endless_writes(self):
        self.override=None;self.profile_lost=True;self.passes()
        self.assertEqual(self.apply.call_count,tdp.DRIFT_MAX_ATTEMPTS)
        self.profile_lost=False;self.passes(1)
        self.assertEqual(tdp._drift_attempts,0)

    def test_ryzenadj_settled_limits_and_later_real_reset_still_recover(self):
        tdp._last_source='ryzenadj';self.override=False
        self.observed.update(spl_limit=15.,sppt_limit=20.,fppt_limit=25.)
        self.passes();self.assertEqual(self.apply.call_count,tdp.DRIFT_MAX_ATTEMPTS)
        self.observed.update(spl_limit=10.,sppt_limit=10.,fppt_limit=10.)
        self.passes();self.assertEqual(self.apply.call_count,2*tdp.DRIFT_MAX_ATTEMPTS)
