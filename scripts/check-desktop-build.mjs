// SPDX-License-Identifier: BSD-3-Clause
import assert from 'node:assert/strict';
import {readFileSync,readdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {createHash} from 'node:crypto';
const root=resolve(import.meta.dirname,'..');
const read=name=>readFileSync(resolve(root,name),'utf8');
const methods=JSON.parse(read('desktop/native/rpc-methods.json'));
assert.equal(methods.length,new Set(methods).size);
assert.deepEqual(JSON.parse(read('dist/desktop/rpc-methods.json')),methods);
const calls=new Set();
for(const file of ['index','tdp','fan','vibration','rgb','remap','controller','battery','display','wifi','updates','firmware'])
  for(const match of read(`src/${file}.tsx`).matchAll(/callable\s*<[\s\S]*?>\s*\(\s*"([a-z_]+)"/g))calls.add(match[1]);
calls.delete('set_active_app');calls.delete('vibe_set_active_app');
assert.deepEqual(methods,[...calls].sort(),'Native API contract drift');
for(const name of methods) assert.ok(read('main.py').includes(`async def ${name}(`),`Missing shared RPC: ${name}`);
assert.ok(!/webbrowser|chromium|open\.html/.test(read('desktop/launch.py')),'Browser launcher remains');
assert.ok(!/WebEngine|\/dev\/(?:port|hidraw|mem)|QProcess/.test(read('desktop/native/rpcclient.cpp')+read('desktop/native/main.cpp')),'UI must not own hardware or embed a browser');
for(const page of ['overview','tdp','fan','controller','battery','display','wifi','settings','modules','logs','about'])
  assert.ok(read('desktop/native/Main.qml').includes(`id:"${page}"`),`Missing native page: ${page}`);
if(process.argv.includes('--runtime')) {
  const bundle=resolve(root,'dist/desktop');
  const manifest=JSON.parse(read('dist/desktop/native-manifest.json'));
  assert.equal(manifest.platform,'linux-x86_64'); assert.equal(manifest.executable,'legiongo2companion');
  assert.ok(manifest.files['licenses/LGPL-3.0-only.txt'] && manifest.files['licenses/Qt-SOURCE.txt']);
  function files(dir,prefix='') { return readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?files(resolve(dir,e.name),prefix+e.name+'/'):[prefix+e.name]); }
  assert.deepEqual(files(bundle).sort(),Object.keys(manifest.files).concat('native-manifest.json','rpc-methods.json').sort(),'Unexpected/stale native bundle file');
  for(const [name,entry] of Object.entries(manifest.files)) {
    assert.ok(!name.includes('..') && !name.startsWith('/') && !name.includes('\\'));
    const data=readFileSync(resolve(bundle,name));
    assert.equal(data.length,entry.bytes); assert.equal(createHash('sha256').update(data).digest('hex'),entry.sha256,`Runtime checksum: ${name}`);
  }
  const elf=readFileSync(resolve(bundle,manifest.executable));
  assert.equal(elf.subarray(0,6).toString('hex'),'7f454c460201');
  assert.equal(elf.readUInt16LE(18),62,'Linux executable must target x86-64');
  console.log(`Native Linux runtime: ${Object.keys(manifest.files).length} exact files, Qt ${manifest.qt_version}. Runtime verification alone does not establish device execution.`);
}
console.log(`Native Desktop source: 11 pages, ${methods.length} existing guarded RPCs; Decky frontend is independent.`);
