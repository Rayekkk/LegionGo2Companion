// SPDX-License-Identifier: BSD-3-Clause
// Desktop RPC allowlist = the Decky panels' public RPCs that the native app uses.
// Without --check it regenerates both copies; --check fails on a stale committed copy.
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
const root=resolve(import.meta.dirname,'..');
const methods=new Set();
for(const file of ['index','tdp','fan','vibration','rgb','remap','controller','battery','display','wifi','updates','firmware']) {
  const code=readFileSync(resolve(root,`src/${file}.tsx`),'utf8');
  for(const match of code.matchAll(/callable\s*<[\s\S]*?>\s*\(\s*"([a-z_]+)"/g))methods.add(match[1]);
}
// Steam foreground-app hints need Steam's view; the rest is unused by the native app.
for(const name of ['set_active_app','vibe_set_active_app','is_ready','modules_restart_session'])methods.delete(name);
// Read-only public RPCs that only the native app shows.
for(const name of ['list_game_profiles'])methods.add(name);
const data=JSON.stringify([...methods].sort(),null,2)+'\n';
const committed=resolve(root,'desktop/native/rpc-methods.json');
if(process.argv.includes('--check')) {
  if(readFileSync(committed,'utf8')!==data) {
    console.error('desktop/native/rpc-methods.json is stale: run `node scripts/desktop-contract.mjs` and commit it.');
    process.exit(1);
  }
} else {
  writeFileSync(committed,data);
  mkdirSync(resolve(root,'dist/desktop'),{recursive:true});
  writeFileSync(resolve(root,'dist/desktop/rpc-methods.json'),data);
}
console.log(`Native Desktop contract: ${methods.size} existing public panel RPCs.`);
