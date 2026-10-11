# SPDX-License-Identifier: BSD-3-Clause
import copy
import unittest
from fan_policy import *


class FanPolicyTests(unittest.TestCase):
    def test_templates_are_valid_ordered_starting_points_and_defaults_are_independent(self):
        self.assertEqual(set(TEMPLATES),{'silent','balanced','aggressive'})
        for curve in TEMPLATES.values():self.assertEqual(validate_curve(curve),curve)
        self.assertEqual(defaults()['global_curve'],TEMPLATES['balanced'])
        self.assertEqual(defaults()['display_unit'],'rpm');self.assertFalse(defaults()['assign_tdp_presets'])
        self.assertFalse(defaults()['enabled']);self.assertEqual(defaults()['presets'],{})
        modified=defaults();modified['global_curve'][0][1]=0
        self.assertEqual(TEMPLATES['balanced'][0][1],1500)
        for temp in range(101):
            self.assertLessEqual(target_for(TEMPLATES['silent'],temp),target_for(TEMPLATES['balanced'],temp))
            self.assertLessEqual(target_for(TEMPLATES['balanced'],temp),target_for(TEMPLATES['aggressive'],temp))

    def test_v3_preserves_curves_units_maps_archives_with_assignments_dormant(self):
        old=dict(defaults(),schema=3,enabled=True,revision=43,display_unit='percent',presets={'custom':TEMPLATES['silent']})
        old.pop('assign_tdp_presets');original=copy.deepcopy(old);new=migrate_settings(old)
        self.assertEqual(old,original);self.assertEqual(new['global_curve'],old['global_curve'])
        self.assertEqual(new['presets'],old['presets']);self.assertEqual(new['display_unit'],'percent')
        self.assertFalse(new['assign_tdp_presets']);self.assertFalse(new['enabled']);self.assertEqual(new['revision'],44)
        self.assertEqual(migrate_settings(new),new)
        for bad in (None,1,'false',[],{}):
            with self.assertRaises(ValueError):validate_settings(dict(new,assign_tdp_presets=bad))

    def test_eight_points_only_final_fixed_one_degree_and_real_stop(self):
        self.assertEqual(validate_curve(DEFAULT_CURVE), DEFAULT_CURVE)
        for curve in [DEFAULT_CURVE[:-1], DEFAULT_CURVE+[DEFAULT_CURVE[-1]],
                      [[50, 0]]*8, [[50, 1499]]*8, [[50, True]]*8]:
            with self.subTest(curve=curve), self.assertRaises(ValueError):
                validate_curve(curve)
        self.assertEqual(validate_curve([[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)])[0][1],0)
        for index, axis, value in [(0,0,-1),(7,0,99),(5,0,61),(7,1,4799),
                                  (7,1,5300),(2,0,True),(2,1,float('nan')),(0,1,1400)]:
            bad = copy.deepcopy(DEFAULT_CURVE); bad[index][axis] = value
            with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                validate_curve(bad)

    def test_interpolation_never_requests_firmware_deadband(self):
        stop_ramp=[[t,0 if i<2 else MIN_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        for c in [DEFAULT_CURVE, stop_ramp, [[t,MIN_RPM] for t,_ in DEFAULT_CURVE], [[t,MAX_RPM] for t,_ in DEFAULT_CURVE]]:
            targets = [target_for(c, t/10) for t in range(-100, 1501)]
            self.assertTrue(all(valid_rpm(r) for r in targets))
            self.assertEqual(targets, sorted(targets))
        for temp in [True, float('inf'), float('nan'), None, '55']:
            with self.assertRaises(ValueError): target_for(DEFAULT_CURVE, temp)

    def test_start_is_direct_rise_is_fast_and_thermal_descent_is_damped(self):
        c = [[t,MIN_RPM] for t,_ in DEFAULT_CURVE]
        p = CurveControl(0,50,c)
        values = [p.step(c,50,i/4) for i in range(120)]
        self.assertEqual(set(values),{MIN_RPM})
        high = [[t,MAX_RPM] for t,_ in DEFAULT_CURVE]
        self.assertEqual(p.step(high,80,30),3500)
        self.assertEqual(p.step(high,80,30.25),MAX_RPM)
        self.assertEqual(p.step(c,79,31),MAX_RPM)  # hysteresis
        self.assertEqual(p.step(c,77,32),MAX_RPM)
        self.assertEqual(p.step(c,77,36),MAX_RPM)  # dwell
        self.assertEqual(p.step(c,77,37),MAX_RPM-300)
        self.assertEqual(p.step(c,77,38),MAX_RPM-300)  # step-down interval
        self.assertEqual(p.step(c,77,39),MAX_RPM-600)

    def test_settings_schema_assignments_and_defaults_are_isolated(self):
        a, b = defaults(), defaults(); a['global_curve'][0][1]=3300
        self.assertEqual(b['global_curve'][0][1],1500)
        self.assertFalse(validate_settings(b)['enabled'])
        for value in [dict(b,schema=99),dict(b,enabled='false'),dict(b,revision=True),
                      dict(b,presets={'qam':DEFAULT_CURVE}),dict(b,presets={'silent': []})]:
            with self.assertRaises(ValueError): validate_settings(value)
        all_presets = dict(b,presets={p:DEFAULT_CURVE for p in PRESETS})
        self.assertEqual(len(validate_settings(all_presets)['presets']),5)
        self.assertIn('max',validate_settings(all_presets)['presets'])
        self.assertIn('custom',validate_settings(all_presets)['presets'])

    def test_edit_or_preset_lower_curve_eventually_applies_at_constant_temperature(self):
        p=CurveControl(0,50)
        high=[[t,MAX_RPM] for t,_ in DEFAULT_CURVE]
        low=[[t,MIN_RPM] for t,_ in DEFAULT_CURVE]
        p.step(high,50,19.75);p.step(high,50,20);self.assertEqual(p.target,MAX_RPM)
        p.retarget(50,21)
        values=[p.step(low,50,i/4) for i in range(84,240)]
        self.assertEqual(values[0],MAX_RPM);self.assertEqual(values[-1],MIN_RPM)
        changes=[b-a for a,b in zip(values,values[1:]) if b!=a]
        self.assertTrue(all(-300<=c<0 for c in changes))

    def test_zero_restart_follows_curve_and_no_hidden_thermal_handover(self):
        stop=[[t,0 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        p=CurveControl(0,45,stop)
        self.assertEqual(p.step(stop,55,2),0)
        self.assertEqual(p.step(stop,85,3),0)
        self.assertEqual(p.step(stop,100,3.25),MAX_RPM)
        self.assertEqual(p.step(stop,105,3.5),MAX_RPM)
        self.assertEqual(CurveControl(0,100,stop).target,MAX_RPM)
        self.assertEqual(p.step(stop,99,4),MAX_RPM)
        p=CurveControl(0,45,stop)
        positive=[[t,MIN_RPM if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        self.assertEqual(p.step(positive,50,10),MIN_RPM)

    def test_full_temperature_range_equal_rpm_and_one_rpm_resolution(self):
        curve=[[0,0],[1,0],[2,1500],[3,1501],[40,3073],[80,3073],[99,4799],[100,4800]]
        self.assertEqual(validate_curve(curve),curve)
        self.assertEqual(target_for(curve,0),0)
        self.assertEqual(target_for(curve,2),1500)
        self.assertEqual(target_for(curve,100),4800)
        for index in range(1,8):
            bad=copy.deepcopy(curve);bad[index][0]=bad[index-1][0]
            with self.assertRaises(ValueError):validate_curve(bad)
        for index in range(1,7):
            bad=copy.deepcopy(DEFAULT_CURVE);bad[index][1]=bad[index-1][1]-1
            with self.assertRaises(ValueError):validate_curve(bad)

    def test_legacy_migration_preserves_all_points_and_assignments_never_arms(self):
        legacy=[[50,3000],[55,3000],[60,3300],[65,3600],[70,3900],[75,4200],[80,4600],[85,4800],[95,4800],[100,4800]]
        low=[[t,1500] for t,_ in legacy]
        old=dict(schema=1,enabled=True,revision=14,global_curve=legacy,presets={'silent':low,'max':legacy})
        original=copy.deepcopy(old);new=migrate_settings(old)
        self.assertEqual(old,original);self.assertFalse(new['enabled']);self.assertEqual(new['revision'],15)
        self.assertEqual(new['schema'],4);self.assertEqual(new['display_unit'],'rpm')
        self.assertEqual(new['legacy_curves'],{'global':legacy,'presets':old['presets']})
        self.assertEqual(set(new['presets']),{'silent','max'})
        for curve in [new['global_curve'],*new['presets'].values()]:self.assertEqual(len(validate_curve(curve)),8);self.assertEqual(curve[-1],[100,4800])
        self.assertEqual(migrate_settings(new),new)
        for bad in (dict(old,schema=99),dict(old,enabled=1),dict(old,global_curve=legacy[:-1]),dict(old,presets={'invalid':legacy})):
            with self.assertRaises(ValueError):migrate_settings(bad)

    def test_running_fan_new_higher_target_gets_ramp_grace_without_masking_a_stall(self):
        low=[[t,MIN_RPM] for t,_ in DEFAULT_CURVE];high=[[t,MAX_RPM] for t,_ in DEFAULT_CURVE]
        p=CurveControl(0,50)
        for i in range(120):p.step(low,50,i/4)
        self.assertFalse(p.is_stalled(1500,30))
        p.retarget(50,30);p.step(high,50,30);p.step(high,50,30.25)
        self.assertFalse(p.is_stalled(1500,30.25),'a previously stable low speed may ramp up')
        self.assertTrue(p.is_stalled(0,30.25),'curve edits must not hide a stopped fan')
        for i in range(122,177):p.step(high,50,i/4)
        self.assertTrue(p.is_stalled(1500,44),'high target must eventually attain speed')
        for i in range(177,200):p.step(high,50,i/4)
        self.assertLessEqual(len(p.history),49)

    def test_v2_migration_archives_all_scopes_and_never_reduces_cooling(self):
        curve=[[50,4800],[57,0],[64,1501],[71,3500],[78,1501],[85,4600],[93,0],[100,4800]]
        legacy=[[50,3000],[55,3000],[60,3300],[65,3600],[70,3900],[75,4200],[80,4600],[85,4800],[95,4800],[100,4800]]
        old=dict(schema=2,enabled=True,revision=42,display_unit='percent',global_curve=curve,
                 presets={name:copy.deepcopy(curve) for name in PRESETS},
                 legacy_curves={'global':legacy,'presets':{'custom':legacy}})
        original=copy.deepcopy(old);new=migrate_settings(old)
        self.assertEqual(old,original);self.assertFalse(new['enabled']);self.assertEqual(new['revision'],43)
        self.assertEqual(new['schema'],4);self.assertEqual(new['display_unit'],'percent')
        self.assertEqual(new['curve_archive_v2'],{'global':curve,'presets':old['presets']})
        self.assertEqual(new['legacy_curves'],old['legacy_curves'])
        for c in [new['global_curve'],*new['presets'].values()]:
            validate_curve(c)
            for tenth in range(1001):self.assertGreaterEqual(target_for(c,tenth/10),target_for(curve,tenth/10))
        self.assertEqual(validate_settings(new),new);self.assertEqual(migrate_settings(new),new)
        new['global_curve'][0][1]=1500
        self.assertEqual(new['curve_archive_v2']['global'],curve)
        for bad in (dict(old,schema=True),dict(old,schema=2.0),dict(old,global_curve=[[0,0]]*8)):
            with self.assertRaises(ValueError):migrate_settings(bad)

    def test_flat_curve_startup_settles_while_temperature_increases(self):
        curve=[[t,1501 if i<7 else MAX_RPM] for i,(t,_) in enumerate(DEFAULT_CURVE)]
        p=CurveControl(0,50,curve)
        values=[p.step(curve,50+min(i/4,15),i/4) for i in range(180)]
        self.assertEqual(values[0],1501)
        self.assertEqual(values[-1],1501)
        self.assertTrue(all(b-a>=-300 for a,b in zip(values,values[1:])))

    def test_restart_uses_requested_speed_without_boost_or_dwell(self):
        stop=[[t,0] for t,_ in DEFAULT_CURVE]
        for rpm in (1500,1501,2000,4000,MAX_RPM):
            curve=[[t,rpm] for t,_ in DEFAULT_CURVE]
            p=CurveControl(0,40,stop)
            self.assertEqual(p.target,0)
            self.assertEqual(p.step(curve,40,30),rpm)
            for tick in range(1,60):self.assertEqual(p.step(curve,40,30+tick/4),rpm)

    def test_failed_start_is_bounded_and_edits_cannot_renew_grace(self):
        curve=[[t,1500] for t,_ in DEFAULT_CURVE]
        stop=[[t,0] for t,_ in DEFAULT_CURVE]
        for restart in (False,True):
            p=CurveControl(0,40,stop if restart else curve)
            start=30 if restart else 0
            p.step(curve,40,start)
            self.assertFalse(p.is_stalled(0,start+12))
            for tick in range(1,52):
                p.retarget(40,start+tick/4);p.step(curve,40,start+tick/4)
            self.assertTrue(p.is_stalled(0,start+13))
            self.assertTrue(p.is_stalled(899,start+13))
            self.assertFalse(p.is_stalled(1500,start+13))
