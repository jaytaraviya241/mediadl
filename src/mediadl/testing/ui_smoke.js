/* Render the downloader dashboard in a real DOM and report its visible product
 * states.  The normal server supplies the auth-required case; one focused
 * inspect response is stubbed so the media-review state is covered without a
 * live Meta account or public network call.
 */
'use strict';

const { JSDOM, VirtualConsole } = require('jsdom');
const BASE = process.argv[2] || 'http://127.0.0.1:8765';
const errors = [];
const connectionStartRequests = [];

const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', (error) =>
  errors.push('jsdomError: ' + (error.stack || error.message).split('\n').slice(0, 2).join(' | ')));
virtualConsole.on('error', (...args) => errors.push('console.error: ' + args.map(String).join(' ')));

function eventSourceClass(base) {
  return class {
    constructor(url) {
      this.url = new URL(url, base).toString();
      this.onmessage = null; this.onopen = null; this.onerror = null;
      this._run();
    }
    async _run() {
      try {
        const response = await fetch(this.url);
        if (this.onopen) this.onopen({});
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let index;
          while ((index = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, index);
            buffer = buffer.slice(index + 2);
            const line = frame.split('\n').find((part) => part.startsWith('data: '));
            if (line && this.onmessage) this.onmessage({ data: line.slice(6) });
          }
        }
      } catch (error) { if (this.onerror) this.onerror(error); }
    }
    close() {}
  };
}

const READY_INSPECTION = {
  supported: true,
  source: 'https://www.instagram.com/reel/ReadyPreview123/',
  connector_id: 'instagram_graph',
  requires_auth: true,
  connection_id: 'safe-test-connection',
  collection: { id: 'instagram_graph:preview', kind: 'carousel', title: 'A safe media preview', asset_count: 1, metadata: { author: 'tester', timestamp: '2026-09-21T10:00:00Z' } },
  assets: [{
    id: 'instagram_graph:preview', position: 1, kind: 'video', media_type: 'video',
    title: 'A safe media preview', availability: 'available', available: true, downloadable: true,
    preview_url: null, metadata: { author: 'tester', timestamp: '2026-09-21T10:00:00Z' },
    selected_format_id: 'primary', selection: { available: true, selected_format_id: 'primary', container: 'mp4' },
    formats: [{ id: 'primary', label: '1080×1920 MP4', downloadable: true, selectable: true, width: 1080, height: 1920, container: 'mp4', duration_s: 18 }],
  }],
  summary: { asset_count: 1, available_count: 1, downloadable_count: 1 },
};

