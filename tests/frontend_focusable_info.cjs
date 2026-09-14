const fs = require('node:fs');
const path = require('node:path');
const ts = require(path.resolve(__dirname, '..', 'node_modules/typescript'));
const assert = require('node:assert/strict');

const sourceRoot = path.resolve(__dirname, '..', 'src');
const missing = [];

for (const file of fs.readdirSync(sourceRoot).filter((name) => name.endsWith('.tsx'))) {
  const source = fs.readFileSync(path.join(sourceRoot, file), 'utf8');
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const visit = (node) => {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
        && node.tagName.getText(tree) === 'Field') {
      const focusable = node.attributes.properties.some((attribute) =>
        ts.isJsxAttribute(attribute) && attribute.name.getText(tree) === 'focusable');
      if (!focusable) {
        const line = tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1;
        missing.push(`${file}:${line}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
}

assert.deepEqual(missing, [], `Information fields without controller focus: ${missing.join(', ')}`);
console.log('Every information Field participates in Decky controller focus and panel scrolling.');
