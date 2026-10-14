#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Fetch license notices for the exact Qt/ICU SDK used by this experiment."""
import argparse
import hashlib
import json
from pathlib import Path
import requests
parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
root=args.output;root.mkdir(parents=True,exist_ok=False)
receipts=[]
for module in ('qtbase','qtdeclarative','qtsvg','qtwayland'):
    response=requests.get(f'https://api.github.com/repos/qt/{module}/contents/LICENSES?ref=v6.11.2',timeout=60);response.raise_for_status()
    for file in response.json():
        target=root/file['name']
        if target.exists():continue
        reply=requests.get(file['download_url'],timeout=60);reply.raise_for_status();target.write_bytes(reply.content)
        receipts.append({'url':file['download_url'],'sha256':hashlib.sha256(reply.content).hexdigest()})
url='https://raw.githubusercontent.com/unicode-org/icu/release-73-2/icu4c/LICENSE'
reply=requests.get(url,timeout=60);reply.raise_for_status();(root/'ICU-LICENSE.txt').write_bytes(reply.content)
receipts.append({'url':url,'sha256':hashlib.sha256(reply.content).hexdigest()})
(root/'source-receipts.json').write_text(json.dumps(receipts,indent=2))
(root/'Qt-SOURCE.txt').write_text('Qt 6.11.2 official corresponding sources and build instructions:\nhttps://code.qt.io/cgit/qt/qtbase.git/?h=v6.11.2\nhttps://code.qt.io/cgit/qt/qtdeclarative.git/?h=v6.11.2\nhttps://code.qt.io/cgit/qt/qtsvg.git/?h=v6.11.2\nhttps://code.qt.io/cgit/qt/qtwayland.git/?h=v6.11.2\nhttps://github.com/unicode-org/icu/tree/release-73-2\nQt is dynamically linked. Replacement compatible shared libraries may be supplied; source/build inputs of this application are included in desktop/native and scripts.\n')
print('Qt and ICU license texts/source references prepared:',len(list(root.glob('*'))))
