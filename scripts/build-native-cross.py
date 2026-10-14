#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Create an experimental x86_64 Linux ELF on a Windows development host.

Uses matching official Qt host/target SDKs and Zig's cross compiler. This is a
cross-build, not proof of SteamOS runtime behavior. CMake remains the normal
Linux build path, including QML ahead-of-time compilation and native tests.
"""
import argparse
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--qt',type=Path,required=True)
parser.add_argument('--host-qt',type=Path,required=True)
parser.add_argument('--zig',type=Path,required=True)
parser.add_argument('--build',type=Path,required=True)
parser.add_argument('--gnu-headers',type=Path,required=True)
args=parser.parse_args()
args.build=args.build.resolve();args.build.mkdir(parents=True,exist_ok=True)
sdk=args.qt.resolve();host=args.host_qt.resolve();source=root/'desktop/native'
toolsuffix='.exe' if (host/'bin/rcc.exe').exists() else ''
def run(command): subprocess.run([str(value) for value in command],check=True)
moc=args.build/'moc_rpcclient.cpp'
run([host/('bin/moc'+toolsuffix),source/'rpcclient.h','-o',moc])
hostmoc=args.build/'moc_apphost.cpp'
run([host/('bin/moc'+toolsuffix),source/'apphost.h','-o',hostmoc])
logmoc=args.build/'moc_logviewer.cpp'
run([host/('bin/moc'+toolsuffix),source/'logviewer.h','-o',logmoc])
pages=sorted(path.stem for path in source.glob('*.qml'))
qmldir=args.build/'qmldir'
qmldir.write_text('module Companion\n'+''.join(f'{name} 1.0 {name}.qml\n' for name in pages))
resource=ET.Element('RCC');section=ET.SubElement(resource,'qresource',prefix='/qt/qml/Companion')
for name,path in [('qmldir',qmldir),('rpc-methods.json',source/'rpc-methods.json')]+[(name+'.qml',source/(name+'.qml')) for name in pages]:
    ET.SubElement(section,'file',alias=name).text=str(path)
qrc=args.build/'companion.qrc';ET.ElementTree(resource).write(qrc,encoding='utf-8')
rcc=args.build/'resources.cpp'
run([host/('bin/rcc'+toolsuffix),qrc,'-o',rcc])
include=['Core','Gui','Widgets','Network','Qml','QmlIntegration','Quick','QuickControls2','OpenGL']
command=[args.zig.resolve(),'c++','-target','x86_64-linux-gnu.2.34','-std=c++17','-O2','-fPIC','-pthread',
         '-Wall','-Wextra','-Wpedantic','-Werror','-DQT_NO_DEBUG','-I'+str(sdk/'include'),'-I'+str(sdk/'mkspecs/linux-g++'),'-I'+str(source)]
gnu=args.gnu_headers.resolve()
command += ['-nostdinc++','-nostdlib++','-I'+str(gnu/'usr/include/c++/12'),
            '-I'+str(gnu/'usr/include/x86_64-linux-gnu/c++/12')]
for module in include:
    command += ['-I'+str(sdk/('include/Qt'+module)),'-DQT_'+module.upper()+'_LIB']
command += [source/'main.cpp',source/'rpcclient.cpp',source/'apphost.cpp',source/'logviewer.cpp',moc,hostmoc,logmoc,rcc]
command += ['-L'+str(sdk/'lib'),'-Wl,--allow-shlib-undefined','-Wl,-rpath,$ORIGIN/lib']
command += [next((gnu/'usr/lib/x86_64-linux-gnu').glob('libstdc++.so.6.*'))]
command += [gnu/'lib/x86_64-linux-gnu/libgcc_s.so.1']
for module in ['QuickControls2','Quick','Qml','Network','Widgets','Gui','Core']:
    command.append('-lQt6'+module)
command += ['-o',args.build/'legiongo2companion']
run(command)
print('Cross-built Linux ELF; native Linux/SteamOS execution is still pending.')
