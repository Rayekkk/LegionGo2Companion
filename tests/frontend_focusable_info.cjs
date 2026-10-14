const fs = require('node:fs');
const path = require('node:path');
const ts = require(path.resolve(__dirname, '..', 'node_modules/typescript'));
const assert = require('node:assert/strict');

const sourceRoot = path.resolve(__dirname, '..', 'src');
function missingFocusHints(source, file) {
  const missing = [];
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const staticallyFalse = attribute => attribute.initializer && ts.isJsxExpression(attribute.initializer)
    && [ts.SyntaxKind.FalseKeyword, ts.SyntaxKind.NullKeyword].includes(attribute.initializer.expression?.kind);
  const visit = (node) => {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
        && node.tagName.getText(tree) === 'Field') {
      const attributes = node.attributes.properties.filter(ts.isJsxAttribute);
      const hint = attributes.find(attribute => attribute.name.getText(tree) === 'focusable');
      // Decky's Field also admits native focus through an action callback.
      // Dynamic props remain a runtime concern; this is only a source lint.
      const action = attributes.some(attribute => ['onActivate', 'onClick'].includes(attribute.name.getText(tree))
        && !staticallyFalse(attribute));
      const focusable = hint ? !staticallyFalse(hint) : action;
      if (!focusable) {
        const line = tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1;
        missing.push(`${file}:${line}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  return missing;
}

assert.deepEqual(missingFocusHints('<Field focusable />', 'fixture.tsx'), []);
assert.deepEqual(missingFocusHints('<Field focusable={true} />', 'fixture.tsx'), []);
assert.deepEqual(missingFocusHints('<Field onActivate={() => {}} />', 'fixture.tsx'), []);
assert.equal(missingFocusHints('<Field focusable={false} />', 'fixture.tsx').length, 1);
assert.equal(missingFocusHints('<Field />', 'fixture.tsx').length, 1);
const missing = fs.readdirSync(sourceRoot).filter(name => name.endsWith('.tsx')).flatMap(file =>
  missingFocusHints(fs.readFileSync(path.join(sourceRoot, file), 'utf8'), file));
assert.deepEqual(missing, [], `Information fields without controller focus: ${missing.join(', ')}`);
console.log('Field source lint: explicit focus or action hints are present; native focus behaviour has separate tests.');
