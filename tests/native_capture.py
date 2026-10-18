#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Launch the real native executable against loopback fake hardware only."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import shutil
import struct
import tempfile
import threading
from native_fixture import MODULE_READS, PAGE_READS, expected_reads, read_owner, states

parser=argparse.ArgumentParser()
parser.add_argument('--exe',type=Path,required=True)
parser.add_argument('--qt',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--pages',nargs='+',choices=tuple(PAGE_READS),default=['overview','tdp','fan','controller','rgb','battery','display','wifi','modules','about'])
parser.add_argument('--fullscreen',action='store_true')
parser.add_argument('--size',help='Capture the native window at WIDTHxHEIGHT.')
parser.add_argument('--modules-off',nargs='*',choices=tuple(MODULE_READS),default=[])
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
fixtures=states(); calls=[]; unexpected=[]
for name in args.modules_off:
    fixtures['get_version']['modules'][name]={'enabled':False,'ready':False,'pending':False,'error':''}
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*unused): pass
    def do_POST(self):
        assert self.path=='/rpc'
        assert self.headers['Authorization']=='Bearer '+ 'a'*43
        assert self.headers['Origin']==f'http://127.0.0.1:{self.server.server_port}'
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        calls.append(data)
        if data['method'] not in fixtures:
            unexpected.append(data['method'])
            self.send_error(400,'No offline fixture for this method')
            return
        result=fixtures[data['method']]
        payload=json.dumps({'result':result}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server,tempfile.TemporaryDirectory() as temporary:
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    session=Path(temporary)/'session.json'
    session.write_text(json.dumps({'origin':f'http://127.0.0.1:{server.server_port}','token':'a'*43}));session.chmod(0o600)
    env=os.environ.copy();qt=args.qt.resolve();env['PATH']=str(qt/'bin')+os.pathsep+env.get('PATH','')
    # The shipped binary has no RUNPATH; point it at this Qt (build or newer runtime).
    env.update(LD_LIBRARY_PATH=str(qt/'lib'),QT_PLUGIN_PATH=str(qt/'plugins'),QML_IMPORT_PATH=str(qt/'qml'),LD_BIND_NOW='1');env['QT_QUICK_BACKEND']='software';env['QT_QPA_PLATFORM']='offscreen';env['QT_FORCE_STDERR_LOGGING']='1'
    if os.name=='nt':
        fonts=Path(temporary)/'fonts';fonts.mkdir()
        for name in ('segoeui.ttf','segoeuib.ttf','seguisym.ttf'):
            shutil.copyfile(Path(os.environ['WINDIR'])/'Fonts'/name,fonts/name)
        env['QT_QPA_FONTDIR']=str(fonts)
    for page in args.pages:
        previous=len(calls)
        command=[str(args.exe.resolve()),'--backend-session',str(session),'--capture',str((args.output/(page+'.png')).resolve()),'--capture-page',page]
        if args.fullscreen:command.append('--capture-fullscreen')
        if args.size:command.extend(['--capture-size',args.size])
        process=subprocess.run(command,env=env,capture_output=True,text=True,timeout=20)
        (args.output/(page+'.txt')).write_text(process.stdout+process.stderr,encoding='utf-8')
        assert process.returncode==0,(page,process.returncode,process.stderr)
        assert 'Error:' not in process.stderr and 'ReferenceError' not in process.stderr and 'TypeError' not in process.stderr,(page,process.stderr)
        assert any(call['method']=='get_version' for call in calls[previous:]),(page,'No actual RPC exchange')
        observed={call['method'] for call in calls[previous:]}
        assert expected_reads(page,args.modules_off)<=observed,(page,'Missing page reads',sorted(expected_reads(page,args.modules_off)-observed))
        assert not unexpected,(page,'Unknown RPCs lack fixtures',unexpected)
        screenshot=args.output/(page+'.png')
        png=screenshot.read_bytes()
        assert png[:16]==b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' and len(png)>=33,(page,'No valid PNG header')
        assert all(struct.unpack('>II',png[16:24])),(page,'Empty screenshot dimensions')
        # Qt may select its user-only default runtime directory on a headless
        # Linux runner. Keep the diagnostic log, without treating that fallback
        # as a UI failure. Other warnings/errors remain visible test failures.
        diagnostics=[line for line in process.stderr.splitlines()
                     if not line.startswith('QStandardPaths: XDG_RUNTIME_DIR not set, defaulting to ')]
        assert not any(line.strip() for line in diagnostics),(page,process.stderr)
        print('Native capture',page,'PASS',flush=True)
    # Viewing a panel must never implicitly change any hardware settings.
    for name in args.modules_off:
        assert not any(read_owner(call['method'])==name for call in calls),(name,'Disabled module was polled')
    assert all(call['method'].startswith(('get_','fan_get_','vibe_get_','vibe_is_','rgb_get_','remap_get_','controller_get_','firmware_get_','battery_get_','display_get_','wifi_get_')) or call['method'] in ('desktop_context','set_panel_active','list_game_profiles') for call in calls)
    (args.output/'rpc-calls.json').write_text(json.dumps(calls,indent=2))
    server.shutdown();thread.join()