(async () => {
  const html = await (await fetch(BASE + '/')).text();
  const dom = new JSDOM(html, {
    url: BASE + '/', runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true,
    virtualConsole,
    beforeParse(window) {
      window.fetch = (input, init) => {
        const url = new URL(input, BASE);
        const body = typeof init?.body === 'string' ? init.body : '';
        if (url.pathname === '/api/inspect' && body.includes('ReadyPreview123')) {
          return Promise.resolve(new Response(JSON.stringify(READY_INSPECTION), {
            status: 200, headers: { 'Content-Type': 'application/json' },
          }));
        }
        if (url.pathname === '/api/connections/instagram/start') {
          const request = body ? JSON.parse(body) : {};
          connectionStartRequests.push(request);
          if (request.client_id === 'fastapi-422') {
            return Promise.resolve(new Response(JSON.stringify({ detail: [{
              type: 'missing', loc: ['body', 'redirect_uri'], msg: 'Field required', input: request,
            }] }), { status: 422, headers: { 'Content-Type': 'application/json' } }));
          }
          return Promise.resolve(new Response(JSON.stringify({
            authorize_url: 'https://www.instagram.com/oauth/authorize?state=safe-test-state',
            scopes: ['instagram_business_basic'], expires_in_s: 600,
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        }
        return fetch(url, init);
      };
      window.alert = (message) => errors.push('alert: ' + message);
      window.confirm = () => true;
      window.open = () => null;
      window.scrollTo = () => {};
      window.EventSource = eventSourceClass(BASE);
    },
  });
  const { window } = dom;
  const document = window.document;
  const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  const text = (selector) => (document.querySelector(selector)?.textContent || '').trim().replace(/\s+/g, ' ');
  const count = (selector) => document.querySelectorAll(selector).length;
  const input = document.querySelector('#source-input');
  const submit = document.querySelector('#submit-form');

  await wait(1200);
  const initial = {
    downloader_visible: document.querySelector('#view-downloader').hidden === false,
    drawer_hidden: document.querySelector('#drawer').hidden === true,
    title: text('#download-title'),
    empty_review: text('#inspection-empty'),
  };

  input.value = 'not a url';
  input.dispatchEvent(new window.Event('input'));
  const invalid_hint = text('#route-hint');

  input.value = 'https://www.instagram.com/reel/ExampleClip123/';
  input.dispatchEvent(new window.Event('input'));
  const instagram_hint = text('#route-hint');
  submit.dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }));
  for (let index = 0; index < 20 && !text('#inspection-content').includes('Connect your Instagram'); index += 1) await wait(100);
  const auth_required = text('#inspection-content');

  input.value = 'https://www.instagram.com/reel/ReadyPreview123/';
  input.dispatchEvent(new window.Event('input'));
  submit.dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }));
  for (let index = 0; index < 20 && !text('.media-title').includes('A safe media preview'); index += 1) await wait(100);
  const review = {
    title: text('.media-title'),
    option: text('#quality-select'),
    download_enabled: document.querySelector('#download-button')?.disabled === false,
    facts: text('.media-facts'),
  };

  document.querySelector('[data-view="setup"]').dispatchEvent(new window.Event('click', { bubbles: true }));
  await wait(50);
  const setup = { visible: document.querySelector('#view-setup').hidden === false, sources: count('#support-list .support-row') };

  const wizard = {
    step_two_initially_locked: document.querySelector('#ig-step-two').disabled === true,
    complete_initially_disabled: document.querySelector('#ig-complete').disabled === true,
  };
  const clientId = document.querySelector('#ig-client-id');
  const redirect = document.querySelector('#ig-redirect');
  const start = document.querySelector('#ig-start');
  start.click();
  await wait(20);
  wizard.required_error = text('#ig-error');
  wizard.requests_after_required_error = connectionStartRequests.length;

  clientId.value = '1234567890123456';
  clientId.dispatchEvent(new window.Event('input', { bubbles: true }));
  redirect.value = 'http://localhost/callback';
  redirect.dispatchEvent(new window.Event('input', { bubbles: true }));
  start.click();
  await wait(20);
  wizard.non_https_error = text('#ig-error');
  wizard.requests_after_non_https_error = connectionStartRequests.length;

  redirect.value = 'https://localhost/callback';
  redirect.dispatchEvent(new window.Event('input', { bubbles: true }));
  start.click();
  await wait(20);
  wizard.https_error = text('#ig-error');
  wizard.requests_after_https_error = connectionStartRequests.length;

  redirect.value = 'https://app.example/auth/instagram/callback';
  redirect.dispatchEvent(new window.Event('input', { bubbles: true }));
  start.click();
  for (let index = 0; index < 10 && document.querySelector('#ig-step-two').disabled; index += 1) await wait(20);
  wizard.unlocked_after_start = document.querySelector('#ig-step-two').disabled === false;
  wizard.registered_redirect = text('#ig-authorized-redirect');
  wizard.requests_after_start = connectionStartRequests.length;
  wizard.step_one_locked_after_start = clientId.readOnly === true && redirect.readOnly === true;

  document.querySelector('#ig-restart').click();
  redirect.value = 'https://changed.example/auth/instagram/callback';
  redirect.dispatchEvent(new window.Event('input', { bubbles: true }));
  await wait(10);
  wizard.relocked_after_step_one_change = document.querySelector('#ig-step-two').disabled === true;
  wizard.stale_status = text('#ig-start-status');

  clientId.value = 'fastapi-422';
  clientId.dispatchEvent(new window.Event('input', { bubbles: true }));
  redirect.value = 'https://validation.example/auth/instagram/callback';
  redirect.dispatchEvent(new window.Event('input', { bubbles: true }));
  start.click();
  await wait(20);
  wizard.fastapi_422 = text('#ig-error');
  wizard.fastapi_422_marks_redirect = redirect.getAttribute('aria-invalid') === 'true';

  document.querySelector('#selftest-button').dispatchEvent(new window.Event('click'));
  for (let index = 0; index < 70 && !text('#selftest-result').match(/passed|ended|error/i); index += 1) await wait(250);
  const selftest = text('#selftest-result');

  process.stdout.write(JSON.stringify({
    errors, live: text('#live-indicator'), initial, invalid_hint, instagram_hint, auth_required, review, setup, wizard, selftest,
  }, null, 2));
  window.close();
  process.exit(0);
})().catch((error) => {
  process.stderr.write('harness failure: ' + (error.stack || error.message));
  process.exit(1);
});
