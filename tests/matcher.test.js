'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('index.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const context = vm.createContext({});
vm.runInContext(script.replace(/\n\s*initializeApp\(\);\s*$/, ''), context);
const plain = value => JSON.parse(JSON.stringify(value));

test('parses block, tab-separated, and multi-space partner records', () => {
    const input = '1\nGoogle\noRTB\nIn-app\nActive\n2\nMissing\n3\tExample\tType\tWeb\tActive\n4  Another  Type  App  Inactive';
    assert.deepEqual(plain(context.parsePartnersData(input)), [
        { name: 'Google', type: 'oRTB', env: 'In-app', statusSource: 'Active' },
        { name: 'Missing', type: '', env: '', statusSource: '' },
        { name: 'Example', type: 'Type', env: 'Web', statusSource: 'Active' },
        { name: 'Another', type: 'Type', env: 'App', statusSource: 'Inactive' },
    ]);
});

test('matches literal partner names, ignoring empty names and case', () => {
    const results = plain(context.matchPlatforms('A+B a+b other', '1\nA+B (note)\ntype\nweb\nActive\n2\n(unknown)'));
    assert.equal(results.length, 1);
    assert.equal(results[0].name, 'A+B');
    assert.equal(results[0].count, 2);
});

test('normalizes comments and whitespace but preserves empty fields', () => {
    assert.equal(context.normalizeLine(' Example.COM, ID, DIRECT # comment'), 'example.com,id,direct');
    assert.notEqual(context.normalizeLine('example.com,,DIRECT'), context.normalizeLine('example.com,DIRECT'));
    assert.deepEqual(plain(context.matchLines('example.com, id, DIRECT # note\n# comment',
        'EXAMPLE.com,id,direct\n# comment\nexample.com,,direct')), [
        { line: 'EXAMPLE.com,id,direct', found: true },
        { line: 'example.com,,direct', found: false },
    ]);
});

test('preserves whitespace inside block names and empty tabular columns', () => {
    const data = '1\nSome  Exchange\noRTB\nWeb\nActive\n' +
        'external-id\tOther Exchange\t\tWeb\tActive\n\tThird\tType\tApp\tInactive';
    assert.deepEqual(plain(context.parsePartnersData(data)), [
        { name: 'Some  Exchange', type: 'oRTB', env: 'Web', statusSource: 'Active' },
        { name: 'Other Exchange', type: '', env: 'Web', statusSource: 'Active' },
        { name: 'Third', type: 'Type', env: 'App', statusSource: 'Inactive' },
    ]);
});
