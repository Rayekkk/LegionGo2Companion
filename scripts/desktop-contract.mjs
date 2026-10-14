// SPDX-License-Identifier: BSD-3-Clause
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {resolve} from 'node:path';
const root=resolve(import.meta.dirname,'..');
const methods=new Set();
for(const file of ['index','tdp','fan','vibration','rgb','remap','controller','battery','display','wifi','updates','firmware']) {
  const code=readFileSync(resolve(root,`src/${file}.tsx`),'utf8');
  for(const match of code.matchAll(/callable\s*<[\s\S]*?>\s*\(\s*"([a-z_]+)"/g))methods.add(match[1]);
}
methods.delete('set_active_app');methods.delete('vibe_set_active_app');
const data=JSON.stringify([...methods].sort(),null,2)+'\n';
writeFileSync(resolve(root,'desktop/native/rpc-methods.json'),data);
mkdirSync(resolve(root,'dist/desktop'),{recursive:true});
writeFileSync(resolve(root,'dist/desktop/rpc-methods.json'),data);
console.log(`Native Desktop contract: ${methods.size} existing public panel RPCs.`);
