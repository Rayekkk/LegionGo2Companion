# SPDX-License-Identifier: BSD-3-Clause
"""Deterministic offline RPC fixture. It never imports a hardware backend."""
import copy

CURVE=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]]

# A rendered page must reach its own read path, not merely the shared gate.
# These are independent public RPC expectations, not copied UI field positions.
MODULE_READS={
    'tdp':{'get_settings','get_caps','get_extras_unlocked','get_power_source','get_tdp_info','get_cpu_power_controls','list_game_profiles'},
    'fan':{'fan_get_state'},
    'vibration':{'vibe_is_ready','vibe_get_settings','vibe_get_capabilities','vibe_get_driver_status'},
    'rgb':{'rgb_get_status'},'remap':{'remap_get_status'},'controller':{'controller_get_status'},
    'battery':{'battery_get_status','battery_get_controller_levels'},
    'display':{'display_get_state'},'wifi':{'wifi_get_status'},
}
PAGE_READS={
    **MODULE_READS,
    'overview':{'get_settings','get_tdp_info','get_power_source','fan_get_state','battery_get_status',
                'battery_get_controller_levels','controller_get_status','vibe_get_settings','vibe_get_capabilities','wifi_get_status'},
    'controller':MODULE_READS['controller'] | MODULE_READS['vibration'] | MODULE_READS['remap']
                 | {'battery_get_controller_levels','firmware_get_status'},
    'settings':set(),'modules':set(),'about':set(),
}

def read_owner(method):
    return next((name for name,methods in MODULE_READS.items() if method in methods),None)

def expected_reads(page,disabled=()):
    return {'get_version','desktop_context'} | {
        method for method in PAGE_READS[page] if read_owner(method) not in disabled
    }

def states():
    modules={name:{'enabled':True,'ready':True,'error':''} for name in
             ('tdp','fan','vibration','rgb','remap','controller','battery','display','wifi')}
    return {
        'get_version':{'version':'2.1.0','blocked':False,'modules':modules},
        'desktop_context':{'app_id':'','gamescope':False},
        'set_panel_active':True,
        'get_settings':{'enabled':True,'spl':16000,'sppt':25000,'fppt':30000,'active_spl':16000,'active_preset':'balanced','advanced_tdp_control':True},
        'get_caps':{'min':5,'std':{'spl':35,'sppt':37,'fppt':45},'max':{'spl':35,'sppt':50,'fppt':50},'tdp_available':True,'wmi':True,'extras':True,
                    'presets':{'silent':{'spl':8,'sppt':15,'fppt':20},'balanced':{'spl':16,'sppt':25,'fppt':30},'performance':{'spl':20,'sppt':32,'fppt':35},'max':{'spl':35,'sppt':37,'fppt':45}}},
        'get_extras_unlocked':False,'get_power_source':{'ac':False},
        'get_tdp_info':{'success':True,'values':{'spl_limit':16,'sppt_limit':25,'fppt_limit':30,'package_draw':9}},
        'get_cpu_power_controls':{'success':True,'available':True,'cpu_boost':{'available':True,'enabled':True},'epp':{'available':True,'value':'balance_performance','profiles':['performance','balance_performance','balance_power','power'],'numeric_supported':True,'numeric_value':128,'min':0,'max':255},'profile':{'cpu_boost_enabled':True,'epp':'balance_performance'}},
        'get_game_profile':{'exists':False,'profile':{},'ac_profile':{},'ac_separate':False},
        'list_game_profiles':{'profiles':[{'app_id':'250900','active':True,'ac_separate':True,'preset':'balanced','spl':16000,'ac_preset':'performance','ac_spl':20000},
                                          {'app_id':'3000000001','active':False,'ac_separate':False,'preset':'custom','spl':12000,'ac_preset':'','ac_spl':12000}]},
        'fan_get_state':{'supported':True,'active_curve':'global','settings':{'enabled':False,'revision':0,'assign_tdp_presets':False,'display_unit':'rpm','global_curve':copy.deepcopy(CURVE),'presets':{}},
                         'templates':{name:copy.deepcopy(CURVE) for name in ('silent','balanced','aggressive')},'presets':['silent','balanced','performance','max','custom'],
                         'limits':{'min_rpm':1500,'last_min_rpm':2000,'max_rpm':4800,'step':1,'hard_max':100},'ownership':{'blocked':False},
                         'readings':{'valid':True,'rpm':1800,'apu_temperature':48,'age_seconds':0,'automatic':True,'source':'CPU Tctl / GPU edge (offline fixture)'},'telemetry':{'active':False,'automatic':True}},
        'vibe_is_ready':{'ready':True},'vibe_get_settings':{'settings':{'level':2,'mode':2,'touchpadIntensity':2,'touchpadEnabled':True},'app_id':'0','profile_id':'0','overwrite':False},
        'vibe_get_capabilities':{'intensity':['off','low','medium','high'],'mode':['fps','racing','standard','spg','rpg'],'tp_intensity':['off','low','medium','high']},
        'vibe_get_driver_status':{'found':True,'method':'offline fixture'},
        'rgb_get_status':{'success':True,'settings':{'control_enabled':True,'rings_enabled':True,'effect':'monocolor','hue':200,'saturation':90,'brightness':50,'speed':50,'power_led_enabled':True},'rgb':{'supported':True,'effects':[{'id':'monocolor','label':'Solid'},{'id':'breathe','label':'Breathe'},{'id':'rainbow','label':'Rainbow'}]},'power_led':{'supported':False,'reason':'Unavailable in offline fixture'}},
        'remap_get_status':{'success':True,'supported':True,'enabled':False,'active':False,'desktop_action':'default','page_action':'default','actions':[{'id':'default','label':'Default'},{'id':'keyboard_key_esc','label':'Escape'}],'inputplumber_version':'offline fixture'},
        'controller_get_status':{'available':True,'gyro_source':'system','conflict':False,'physical':None,'virtual':None,'imu':{'available':False},'reason':'Offline fixture'},
        'firmware_get_status':{'stage':'image_validation','flash_available':False,'catalog_date':'2026-10-12','catalog_live':False,'installed_versions':None,'reason':'Device transfer is not enabled. Linux transport and recovery checks are pending.','job':{'id':'','phase':'idle','component':'','error':''}},
        'battery_get_status':{'success':True,'supported':True,'managed':False,'enabled':False,'capacity':75,'charging_status':'Discharging','current_mode':'Standard'},
        'battery_get_controller_levels':{'available':True,'left':80,'right':85},
        'display_get_state':{'setup_done':True,'panel_mode':'pq','enabled':False,'edid_fix':True,'panel_desc':'Offline OLED fixture','gamescope_reachable':False,'reason':'Waiting for Gaming Mode'},
        'wifi_get_status':{'success':True,'connected':True,'settings':{'driver':'mt7921e','band_policy':'off'},'live':{'ssid':'Offline fixture','frequency':'5180 MHz','wifi_backend':'iwd'}},
        'updates_check':{'success':True,'current_version':'2.1.0','latest_version':'2.1.0','update_available':False,'download_available':False},
    }
