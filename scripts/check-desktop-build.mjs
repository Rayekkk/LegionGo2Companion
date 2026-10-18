// SPDX-License-Identifier: BSD-3-Clause
import assert from 'node:assert/strict';
import {readFileSync,readdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {createHash} from 'node:crypto';
const root=resolve(import.meta.dirname,'..');
const read=name=>readFileSync(resolve(root,name),'utf8');
const methods=JSON.parse(read('desktop/native/rpc-methods.json'));
assert.equal(methods.length,new Set(methods).size);
assert.deepEqual(JSON.parse(read('dist/desktop/rpc-methods.json')),methods,'Run npm run build to refresh dist/desktop');
for(const name of methods) assert.ok(read('main.py').includes(`async def ${name}(`),`Missing shared RPC: ${name}`);
if(process.argv.includes('--runtime')) {
  // The payload is the executable alone; Qt comes from the system (SteamOS).
  const bundle=resolve(root,'dist/desktop');
  const manifest=JSON.parse(read('dist/desktop/native-manifest.json'));
  assert.equal(manifest.platform,'linux-x86_64'); assert.equal(manifest.executable,'legiongo2companion');
  assert.deepEqual(Object.keys(manifest.files),['legiongo2companion']);
  assert.deepEqual(readdirSync(bundle).sort(),['legiongo2companion','native-manifest.json','rpc-methods.json'],
    'Unexpected/stale native bundle file (an old bundled Qt runtime?)');
  const elf=readFileSync(resolve(bundle,manifest.executable));
  assert.equal(elf.length,manifest.files.legiongo2companion.bytes);
  assert.equal(createHash('sha256').update(elf).digest('hex'),manifest.files.legiongo2companion.sha256,'Executable checksum');
  assert.equal(elf.subarray(0,6).toString('hex'),'7f454c460201');
  assert.equal(elf.readUInt16LE(18),62,'Linux executable must target x86-64');
  console.log('Native Linux executable verified; it links the system Qt 6.9+. This does not establish device execution.');
}
console.log(`Native Desktop contract: ${methods.length} existing guarded RPCs; Decky frontend is independent.`);
