const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(`${__dirname}/app.js`, 'utf8');
const start = source.indexOf('// --- room background');
const end = source.indexOf('// True while the box holds', start);
assert.ok(start >= 0 && end > start, 'wallpaper Settings helpers are present');

const css = [];
const context = vm.createContext({
  state: {wallpaper: {
    selected: 'evening.png', max_bytes: 10 * 1024 * 1024,
    current: {id: 'evening.png', name: 'Evening garden', url: '/artwork/garden/evening.png', width: 1600, height: 900},
    items: [
      {id: 'default', name: 'The garden of Ada', url: '/artwork/garden.png', width: 1400, height: 900, built_in: true},
      {id: 'evening.png', name: 'Evening garden', url: '/artwork/garden/evening.png', width: 1600, height: 900},
    ],
  }},
  esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  document: {body: {style: {setProperty: (...args) => css.push(args)}}},
  $: () => null, MY_NAME: 'Ada',
  MAIN: {querySelectorAll: () => []},
});
vm.runInContext(source.slice(start, end), context);

const html = context.devWallpaper();
assert.match(html, /room background/i, 'Settings describes the room choice');
assert.match(html, /evening\.png/, 'catalogue options are rendered');
assert.match(html, /aria-checked="true"/, 'the persisted selection is marked current');
assert.match(html, /upload and use it/, 'the upload action is visible');
assert.match(html, /image\/png,image\/jpeg,image\/gif,image\/webp/, 'the picker limits browser choices to supported images');

context.applyWallpaper(context.state.wallpaper);
assert.deepEqual(css, [["--room-wallpaper", 'url("/artwork/garden/evening.png")']], 'the chosen server URL becomes the room background');
assert.equal(context.wallpaperFileProblem({name: 'room.png', type: 'image/png', size: 1024}, 2048), '', 'a supported image in range can upload');
assert.match(context.wallpaperFileProblem({name: 'room.svg', type: 'image/svg+xml', size: 1}, 2048), /supported/, 'unsupported browser types explain why they cannot upload');
assert.match(context.wallpaperFileProblem({name: 'large.jpg', type: 'image/jpeg', size: 2049}, 2048), /larger/, 'browser-side size feedback happens before reading the file');

console.log('Wallpaper Settings renders previews/current state, applies the chosen safe URL, and gives upload validation feedback.');
