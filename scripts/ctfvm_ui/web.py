#!/usr/bin/env python3
import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import SERVICE


BASE_STYLE = """
:root {
  --bg: #081019;
  --card: rgba(10, 18, 28, 0.88);
  --line: rgba(133, 156, 179, 0.22);
  --line-strong: rgba(133, 156, 179, 0.38);
  --text: #eef5ff;
  --muted: #9bb0c8;
  --teal: #67e8c8;
  --amber: #f7c66a;
  --red: #ff8f8f;
  --blue: #8bbcff;
  --green: #7ee787;
  --shadow: 0 18px 48px rgba(0, 0, 0, 0.24);
  --mono: "IBM Plex Mono", "SFMono-Regular", "Menlo", monospace;
  --sans: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  color: var(--text);
  font-family: var(--sans);
  background:
    radial-gradient(circle at top left, rgba(103, 232, 200, 0.12), transparent 32%),
    radial-gradient(circle at top right, rgba(139, 188, 255, 0.16), transparent 30%),
    linear-gradient(180deg, #0b1220 0%, #07101a 46%, #060b12 100%);
}
pre, code, input, button, select, textarea { font-family: var(--mono); }
a { color: inherit; text-decoration: none; }
.wrap { padding: 18px; display: grid; gap: 14px; }
.nav {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
  padding: 12px 16px;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: rgba(8, 14, 22, 0.76);
  box-shadow: var(--shadow);
}
.nav-links { display: flex; gap: 10px; flex-wrap: wrap; }
.nav-link {
  padding: 9px 12px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--muted);
}
.nav-link.active {
  color: var(--text);
  border-color: rgba(103, 232, 200, 0.38);
  background: rgba(103, 232, 200, 0.1);
}
.brand { display: grid; gap: 4px; }
.brand-title { font-size: 12px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--teal); }
.brand-copy { font-size: 13px; color: var(--muted); }
.hero {
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(320px, 1.1fr) minmax(320px, 0.9fr);
}
.hero.compact {
  grid-template-columns: minmax(420px, 1.15fr) minmax(360px, 0.85fr);
  align-items: start;
}
.topbar-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: minmax(420px, 1.2fr) minmax(340px, 0.8fr);
  align-items: start;
}
.grid-two { display: grid; gap: 14px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid-three { display: grid; gap: 14px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 18px;
  overflow: hidden;
  box-shadow: var(--shadow);
  backdrop-filter: blur(14px);
}
.card-head {
  padding: 14px 16px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid var(--line);
}
.card-head h2, .card-head h3 {
  margin: 0;
  font-size: 15px;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
}
.card-body { padding: 16px; display: grid; gap: 12px; }
.card.compact .card-body { padding: 14px; gap: 10px; }
.card.micro .card-body { padding: 12px 14px; gap: 8px; }
.card.micro .card-head { padding: 10px 14px 8px; }
.eyebrow {
  color: var(--teal);
  text-transform: uppercase;
  letter-spacing: 0.12em;
  font-size: 11px;
  font-weight: 700;
}
.headline {
  margin: 0;
  font-size: clamp(28px, 4vw, 42px);
  line-height: 0.95;
  letter-spacing: -0.04em;
}
.subline {
  margin: 0;
  color: var(--muted);
  line-height: 1.45;
  font-size: 14px;
}
.mini-title {
  margin: 0;
  font-size: 18px;
  line-height: 1.1;
  letter-spacing: -0.03em;
}
.mini-copy {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.4;
}
.field {
  display: grid;
  gap: 6px;
  min-width: 140px;
  flex: 1 1 180px;
}
.field label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
}
.field-row, .action-row, .meta-grid, .toolbar, .toggle-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}
.toolbar.tight { gap: 8px; align-items: flex-end; }
.toolbar.tight .field { flex: 1 1 260px; min-width: 220px; }
.toolbar.tight button,
.toolbar.tight .button-link { padding: 9px 11px; }
.toolbar.micro { gap: 8px; align-items: flex-end; }
.toolbar.micro .field { flex: 1 1 260px; min-width: 220px; }
.toolbar.micro button,
.toolbar.micro .button-link,
.toolbar.micro input,
.toolbar.micro select { padding: 8px 10px; }
input, select, textarea, button, .button-link {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: rgba(4, 9, 15, 0.76);
  color: var(--text);
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.25;
}
textarea {
  min-height: 104px;
  resize: vertical;
  width: 100%;
}
button, .button-link { cursor: pointer; }
button.primary, .button-link.primary {
  background: linear-gradient(135deg, rgba(103, 232, 200, 0.22), rgba(139, 188, 255, 0.18));
  border-color: rgba(103, 232, 200, 0.34);
}
button.secondary, .button-link.secondary {
  background: rgba(11, 19, 29, 0.85);
}
button.ghost, .button-link.ghost {
  background: transparent;
  border-color: var(--line);
}
.button-link.disabled, button:disabled { opacity: 0.45; pointer-events: none; }
.hint { font-size: 12px; color: var(--muted); line-height: 1.45; }
.mono-muted { font-family: var(--mono); color: var(--muted); font-size: 12px; }
.status-text { font-size: 12px; color: var(--muted); min-height: 16px; }
.status-text.ok { color: var(--green); }
.status-text.warn { color: var(--red); }
.hint.warn, .mono-muted.warn { color: var(--red); }
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 999px;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  border: 1px solid var(--line);
  color: var(--muted);
  background: rgba(255, 255, 255, 0.02);
}
.pill.running, .pill.progressing, .pill.solved, .pill.succeeded, .pill.active { color: var(--green); border-color: rgba(126, 231, 135, 0.36); }
.pill.halted, .pill.failed, .pill.stopped { color: var(--red); border-color: rgba(255, 143, 143, 0.36); }
.pill.stalled, .pill.blocked { color: var(--amber); border-color: rgba(247, 198, 106, 0.36); }
.pill.unknown { color: var(--blue); border-color: rgba(139, 188, 255, 0.36); }
.banner {
  padding: 14px 16px;
  border-radius: 16px;
  border: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.01));
  display: grid;
  gap: 6px;
}
.banner.compact {
  padding: 10px 12px;
  gap: 4px;
}
.banner-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.banner-title { margin: 0; font-size: 16px; letter-spacing: -0.02em; }
.banner-copy { margin: 0; color: var(--muted); line-height: 1.45; font-size: 13px; }
.banner.compact .banner-title { font-size: 14px; }
.banner.compact .banner-copy { font-size: 12px; line-height: 1.35; }
.banner.solved { border-color: rgba(126, 231, 135, 0.36); background: linear-gradient(135deg, rgba(126, 231, 135, 0.14), rgba(126, 231, 135, 0.04)); }
.banner.progressing { border-color: rgba(103, 232, 200, 0.34); background: linear-gradient(135deg, rgba(103, 232, 200, 0.14), rgba(139, 188, 255, 0.06)); }
.banner.stalled, .banner.blocked { border-color: rgba(247, 198, 106, 0.38); background: linear-gradient(135deg, rgba(247, 198, 106, 0.14), rgba(247, 198, 106, 0.04)); }
.banner.halted, .banner.stopped { border-color: rgba(255, 143, 143, 0.36); background: linear-gradient(135deg, rgba(255, 143, 143, 0.14), rgba(255, 143, 143, 0.04)); }
.banner.unknown { border-color: rgba(139, 188, 255, 0.36); background: linear-gradient(135deg, rgba(139, 188, 255, 0.12), rgba(139, 188, 255, 0.04)); }
.meta-grid div {
  min-width: 140px;
  flex: 1 1 160px;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: rgba(5, 10, 15, 0.65);
}
.meta-grid strong {
  display: block;
  color: var(--text);
  margin-bottom: 4px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.meta-grid.tight { gap: 8px; }
.meta-grid.tight div {
  min-width: 92px;
  flex: 1 1 98px;
  padding: 7px 9px;
}
.meta-grid.tight strong {
  margin-bottom: 3px;
  font-size: 10px;
}
.meta-inline {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.35;
  padding-top: 2px;
}
.meta-inline strong {
  color: var(--text);
  font-size: 11px;
  font-weight: 600;
}
.meta-inline .sep {
  color: rgba(155, 176, 200, 0.48);
}
.status-tools {
  display: grid;
  gap: 8px;
}
.status-tools .inline-field {
  min-width: 220px;
  flex: 1 1 260px;
}
.inline-field {
  display: grid;
  gap: 4px;
}
.inline-field label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
}
.action-row.tight {
  gap: 8px;
  align-items: flex-end;
}
.action-row.tight button,
.action-row.tight .button-link,
.action-row.tight input {
  padding: 8px 10px;
}
.list-grid { display: grid; gap: 10px; }
.tile-grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
.job-grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.pane-grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.mission-grid {
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
}
.stream-shell {
  display: grid;
  gap: 12px;
}
.tab-strip {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.tab-chip {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 8px 11px;
  background: rgba(4, 9, 15, 0.78);
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
}
.tab-chip.active {
  color: var(--text);
  border-color: rgba(103, 232, 200, 0.38);
  background: rgba(103, 232, 200, 0.12);
}
.stream-frame {
  border: 1px solid var(--line);
  border-radius: 16px;
  background:
    linear-gradient(180deg, rgba(9, 15, 24, 0.96), rgba(5, 10, 16, 0.94)),
    radial-gradient(circle at top right, rgba(139, 188, 255, 0.08), transparent 24%);
  overflow: hidden;
}
.stream-head {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.stream-title {
  margin: 0;
  font-size: 14px;
  letter-spacing: 0.02em;
}
.stream-copy {
  color: var(--muted);
  font-size: 12px;
}
.stream-frame pre {
  border: 0;
  border-radius: 0;
  background: transparent;
  max-height: 56vh;
  min-height: 52vh;
  padding: 16px;
}
.run-split {
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(320px, 0.95fr) minmax(360px, 1.05fr);
}
.findings-split {
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(420px, 1.15fr) minmax(300px, 0.85fr);
}
.list-card, .pane-card {
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 14px;
  background: rgba(7, 12, 20, 0.76);
  display: grid;
  gap: 10px;
}
.list-card.selected, .pane-card.selected-pane {
  border-color: rgba(103, 232, 200, 0.5);
  box-shadow: 0 0 0 1px rgba(103, 232, 200, 0.25) inset;
}
.list-top, .pane-top {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: flex-start;
}
.artifact-item {
  padding: 10px 12px;
  gap: 8px;
  cursor: pointer;
}
.artifact-item .list-top {
  align-items: center;
}
.artifact-item .list-title {
  font-size: 13px;
}
.artifact-item .list-meta {
  font-size: 11px;
}
.list-title, .pane-title {
  margin: 0;
  font-size: 14px;
  letter-spacing: -0.02em;
  word-break: break-word;
}
.list-meta { color: var(--muted); font-size: 12px; line-height: 1.45; }
.closed-panes {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}
.closed-panes:empty { display: none; }
.empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
pre {
  margin: 0;
  padding: 14px;
  border-radius: 14px;
  border: 1px solid var(--line);
  background: rgba(2, 6, 10, 0.86);
  max-height: 38vh;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.45;
}
.pane-card.pane-collapsed { display: none; }
img#artifactImage {
  display: none;
  max-width: 100%;
  max-height: 48vh;
  border-radius: 14px;
  border: 1px solid var(--line);
}
@media (max-width: 1100px) {
  .hero, .grid-two, .grid-three { grid-template-columns: 1fr; }
  .hero.compact, .run-split, .topbar-grid, .findings-split, .mission-grid { grid-template-columns: 1fr; }
  pre { max-height: 28vh; }
  .stream-frame pre { min-height: 34vh; max-height: 40vh; }
}
"""


PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__TITLE__</title>
  <style>__STYLE__</style>
</head>
<body>
  <div class="wrap">
    __NAV__
    __BODY__
  </div>
  <script>__SCRIPT__</script>
</body>
</html>
"""


def render_nav(active: str) -> str:
    items = [
        ("/", "Overview", "overview"),
        ("/run", "Run Monitor", "run"),
    ]
    links = []
    for href, label, key in items:
        cls = "nav-link active" if key == active else "nav-link"
        links.append(f'<a class="{cls}" href="{href}">{label}</a>')
    return (
        '<div class="nav">'
        '<div class="brand">'
        '<div class="brand-title">CTFVM Control Room</div>'
        "</div>"
        f'<div class="nav-links">{"".join(links)}</div>'
        "</div>"
    )


def render_page(title: str, active: str, body: str, script: str) -> str:
    return (
        PAGE_TEMPLATE.replace("__TITLE__", title)
        .replace("__STYLE__", BASE_STYLE)
        .replace("__NAV__", render_nav(active))
        .replace("__BODY__", body)
        .replace("__SCRIPT__", script)
    )


def render_overview_page() -> str:
    body = """
    <section class="card">
      <div class="card-head">
        <h2>Spawn VM</h2>
        <span class="mono-muted">Backed by ./scripts/ctfvm start</span>
      </div>
      <div class="card-body">
        <div class="field-row">
          <div class="field">
            <label for="spawnProvider">Provider</label>
            <select id="spawnProvider" onchange="syncSpawnProviderFields()">
              <option value="gcp">gcp</option>
              <option value="digitalocean">digitalocean</option>
            </select>
          </div>
          <div class="field">
            <label for="spawnAgent">Agent</label>
            <select id="spawnAgent" onchange="persistAgent()">
              <option value="codex">codex</option>
              <option value="claude">claude</option>
            </select>
          </div>
          <div class="field">
            <label for="spawnTimeout">Timeout (min)</label>
            <input id="spawnTimeout" placeholder="1440" />
          </div>
          <div class="field">
            <label for="spawnVariant">Toolbox Variant</label>
            <select id="spawnVariant" onchange="updateLocalArchiveHint()">
              <option value="lean">lean</option>
              <option value="full">full</option>
            </select>
            <div id="spawnVariantHint" class="hint mono-muted"></div>
          </div>
          <div class="field">
            <label for="spawnBatchMode">Spawn Mode</label>
            <select id="spawnBatchMode" onchange="syncSpawnModeFields()">
              <option value="single">single challenge</option>
              <option value="batch">one VM per child directory</option>
            </select>
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label id="spawnChallengeDirLabel" for="spawnChallengeDir">Challenge Directory</label>
            <input id="spawnChallengeDir" placeholder="./challenge or /absolute/path" />
            <div class="action-row tight">
              <button class="ghost" type="button" onclick="browseChallengeDir()">Choose In Finder</button>
            </div>
          </div>
          <div class="field">
            <label for="spawnFlagFormat">Flag Format</label>
            <input id="spawnFlagFormat" placeholder="flag{...} or HTB{...}" />
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="spawnZone">Zone / Region</label>
            <input id="spawnZone" placeholder="Optional" />
          </div>
          <div class="field" id="spawnProjectField">
            <label for="spawnProject">Project (GCP)</label>
            <input id="spawnProject" placeholder="Optional" />
          </div>
          <div class="field" id="spawnMachineField">
            <label for="spawnMachineType">Machine Type (GCP)</label>
            <input id="spawnMachineType" placeholder="e2-standard-4" />
          </div>
          <div class="field" id="spawnSizeField" style="display:none;">
            <label for="spawnSizeSlug">Size Slug (DigitalOcean)</label>
            <input id="spawnSizeSlug" placeholder="s-4vcpu-8gb" />
          </div>
        </div>
        <div class="field">
          <label for="spawnDesc">Challenge Context</label>
          <textarea id="spawnDesc" placeholder="Problem statement, notable files, service hints, constraints..."></textarea>
        </div>
        <div class="field">
          <label for="spawnIdeas">Initial Ideas</label>
          <textarea id="spawnIdeas" placeholder="Optional starting hypotheses or exploit directions."></textarea>
        </div>
        <div id="spawnBatchHint" class="hint" style="display:none;">Batch mode reads each immediate child directory under the root path as one challenge. It loads `description.txt` and `ideas.txt` from each child automatically and applies the shared flag format to all of them.</div>
        <div class="toggle-row">
          <input type="checkbox" id="spawnUseLocalImage" onchange="updateLocalArchiveHint()" />
          <label for="spawnUseLocalImage">Use cached local toolbox archive</label>
          <input type="checkbox" id="spawnNoAuthSync" />
          <label for="spawnNoAuthSync">Skip agent auth sync</label>
        </div>
        <div id="spawnLocalArchiveStatus" class="hint mono-muted"></div>
        <div class="action-row">
          <button class="primary" onclick="spawnRun()">Start VM</button>
          <span id="spawnStatus" class="status-text"></span>
        </div>
        <div class="hint">Provider-specific blanks fall back to repo `.env`, cloud CLI defaults, or saved toolbox config.</div>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Fleet</h2>
        <div class="action-row tight">
          <span id="fleetSummary" class="mono-muted">Loading</span>
          <button class="ghost" type="button" onclick="refreshOverview(true)">Refresh</button>
        </div>
      </div>
      <div class="card-body">
        <div id="runCards" class="tile-grid"></div>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Provisioning Jobs</h2>
        <div class="action-row tight">
          <span id="jobSummary" class="mono-muted">No jobs yet</span>
          <button class="ghost" type="button" onclick="refreshOverview(true)">Refresh</button>
        </div>
      </div>
      <div class="card-body">
        <div id="spawnJobs" class="job-grid"></div>
      </div>
    </section>
    """
    script = """
    let latestDefaultsApplied = false;
    let defaultsHydratedFromCache = false;
    let overviewRefreshInFlight = false;
    let lastRunsSignature = '';
    let lastJobsSignature = '';
    const FLAG_FORMAT_STORAGE_KEY = 'ctfvm.spawn.flag_format';
    const AGENT_STORAGE_KEY = 'ctfvm.spawn.agent';
    const OVERVIEW_CACHE_STORAGE_KEY = 'ctfvm.overview.cache.v1';
    const MAX_OVERVIEW_JOB_OUTPUT_CHARS = 12000;
    const SUPPORTED_AGENTS = ['codex', 'claude'];

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, '&#96;');
    }

    function setStatus(id, msg, tone='') {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = msg || '';
      el.className = 'status-text' + (tone ? (' ' + tone) : '');
    }

    function pillTone(value) {
      const tone = String(value || 'unknown').toLowerCase();
      if (['running', 'active', 'progressing', 'solved', 'succeeded'].includes(tone)) return 'running';
      if (['halted', 'failed', 'stopped'].includes(tone)) return 'halted';
      if (tone === 'stalled') return 'stalled';
      return 'unknown';
    }

    let latestLocalArchives = null;

    function applySpawnDefaults(defaults, options={}) {
      const force = !!options.force;
      const source = options.source || 'network';
      if (!defaults || (latestDefaultsApplied && !force)) return;
      latestDefaultsApplied = true;
      defaultsHydratedFromCache = source === 'cache';
      const provider = defaults.provider || 'gcp';
      document.getElementById('spawnProvider').value = provider;
      document.getElementById('spawnBatchMode').value = 'single';
      document.getElementById('spawnTimeout').value = defaults.timeout_min || '1440';
      document.getElementById('spawnVariant').value = defaults.toolbox_variant || 'lean';
      document.getElementById('spawnProject').value = defaults.gcp_project || '';
      document.getElementById('spawnZone').value = provider === 'gcp' ? (defaults.gcp_zone || '') : (defaults.do_region || '');
      document.getElementById('spawnMachineType').value = defaults.gcp_machine_type || '';
      document.getElementById('spawnSizeSlug').value = defaults.do_size_slug || '';
      latestLocalArchives = defaults.local_archives || null;
      applyCachedAgent(defaults.agent || 'codex');
      applyCachedFlagFormat();
      syncSpawnProviderFields();
      syncSpawnModeFields();
      updateLocalArchiveHint();
    }

    function formatBytes(n) {
      if (!n || n <= 0) return '';
      const units = ['B', 'KB', 'MB', 'GB'];
      let i = 0; let v = n;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return v.toFixed(v < 10 ? 1 : 0) + ' ' + units[i];
    }

    function archiveEntry(variant) {
      if (!latestLocalArchives) return null;
      return latestLocalArchives[variant] || null;
    }

    function updateLocalArchiveHint() {
      const variantSel = document.getElementById('spawnVariant');
      const variantHint = document.getElementById('spawnVariantHint');
      const statusEl = document.getElementById('spawnLocalArchiveStatus');
      const useLocal = document.getElementById('spawnUseLocalImage');
      if (!variantSel || !variantHint || !statusEl) return;
      const variant = variantSel.value || 'lean';
      const entry = archiveEntry(variant);
      const legacy = archiveEntry('legacy');
      if (entry && entry.present) {
        const size = formatBytes(entry.size);
        variantHint.textContent = 'cached locally' + (size ? ' (' + size + ')' : '');
      } else if (legacy && legacy.present) {
        variantHint.textContent = 'using legacy archive as fallback — rebuild with `scripts/ctfvm image build-local --variant ' + variant + '`';
      } else if (latestLocalArchives) {
        variantHint.textContent = 'not built locally — `scripts/ctfvm image build-local --variant ' + variant + '`';
      } else {
        variantHint.textContent = '';
      }
      if (!latestLocalArchives) {
        statusEl.textContent = '';
        return;
      }
      const parts = [];
      for (const v of ['lean', 'full']) {
        const e = archiveEntry(v);
        if (e && e.present) {
          parts.push(v + ' ✓' + (e.size ? ' ' + formatBytes(e.size) : ''));
        } else {
          parts.push(v + ' ✗');
        }
      }
      if (legacy && legacy.present) {
        parts.push('legacy ✓ (fallback)');
      }
      const useChecked = !!(useLocal && useLocal.checked);
      const hasFallback = legacy && legacy.present;
      const missing = useChecked && !(entry && entry.present) && !hasFallback;
      statusEl.textContent = 'Local archives: ' + parts.join(' • ');
      statusEl.classList.toggle('warn', missing);
    }

    function applyCachedFlagFormat() {
      const input = document.getElementById('spawnFlagFormat');
      if (!input) return;
      const cached = window.localStorage.getItem(FLAG_FORMAT_STORAGE_KEY) || '';
      if (cached && !input.value.trim()) {
        input.value = cached;
      }
    }

    function persistFlagFormat() {
      const input = document.getElementById('spawnFlagFormat');
      if (!input) return;
      const value = input.value.trim();
      if (value) window.localStorage.setItem(FLAG_FORMAT_STORAGE_KEY, value);
      else window.localStorage.removeItem(FLAG_FORMAT_STORAGE_KEY);
    }

    function applyCachedAgent(fallback) {
      const select = document.getElementById('spawnAgent');
      if (!select) return;
      const cached = window.localStorage.getItem(AGENT_STORAGE_KEY) || '';
      const desired = SUPPORTED_AGENTS.includes(cached)
        ? cached
        : (SUPPORTED_AGENTS.includes(fallback) ? fallback : 'codex');
      select.value = desired;
    }

    function persistAgent() {
      const select = document.getElementById('spawnAgent');
      if (!select) return;
      const value = SUPPORTED_AGENTS.includes(select.value) ? select.value : 'codex';
      window.localStorage.setItem(AGENT_STORAGE_KEY, value);
    }

    function syncSpawnProviderFields() {
      const provider = document.getElementById('spawnProvider').value || 'gcp';
      document.getElementById('spawnProjectField').style.display = provider === 'gcp' ? '' : 'none';
      document.getElementById('spawnMachineField').style.display = provider === 'gcp' ? '' : 'none';
      document.getElementById('spawnSizeField').style.display = provider === 'digitalocean' ? '' : 'none';
      const zoneLabel = document.querySelector('label[for=\"spawnZone\"]');
      if (zoneLabel) zoneLabel.textContent = provider === 'gcp' ? 'Zone' : 'Region';
    }

    function syncSpawnModeFields() {
      const batchMode = (document.getElementById('spawnBatchMode').value || 'single') === 'batch';
      const dirLabel = document.getElementById('spawnChallengeDirLabel');
      const dirInput = document.getElementById('spawnChallengeDir');
      const desc = document.getElementById('spawnDesc');
      const ideas = document.getElementById('spawnIdeas');
      const hint = document.getElementById('spawnBatchHint');
      if (dirLabel) dirLabel.textContent = batchMode ? 'Challenges Root Directory' : 'Challenge Directory';
      if (dirInput) dirInput.placeholder = batchMode ? './challenges-root or /absolute/path' : './challenge or /absolute/path';
      if (desc) desc.disabled = batchMode;
      if (ideas) ideas.disabled = batchMode;
      if (desc) desc.placeholder = batchMode
        ? 'Batch mode ignores this field and reads description.txt in each child directory.'
        : 'Problem statement, notable files, service hints, constraints...';
      if (ideas) ideas.placeholder = batchMode
        ? 'Batch mode ignores this field and reads ideas.txt in each child directory.'
        : 'Optional starting hypotheses or exploit directions.';
      if (hint) hint.style.display = batchMode ? '' : 'none';
    }

    function collectSpawnPayload() {
      const provider = document.getElementById('spawnProvider').value || 'gcp';
      const batchMode = (document.getElementById('spawnBatchMode').value || 'single') === 'batch';
      const agentRaw = (document.getElementById('spawnAgent').value || 'codex').toLowerCase();
      const agent = SUPPORTED_AGENTS.includes(agentRaw) ? agentRaw : 'codex';
      return {
        provider,
        agent,
        batch_mode: batchMode,
        challenge_dir: document.getElementById('spawnChallengeDir').value.trim(),
        flag_format: document.getElementById('spawnFlagFormat').value.trim(),
        desc: batchMode ? '' : document.getElementById('spawnDesc').value.trim(),
        ideas: batchMode ? '' : document.getElementById('spawnIdeas').value.trim(),
        timeout_min: document.getElementById('spawnTimeout').value.trim(),
        toolbox_variant: document.getElementById('spawnVariant').value || 'lean',
        zone: document.getElementById('spawnZone').value.trim(),
        project: provider === 'gcp' ? document.getElementById('spawnProject').value.trim() : '',
        machine_type: provider === 'gcp' ? document.getElementById('spawnMachineType').value.trim() : '',
        size_slug: provider === 'digitalocean' ? document.getElementById('spawnSizeSlug').value.trim() : '',
        use_local_image: !!document.getElementById('spawnUseLocalImage').checked,
        no_auth_sync: !!document.getElementById('spawnNoAuthSync').checked,
      };
    }

    async function browseChallengeDir() {
      const batchMode = (document.getElementById('spawnBatchMode').value || 'single') === 'batch';
      const currentPath = document.getElementById('spawnChallengeDir').value.trim();
      setStatus('spawnStatus', batchMode ? 'Opening Finder for challenges root...' : 'Opening Finder...');
      try {
        const r = await fetch('/api/select-directory', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({
            current_path: currentPath,
            batch_mode: batchMode,
          }),
        });
        const d = await r.json();
        if (!d.ok) {
          setStatus('spawnStatus', d.canceled ? 'Directory selection canceled.' : (d.error || 'Directory selection failed.'), d.canceled ? '' : 'warn');
          return;
        }
        document.getElementById('spawnChallengeDir').value = d.path || '';
        if (!batchMode && d.description_text) {
          document.getElementById('spawnDesc').value = d.description_text;
        }
        setStatus('spawnStatus', batchMode ? 'Challenges root selected.' : 'Challenge directory selected.', 'ok');
      } catch (e) {
        setStatus('spawnStatus', 'Directory selection request failed.', 'warn');
      }
    }

    async function spawnRun() {
      const payload = collectSpawnPayload();
      persistFlagFormat();
      if (!payload.challenge_dir) {
        setStatus('spawnStatus', payload.batch_mode ? 'Challenges root directory is required.' : 'Challenge directory is required.', 'warn');
        return;
      }
      setStatus('spawnStatus', payload.batch_mode ? 'Starting batch provisioning jobs...' : 'Starting VM provisioning job...');
      try {
        const r = await fetch('/api/spawn', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify(payload)
        });
        const d = await r.json();
        if (!d.ok) {
          setStatus('spawnStatus', d.error || 'VM start failed.', 'warn');
          return;
        }
        if (d.mode === 'batch') {
          setStatus('spawnStatus', `Started ${d.count} provisioning jobs.`, 'ok');
        } else {
          setStatus('spawnStatus', `Provisioning job ${d.job_id} started.`, 'ok');
        }
        await refreshOverview(true);
      } catch (e) {
        setStatus('spawnStatus', 'Provisioning request failed.', 'warn');
      }
    }

    function renderJobs(jobs) {
      const holder = document.getElementById('spawnJobs');
      const summary = document.getElementById('jobSummary');
      holder.innerHTML = '';
      const list = Array.isArray(jobs) ? jobs : [];
      if (!list.length) {
        holder.innerHTML = '<div class=\"empty\">No provisioning jobs started from this UI session.</div>';
        summary.textContent = 'No jobs yet';
        return;
      }
      const running = list.filter(job => job.status === 'running').length;
      summary.textContent = `${list.length} job(s) • ${running} active`;
      list.forEach(job => {
        const tone = pillTone(job.status);
        const summaryBits = [];
        if (job.summary && job.summary.challenge_name) summaryBits.push(job.summary.challenge_name);
        if (job.summary && job.summary.provider) summaryBits.push(job.summary.provider);
        if (job.summary && job.summary.agent) summaryBits.push(job.summary.agent);
        if (job.summary && job.summary.zone) summaryBits.push(job.summary.zone);
        if (job.instance) summaryBits.push(job.instance);
        if (job.summary && job.summary.challenge_dir) summaryBits.push(job.summary.challenge_dir);
        const selector = job.run_selector || job.run_id || '';
        const openRun = selector
          ? `<a class=\"button-link secondary\" href=\"/run?run_id=${encodeURIComponent(selector)}\">Open Run</a>`
          : '';
        holder.insertAdjacentHTML('beforeend', `
          <div class=\"list-card\">
            <div class=\"list-top\">
              <div>
                <h3 class=\"list-title\">Job ${escapeHtml(job.job_id)}</h3>
                <div class=\"list-meta\">${escapeHtml(summaryBits.join(' • '))}</div>
              </div>
              <span class=\"pill ${tone}\">${escapeHtml(job.status)}</span>
            </div>
            <div class=\"list-meta\">${escapeHtml(job.started_at || '')}${job.finished_at ? ' → ' + escapeHtml(job.finished_at) : ''}</div>
            <div class=\"action-row\">${openRun}</div>
            <pre>${escapeHtml(job.output || '(waiting for output)')}</pre>
          </div>
        `);
      });
    }

    function renderRuns(runs, currentRunId) {
      const holder = document.getElementById('runCards');
      const summary = document.getElementById('fleetSummary');
      holder.innerHTML = '';
      const list = Array.isArray(runs) ? runs : [];
      if (!list.length) {
        holder.innerHTML = '<div class=\"empty\">No runs discovered yet.</div>';
        summary.textContent = 'No runs discovered';
        return;
      }
      const activeCount = list.filter(run => run.is_runtime_active).length;
      summary.textContent = `${list.length} run(s) • ${activeCount} active`;
      list.forEach(run => {
        const status = run.runtime_status || 'UNKNOWN';
        const tone = pillTone(status);
        const selector = run.run_key || run.run_id || '';
        holder.insertAdjacentHTML('beforeend', `
          <div class=\"list-card\">
            <div class=\"list-top\">
              <div>
                <h3 class=\"list-title\">${escapeHtml(run.instance || 'unknown')}</h3>
                <div class=\"list-meta\">${escapeHtml(run.run_id || '-')}</div>
              </div>
              <span class=\"pill ${tone}\">${escapeHtml(status)}</span>
            </div>
            <div class=\"list-meta\">${escapeHtml(run.provider || '-')} • ${escapeHtml(run.zone || '-')}</div>
            <div class=\"list-meta\">${escapeHtml(run.project || '-')}</div>
            <div class=\"list-meta\">${escapeHtml(run.ip || 'no IP recorded')}</div>
            <div class=\"action-row\">
              <a class=\"button-link secondary\" href=\"/run?run_id=${encodeURIComponent(selector)}\">Open Monitor</a>
            </div>
            <div class=\"list-meta\">${run.run_key === currentRunId ? 'current local state' : 'tracked run'}</div>
          </div>
        `);
      });
    }

    function trimTail(value, maxChars=MAX_OVERVIEW_JOB_OUTPUT_CHARS) {
      const text = String(value || '');
      if (text.length <= maxChars) return text;
      return text.slice(-maxChars);
    }

    function buildOverviewCachePayload(payload) {
      const runs = (Array.isArray(payload && payload.runs) ? payload.runs : []).map(run => ({
        run_key: run.run_key || '',
        run_id: run.run_id || '',
        instance: run.instance || '',
        runtime_status: run.runtime_status || '',
        is_runtime_active: !!run.is_runtime_active,
        provider: run.provider || '',
        zone: run.zone || '',
        project: run.project || '',
        ip: run.ip || '',
      }));
      const spawnJobs = (Array.isArray(payload && payload.spawn_jobs) ? payload.spawn_jobs : []).map(job => ({
        job_id: job.job_id || '',
        status: job.status || '',
        started_at: job.started_at || '',
        finished_at: job.finished_at || '',
        run_id: job.run_id || '',
        run_selector: job.run_selector || '',
        instance: job.instance || '',
        output: trimTail(job.output || ''),
        summary: {
          challenge_name: (job.summary && job.summary.challenge_name) || '',
          provider: (job.summary && job.summary.provider) || '',
          agent: (job.summary && job.summary.agent) || '',
          zone: (job.summary && job.summary.zone) || '',
          challenge_dir: (job.summary && job.summary.challenge_dir) || '',
        },
      }));
      return {
        cached_at: Date.now(),
        current_run_id: payload && payload.current_run_id || '',
        spawn_defaults: payload && payload.spawn_defaults || {},
        runs,
        spawn_jobs: spawnJobs,
      };
    }

    function persistOverviewCache(payload) {
      try {
        window.localStorage.setItem(
          OVERVIEW_CACHE_STORAGE_KEY,
          JSON.stringify(buildOverviewCachePayload(payload || {}))
        );
      } catch (e) {
        console.warn('overview cache write failed', e);
      }
    }

    function loadOverviewCache() {
      try {
        const raw = window.localStorage.getItem(OVERVIEW_CACHE_STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' ? parsed : null;
      } catch (e) {
        console.warn('overview cache read failed', e);
        return null;
      }
    }

    function hydrateOverviewFromCache() {
      const cached = loadOverviewCache();
      if (!cached) return false;
      applySpawnDefaults(cached.spawn_defaults || {}, { source: 'cache' });
      const runsSignature = buildRunsSignature(cached.runs || [], cached.current_run_id || '');
      renderRuns(cached.runs || [], cached.current_run_id || '');
      lastRunsSignature = runsSignature;
      const jobsSignature = buildJobsSignature(cached.spawn_jobs || []);
      renderJobs(cached.spawn_jobs || []);
      lastJobsSignature = jobsSignature;
      return true;
    }

    function buildRunsSignature(runs, currentRunId) {
      return JSON.stringify({
        currentRunId: currentRunId || '',
        runs: (Array.isArray(runs) ? runs : []).map(run => [
          run.run_key || '',
          run.run_id || '',
          run.instance || '',
          run.runtime_status || '',
          !!run.is_runtime_active,
          run.provider || '',
          run.zone || '',
          run.project || '',
          run.ip || '',
        ]),
      });
    }

    function buildJobsSignature(jobs) {
      return JSON.stringify(
        (Array.isArray(jobs) ? jobs : []).map(job => [
          job.job_id || '',
          job.status || '',
          job.run_id || '',
          job.run_selector || '',
          job.instance || '',
          job.started_at || '',
          job.finished_at || '',
          job.output || '',
        ])
      );
    }

    async function refreshOverview(force=false) {
      if (overviewRefreshInFlight) return;
      overviewRefreshInFlight = true;
      try {
        const query = force ? '?force=1' : '';
        const r = await fetch('/api/overview' + query, { cache: 'no-store' });
        const d = await r.json();
        applySpawnDefaults(d.spawn_defaults || {}, { force: defaultsHydratedFromCache, source: 'network' });
        defaultsHydratedFromCache = false;
        if (d.spawn_defaults && d.spawn_defaults.local_archives) {
          latestLocalArchives = d.spawn_defaults.local_archives;
          updateLocalArchiveHint();
        }
        const runsSignature = buildRunsSignature(d.runs || [], d.current_run_id || '');
        if (runsSignature !== lastRunsSignature) {
          renderRuns(d.runs || [], d.current_run_id || '');
          lastRunsSignature = runsSignature;
        }
        const jobsSignature = buildJobsSignature(d.spawn_jobs || []);
        if (jobsSignature !== lastJobsSignature) {
          renderJobs(d.spawn_jobs || []);
          lastJobsSignature = jobsSignature;
        }
        persistOverviewCache(d);
      } catch (e) {
        console.error('overview refresh failed', e);
      } finally {
        overviewRefreshInFlight = false;
      }
    }

    syncSpawnModeFields();
    syncSpawnProviderFields();
    applyCachedFlagFormat();
    document.getElementById('spawnFlagFormat').addEventListener('input', persistFlagFormat);
    hydrateOverviewFromCache();
    refreshOverview(true);
    """
    return render_page("ctfvm overview", "overview", body, script)


def render_run_page(initial_run_id: str) -> str:
    body = """
    <section class="card micro">
      <div class="card-body">
        <div class="topbar-grid">
          <div style="display:grid;gap:8px;">
            <div class="eyebrow">Run Monitor</div>
            <div class="toolbar micro">
              <div class="field" style="min-width:260px;flex:1 1 280px;">
                <label for="runSelect">Selected Run</label>
                <select id="runSelect" onchange="onRunChange()"></select>
              </div>
              <button class="secondary" onclick="refreshNow()">Refresh</button>
              <button class="secondary" onclick="refreshArtifacts()">Refresh Artifacts</button>
              <button class="ghost" onclick="collapseAllPanes()">Hide Panes</button>
              <button class="ghost" onclick="expandAllPanes()">Show Panes</button>
              <a id="bundleDownload" class="button-link secondary disabled" href="#" download>Bundle</a>
            </div>
            <div id="meta" class="hint">Loading run data...</div>
          </div>
          <div style="display:grid;gap:8px;">
            <div id="challengeBanner" class="banner compact unknown">
              <div class="banner-head">
                <h3 class="banner-title">No run selected</h3>
                <span class="pill unknown">Idle</span>
              </div>
              <p class="banner-copy">Choose a run to inspect its current execution state.</p>
            </div>
            <div class="action-row tight">
              <div class="inline-field" style="flex:1 1 220px;">
                <label for="statusNote">Explicit Status Note</label>
                <input id="statusNote" placeholder="Optional note" />
              </div>
              <button class="secondary" onclick="markExplicitStatus('solved')">Solved</button>
              <button class="secondary" onclick="markExplicitStatus('blocked')">Blocked</button>
              <button class="ghost" onclick="clearExplicitStatus()">Clear</button>
            </div>
            <div id="selectedRunMeta" class="meta-inline"></div>
            <span id="statusMarkerFeedback" class="status-text"></span>
          </div>
        </div>
      </div>
    </section>

    <section class="mission-grid">
      <div class="card">
        <div class="card-head">
          <h2>Live Output</h2>
          <span class="mono-muted">focused pane</span>
        </div>
        <div class="card-body stream-shell">
          <div id="paneTabs" class="tab-strip"></div>
          <div class="stream-frame">
            <div class="stream-head">
              <div>
                <h3 id="focusedPaneTitle" class="stream-title">No pane selected</h3>
                <div id="focusedPaneMeta" class="stream-copy">Choose a tmux pane to focus the stream.</div>
              </div>
              <div class="closed-panes" id="closedPanes"></div>
            </div>
            <pre id="focusedPaneOutput"></pre>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>Run Control</h2>
          <span id="sendStatus" class="status-text"></span>
        </div>
        <div class="card-body">
          <div class="field">
            <label for="targetSelect">TMUX Target</label>
            <select id="targetSelect" onchange="onTargetModeChange()"></select>
            <input id="targetCustom" style="display:none;" placeholder="custom session:window" />
          </div>
          <div class="field">
            <label for="chatmsg">Direct Message</label>
            <input id="chatmsg" placeholder="Immediate input to the selected pane" />
          </div>
          <div class="action-row">
            <button class="primary" onclick="sendChat()">Send</button>
            <button class="secondary" onclick="acceptTrust()">Accept Trust</button>
            <button class="secondary" onclick="sendCtrlC()">Ctrl-C</button>
            <button class="secondary" onclick="submitEnter()">Enter</button>
          </div>
          <div class="field">
            <label for="windows">Window Inventory</label>
            <pre id="windows"></pre>
          </div>
        </div>
      </div>
    </section>

    <section class="findings-split">
      <div class="card">
        <div class="card-head">
          <h2>Findings</h2>
          <span class="mono-muted">tail</span>
        </div>
        <div class="card-body">
          <pre id="findings_tail"></pre>
        </div>
      </div>
      <div class="card">
        <div class="card-head">
          <h2>Supervisor Log</h2>
          <span class="mono-muted">tail</span>
        </div>
        <div class="card-body">
          <pre id="supervisor_tail"></pre>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Pane Mirrors</h2>
        <span class="mono-muted">all tmux windows</span>
      </div>
      <div class="card-body">
        <div id="panes" class="pane-grid"></div>
      </div>
    </section>

    <section class="grid-two">
      <div class="card">
        <div class="card-head">
          <h2>Artifacts</h2>
          <span id="artifactListSummary" class="mono-muted">No files</span>
        </div>
        <div class="card-body">
          <div id="artifactsList" class="list-grid"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head">
          <h2>Artifact Preview</h2>
          <span id="artifactMeta" class="mono-muted"></span>
        </div>
        <div class="card-body">
          <img id="artifactImage" alt="artifact preview" />
          <pre id="artifactPreview"></pre>
        </div>
      </div>
    </section>
    """
    script = """
    const initialRunId = __INITIAL_RUN_ID__;
    let selectedRunId = initialRunId || '';
    let selectedArtifact = null;
    let refreshInFlight = false;
    let refreshCounter = 0;
    let latestWindowsList = [];
    let focusedPaneTarget = '';
    const followState = {};
    const paneCollapsedState = {};

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function setStatus(id, msg, tone='') {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = msg || '';
      el.className = 'status-text' + (tone ? (' ' + tone) : '');
    }

    function challengeTone(state) {
      const tone = String((state && state.state) || 'unknown').toLowerCase();
      if (['solved', 'progressing', 'stalled', 'halted', 'stopped', 'blocked'].includes(tone)) return tone;
      return 'unknown';
    }

    function isNearBottom(el, threshold=24) {
      if (!el) return true;
      return (el.scrollTop + el.clientHeight) >= (el.scrollHeight - threshold);
    }

    function bindFollowTracking(el, key) {
      if (!el || !key) return;
      if (!el.dataset.followBound) {
        el.addEventListener('scroll', () => {
          followState[key] = isNearBottom(el);
        }, { passive: true });
        el.dataset.followBound = '1';
      }
    }

    function stickBottom(el) {
      if (!el) return;
      el.scrollTop = el.scrollHeight;
    }

    function setPreAndStickBottom(id, value) {
      const el = document.getElementById(id);
      if (!el) return;
      const nextText = value || '';
      if (el.textContent === nextText) return;
      const key = `static:${id}`;
      bindFollowTracking(el, key);
      const follow = followState[key] !== false ? isNearBottom(el) : followState[key];
      const prevTop = el.scrollTop;
      el.textContent = nextText;
      requestAnimationFrame(() => {
        if (follow) {
          stickBottom(el);
        } else {
          el.scrollTop = prevTop;
        }
      });
    }

    function paneCollapsed(target) {
      return paneCollapsedState[target] === true;
    }

    function paneCardIdFromTarget(target) {
      const safe = String(target || '').replace(/[^A-Za-z0-9_.-]/g, '_');
      return `pane-card-${safe}`;
    }

    function ensureFocusedPane(windowsList) {
      const list = Array.isArray(windowsList) ? windowsList : [];
      const known = new Set(list.map(w => w.target || `${w.session}:${w.index}`));
      if (focusedPaneTarget && known.has(focusedPaneTarget)) return;
      const active = list.find(w => !!w.active);
      const fallback = active || list[0];
      focusedPaneTarget = fallback ? (fallback.target || `${fallback.session}:${fallback.index}`) : '';
    }

    function selectPaneTarget(target, syncSelect=true) {
      focusedPaneTarget = target || '';
      if (syncSelect) {
        const sel = document.getElementById('targetSelect');
        if (sel) {
          const options = Array.from(sel.options || []).map(opt => opt.value);
          if (focusedPaneTarget && options.includes(focusedPaneTarget)) {
            sel.value = focusedPaneTarget;
          }
        }
      }
      highlightSelectedPane();
      renderPaneTabs(latestWindowsList);
      renderFocusedPane(latestWindowsList);
    }

    function applyPaneCollapsedUI(card, target, toggleBtn) {
      const collapsed = paneCollapsed(target);
      if (card) card.classList.toggle('pane-collapsed', collapsed);
      if (toggleBtn) toggleBtn.textContent = collapsed ? 'Open Output' : 'Hide Output';
    }

    function setPaneCollapsed(target, collapsed) {
      if (!target) return;
      paneCollapsedState[target] = !!collapsed;
      const card = document.getElementById(paneCardIdFromTarget(target));
      if (card) {
        const toggleBtn = card.querySelector('.pane-toggle');
        applyPaneCollapsedUI(card, target, toggleBtn);
      }
      renderClosedPanes(latestWindowsList);
    }

    function collapseAllPanes() {
      document.querySelectorAll('[data-pane-card=\"1\"]').forEach(card => {
        const target = card.dataset.paneTarget || '';
        if (target) setPaneCollapsed(target, true);
      });
    }

    function expandAllPanes() {
      document.querySelectorAll('[data-pane-card=\"1\"]').forEach(card => {
        const target = card.dataset.paneTarget || '';
        if (target) setPaneCollapsed(target, false);
      });
    }

    function renderClosedPanes(windowsList) {
      const holder = document.getElementById('closedPanes');
      if (!holder) return;
      holder.innerHTML = '';
      const list = Array.isArray(windowsList) ? windowsList : [];
      const closed = list.filter(w => paneCollapsed(w.target || `${w.session}:${w.index}`));
      if (!closed.length) return;
      holder.insertAdjacentHTML('beforeend', '<span class=\"mono-muted\">Hidden:</span>');
      closed.forEach(w => {
        const target = w.target || `${w.session}:${w.index}`;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'ghost';
        btn.textContent = target;
        btn.onclick = () => setPaneCollapsed(target, false);
        holder.appendChild(btn);
      });
    }

    function syncRunOptions(runs, currentRunId, snapshotRunId) {
      const sel = document.getElementById('runSelect');
      const prev = selectedRunId || sel.value || '';
      sel.innerHTML = '';
      sel.appendChild(new Option('Select run', ''));
      const list = Array.isArray(runs) ? runs : [];
      list.forEach(run => {
        sel.appendChild(new Option(run.instance || run.run_id || 'unknown', run.run_key || run.run_id || ''));
      });
      const available = Array.from(sel.options).map(o => o.value);
      const target = [snapshotRunId || '', prev, currentRunId || ''].find(v => v && available.includes(v));
      sel.value = target || '';
      selectedRunId = sel.value || '';
      updateRunLinks();
    }

    function updateRunLinks() {
      const bundle = document.getElementById('bundleDownload');
      if (bundle) {
        if (selectedRunId) {
          bundle.href = '/api/run-bundle?run_id=' + encodeURIComponent(selectedRunId);
          bundle.classList.remove('disabled');
        } else {
          bundle.href = '#';
          bundle.classList.add('disabled');
        }
      }
    }

    function onRunChange() {
      selectedRunId = document.getElementById('runSelect').value || '';
      selectedArtifact = null;
      updateRunLinks();
      refreshNow();
      refreshArtifacts();
    }

    function onTargetModeChange() {
      const sel = document.getElementById('targetSelect');
      const custom = document.getElementById('targetCustom');
      custom.style.display = sel.value === '__custom__' ? 'block' : 'none';
      if (sel.value && sel.value !== '__custom__') {
        focusedPaneTarget = sel.value;
      }
      highlightSelectedPane();
      renderPaneTabs(latestWindowsList);
      renderFocusedPane(latestWindowsList);
    }

    function selectedPaneTargetFromTarget() {
      const sel = document.getElementById('targetSelect');
      const custom = document.getElementById('targetCustom');
      let target = sel.value || '';
      if (target === '__custom__') target = (custom.value || '').trim();
      return /^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$/.test(target) ? target : null;
    }

    function highlightSelectedPane() {
      document.querySelectorAll('[data-pane-card=\"1\"]').forEach(card => card.classList.remove('selected-pane'));
      const target = selectedPaneTargetFromTarget() || focusedPaneTarget;
      if (!target) return;
      const card = document.getElementById(paneCardIdFromTarget(target));
      if (card) card.classList.add('selected-pane');
    }

    function resolveTarget() {
      const sel = document.getElementById('targetSelect');
      const custom = document.getElementById('targetCustom');
      return sel.value === '__custom__' ? ((custom.value || '').trim() || 'ctf:supervisor') : (sel.value || 'ctf:supervisor');
    }

    function syncTargetOptions(windowsList) {
      const sel = document.getElementById('targetSelect');
      const previous = sel.value || 'ctf:supervisor';
      sel.innerHTML = '';
      const list = Array.isArray(windowsList) ? windowsList : [];
      if (list.length) {
        list.forEach(w => sel.appendChild(new Option(`${w.active ? '* ' : ''}${w.name} (${w.target})`, w.target || `${w.session}:${w.index}`)));
      } else {
        sel.appendChild(new Option('supervisor (ctf:supervisor)', 'ctf:supervisor'));
      }
      sel.appendChild(new Option('Custom target...', '__custom__'));
      const available = Array.from(sel.options).map(o => o.value);
      sel.value = available.includes(previous) ? previous : sel.options[0].value;
      if (sel.value && sel.value !== '__custom__') focusedPaneTarget = sel.value;
      onTargetModeChange();
    }

    function renderPaneTabs(windowsList) {
      const holder = document.getElementById('paneTabs');
      if (!holder) return;
      holder.innerHTML = '';
      const list = Array.isArray(windowsList) ? windowsList : [];
      if (!list.length) {
        holder.innerHTML = '<span class=\"mono-muted\">No tmux panes detected.</span>';
        return;
      }
      ensureFocusedPane(list);
      list.forEach(w => {
        const target = w.target || `${w.session}:${w.index}`;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'tab-chip' + (target === focusedPaneTarget ? ' active' : '');
        button.textContent = `${w.active ? '* ' : ''}${w.name}`;
        button.onclick = () => selectPaneTarget(target);
        holder.appendChild(button);
      });
    }

    function renderFocusedPane(windowsList) {
      const title = document.getElementById('focusedPaneTitle');
      const meta = document.getElementById('focusedPaneMeta');
      const output = document.getElementById('focusedPaneOutput');
      if (!title || !meta || !output) return;
      const list = Array.isArray(windowsList) ? windowsList : [];
      ensureFocusedPane(list);
      const entry = list.find(w => (w.target || `${w.session}:${w.index}`) === focusedPaneTarget) || list[0];
      if (!entry) {
        title.textContent = 'No pane selected';
        meta.textContent = 'No tmux panes are currently visible for this run.';
        setPreAndStickBottom('focusedPaneOutput', '');
        return;
      }
      const target = entry.target || `${entry.session}:${entry.index}`;
      title.textContent = entry.name || target;
      meta.textContent = `${target} • ${entry.active ? 'active window' : 'tracked window'}`;
      focusedPaneTarget = target;
      setPreAndStickBottom('focusedPaneOutput', entry.output || '');
    }

    function renderChallengeBanner(state, snapshot) {
      const holder = document.getElementById('challengeBanner');
      const tone = challengeTone(state);
      const label = (state && state.label) || 'Unknown';
      const summary = (state && state.summary) || 'No challenge status available.';
      const detail = (state && state.detail) || '';
      holder.className = `banner ${tone}`;
      holder.innerHTML = `
        <div class=\"banner-head\">
          <h3 class=\"banner-title\">${escapeHtml(label)}</h3>
          <span class=\"pill ${tone}\">${escapeHtml(label)}</span>
        </div>
        <p class=\"banner-copy\">${escapeHtml(summary)}</p>
        ${detail ? `<p class=\"banner-copy\">${escapeHtml(detail)}</p>` : ''}
      `;
      const meta = document.getElementById('selectedRunMeta');
      if (!snapshot || !snapshot.run_id) {
        meta.innerHTML = '';
        return;
      }
      const metrics = snapshot.metrics || {};
      const bits = [
        `<strong>Run</strong> ${escapeHtml(snapshot.run_id || '-')}`,
        `<strong>Provider</strong> ${escapeHtml(snapshot.provider || '-')}`,
        `<strong>Runtime</strong> ${escapeHtml(snapshot.status || '-')}`,
        `<strong>Location</strong> ${escapeHtml(snapshot.zone || '-')}`,
        `<strong>IP</strong> ${escapeHtml(snapshot.ip || '-')}`,
        `<strong>Artifacts</strong> ${escapeHtml(String(metrics.artifact_count || '0'))}`,
        `<strong>Project</strong> ${escapeHtml(snapshot.project || '-')}`
      ];
      meta.innerHTML = bits.join('<span class="sep">•</span>');
      const explicitStatus = snapshot.explicit_status || {};
      const noteInput = document.getElementById('statusNote');
      if (noteInput) noteInput.value = explicitStatus.note || '';
    }

    function renderPanes(list) {
      const panes = document.getElementById('panes');
      const windowsList = Array.isArray(list) ? list : [];
      latestWindowsList = windowsList;
      renderPaneTabs(windowsList);
      renderClosedPanes(windowsList);
      const expectedIds = new Set(windowsList.map(w => paneCardIdFromTarget(w.target || `${w.session}:${w.index}`)));
      panes.querySelectorAll('[data-pane-card=\"1\"]').forEach(card => {
        if (!expectedIds.has(card.id)) card.remove();
      });
      if (!windowsList.length) {
        panes.innerHTML = '<div class=\"empty\">No tmux windows detected for the selected run.</div>';
        renderFocusedPane([]);
        return;
      }
      if (panes.querySelector('.empty')) panes.innerHTML = '';
      windowsList.forEach(w => {
        const target = w.target || `${w.session}:${w.index}`;
        const paneKey = `pane:${target}`;
        const cardId = paneCardIdFromTarget(target);
        let card = document.getElementById(cardId);
        if (!card) {
          card = document.createElement('div');
          card.className = 'pane-card';
          card.id = cardId;
          card.dataset.paneCard = '1';
          card.dataset.paneTarget = target;
          card.innerHTML = `
            <div class=\"pane-top\">
              <div>
                <h3 class=\"pane-title\"></h3>
                <div class=\"mono-muted pane-target\"></div>
              </div>
              <button class=\"ghost pane-toggle\" type=\"button\"></button>
            </div>
            <pre></pre>
          `;
          panes.appendChild(card);
        }
        const title = card.querySelector('.pane-title');
        const targetEl = card.querySelector('.pane-target');
        const toggleBtn = card.querySelector('.pane-toggle');
        const pre = card.querySelector('pre');
        card.dataset.paneTarget = target;
        card.onclick = event => {
          if (event.target && event.target.closest('.pane-toggle')) return;
          selectPaneTarget(target);
        };
        title.textContent = `${w.active ? '* ' : ''}${w.name}`;
        targetEl.textContent = target;
        toggleBtn.onclick = () => setPaneCollapsed(target, !paneCollapsed(target));
        applyPaneCollapsedUI(card, target, toggleBtn);
        bindFollowTracking(pre, paneKey);
        const nextOutput = w.output || '';
        if (pre.textContent !== nextOutput) {
          const follow = followState[paneKey] !== false ? isNearBottom(pre) : followState[paneKey];
          const prevTop = pre.scrollTop;
          pre.textContent = nextOutput;
          requestAnimationFrame(() => {
            if (follow) stickBottom(pre);
            else pre.scrollTop = prevTop;
          });
        }
      });
      renderFocusedPane(windowsList);
      highlightSelectedPane();
    }

    async function postJson(url, payload) {
      const r = await fetch(url, {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(payload || {})
      });
      return r.json();
    }

    function artifactDownloadHref(path) {
      const runQuery = selectedRunId ? ('&run_id=' + encodeURIComponent(selectedRunId)) : '';
      return '/api/artifact/download?path=' + encodeURIComponent(path) + runQuery;
    }

    function renderArtifactsList(raw) {
      const holder = document.getElementById('artifactsList');
      const summary = document.getElementById('artifactListSummary');
      if (!holder || !summary) return;
      holder.innerHTML = '';
      const lines = String(raw || '').split('\\n').map(s => s.trim()).filter(Boolean);
      summary.textContent = `${lines.length} file(s)`;
      if (!lines.length) {
        holder.innerHTML = '<div class=\"empty\">No artifact files yet.</div>';
        const pre = document.getElementById('artifactPreview');
        const meta = document.getElementById('artifactMeta');
        const img = document.getElementById('artifactImage');
        if (pre) pre.textContent = 'No artifact selected.';
        if (meta) meta.textContent = '';
        if (img) {
          img.style.display = 'none';
          img.src = '';
        }
        return;
      }
      let shouldLoadPreview = false;
      lines.forEach(path => {
        holder.insertAdjacentHTML('beforeend', `
          <div class=\"list-card artifact-item${selectedArtifact === path ? ' selected' : ''}\" onclick='previewArtifact(${JSON.stringify(path)})'>
            <div class=\"list-top\">
              <div>
                <h3 class=\"list-title\">${escapeHtml(path)}</h3>
                <div class=\"list-meta\">${escapeHtml(path.split('/').slice(0, -1).join('/') || 'artifacts')}</div>
              </div>
              <a class=\"button-link ghost\" href=\"${artifactDownloadHref(path)}\" download onclick=\"event.stopPropagation()\">Download</a>
            </div>
          </div>
        `);
      });
      if (!selectedArtifact || !lines.includes(selectedArtifact)) {
        selectedArtifact = lines[0];
        shouldLoadPreview = true;
      }
      if (shouldLoadPreview) loadArtifactPreview(selectedArtifact);
    }

    async function loadArtifactPreview(path) {
      const pre = document.getElementById('artifactPreview');
      const meta = document.getElementById('artifactMeta');
      const img = document.getElementById('artifactImage');
      if (!pre || !meta || !img) return;
      if (!path) {
        pre.textContent = 'No artifact selected.';
        meta.textContent = '';
        img.style.display = 'none';
        img.src = '';
        return;
      }
      pre.style.display = 'block';
      pre.textContent = 'Loading...';
      meta.textContent = '';
      const runQuery = selectedRunId ? ('&run_id=' + encodeURIComponent(selectedRunId)) : '';
      const r = await fetch('/api/artifact?path=' + encodeURIComponent(path) + runQuery, { cache: 'no-store' });
      const d = await r.json();
      if (!d.ok) {
        img.style.display = 'none';
        img.src = '';
        pre.style.display = 'block';
        pre.textContent = 'Error: ' + (d.error || 'failed to load');
        return;
      }
      meta.textContent = `${d.mime} • ${d.size} bytes${d.truncated ? ' • truncated preview' : ''}`;
      if (d.is_image && d.image_data_url) {
        img.src = d.image_data_url;
        img.style.display = 'block';
        pre.style.display = 'none';
        pre.textContent = '';
      } else {
        img.style.display = 'none';
        img.src = '';
        pre.style.display = 'block';
        pre.textContent = d.content || '';
      }
    }

    function previewArtifact(path) {
      selectedArtifact = path;
      loadArtifactPreview(path);
      refreshArtifacts();
    }

    async function refreshArtifacts() {
      try {
        const query = selectedRunId ? ('?run_id=' + encodeURIComponent(selectedRunId)) : '';
        const r = await fetch('/api/artifacts-index' + query, { cache: 'no-store' });
        const d = await r.json();
        renderArtifactsList(d.artifacts || '');
      } catch (e) {
        const pre = document.getElementById('artifactPreview');
        if (pre) pre.textContent = 'Artifact refresh failed: ' + String(e);
      }
    }

    async function markExplicitStatus(state) {
      if (!selectedRunId) return setStatus('statusMarkerFeedback', 'Select a run first.', 'warn');
      const note = (document.getElementById('statusNote') || {}).value || '';
      try {
        const d = await postJson('/api/status-marker', { run_id: selectedRunId, state, note });
        setStatus('statusMarkerFeedback', d.ok ? `Marked ${state}.` : ('Error: ' + (d.error || 'failed')), d.ok ? 'ok' : 'warn');
      } catch (e) {
        setStatus('statusMarkerFeedback', 'Status update failed.', 'warn');
      }
      refreshNow();
    }

    async function clearExplicitStatus() {
      if (!selectedRunId) return setStatus('statusMarkerFeedback', 'Select a run first.', 'warn');
      try {
        const d = await postJson('/api/status-marker/clear', { run_id: selectedRunId });
        setStatus('statusMarkerFeedback', d.ok ? 'Marker cleared.' : ('Error: ' + (d.error || 'failed')), d.ok ? 'ok' : 'warn');
      } catch (e) {
        setStatus('statusMarkerFeedback', 'Status clear failed.', 'warn');
      }
      refreshNow();
    }

    async function sendChat() {
      if (!selectedRunId) return setStatus('sendStatus', 'Select a run first.', 'warn');
      const input = document.getElementById('chatmsg');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      setStatus('sendStatus', 'Sending...');
      try {
        const d = await postJson('/api/send', { run_id: selectedRunId, target: resolveTarget(), text, enter: true });
        setStatus('sendStatus', d.ok ? 'Sent.' : ('Error: ' + (d.error || 'failed')), d.ok ? 'ok' : 'warn');
      } catch (e) {
        input.value = text;
        setStatus('sendStatus', 'Send failed.', 'warn');
      }
      refreshNow();
    }

    async function acceptTrust() {
      if (!selectedRunId) return setStatus('sendStatus', 'Select a run first.', 'warn');
      const d = await postJson('/api/trust', { run_id: selectedRunId, target: resolveTarget() });
      setStatus('sendStatus', d.ok ? 'Trust accepted.' : ('Error: ' + (d.error || 'failed')), d.ok ? 'ok' : 'warn');
      refreshNow();
    }

    async function sendCtrlC() {
      if (!selectedRunId) return setStatus('sendStatus', 'Select a run first.', 'warn');
      const d = await postJson('/api/key', { run_id: selectedRunId, target: resolveTarget(), keys: ['C-c'] });
      setStatus('sendStatus', d.ok ? 'Sent Ctrl-C.' : ('Error: ' + (d.error || 'failed')), d.ok ? 'ok' : 'warn');
      refreshNow();
    }

    async function submitEnter() {
      if (!selectedRunId) return setStatus('sendStatus', 'Select a run first.', 'warn');
      const d = await postJson('/api/key', { run_id: selectedRunId, target: resolveTarget(), keys: ['C-m'] });
      setStatus('sendStatus', d.ok ? 'Sent Enter.' : ('Error: ' + (d.error || 'failed')), d.ok ? 'ok' : 'warn');
      refreshNow();
    }

    async function refreshNow() {
      if (refreshInFlight) return;
      refreshInFlight = true;
      const refreshId = ++refreshCounter;
      try {
        const query = selectedRunId ? ('?run_id=' + encodeURIComponent(selectedRunId)) : '';
        const r = await fetch('/api/run-detail' + query, { cache: 'no-store' });
        const d = await r.json();
        syncRunOptions(d.runs || [], d.current_run_id || '', d.selected_run_id || '');
        renderChallengeBanner(d.challenge_state || null, d);
        ['windows', 'findings_tail', 'supervisor_tail'].forEach(key => setPreAndStickBottom(key, d[key] || ''));
        syncTargetOptions(d.windows_list || []);
        renderPanes(d.windows_list || []);
        const meta = document.getElementById('meta');
        if (meta) {
          meta.textContent = d.error
            ? d.error
            : `${d.instance || 'No run selected'} • ${d.status || '-'} • ${d.zone || '-'} • ${d.ip || '-'} • refresh #${refreshId}`;
        }
      } catch (e) {
        const meta = document.getElementById('meta');
        if (meta) meta.textContent = `Run refresh failed: ${String(e)}`;
      } finally {
        refreshInFlight = false;
      }
    }

    document.getElementById('chatmsg').addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        sendChat();
      }
    });
    document.getElementById('targetCustom').addEventListener('input', highlightSelectedPane);

    updateRunLinks();
    refreshNow();
    refreshArtifacts();
    """
    return render_page(
        "ctfvm run monitor",
        "run",
        body,
        script.replace("__INITIAL_RUN_ID__", json.dumps(initial_run_id or "")),
    )


def render_artifacts_page(initial_run_id: str) -> str:
    body = """
    <section class="hero">
      <div class="card">
        <div class="card-body">
          <div class="eyebrow">Artifacts</div>
          <h1 class="headline">Browse and download outputs without opening the live monitor.</h1>
          <p class="subline">This page only refreshes artifact listings and previews for the selected run.</p>
          <div class="toolbar">
            <div class="field" style="min-width:260px;flex:1 1 280px;">
              <label for="runSelect">Selected Run</label>
              <select id="runSelect" onchange="onRunChange()"></select>
            </div>
            <button class="secondary" onclick="refreshArtifacts()">Refresh</button>
            <a id="runPageLink" class="button-link secondary disabled" href="#">Open Monitor</a>
            <a id="bundleDownload" class="button-link secondary disabled" href="#" download>Download Run Bundle</a>
          </div>
          <div id="artifactMetaTop" class="hint">Loading artifact data...</div>
        </div>
      </div>
      <div class="card">
        <div class="card-head">
          <h2>Selected Run</h2>
          <span id="artifactSummary" class="mono-muted">No files</span>
        </div>
        <div class="card-body">
          <div id="selectedRunMeta" class="meta-grid"></div>
        </div>
      </div>
    </section>

    <section class="grid-two">
      <div class="card">
        <div class="card-head">
          <h2>Artifact Files</h2>
          <span id="artifactListSummary" class="mono-muted">No files</span>
        </div>
        <div class="card-body">
          <div id="artifactsList" class="list-grid"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head">
          <h2>Preview</h2>
          <span id="artifactMeta" class="mono-muted"></span>
        </div>
        <div class="card-body">
          <img id="artifactImage" alt="artifact preview" />
          <pre id="artifactPreview"></pre>
        </div>
      </div>
    </section>
    """
    script = """
    const initialRunId = __INITIAL_RUN_ID__;
    let selectedRunId = initialRunId || '';
    let selectedArtifact = null;

    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function syncRunOptions(runs, currentRunId, snapshotRunId) {
      const sel = document.getElementById('runSelect');
      const prev = selectedRunId || sel.value || '';
      sel.innerHTML = '';
      sel.appendChild(new Option('Select run', ''));
      (Array.isArray(runs) ? runs : []).forEach(run => {
        sel.appendChild(new Option(run.instance || run.run_id || 'unknown', run.run_key || run.run_id || ''));
      });
      const available = Array.from(sel.options).map(o => o.value);
      const target = [snapshotRunId || '', prev, currentRunId || ''].find(v => v && available.includes(v));
      sel.value = target || '';
      selectedRunId = sel.value || '';
      updateLinks();
    }

    function updateLinks() {
      const runPage = document.getElementById('runPageLink');
      const bundle = document.getElementById('bundleDownload');
      if (selectedRunId) {
        runPage.href = '/run?run_id=' + encodeURIComponent(selectedRunId);
        runPage.classList.remove('disabled');
        bundle.href = '/api/run-bundle?run_id=' + encodeURIComponent(selectedRunId);
        bundle.classList.remove('disabled');
      } else {
        runPage.href = '#';
        runPage.classList.add('disabled');
        bundle.href = '#';
        bundle.classList.add('disabled');
      }
    }

    function onRunChange() {
      selectedRunId = document.getElementById('runSelect').value || '';
      selectedArtifact = null;
      updateLinks();
      refreshArtifacts();
    }

    function artifactDownloadHref(path) {
      const runQuery = selectedRunId ? ('&run_id=' + encodeURIComponent(selectedRunId)) : '';
      return '/api/artifact/download?path=' + encodeURIComponent(path) + runQuery;
    }

    function renderArtifactsList(raw) {
      const holder = document.getElementById('artifactsList');
      const summary = document.getElementById('artifactListSummary');
      holder.innerHTML = '';
      const lines = String(raw || '').split('\\n').map(s => s.trim()).filter(Boolean);
      summary.textContent = `${lines.length} file(s)`;
      if (!lines.length) {
        holder.innerHTML = '<div class=\"empty\">No artifact files yet.</div>';
        return;
      }
      lines.forEach(path => {
        holder.insertAdjacentHTML('beforeend', `
          <div class=\"list-card${selectedArtifact === path ? ' selected' : ''}\">
            <div class=\"list-top\">
              <div>
                <h3 class=\"list-title\">${escapeHtml(path)}</h3>
                <div class=\"list-meta\">${escapeHtml(path.split('/').slice(0, -1).join('/') || 'artifacts')}</div>
              </div>
            </div>
            <div class=\"action-row\">
              <button class=\"secondary\" onclick=\"previewArtifact(${JSON.stringify(path)})\">Preview</button>
              <a class=\"button-link secondary\" href=\"${artifactDownloadHref(path)}\" download>Download</a>
            </div>
          </div>
        `);
      });
      if (!selectedArtifact || !lines.includes(selectedArtifact)) selectedArtifact = lines[0];
      loadArtifactPreview(selectedArtifact);
    }

    function renderRunMeta(data) {
      const meta = document.getElementById('selectedRunMeta');
      const metrics = data.metrics || {};
      meta.innerHTML = data.run_id ? `
        <div><strong>Run</strong>${escapeHtml(data.run_id || '-')}</div>
        <div><strong>Provider</strong>${escapeHtml(data.provider || '-')}</div>
        <div><strong>Runtime</strong>${escapeHtml(data.status || '-')}</div>
        <div><strong>Instance</strong>${escapeHtml(data.instance || '-')}</div>
        <div><strong>Location</strong>${escapeHtml(data.zone || '-')}</div>
        <div><strong>IP</strong>${escapeHtml(data.ip || '-')}</div>
        <div><strong>Project</strong>${escapeHtml(data.project || '-')}</div>
        <div><strong>Artifacts</strong>${escapeHtml(String(metrics.artifact_count || '0'))}</div>
      ` : '';
      const top = document.getElementById('artifactMetaTop');
      top.textContent = data.error
        ? data.error
        : `${data.instance || 'No run selected'} • ${data.status || '-'} • ${metrics.artifact_count || 0} file(s)`;
      document.getElementById('artifactSummary').textContent = metrics.artifact_count
        ? `${metrics.artifact_count} file(s)`
        : 'No files';
    }

    async function loadArtifactPreview(path) {
      const pre = document.getElementById('artifactPreview');
      const meta = document.getElementById('artifactMeta');
      const img = document.getElementById('artifactImage');
      if (!path) {
        pre.textContent = 'No artifact selected.';
        meta.textContent = '';
        img.style.display = 'none';
        img.src = '';
        return;
      }
      pre.style.display = 'block';
      pre.textContent = 'Loading...';
      meta.textContent = '';
      const runQuery = selectedRunId ? ('&run_id=' + encodeURIComponent(selectedRunId)) : '';
      const r = await fetch('/api/artifact?path=' + encodeURIComponent(path) + runQuery, { cache: 'no-store' });
      const d = await r.json();
      if (!d.ok) {
        img.style.display = 'none';
        img.src = '';
        pre.textContent = 'Error: ' + (d.error || 'failed to load');
        return;
      }
      meta.textContent = `${d.mime} • ${d.size} bytes${d.truncated ? ' • truncated preview' : ''}`;
      if (d.is_image && d.image_data_url) {
        img.src = d.image_data_url;
        img.style.display = 'block';
        pre.style.display = 'none';
        pre.textContent = '';
      } else {
        img.style.display = 'none';
        img.src = '';
        pre.style.display = 'block';
        pre.textContent = d.content || '';
      }
    }

    function previewArtifact(path) {
      selectedArtifact = path;
      loadArtifactPreview(path);
      refreshArtifacts();
    }

    async function refreshArtifacts() {
      try {
        const query = selectedRunId ? ('?run_id=' + encodeURIComponent(selectedRunId)) : '';
        const r = await fetch('/api/artifacts-index' + query, { cache: 'no-store' });
        const d = await r.json();
        syncRunOptions(d.runs || [], d.current_run_id || '', d.selected_run_id || '');
        renderRunMeta(d);
        renderArtifactsList(d.artifacts || '');
      } catch (e) {
        document.getElementById('artifactMetaTop').textContent = `Artifact refresh failed: ${String(e)}`;
      }
    }

    updateLinks();
    refreshArtifacts();
    """
    return render_page(
        "ctfvm artifacts",
        "artifacts",
        body,
        script.replace("__INITIAL_RUN_ID__", json.dumps(initial_run_id or "")),
    )


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content):
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type, filename, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query or "")
        run_id = (params.get("run_id") or [""])[0]

        if parsed.path == "/":
            self._send_html(render_overview_page())
            return

        if parsed.path == "/run":
            self._send_html(render_run_page(run_id))
            return

        if parsed.path == "/artifacts":
            self._send_html(render_run_page(run_id))
            return

        if parsed.path == "/api/run-detail":
            self._send_json(SERVICE.get_run_detail(run_id=run_id))
            return

        if parsed.path == "/api/artifacts-index":
            self._send_json(SERVICE.get_artifacts_index(run_id=run_id))
            return

        if parsed.path == "/api/run-bundle":
            bundle = SERVICE.get_run_bundle_download(run_id=run_id)
            if not bundle.get("ok"):
                self._send_json(bundle, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_bytes(
                bundle.get("content", b""),
                bundle.get("mime", "application/gzip"),
                bundle.get("filename", "ctfvm-bundle.tar.gz"),
            )
            return

        if parsed.path == "/api/artifact/download":
            relpath = (params.get("path") or [""])[0]
            artifact = SERVICE.get_artifact_download(relpath, run_id=run_id)
            if not artifact.get("ok"):
                self._send_json(artifact, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_bytes(
                artifact.get("content", b""),
                artifact.get("mime", "application/octet-stream"),
                artifact.get("filename", "artifact.bin"),
            )
            return

        if parsed.path == "/api/artifact":
            relpath = (params.get("path") or [""])[0]
            preview = SERVICE.get_artifact_preview(relpath, run_id=run_id)
            self._send_json(preview, status=HTTPStatus.OK if preview.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/api/snapshot":
            self._send_json(SERVICE.get_snapshot(run_id=run_id))
            return

        if parsed.path == "/api/overview":
            force = ((params.get("force") or [""])[0] or "").strip() in {"1", "true", "yes"}
            self._send_json(SERVICE.get_overview(force=force))
            return

        if parsed.path == "/api/runs":
            runs, current_run_id = SERVICE.list_runs(include_status=True)
            self._send_json({"runs": runs, "current_run_id": current_run_id})
            return

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path == "/api/spawn":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception:
                self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
                return
            result = SERVICE.start_run(payload)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/select-directory":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                current_path = str(payload.get("current_path", "")).strip()
                batch_mode = bool(payload.get("batch_mode"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
                return
            result = SERVICE.choose_local_directory(current_path=current_path, batch_mode=batch_mode)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") or result.get("canceled") else HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/status-marker":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                run_id = str(payload.get("run_id", "")).strip()
                state = str(payload.get("state", "")).strip()
                note = str(payload.get("note", "")).strip()
            except Exception:
                self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
                return
            result = SERVICE.set_explicit_status(run_id, state, note=note)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/status-marker/clear":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                run_id = str(payload.get("run_id", "")).strip()
            except Exception:
                self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
                return
            result = SERVICE.clear_explicit_status(run_id)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/send":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                text = str(payload.get("text", "")).strip()
                target = str(payload.get("target", "ctf:supervisor")).strip() or "ctf:supervisor"
                enter = bool(payload.get("enter", True))
                run_id = str(payload.get("run_id", "")).strip()
            except Exception:
                self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
                return
            result = SERVICE.send_to_tmux(run_id, target, text, enter=enter)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/key":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                target = str(payload.get("target", "ctf:supervisor")).strip() or "ctf:supervisor"
                keys = payload.get("keys", [])
                if not isinstance(keys, list):
                    raise ValueError("keys must be a list")
                keys = [str(key).strip() for key in keys if str(key).strip()]
                run_id = str(payload.get("run_id", "")).strip()
            except Exception:
                self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
                return
            result = SERVICE.send_keys_tmux(run_id, target, keys)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/trust":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                run_id = str(payload.get("run_id", "")).strip()
                target = str(payload.get("target", "ctf:supervisor")).strip() or "ctf:supervisor"
            except Exception:
                run_id = ""
                target = "ctf:supervisor"
            result = SERVICE.trust_prompt(run_id, target=target)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format, *args):
        return


def main():
    parser = argparse.ArgumentParser(description="Local web UI for ctfvm runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ctfvm ui listening on http://{args.host}:{args.port}")
    print("Overview: /   Run monitor: /run   Artifacts: /artifacts")
    httpd.serve_forever()
