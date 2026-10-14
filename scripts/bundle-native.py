#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Bundle an explicit Linux Qt runtime, with a complete file hash manifest.

No system package manager, hardware access or downloads. The supplied license
directory is prepared from matching official Qt/ICU source trees separately.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct

def needed(path):
    data=path.read_bytes()
    if data[:6]!=b'\x7fELF\x02\x01':raise ValueError('Expected little-endian ELF64: '+str(path))
    offset=struct.unpack_from('<Q',data,32)[0];size,count=struct.unpack_from('<HH',data,54)
    loads=[];dynamic=None
    for index in range(count):
        kind,flags,start,virtual,physical,filesize,memory,align=struct.unpack_from('<IIQQQQQQ',data,offset+index*size)
        if kind==1:loads.append((virtual,start,filesize))
        if kind==2:dynamic=(start,filesize)
    if dynamic is None:return []
    tags=[];strings=None
    for position in range(dynamic[0],sum(dynamic),16):
        tag,value=struct.unpack_from('<qQ',data,position)
        if tag==0:break
        if tag==1:tags.append(value)
        if tag==5:strings=value
    base=next(start+strings-virtual for virtual,start,length in loads if virtual<=strings<virtual+length)
    return [data[base+index:data.index(b'\0',base+index)].decode() for index in tags]

def bundle(executable,sdk,output,licenses):
    output.mkdir(parents=True,exist_ok=True)
    if (output/'native-manifest.json').exists():raise ValueError('Use a fresh bundle directory; stale runtimes must not be merged.')
    def copy(source,relative):
        target=output/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    copy(executable,'legiongo2companion')
    (output/'legiongo2companion').chmod(0o755)
    modules=['QML','QtQml','QtQml/Models','QtQml/WorkerScript','QtQuick','QtQuick/Window','QtQuick/Layouts','QtQuick/Shapes',
             'QtQuick/Controls','QtQuick/Controls/impl','QtQuick/Controls/Basic','QtQuick/Controls/Basic/impl','QtQuick/Templates','Qt/labs/qmlmodels']
    for module in modules:
        directory=sdk/'qml'/module
        if not (directory/'qmldir').is_file():raise ValueError('Missing Qt QML module: '+module)
        # Only files directly in this module, not optional unrelated styles/tests.
        for source in directory.iterdir():
            if source.is_file() and (source.suffix in ('.so','.qml','.js') or source.name=='qmldir'):
                copy(source,'qml/'+module+'/'+source.name)
    for group in ['platforms','xcbglintegrations','wayland-graphics-integration-client','wayland-decoration-client','wayland-shell-integration','imageformats']:
        directory=sdk/'plugins'/group
        if not directory.exists():continue
        for source in directory.glob('*.so'):
            if group=='platforms' and not (source.name in ('libqxcb.so','libqoffscreen.so') or source.name.startswith('libqwayland')):continue
            if group=='imageformats' and source.name!='libqsvg.so':continue
            copy(source,'plugins/'+group+'/'+source.name)
    queue=list(output.rglob('*.so'))+[output/'legiongo2companion'];done=set();system=set()
    while queue:
        source=queue.pop()
        for library in needed(source):
            if library.startswith(('libQt6','libicu')):
                if library in done:continue
                original=sdk/'lib'/library
                if not original.is_file():raise ValueError('Missing runtime dependency: '+library)
                copy(original,'lib/'+library);done.add(library);queue.append(output/'lib'/library)
            else:system.add(library)
    for source in licenses.rglob('*'):
        if source.is_file():copy(source,'licenses/'+source.relative_to(licenses).as_posix())
    for module in ('qtbase','qtdeclarative','qtsvg','qtwayland'):
        source=sdk/'sbom'/(module+'-6.11.2.cdx.json')
        if not source.is_file():raise ValueError('Missing upstream SDK license inventory: '+module)
        copy(source,'licenses/SDK-SBOM/'+source.name)
    (output/'qt.conf').write_text('[Paths]\nPrefix=.\nLibraries=lib\nPlugins=plugins\nQmlImports=qml\n',encoding='utf-8')
    files={path.relative_to(output).as_posix():{'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size} for path in sorted(output.rglob('*')) if path.is_file()}
    manifest={'schema':1,'platform':'linux-x86_64','qt_version':'6.11.2','executable':'legiongo2companion','system_dependencies':sorted(system),'files':files}
    (output/'native-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'files':len(files),'bytes':sum(file['bytes'] for file in files.values()),'system_dependencies':sorted(system)},indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for option in ('exe','qt','output','licenses'):parser.add_argument('--'+option,type=Path,required=True)
    args=parser.parse_args();bundle(args.exe,args.qt,args.output,args.licenses)
