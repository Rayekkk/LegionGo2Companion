// SPDX-License-Identifier: BSD-3-Clause
import assert from 'node:assert/strict';
import {readFileSync,readdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
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
  // The executable is built from desktop/native alone: it must be that tree as it is now.
  const rebuild='Rebuild it: python3 scripts/build-native.py ... --bundle dist/desktop';
  assert.ok(/^[0-9a-f]{40}$/.test(manifest.source_commit ?? '') && manifest.source_dirty===false,
    `dist/desktop was built from an unknown or uncommitted desktop/native. ${rebuild}`);
  const git=(...args)=>spawnSync('git',['-C',root,...args],{encoding:'utf8'});
  const changed=git('diff','--quiet',manifest.source_commit,'--','desktop/native');
  const untracked=git('status','--porcelain','--','desktop/native');
  assert.ok(changed.status===0 && untracked.status===0 && untracked.stdout==='',
    `desktop/native changed since ${manifest.source_commit.slice(0,7)}, the commit dist/desktop was built from. ${rebuild}`);
  console.log('Native Linux executable verified; it links the system Qt 6.9+. This does not establish device execution.');
}
console.log(`Native Desktop contract: ${methods.length} existing guarded RPCs; Decky frontend is independent.`);
