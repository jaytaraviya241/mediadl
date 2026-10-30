/* mediadl downloader dashboard.
 *
 * The page deliberately models the user-facing lifecycle rather than exposing
 * pipeline internals: paste → validate → inspect → download → finish. Jobs
 * still use the same server-side state machine as the CLI and are reconciled
 * through SSE snapshots.
 */
'use strict';

const state = {
  env: null,
  jobs: [],
  connections: [],
  progress: new Map(),
  view: 'downloader',
  inspection: null,
  inspectionSource: '',
  inspectionState: 'idle',
  openJobId: null,
  selftestJobId: null,
  instagramAuthorization: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child && child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function icon(name, cls = 'icon') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', cls);
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

function bytes(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let number = Number(value);
  let index = 0;
  while (number >= 1024 && index < units.length - 1) { number /= 1024; index += 1; }
  const precision = number < 10 && index ? 1 : 0;
  return `${number.toFixed(precision)} ${units[index]}`;
}

function ago(iso) {
  if (!iso) return '';
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return '';
  const total = Math.max(0, Math.round(Number(seconds)));
  const minutes = Math.floor(total / 60);
  const remaining = total % 60;
  return minutes ? `${minutes}:${String(remaining).padStart(2, '0')}` : `${remaining}s`;
}

function titleFromUrl(value) {
  try {
    const parsed = new URL(value);
    const last = parsed.pathname.split('/').filter(Boolean).pop();
    return last || parsed.hostname;
  } catch {
    return value || 'Media link';
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = null; }
  if (!response.ok) {
    const detail = (payload && payload.detail) || {};
    const validation = fastApiValidation(detail);
    const error = new Error(
      (detail && !Array.isArray(detail) && detail.message)
        || (validation && validation.message)
        || `Request failed (${response.status})`,
    );
    error.code = (detail && !Array.isArray(detail) && detail.code)
      || (validation ? 'INVALID_INPUT' : String(response.status));
    error.retryable = Boolean(detail && !Array.isArray(detail) && detail.retryable);
    error.extra = (detail && !Array.isArray(detail) && detail.detail) || {};
    error.fieldErrors = validation ? validation.fieldErrors : {};
    throw error;
  }
  return payload;
}

const API_FIELD_LABELS = {
  client_id: 'Instagram App ID',
  redirect_uri: 'Redirect URI',
  redirect_url: 'Redirect URL',
  client_secret: 'Instagram App Secret',
};

function fastApiValidation(detail) {
  if (!Array.isArray(detail)) return null;
  const fieldErrors = {};
  const messages = [];
  for (const issue of detail) {
    if (!issue || typeof issue !== 'object') continue;
    const location = Array.isArray(issue.loc) ? issue.loc : [];
    const field = [...location].reverse().find(
      (part) => typeof part === 'string' && part !== 'body' && part !== 'query' && part !== 'path',
    );
    const label = API_FIELD_LABELS[field] || (field
      ? String(field).replace(/_/g, ' ')
      : 'Request');
    const message = String(issue.msg || 'is not valid').replace(/\.$/, '');
    const readable = message === 'Field required' ? 'is required' : message;
    if (field && !fieldErrors[field]) fieldErrors[field] = readable;
    messages.push(`${label}: ${readable}.`);
  }
  return messages.length ? { message: messages.join(' '), fieldErrors } : null;
}

const NEXT_STEP = {
  INVALID_INPUT: 'Use a full Instagram post, reel, or video URL, or a Facebook reel, watch, post, or video URL.',
  UNSUPPORTED_SOURCE: 'This source is not available in this installation. Check the supported-source status in Setup.',
  AMBIGUOUS_SOURCE: 'Try the canonical post or reel link copied from the platform.',
  AUTH_REQUIRED: 'Connect the Instagram professional account that owns this media, then check the link again.',
  AUTH_EXPIRED: 'Reconnect the account in Setup, then check the link again.',
  PERMISSION_MISSING: 'Reconnect the account and approve the required media permission.',
  NOT_ACCESSIBLE_WITH_AUTHORIZED_ACCOUNT: 'The connected account cannot access this media. Only authorized account media can be downloaded.',
  NOT_IN_INDEX_WINDOW: 'This post is outside the media window the platform makes available through its official API.',
  UNAVAILABLE_COPYRIGHT: 'The provider does not make this media file available for download.',
  UNAVAILABLE_DRM: 'This media is protected and cannot be downloaded.',
  NOT_FOUND: 'The media may have been deleted, moved, or made unavailable.',
  NETWORK_ERROR: 'The source could not be reached. Check your connection and try again.',
  UPSTREAM_ERROR: 'The provider had a temporary problem. Try again in a moment.',
  CONTENT_TYPE_MISMATCH: 'That link did not return media. Use a direct post, reel, watch, or video link.',
  SIZE_LIMIT_EXCEEDED: 'This file is larger than the current download limit.',
  TOOL_MISSING: 'FFmpeg is not available for this media type. Install it, then try again.',
  NO_ACCEPTABLE_REPRESENTATION: 'The selected source option is no longer available. Check the link again to refresh the choices.',
  LOCATOR_EXPIRED: 'The provider’s temporary media link expired. Check the link again to get a fresh option.',
  INTEGRITY_ERROR: 'The downloaded data could not be verified. Try the download again.',
  PROCESSING_FAILED: 'The media was downloaded but could not be prepared for saving. Try a different source option.',
  PROCESSING_TIMEOUT: 'Preparing this media took too long. Try again or choose a simpler option.',
  CANCELLED: 'This download was cancelled. Check the link again when you are ready to restart.',
  RATE_LIMITED: 'The provider asked us to slow down. Use Retry when you are ready to try again.',
  POLICY_DENIED: 'The current download settings do not allow this request.',
};

function errorCopy(error) {
  return NEXT_STEP[error && error.code] || 'Check the link and try again.';
}

function setHint(tone, message, iconName = 'link') {
  const hint = $('#route-hint');
  hint.dataset.tone = tone;
  hint.replaceChildren(icon(iconName), el('span', { text: message }));
}

function showAlert(selector, error) {
  const host = $(selector);
  host.replaceChildren(
    el('strong', { text: error.message || 'Something went wrong.' }),
    el('span', { class: 'next', text: errorCopy(error) }),
  );
  host.hidden = false;
}

function clearAlert(selector) {
  const node = $(selector);
  if (node) { node.hidden = true; node.replaceChildren(); }
}

/* ---------- views ---------- */
function goto(view) {
  state.view = view;
  $$('.view').forEach((panel) => {
    const active = panel.id === `view-${view}`;
    panel.hidden = !active;
    panel.classList.toggle('active', active);
  });
  $$('.nav-link').forEach((button) => {
    const active = button.dataset.view === view;
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function initNavigation() {
  document.addEventListener('click', (event) => {
    const target = event.target.closest('[data-view]');
    if (!target) return;
    event.preventDefault();
    goto(target.dataset.view);
  });
}

/* ---------- client-side URL validation ---------- */
function classifySource(raw) {
  const value = (raw || '').trim();
  if (!value) return { state: 'empty', platform: null, valid: false };
  if (/^ig-media:\d+$/i.test(value)) return {
    state: 'valid', platform: 'instagram', valid: true,
    message: 'Instagram media reference detected. Check the link to retrieve its details.',
  };
  let parsed;
  try { parsed = new URL(value); } catch {
    return { state: 'invalid', platform: null, valid: false, message: 'Enter a complete URL beginning with https://.' };
  }
  if (parsed.protocol !== 'https:') {
    return { state: 'invalid', platform: null, valid: false, message: 'Use an https:// Instagram or Facebook URL.' };
  }
  const host = parsed.hostname.toLowerCase().replace(/^(www|m|web)\./, '');
  const path = parsed.pathname.toLowerCase();
  const parts = path.split('/').filter(Boolean);
  if (host === 'instagram.com' || host === 'instagr.am') {
    const surface = parts[0];
    if (['p', 'reel', 'reels', 'tv'].includes(surface) && parts[1]) return {
      state: 'valid', platform: 'instagram', valid: true,
      message: 'Instagram link detected. We’ll check the media before downloading.',
    };
    return {
      state: 'invalid', platform: 'instagram', valid: false,
      message: 'Use a direct Instagram post, reel, reels, or video URL.',
    };
  }
  const facebookHost = host === 'facebook.com' || host === 'fb.com' || host === 'fb.watch'
    || host.endsWith('.facebook.com') || host.endsWith('.fb.com');
  if (facebookHost) {
    const likelyMedia = host === 'fb.watch' || parts.includes('reel') || parts.includes('watch')
      || parts.includes('videos') || parts.includes('posts') || parsed.searchParams.has('v');
    if (likelyMedia) return {
      state: 'valid', platform: 'facebook', valid: true,
      message: 'Facebook media link detected. We’ll check whether Facebook support is enabled before downloading.',
    };
    return {
      state: 'invalid', platform: 'facebook', valid: false,
      message: 'Use a direct Facebook reel, watch, post, or video link.',
    };
  }
  return {
    state: 'invalid', platform: null, valid: false,
    message: 'This downloader checks Instagram and Facebook media links. Paste a supported link to continue.',
  };
}

function updateComposerValidation() {
  const input = $('#source-input');
  const value = input.value.trim();
  const shape = classifySource(value);
  $('#clear-input').hidden = !value;
  clearAlert('#submit-error');
  if (state.inspectionSource && state.inspectionSource !== value) resetInspection();
  if (shape.state === 'empty') {
    $('#composer-step').textContent = 'Ready';
    $('#composer-step').removeAttribute('data-state');
    setHint('neutral', 'Paste a link and we’ll check whether it can be downloaded.');
  } else if (shape.valid) {
    $('#composer-step').textContent = 'Link detected';
    $('#composer-step').dataset.state = 'ready';
    setHint('success', shape.message, 'check');
  } else {
    $('#composer-step').textContent = 'Check link';
    $('#composer-step').dataset.state = 'error';
    setHint('error', shape.message, 'alert');
  }
  return shape;
}

async function pasteFromClipboard() {
  const input = $('#source-input');
  try {
    const value = await navigator.clipboard.readText();
    if (value) {
      input.value = value.trim();
      updateComposerValidation();
      input.focus();
    }
  } catch {
    setHint('warning', 'Clipboard access is unavailable. Paste the link into the field instead.', 'alert');
    input.focus();
  }
}

/* ---------- inspect / preflight ---------- */
function resetInspection() {
  state.inspection = null;
  state.inspectionSource = '';
  state.inspectionState = 'idle';
  $('#inspection-empty').hidden = false;
  $('#inspection-content').hidden = true;
  $('#inspection-content').replaceChildren();
}

function setInspectionLoading() {
  state.inspectionState = 'loading';
  $('#inspection-empty').hidden = true;
  const host = $('#inspection-content');
  host.hidden = false;
  host.replaceChildren(el('div', { class: 'inspection-loading' },
    el('div', { class: 'skeleton media' }),
    el('div', {}, el('div', { class: 'skeleton pill' }), el('div', { class: 'skeleton line' }),
      el('div', { class: 'skeleton line short' }), el('div', { class: 'skeleton line' }),
      el('div', { class: 'skeleton line short' })),
  ));
  $('#composer-step').textContent = 'Checking…';
  $('#composer-step').dataset.state = 'loading';
  setHint('neutral', 'Checking the link and retrieving available media details…', 'clock');
}

function selectedConnectionId() {
  const active = state.connections.filter((connection) => connection.status === 'active');
  return active[0] ? active[0].id : null;
}

async function inspectSource() {
  const raw = $('#source-input').value.trim();
  const shape = updateComposerValidation();
  if (!shape.valid) {
    const error = new Error(shape.message || 'This link is not valid.');
    error.code = 'INVALID_INPUT';
    showAlert('#submit-error', error);
    renderInspectionError(error);
    return;
  }
  clearAlert('#submit-error');
  setInspectionLoading();
  $('#submit-button').disabled = true;
  try {
    const inspection = await api('/api/inspect', {
      method: 'POST',
      body: { source: raw, connection_id: selectedConnectionId() },
    });
    if ($('#source-input').value.trim() !== raw) return;
    state.inspection = inspection;
    state.inspectionSource = raw;
    state.inspectionState = 'ready';
    renderInspection(inspection);
    $('#composer-step').textContent = 'Ready to download';
    $('#composer-step').dataset.state = 'ready';
    setHint('success', 'Media is ready to review. Choose an available option, then download.', 'check');
  } catch (error) {
    if ($('#source-input').value.trim() !== raw) return;
    state.inspection = null;
    state.inspectionSource = raw;
    state.inspectionState = 'error';
    $('#composer-step').textContent = 'Needs attention';
    $('#composer-step').dataset.state = 'error';
    setHint('error', error.message || 'We could not check this link.', 'alert');
    showAlert('#submit-error', error);
    renderInspectionError(error);
  } finally {
    $('#submit-button').disabled = false;
  }
}

function firstAsset(inspection) {
  const assets = (inspection && inspection.assets) || [];
  return assets.find(
    (asset) => asset.available !== false
      && (!asset.availability || asset.availability === 'available')
      && asset.downloadable !== false
      && (!asset.selection || asset.selection.available !== false),
  ) || assets[0] || null;
}

function metadataOf(asset, inspection) {
  return (asset && asset.metadata) || (inspection && inspection.collection && inspection.collection.metadata) || {};
}

function mediaTitle(inspection, asset) {
  const metadata = metadataOf(asset, inspection);
  return (asset && asset.title) || (inspection.collection && inspection.collection.title)
    || metadata.caption || metadata.title || metadata.filename || titleFromUrl(state.inspectionSource);
}

function mediaCreator(inspection, asset) {
  const metadata = metadataOf(asset, inspection);
  return (asset && asset.creator) || (inspection.collection && inspection.collection.creator)
    || metadata.author || metadata.username || metadata.owner || '';
}

function formatDescription(format) {
  if (!format) return 'Best available';
  if (format.label) return format.label;
  const parts = [];
  if (format.width && format.height) parts.push(`${format.width}×${format.height}`);
  if (format.container) parts.push(String(format.container).toUpperCase());
  if (format.mime_type) parts.push(format.mime_type.split('/').pop().toUpperCase());
  if (format.filesize_bytes) parts.push(bytes(format.filesize_bytes));
  return parts.join(' · ') || 'Best available';
}

function typeIcon(kind) {
  if (kind === 'video') return 'video';
  if (kind === 'image') return 'image';
  return 'layers';
}

function renderInspection(inspection) {
  const asset = firstAsset(inspection);
  if (!asset) {
    const error = new Error('No downloadable media was returned for this link.');
    error.code = 'UNAVAILABLE_COPYRIGHT';
    renderInspectionError(error);
    return;
  }
  if (asset.available === false || (asset.availability && asset.availability !== 'available')) {
    const error = new Error(asset.unavailable_reason || 'The provider does not make this media available for download.');
    error.code = asset.error_code || (asset.availability === 'unavailable_drm' ? 'UNAVAILABLE_DRM' : 'UNAVAILABLE_COPYRIGHT');
    renderInspectionError(error);
    return;
  }
  if (asset.downloadable === false || (asset.selection && asset.selection.available === false)) {
    const selectionError = asset.selection && asset.selection.error;
    const error = new Error(
      (selectionError && selectionError.message)
        || 'No downloadable version of this media is available.',
    );
    error.code = (selectionError && selectionError.code) || 'NO_ACCEPTABLE_REPRESENTATION';
    renderInspectionError(error);
    return;
  }

  const assets = inspection.assets || [];
  const formats = (asset.formats || []).filter(
    (format) => format.downloadable && format.selectable !== false,
  );
  const title = mediaTitle(inspection, asset);
  const creator = mediaCreator(inspection, asset);
  const kind = asset.kind || (inspection.collection && inspection.collection.kind) || 'media';
  const metadata = metadataOf(asset, inspection);
  const date = asset.timestamp || metadata.timestamp || (inspection.collection && inspection.collection.timestamp);
  const selectedFormat = formats.find((format) => format.id === asset.selected_format_id) || formats[0];
  const assetDuration = asset.duration_s || metadata.duration_s || (selectedFormat && selectedFormat.duration_s);
  const preview = asset.preview_url;
  const host = $('#inspection-content');
  $('#inspection-empty').hidden = true;
  host.hidden = false;

  const previewBox = el('div', { class: 'media-preview' },
    preview
      ? el('img', { src: preview, alt: '', loading: 'eager', onerror: (event) => event.currentTarget.remove() })
      : icon(typeIcon(kind)),
    el('span', { class: 'media-preview-type' }, icon(typeIcon(kind)), kind === 'media' ? 'Media' : kind),
  );
  const facts = [];
  if (assets.length > 1) facts.push(el('span', { class: 'fact' }, icon('layers'), `${assets.length} items`));
  else facts.push(el('span', { class: 'fact' }, icon(typeIcon(kind)), kind));
  if (assetDuration) facts.push(el('span', { class: 'fact' }, icon('clock'), duration(assetDuration)));
  if (date) facts.push(el('span', { class: 'fact' }, icon('clock'), String(date).slice(0, 10)));
  if (asset.selection && asset.selection.container) facts.push(el('span', { class: 'fact' }, asset.selection.container));

  const select = el('select', { class: 'format-select', id: 'quality-select', 'aria-label': 'Download option' });
  if (assets.length > 1) {
    select.append(el('option', { value: '', text: 'Best available for each item' }));
  } else if (formats.length) {
    formats.forEach((format) => select.append(el('option', {
      value: format.id || format.rep_id || '',
      selected: (format.id || format.rep_id) === asset.selected_format_id,
      text: formatDescription(format),
    })));
  } else {
    select.append(el('option', { value: '', text: 'Best available' }));
  }
  select.disabled = assets.length > 1 || formats.length < 2;
  const formatNote = assets.length > 1
    ? 'Best available will be selected for each item'
    : formats.length > 1
    ? `${formats.length} source options available`
    : 'Best available option selected';
  const availabilityNote = assets.some((item) => item.available === false || item.availability !== 'available')
    ? `${assets.filter((item) => item.available !== false && item.availability === 'available').length} of ${assets.length} items are available`
    : null;

  host.replaceChildren(el('div', { class: 'inspection-card' },
    previewBox,
    el('div', { class: 'media-content' },
      el('p', { class: 'media-overline', text: inspection.connector_id === 'instagram_graph' ? 'Instagram media found' : 'Media found' }),
      el('h2', { class: 'media-title', text: title }),
      creator ? el('p', { class: 'media-subtitle', text: `Posted by ${creator}` }) : null,
      el('div', { class: 'media-facts' }, facts),
      availabilityNote ? el('div', { class: 'carousel-summary' }, icon('alert'), availabilityNote) : null,
      el('div', { class: 'format-box' },
        el('div', { class: 'format-box-head' }, el('label', { for: 'quality-select', text: 'Download option' }), el('small', { text: formatNote })),
        select,
      ),
      el('div', { class: 'review-actions' },
        el('p', { text: inspection.requires_auth ? 'Using your connected account for this media.' : 'Your choice will be verified again when the download starts.' }),
        el('button', { class: 'button primary', type: 'button', id: 'download-button', onclick: startDownload },
          icon('download'), ' Download'),
      ),
    ),
  ));
}

function renderInspectionError(error) {
  $('#inspection-empty').hidden = true;
  const host = $('#inspection-content');
  host.hidden = false;
  const action = ['AUTH_REQUIRED', 'AUTH_EXPIRED', 'PERMISSION_MISSING'].includes(error.code)
    ? el('button', { class: 'button secondary', type: 'button', onclick: () => goto('setup') }, icon('lock'), ' Connect account')
    : el('button', { class: 'button secondary', type: 'button', onclick: () => $('#source-input').focus() }, ' Edit link');
  host.replaceChildren(el('div', { class: 'inspection-error' },
    el('span', { class: 'error-mark' }, icon(error.code === 'AUTH_REQUIRED' ? 'lock' : 'alert')),
    el('div', {}, el('h2', { text: error.message || 'We could not check this media.' }),
      el('p', { text: errorCopy(error) }), el('div', { class: 'error-actions' }, action)),
  ));
}

async function startDownload() {
  if (!state.inspection || !state.inspectionSource) return;
  const button = $('#download-button');
  if (button) { button.disabled = true; button.replaceChildren(icon('clock'), ' Starting…'); }
  try {
    const selector = $('#quality-select');
    const asset = firstAsset(state.inspection);
    const formats = (asset && asset.formats ? asset.formats : []).filter(
      (format) => format.downloadable && format.selectable !== false,
    );
    const selectedFormat = state.inspection.assets.length === 1 && formats.length > 1 && selector
      ? selector.value
      : null;
    const job = await api('/api/jobs', {
      method: 'POST',
      body: {
        source: state.inspectionSource,
        connection_id: selectedConnectionId(),
        options: selectedFormat ? { format_id: selectedFormat } : {},
      },
    });
    await refreshJobs();
    renderDownloadStarted(job);
  } catch (error) {
    showAlert('#submit-error', error);
    if (button) { button.disabled = false; button.replaceChildren(icon('download'), ' Download'); }
  }
}

function renderDownloadStarted(job) {
  $('#composer-step').textContent = 'Downloading';
  $('#composer-step').dataset.state = 'loading';
  setHint('success', 'Your download has started. Follow live progress in Recent downloads.', 'check');
  const asset = firstAsset(state.inspection);
  const host = $('#inspection-content');
  host.hidden = false;
  host.replaceChildren(el('div', { class: 'inspection-error' },
    el('span', { class: 'error-mark', style: 'color:var(--teal);background:var(--teal-soft)' }, icon('check')),
    el('div', {}, el('h2', { text: 'Download started' }),
      el('p', { text: `We’re preparing ${mediaTitle(state.inspection, asset)}. Progress will update here and in your download history.` }),
      el('div', { class: 'error-actions' },
        el('button', { class: 'button primary', type: 'button', onclick: () => openJob(job.id) }, ' View progress'),
        el('button', { class: 'button secondary', type: 'button', onclick: () => { resetInspection(); $('#source-input').value = ''; updateComposerValidation(); $('#source-input').focus(); } }, ' Download another'),
      )),
  ));
}

/* ---------- jobs / activity ---------- */
const BUSY = new Set(['QUEUED', 'VALIDATING', 'RESOLVING', 'DOWNLOADING', 'PROCESSING', 'TRANSFERRING']);
const RETRYABLE = new Set(['FAILED', 'PARTIAL_SUCCESS', 'AUTHORIZATION_REQUIRED', 'RATE_LIMITED', 'PAUSED']);

function progressFor(job) {
  const progress = state.progress.get(job.id);
  return progress && progress.size ? Array.from(progress.values()) : (job.progress || []);
}

function jobTotals(job) {
  return progressFor(job).reduce((total, item) => ({
    done: total.done + (Number(item.done) || 0),
    size: total.size + (item.total ? Number(item.total) : 0),
  }), { done: 0, size: 0 });
}

function jobError(job) {
  if (job.error_code) return { code: job.error_code, message: NEXT_STEP[job.error_code] || job.error_code };
  if (job.state === 'FAILED') return { code: 'FAILED', message: 'The download could not finish. Open details to see the affected media and next step.' };
  if (job.state === 'PARTIAL_SUCCESS') return { code: 'PARTIAL_SUCCESS', message: 'Some items were downloaded; one or more items need attention.' };
  if (job.state === 'AUTHORIZATION_REQUIRED') return { code: 'AUTH_REQUIRED', message: NEXT_STEP.AUTH_REQUIRED };
  if (job.state === 'RATE_LIMITED') return { code: 'RATE_LIMITED', message: NEXT_STEP.RATE_LIMITED };
  return null;
}

function readableState(stateName) { return (stateName || 'unknown').replace(/_/g, ' ').toLowerCase(); }

function jobCard(job) {
  const busy = Boolean(job.running) || BUSY.has(job.state);
  const totals = jobTotals(job);
  const percent = totals.size ? Math.min(100, Math.round((totals.done / totals.size) * 100)) : null;
  const issue = jobError(job);
  const source = job.source || 'Media download';
  const actions = [];
  if (busy) actions.push(el('button', { class: 'small-button danger', type: 'button', onclick: (event) => { event.stopPropagation(); act(`/api/jobs/${job.id}/cancel`); } }, 'Cancel'));
  if (RETRYABLE.has(job.state)) actions.push(el('button', { class: 'small-button', type: 'button', onclick: (event) => { event.stopPropagation(); act(`/api/jobs/${job.id}/retry`); } }, icon('refresh'), 'Retry'));
  actions.push(el('button', { class: 'small-button', type: 'button', onclick: (event) => { event.stopPropagation(); openJob(job.id); } }, 'Details'));
  return el('article', { class: 'job', tabindex: '0', role: 'button', onclick: () => openJob(job.id), onkeydown: (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openJob(job.id); } } },
    el('div', { class: 'job-main' },
      el('div', { class: 'job-source', text: source, title: source }),
      el('div', { class: 'job-subline' },
        el('span', { class: `pill ${job.state}`, text: readableState(job.state) }),
        el('span', { text: job.connector || 'media' }),
        el('span', { text: ago(job.updated_at) || 'now' }),
      ),
    ),
    el('div', { class: 'job-actions' }, actions),
    busy ? el('div', { class: 'job-progress' },
      el('div', { class: `progress-track${percent === null ? ' indeterminate' : ''}` }, el('span', { style: percent === null ? '' : `--progress:${percent}%` })),
      el('div', { class: 'progress-label' },
        el('span', { text: totals.size ? `${bytes(totals.done)} of ${bytes(totals.size)}` : `${readableState(job.state)}…` }),
        el('span', { text: percent === null ? 'Working' : `${percent}%` }),
      ),
    ) : null,
    issue && !busy ? el('div', { class: 'job-error' }, el('strong', { text: 'Needs attention.' }), issue.message) : null,
  );
}

function activityCard(job) {
  const issue = jobError(job);
  return el('button', { class: 'activity-job', type: 'button', onclick: () => openJob(job.id) },
    el('div', { class: 'activity-job-top' },
      el('span', { class: 'activity-job-source', text: titleFromUrl(job.source || ''), title: job.source || '' }),
      el('span', { class: `status-dot ${job.state}`, title: readableState(job.state) }),
    ),
    el('div', { class: 'activity-job-meta' },
      el('span', { text: issue ? 'Needs attention' : readableState(job.state) }),
      el('span', { text: ago(job.updated_at) || 'now' }),
    ),
  );
}

function renderHistoryStats() {
  const jobs = state.jobs;
  const counts = {
    total: jobs.length,
    running: jobs.filter((job) => job.running || BUSY.has(job.state)).length,
    completed: jobs.filter((job) => job.state === 'COMPLETED').length,
    attention: jobs.filter((job) => ['FAILED', 'PARTIAL_SUCCESS', 'AUTHORIZATION_REQUIRED', 'RATE_LIMITED', 'PAUSED'].includes(job.state)).length,
  };
  const host = $('#history-stats');
  host.replaceChildren(
    statCard('All downloads', counts.total, ''),
    statCard('In progress', counts.running, 'running'),
    statCard('Completed', counts.completed, 'done'),
    statCard('Needs attention', counts.attention, 'attention'),
  );
}

function statCard(label, value, className) {
  return el('div', { class: `history-stat ${className}` }, el('span', { text: label }), el('strong', { text: String(value) }));
}

function renderJobs() {
  const jobs = state.jobs;
  $('#jobs-badge').textContent = String(jobs.length);
  $('#history-count').textContent = `${jobs.length} total`;
  $('#jobs-list').replaceChildren(...jobs.map(jobCard));
  $('#jobs-empty').hidden = jobs.length > 0;
  const recent = jobs.slice(0, 4);
  $('#activity-list').replaceChildren(...recent.map(activityCard));
  $('#activity-empty').hidden = recent.length > 0;
  renderHistoryStats();
}

async function refreshJobs() {
  const payload = await api('/api/jobs');
  state.jobs = payload.jobs || [];
  renderJobs();
}

async function act(path) {
  try {
    await api(path, { method: 'POST' });
    await refreshJobs();
  } catch (error) {
    showAlert('#submit-error', error);
    goto('downloader');
  }
}

/* ---------- details drawer ---------- */
async function openJob(jobId) {
  state.openJobId = jobId;
  $('#drawer').hidden = false;
  $('#scrim').hidden = false;
  $('#drawer-title').textContent = 'Loading download…';
  $('#drawer-id').textContent = jobId;
  $('#drawer-body').replaceChildren(el('div', { class: 'inspection-loading' }, el('div', { class: 'skeleton line' }), el('div', { class: 'skeleton line short' })));
  try {
    const [detail, outputData] = await Promise.all([
      api(`/api/jobs/${jobId}`),
      api(`/api/jobs/${jobId}/outputs`).catch(() => ({ outputs: [] })),
    ]);
    if (state.openJobId !== jobId) return;
    renderDrawer(detail, outputData.outputs || []);
  } catch (error) {
    if (state.openJobId !== jobId) return;
    renderDrawerError(jobId, error);
  }
}

function closeDrawer() {
  state.openJobId = null;
  $('#drawer').hidden = true;
  $('#scrim').hidden = true;
}

function drawerSection(title, ...children) {
  return el('section', { class: 'drawer-section' }, el('p', { class: 'drawer-section-title', text: title }), children);
}

function renderDrawerError(jobId, error) {
  $('#drawer-title').textContent = 'Could not open this download';
  $('#drawer-id').textContent = jobId;
  $('#drawer-body').replaceChildren(drawerSection('DETAILS',
    el('div', { class: 'drawer-message' }, error.message || 'The download details are unavailable.'),
    el('button', { class: 'button secondary', type: 'button', onclick: () => openJob(jobId) }, icon('refresh'), 'Try again'),
  ));
}

function renderDrawer(detail, outputs) {
  $('#drawer-title').textContent = titleFromUrl(detail.source || 'Media download');
  $('#drawer-id').textContent = detail.id;
  const errorDetail = detail.error_detail || {};
  const topError = errorDetail.user_message || errorDetail.message || null;
  const items = detail.items || [];
  const itemErrors = items.filter((item) => item.error_message);
  const summary = drawerSection('STATUS',
    el('div', { class: 'drawer-summary' },
      el('span', { class: `pill ${detail.state}`, text: readableState(detail.state) }),
      el('span', { class: 'job-status', text: detail.connector || 'media' }),
      el('span', { class: 'job-status', text: detail.policy ? detail.policy.replace(/_/g, ' ') : '' }),
    ),
    topError ? el('div', { class: 'drawer-message' }, topError, ' ', NEXT_STEP[errorDetail.code] || '') : null,
  );
  const itemBlock = drawerSection(`MEDIA (${items.length})`, items.length
    ? items.map((item) => el('div', { class: 'drawer-item' },
      el('span', { class: 'drawer-item-pos', text: String(item.position).padStart(2, '0') }),
      el('div', { class: 'drawer-item-copy' },
        el('strong', { text: `${item.kind || 'media'} · ${item.asset_id || 'item'}` }),
        el('small', { text: `${item.state || 'waiting'}${item.output && item.output.selected_rep_id ? ` · ${item.output.selected_rep_id}` : ''}` }),
        item.error_message ? el('small', { class: 'drawer-item-error', text: `${item.error_message} ${NEXT_STEP[item.error_code] || ''}` }) : null,
      ),
      el('span', { class: `pill ${item.state}`, text: readableState(item.state) }),
    ))
    : el('p', { class: 'muted-copy', text: 'Media details will appear once the link has been resolved.' }),
  );
  const outputBlock = drawerSection(`FILES (${outputs.length})`, outputs.length
    ? outputs.map(outputRow)
    : el('p', { class: 'muted-copy', text: detail.state === 'COMPLETED' ? 'No files were reported.' : 'Files will appear here when the download finishes.' }),
  );
  const events = detail.events || [];
  const historyBlock = drawerSection('ACTIVITY', events.length
    ? el('div', { class: 'timeline' }, events.map((event) => el('div', { class: 'timeline-row' },
      el('time', { text: (event.at || '').slice(11, 19) || '—' }),
      el('span', { text: `${event.from_state || 'created'} → ${event.to_state || 'updated'}` }),
    )))
    : el('p', { class: 'muted-copy', text: 'No status updates recorded yet.' }),
  );
  const retryBlock = itemErrors.length || RETRYABLE.has(detail.state)
    ? drawerSection('NEXT STEP', el('p', { class: 'muted-copy', text: itemErrors.length
      ? 'Review the media errors above, then retry the download when the issue is resolved.'
      : errorCopy({ code: detail.state }) }),
      RETRYABLE.has(detail.state) ? el('button', { class: 'button secondary', type: 'button', onclick: () => { act(`/api/jobs/${detail.id}/retry`); closeDrawer(); } }, icon('refresh'), 'Retry download') : null)
    : null;
  $('#drawer-body').replaceChildren(summary, itemBlock, outputBlock, historyBlock, retryBlock);
}

function outputRow(output) {
  const url = `/api/files?path=${encodeURIComponent(output.relative_path)}`;
  const thumbnail = (output.mime_type || '').startsWith('image/')
    ? el('div', { class: 'output-thumb' }, el('img', { src: url, alt: '', loading: 'lazy' }))
    : el('div', { class: 'output-thumb', text: (output.relative_path || '').split('.').pop() || 'file' });
  return el('div', { class: 'output-row' },
    thumbnail,
    el('div', { class: 'output-copy' },
      el('strong', { text: (output.relative_path || '').split('/').pop() || 'Download' }),
      el('small', { text: `${bytes(output.size_bytes)}${output.selected_rep_id ? ` · ${output.selected_rep_id}` : ''}${output.sha256 ? ` · ${output.sha256.slice(0, 12)}…` : ''}` }),
    ),
    el('div', { class: 'output-actions' },
      el('a', { href: `${url}&download=true`, text: 'Save' }),
      output.sidecar ? el('a', { href: `/api/files?path=${encodeURIComponent(output.sidecar)}`, target: '_blank', rel: 'noopener', text: 'Info' }) : null,
    ),
  );
}

/* ---------- Setup / account connection ---------- */
async function loadState() {
  state.env = await api('/api/state');
  const output = state.env.output_root || 'Your Downloads folder';
  $('#meta-output').textContent = output;
  $('#setup-output').textContent = output;
  renderSupport();
}

function renderSupport() {
  const connectorIds = new Set(((state.env && state.env.connectors) || []).map((connector) => connector.id));
  const rows = [
    { name: 'Instagram media', enabled: connectorIds.has('instagram_graph'), note: connectorIds.has('instagram_graph') ? 'Connect account' : 'Not enabled' },
    { name: 'Facebook media', enabled: connectorIds.has('facebook_graph'), note: connectorIds.has('facebook_graph') ? 'Available' : 'Connector not installed' },
    { name: 'Direct files & streams', enabled: connectorIds.has('direct_url') || connectorIds.has('stream_manifest'), note: 'Optional sources' },
  ];
  $('#support-list').replaceChildren(...rows.map((row) => el('div', { class: `support-row${row.enabled ? '' : ' unsupported'}` },
    icon(row.enabled ? 'check' : 'alert'), el('span', { text: row.name }), el('small', { text: row.note }),
  )));
}

async function loadConnections() {
  const payload = await api('/api/connections');
  state.connections = payload.connections || [];
  renderConnections();
}

function renderConnections() {
  const active = state.connections.filter((connection) => connection.status === 'active');
  $('#connection-summary').textContent = active.length ? `${active.length} connected` : 'Not connected';
  $('#connection-summary').dataset.state = active.length ? 'ready' : 'error';
  $('#connections-empty').hidden = state.connections.length > 0;
  $('#connections-list').replaceChildren(...state.connections.map((connection) => el('div', { class: 'connection-row' },
    el('div', { class: 'connection-row-main' },
      el('span', { class: 'connection-icon' }, icon(connection.status === 'active' ? 'check' : 'alert')),
      el('div', {}, el('strong', { text: connection.provider_user_id ? `Instagram account ${connection.provider_user_id}` : 'Instagram account' }),
        el('small', { text: connection.status === 'active'
          ? `Connected · ${connection.scopes_granted.join(', ') || 'media access'}`
          : `Status: ${connection.status}` })),
    ),
    el('div', { class: 'connection-row-actions' },
      el('span', { class: `pill ${connection.status === 'active' ? 'COMPLETED' : 'AUTHORIZATION_REQUIRED'}`, text: connection.status === 'active' ? 'connected' : connection.status }),
      el('button', { class: 'small-button danger', type: 'button', onclick: () => disconnect(connection) }, 'Disconnect'),
    ),
  )));
}

async function disconnect(connection) {
  if (!window.confirm('Disconnect this account? Downloads that need it will no longer run.')) return;
  try {
    await api(`/api/connections/${connection.id}`, { method: 'DELETE' });
    await loadConnections();
  } catch (error) {
    showAlert('#ig-error', error);
    $('#connection-wizard').open = true;
  }
}

const WIZARD_FIELDS = {
  client_id: { input: 'ig-client-id', message: 'ig-client-id-message', label: 'Instagram App ID' },
  redirect_uri: { input: 'ig-redirect', message: 'ig-redirect-message', label: 'Redirect URI' },
  redirect_url: { input: 'ig-redirect-url', message: 'ig-redirect-url-message', label: 'Redirect URL' },
  client_secret: { input: 'ig-secret', message: 'ig-secret-message', label: 'Instagram App Secret' },
};

function wizardValue(field) {
  const definition = WIZARD_FIELDS[field];
  return definition ? $(`#${definition.input}`).value.trim() : '';
}

function clearWizardFieldError(field) {
  const definition = WIZARD_FIELDS[field];
  if (!definition) return;
  const input = $(`#${definition.input}`);
  const message = $(`#${definition.message}`);
  input.removeAttribute('aria-invalid');
  input.setCustomValidity('');
  message.textContent = message.dataset.helper || '';
  delete message.dataset.error;
}

function setWizardFieldError(field, messageText) {
  const definition = WIZARD_FIELDS[field];
  if (!definition) return;
  const input = $(`#${definition.input}`);
  const message = $(`#${definition.message}`);
  const text = String(messageText || 'is not valid');
  input.setAttribute('aria-invalid', 'true');
  input.setCustomValidity(text);
  message.textContent = text;
  message.dataset.error = 'true';
}

function clearWizardFieldErrors() {
  Object.keys(WIZARD_FIELDS).forEach(clearWizardFieldError);
}

function wizardValidationError(fieldErrors) {
  const messages = Object.entries(fieldErrors).map(([field, message]) => {
    const definition = WIZARD_FIELDS[field];
    return `${definition ? definition.label : field}: ${message}.`;
  });
  const error = new Error(messages.join(' '));
  error.code = 'INVALID_INPUT';
  error.fieldErrors = fieldErrors;
  return error;
}

function showWizardError(error) {
  clearWizardFieldErrors();
  const fields = (error && error.fieldErrors) || {};
  Object.entries(fields).forEach(([field, message]) => setWizardFieldError(field, message));
  const host = $('#ig-error');
  const hasFields = Object.keys(fields).some((field) => WIZARD_FIELDS[field]);
  host.replaceChildren(
    el('strong', { text: (error && error.message) || 'Check the connection details and try again.' }),
    el('span', { class: 'next', text: hasFields
      ? 'Fix the highlighted field, then generate a fresh authorization link.'
      : 'Keep the Redirect URI identical in Meta, Step 1, and the final callback URL.' }),
  );
  host.hidden = false;
}

function setStepTwoLocked(locked, message) {
  const completion = $('#ig-step-two');
  const button = $('#ig-complete');
  completion.disabled = locked;
  button.disabled = locked;
  completion.setAttribute('aria-disabled', String(locked));
  const status = $('#ig-step-two-status');
  status.textContent = message;
  status.classList.toggle('locked', locked);
  status.classList.toggle('ready', !locked);
}

function setStepOneLocked(locked) {
  ['client_id', 'redirect_uri'].forEach((field) => {
    const input = $(`#${WIZARD_FIELDS[field].input}`);
    input.readOnly = locked;
    input.setAttribute('aria-readonly', String(locked));
  });
  $('#ig-restart').hidden = !locked;
}

function clearAuthorizationLink() {
  $('#ig-link').hidden = true;
  $('#ig-authorize').removeAttribute('href');
  $('#ig-authorize').textContent = '';
  $('#ig-authorized-redirect').textContent = '';
}

function invalidateInstagramAuthorization(message) {
  if (!state.instagramAuthorization) return;
  state.instagramAuthorization = null;
  clearAuthorizationLink();
  $('#ig-redirect-url').value = '';
  $('#ig-secret').value = '';
  clearAlert('#ig-success');
  setStepOneLocked(false);
  setStepTwoLocked(true, 'Locked until Step 1 creates a new authorization link.');
  $('#ig-start-status').textContent = message || 'Step 1 details changed. Generate a new authorization link before continuing.';
}

function validatePublicHttpsUrl(value, field, { allowQuery = false } = {}) {
  if (!value) return 'is required';
  if (!value.startsWith('https://')) return 'must start with https://';
  let parsed;
  try { parsed = new URL(value); } catch { return 'must be a complete HTTPS URL'; }
  if (parsed.protocol !== 'https:' || !parsed.hostname) return 'must be a complete HTTPS URL';
  if (parsed.username || parsed.password) return 'cannot include a username or password';
  const hostname = parsed.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (hostname === 'localhost' || hostname === '::1' || hostname.startsWith('127.')) {
    return 'must use a public HTTPS domain; Meta does not accept localhost or loopback callbacks';
  }
  if (!allowQuery && (parsed.search || parsed.hash)) {
    return 'must not include query parameters or a fragment; paste the full callback URL in Step 2 instead';
  }
  if (!allowQuery && parsed.pathname === '/' && !value.endsWith('/')) {
    return 'needs a trailing slash or a callback path so the registered value remains exact';
  }
  return null;
}

function validateStartFields() {
  const clientId = wizardValue('client_id');
  const redirectUri = wizardValue('redirect_uri');
  const fieldErrors = {};
  if (!clientId) fieldErrors.client_id = 'is required';
  const redirectError = validatePublicHttpsUrl(redirectUri, 'redirect_uri');
  if (redirectError) fieldErrors.redirect_uri = redirectError;
  return { valid: Object.keys(fieldErrors).length === 0, fieldErrors, clientId, redirectUri };
}

function callbackBase(value) {
  const index = value.search(/[?#]/);
  return index === -1 ? value : value.slice(0, index);
}

function validateCompleteFields(authorization) {
  const redirectUrl = wizardValue('redirect_url');
  const clientSecret = wizardValue('client_secret');
  const fieldErrors = {};
  if (!authorization) {
    fieldErrors.redirect_url = 'requires a successful Step 1 authorization link first';
    return { valid: false, fieldErrors, redirectUrl, clientSecret };
  }
  const urlError = validatePublicHttpsUrl(redirectUrl, 'redirect_url', { allowQuery: true });
  if (urlError) fieldErrors.redirect_url = urlError;
  else {
    const parsed = new URL(redirectUrl);
    if (!parsed.searchParams.get('code') || !parsed.searchParams.get('state')) {
      fieldErrors.redirect_url = 'must include both code and state from the browser address bar';
    } else if (callbackBase(redirectUrl) !== authorization.redirectUri) {
      fieldErrors.redirect_url = 'must start with the exact Redirect URI used in Step 1';
    }
  }
  if (!clientSecret) fieldErrors.client_secret = 'is required';
  return { valid: Object.keys(fieldErrors).length === 0, fieldErrors, redirectUrl, clientSecret };
}

function addKnownWizardField(error, field) {
  if (!error || error.code !== 'INVALID_INPUT' || Object.keys(error.fieldErrors || {}).length) return error;
  error.fieldErrors = { [field]: error.message || 'is not valid' };
  return error;
}

function initConnectWizard() {
  setStepOneLocked(false);
  setStepTwoLocked(true, 'Locked until Step 1 creates an authorization link.');
  const invalidateOnChange = (event) => {
    const field = Object.entries(WIZARD_FIELDS).find(([, definition]) => definition.input === event.target.id);
    if (field) clearWizardFieldError(field[0]);
    const started = state.instagramAuthorization;
    if (started && (event.target.id === 'ig-client-id' || event.target.id === 'ig-redirect')
      && (wizardValue('client_id') !== started.clientId || wizardValue('redirect_uri') !== started.redirectUri)) {
      invalidateInstagramAuthorization('Step 1 details changed. Generate a new authorization link before continuing.');
    }
  };
  ['ig-client-id', 'ig-redirect', 'ig-redirect-url', 'ig-secret'].forEach((id) => {
    $(`#${id}`).addEventListener('input', invalidateOnChange);
  });
  $('#ig-restart').addEventListener('click', () => {
    invalidateInstagramAuthorization('Step 1 is ready to edit. Update the registered Redirect URI if needed, then generate a new authorization link.');
    $('#ig-client-id').focus();
  });

  $('#ig-start').addEventListener('click', async () => {
    clearAlert('#ig-error');
    clearAlert('#ig-success');
    clearWizardFieldErrors();
    const values = validateStartFields();
    if (!values.valid) {
      showWizardError(wizardValidationError(values.fieldErrors));
      return;
    }
    const button = $('#ig-start');
    button.disabled = true;
    $('#ig-start-status').textContent = 'Creating a secure authorization link…';
    try {
      const result = await api('/api/connections/instagram/start', {
        method: 'POST',
        body: {
          client_id: values.clientId,
          redirect_uri: values.redirectUri,
          force_reauth: $('#ig-force').checked,
        },
      });
      if (wizardValue('client_id') !== values.clientId || wizardValue('redirect_uri') !== values.redirectUri) {
        clearAuthorizationLink();
        setStepOneLocked(false);
        setStepTwoLocked(true, 'Locked until Step 1 creates an authorization link.');
        $('#ig-start-status').textContent = 'Step 1 details changed while the link was being created. Generate a new link.';
        return;
      }
      state.instagramAuthorization = { clientId: values.clientId, redirectUri: values.redirectUri };
      $('#ig-client-id').value = values.clientId;
      $('#ig-redirect').value = values.redirectUri;
      $('#ig-authorize').href = result.authorize_url;
      $('#ig-authorize').textContent = result.authorize_url;
      $('#ig-authorized-redirect').textContent = values.redirectUri;
      $('#ig-link').hidden = false;
      setStepOneLocked(true);
      setStepTwoLocked(false, 'Authorization link created. Complete approval in Meta, then paste the full callback URL below.');
      $('#ig-start-status').textContent = 'Link ready. Meta must list the exact Redirect URI shown below.';
      window.open(result.authorize_url, '_blank', 'noopener');
    } catch (error) {
      showWizardError(addKnownWizardField(error, 'redirect_uri'));
      $('#ig-start-status').textContent = 'The authorization link was not created. Correct the details and try again.';
    } finally {
      button.disabled = false;
    }
  });

  $('#ig-complete').addEventListener('click', async () => {
    clearAlert('#ig-error');
    clearAlert('#ig-success');
    clearWizardFieldErrors();
    const values = validateCompleteFields(state.instagramAuthorization);
    if (!values.valid) {
      showWizardError(wizardValidationError(values.fieldErrors));
      return;
    }
    const button = $('#ig-complete');
    button.disabled = true;
    try {
      const result = await api('/api/connections/instagram/complete', {
        method: 'POST',
        body: {
          redirect_url: values.redirectUrl,
          client_id: state.instagramAuthorization.clientId,
          client_secret: $('#ig-secret').value,
          redirect_uri: state.instagramAuthorization.redirectUri,
        },
      });
      $('#ig-secret').value = '';
      $('#ig-redirect-url').value = '';
      state.instagramAuthorization = null;
      clearAuthorizationLink();
      setStepTwoLocked(true, 'Connection finished. Start Step 1 again only when you need to connect another account.');
      $('#ig-start-status').textContent = 'Instagram account connected.';
      const success = $('#ig-success');
      success.replaceChildren(el('strong', { text: `Connected as user ${result.connection.provider_user_id || 'your Instagram account'}.` }),
        el('span', { class: 'next', text: result.note || 'You can now check media that this account is authorized to access.' }));
      success.hidden = false;
      await loadConnections();
    } catch (error) {
      showWizardError(addKnownWizardField(error, 'redirect_url'));
    } finally {
      button.disabled = !state.instagramAuthorization;
    }
  });
}

/* ---------- self-test ---------- */
async function runSelfTest() {
  const button = $('#selftest-button');
  const result = $('#selftest-result');
  button.disabled = true;
  result.replaceChildren(el('div', { class: 'alert success' }, el('strong', { text: 'Running a local system check…' }), el('span', { class: 'next', text: 'Preparing a sample through the real download pipeline.' })));
  try {
    const started = await api('/api/selftest', { method: 'POST' });
    state.selftestJobId = started.job_id;
    let detail = null;
    for (let attempt = 0; attempt < 80; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      detail = await api(`/api/jobs/${started.job_id}`);
      if (!BUSY.has(detail.state)) break;
    }
    const done = detail && detail.state === 'COMPLETED';
    result.replaceChildren(el('div', { class: `alert ${done ? 'success' : 'error'}` },
      el('strong', { text: done ? 'System check passed' : `System check ended as ${readableState(detail && detail.state)}` }),
      el('span', { class: 'next', text: done
        ? 'The downloader successfully processed a local media sample.'
        : 'Open the download details to see what needs attention.' }),
    ));
    await refreshJobs();
  } catch (error) {
    result.replaceChildren(el('div', { class: 'alert error' }, el('strong', { text: error.message }), el('span', { class: 'next', text: errorCopy(error) })));
  } finally {
    button.disabled = false;
  }
}

/* ---------- live events ---------- */
function connectEvents() {
  const source = new EventSource('/api/events');
  source.onopen = () => {
    $('#live-indicator').dataset.state = 'live';
    $('#live-text').textContent = 'Live';
  };
  source.onerror = () => {
    $('#live-indicator').dataset.state = 'error';
    $('#live-text').textContent = 'Reconnecting';
  };
  source.onmessage = (message) => {
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    if (event.type === 'snapshot') {
      state.jobs = event.jobs || [];
      renderJobs();
      if (state.openJobId) refreshOpenDrawer(state.openJobId);
      return;
    }
    if (event.type === 'progress') {
      if (!state.progress.has(event.job_id)) state.progress.set(event.job_id, new Map());
      state.progress.get(event.job_id).set(event.asset_id, { done: event.done, total: event.total, percent: event.percent });
      renderJobs();
      return;
    }
    if (event.type === 'job.finished') {
      state.progress.delete(event.job_id);
      refreshJobs().catch(() => {});
      if (state.openJobId === event.job_id) refreshOpenDrawer(event.job_id);
      return;
    }
    if (event.type === 'connection.changed') { loadConnections().catch(() => {}); return; }
    if (event.type && event.type.startsWith('job.')) refreshJobs().catch(() => {});
  };
}

async function refreshOpenDrawer(jobId) {
  try {
    const [detail, out] = await Promise.all([api(`/api/jobs/${jobId}`), api(`/api/jobs/${jobId}/outputs`).catch(() => ({ outputs: [] }))]);
    if (state.openJobId === jobId) renderDrawer(detail, out.outputs || []);
  } catch (error) {
    if (state.openJobId === jobId) renderDrawerError(jobId, error);
  }
}

/* ---------- boot ---------- */
function initComposer() {
  $('#source-input').addEventListener('input', updateComposerValidation);
  $('#paste-button').addEventListener('click', pasteFromClipboard);
  $('#clear-input').addEventListener('click', () => {
    $('#source-input').value = '';
    resetInspection();
    updateComposerValidation();
    $('#source-input').focus();
  });
  $('#submit-form').addEventListener('submit', (event) => {
    event.preventDefault();
    inspectSource();
  });
}

async function boot() {
  initNavigation();
  initComposer();
  initConnectWizard();
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDrawer(); });
  $('#selftest-button').addEventListener('click', runSelfTest);
  resetInspection();
  updateComposerValidation();
  await Promise.all([
    loadState().catch((error) => { console.error('state', error); }),
    loadConnections().catch((error) => { console.error('connections', error); }),
    refreshJobs().catch((error) => { console.error('jobs', error); }),
  ]);
  connectEvents();
}

boot();
