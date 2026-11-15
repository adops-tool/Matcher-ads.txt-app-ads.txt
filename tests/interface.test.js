'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function createApp(options = {}) {
    const dom = new JSDOM(html, { url: 'https://example.test', runScripts: 'dangerously', ...options });
    const { document, Event } = dom.window;
    const get = id => document.getElementById(id);
    const input = (id, value) => {
        get(id).value = value;
        get(id).dispatchEvent(new Event('input', { bubbles: true }));
    };
    return { dom, get, input, Event };
}

test('checks both modes, updates the filter and invalidates stale results', () => {
    const { dom, get, input, Event } = createApp();
    try {
        input('adsInput', 'google.com, pub-123, DIRECT');
        input('partnersInput', '1\nGoogle\noRTB\nApp\nActive\n2\nMissing\noRTB\nWeb\nActive');
        get('checkPlatforms').click();
        assert.equal(get('platformTableBody').children.length, 1);
        get('showMissingToggle').checked = true;
        get('showMissingToggle').dispatchEvent(new Event('change'));
        assert.equal(get('platformTableBody').children.length, 2);
        get('lineTab').click();
        assert.equal(get('lineTab').getAttribute('aria-selected'), 'true');
        input('checkLinesInput', 'GOOGLE.com,pub-123,direct\nmissing.com,1,direct');
        get('checkLines').click();
        assert.equal(get('lineTableBody').children.length, 2);
        assert.match(get('lineStatus').textContent, /1 of 2/);
        input('adsInput', 'changed');
        assert.equal(get('lineTable').style.display, 'none');
        assert.equal(get('platformTable').style.display, 'none');
    } finally { dom.window.close(); }
});

test('renders pasted markup as text rather than HTML', () => {
    const { dom, get, input } = createApp();
    try {
        input('adsInput', '<img src=x onerror=alert(1)>');
        input('checkLinesInput', '<img src=x onerror=alert(1)>');
        get('checkLines').click();
        assert.equal(get('lineTableBody').querySelectorAll('img').length, 0);
        assert.equal(get('lineTableBody').querySelector('td').textContent, '<img src=x onerror=alert(1)>');
    } finally { dom.window.close(); }
});

test('clears saved data without recreating it on pagehide', () => {
    const { dom, get, input } = createApp();
    try {
        input('adsInput', 'sensitive data');
        dom.window.dispatchEvent(new dom.window.Event('pagehide'));
        assert.equal(dom.window.localStorage.getItem('adsInput'), 'sensitive data');
        dom.window.confirm = () => true;
        get('clearWorkspace').click();
        dom.window.dispatchEvent(new dom.window.Event('pagehide'));
        assert.equal(get('adsInput').value, '');
        assert.equal(dom.window.localStorage.length, 0);
    } finally { dom.window.close(); }
});

test('remains usable when browser storage is blocked', () => {
    const { dom, get, input } = createApp({ beforeParse(window) {
        Object.defineProperty(window, 'localStorage', { get() { throw new Error('denied'); } });
    } });
    try {
        assert.match(get('storageStatus').textContent, /unavailable/);
        input('adsInput', 'example.com,1,DIRECT');
        input('checkLinesInput', 'example.com,1,DIRECT');
        get('checkLines').click();
        assert.match(get('lineStatus').textContent, /1 of 1/);
        dom.window.confirm = () => true;
        get('clearWorkspace').click();
        assert.match(get('storageStatus').textContent, /manually/);
    } finally { dom.window.close(); }
});

test('resumes persistence after clearing a full storage quota', () => {
    let full = true;
    const { dom, get, input } = createApp({ beforeParse(window) {
        const storage = window.localStorage;
        Object.defineProperty(window, 'localStorage', { value: {
            getItem: key => storage.getItem(key),
            removeItem: key => { storage.removeItem(key); full = false; },
            setItem: (key, value) => {
                if (full) throw new Error('quota exceeded');
                storage.setItem(key, value);
            },
        } });
    } });
    try {
        input('adsInput', 'first');
        dom.window.dispatchEvent(new dom.window.Event('pagehide'));
        assert.match(get('storageStatus').textContent, /full/);
        dom.window.confirm = () => true;
        get('clearWorkspace').click();
        assert.equal(get('storageStatus').textContent, '');
        input('adsInput', 'new value');
        dom.window.dispatchEvent(new dom.window.Event('pagehide'));
        assert.equal(dom.window.localStorage.getItem('adsInput'), 'new value');
    } finally { dom.window.close(); }
});
