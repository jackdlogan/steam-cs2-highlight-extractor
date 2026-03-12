<script>
  import { onMount } from 'svelte'
  import { open as openDialog } from '@tauri-apps/plugin-dialog'
  import { getStatus, getDefaults, getSessions, stopExport, openOutput, streamPost } from './lib/api.js'

  // ── Server / init state ────────────────────────────────────────────
  let serverOk    = false
  let ffmpegFound = false
  let serverError = ''

  // ── Config ─────────────────────────────────────────────────────────
  let recordingPath = ''
  let outputFolder  = ''
  let padBefore     = 10
  let padAfter      = 5
  let preShift      = 3
  let mergeWindow   = 45
  let extractKills  = true
  let extractDeaths = false
  let doMerge       = false

  // ── Sessions ───────────────────────────────────────────────────────
  let sessions         = []
  let selectedSessions = new Set()

  // ── Kill feed ──────────────────────────────────────────────────────
  let groups         = []
  let selectedGroups = new Set()  // by out_name

  // ── Progress / logs ────────────────────────────────────────────────
  let phase    = 'idle'  // idle | scanning | results | exporting | done
  let progress = 0
  let logs     = []
  let stopped  = false

  // ── Derived ────────────────────────────────────────────────────────
  $: selectedGroupList = groups.filter(g => selectedGroups.has(g.out_name))
  $: selectedCount     = selectedGroups.size
  $: multiKillCount    = groups.filter(g => g.tag !== 'KILL' && g.tag !== 'DEATH').length
  $: busy              = phase === 'scanning' || phase === 'exporting'

  function config() {
    return {
      recording_path: recordingPath,
      output_folder:  outputFolder,
      pad_before:     padBefore,
      pad_after:      padAfter,
      pre_shift:      preShift,
      merge_window:   mergeWindow,
      extract_kills:  extractKills,
      extract_deaths: extractDeaths,
    }
  }

  // ── Lifecycle ──────────────────────────────────────────────────────
  onMount(async () => {
    await new Promise(r => setTimeout(r, 800))
    await connectToServer()
  })

  async function connectToServer() {
    for (let attempt = 0; attempt < 10; attempt++) {
      try {
        const status = await getStatus()
        serverOk    = status.ok
        ffmpegFound = status.ffmpeg_found
        if (serverOk) {
          await loadDefaults()
          return
        }
      } catch { /* not ready */ }
      await new Promise(r => setTimeout(r, 600))
    }
    serverError = 'Could not connect to backend. Start server.py manually.'
  }

  async function loadDefaults() {
    try {
      const d = await getDefaults()
      recordingPath = d.recording_path || ''
      outputFolder  = d.output_folder  || ''
      padBefore     = d.pad_before     ?? 10
      padAfter      = d.pad_after      ?? 5
      preShift      = d.pre_shift      ?? 3
      mergeWindow   = d.merge_window   ?? 45
    } catch { /* ignore */ }
    await refreshSessions()
  }

  async function refreshSessions() {
    try {
      const data = await getSessions(recordingPath)
      if (data.recording_path) recordingPath = data.recording_path
      sessions = data.sessions || []
      if (sessions.length > 0 && selectedSessions.size === 0) {
        selectedSessions = new Set([sessions[sessions.length - 1]])
        onScan()
      }
    } catch { /* ignore */ }
  }

  // ── Session selection ──────────────────────────────────────────────
  function toggleSession(name) {
    if (selectedSessions.has(name)) selectedSessions.delete(name)
    else selectedSessions.add(name)
    selectedSessions = selectedSessions
    onScan()
  }

  function selectAllSessions() {
    selectedSessions = new Set(sessions)
    onScan()
  }

  function clearSessionSelection() {
    selectedSessions = new Set()
  }

  // ── Group selection ────────────────────────────────────────────────
  function toggleGroup(outName) {
    if (selectedGroups.has(outName)) selectedGroups.delete(outName)
    else selectedGroups.add(outName)
    selectedGroups = selectedGroups
  }

  function selectAllGroups() {
    selectedGroups = new Set(groups.map(g => g.out_name))
  }

  function clearGroupSelection() {
    selectedGroups = new Set()
  }

  // ── Scan ───────────────────────────────────────────────────────────
  async function onScan() {
    if (selectedSessions.size === 0) return
    phase    = 'scanning'
    progress = 0
    logs     = []
    groups   = []
    selectedGroups = new Set()
    stopped  = false

    const newGroups = []

    await streamPost('/api/scan', {
      session_names: [...selectedSessions],
      config:        config(),
    }, (event) => {
      if (event.type === 'log') {
        logs = [...logs, { text: event.text, level: event.level }]
      } else if (event.type === 'group') {
        newGroups.push(event.data)
        groups = [...newGroups]
        selectedGroups = new Set(newGroups.map(g => g.out_name))
      } else if (event.type === 'progress') {
        progress = event.value
      } else if (event.type === 'done') {
        phase    = 'results'
        progress = 1
      } else if (event.type === 'error') {
        logs  = [...logs, { text: 'Error: ' + event.message, level: 'err' }]
        phase = 'results'
      }
    })
  }

  // ── Export ─────────────────────────────────────────────────────────
  async function onExport() {
    if (selectedCount === 0) return
    phase    = 'exporting'
    progress = 0
    logs     = []
    stopped  = false

    await streamPost('/api/export', {
      groups: selectedGroupList,
      config: config(),
      merge:  doMerge,
    }, (event) => {
      if (event.type === 'log') {
        logs = [...logs, { text: event.text, level: event.level }]
        const el = document.getElementById('log-panel')
        if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
      } else if (event.type === 'progress') {
        progress = event.value
      } else if (event.type === 'done') {
        stopped  = event.stopped
        phase    = 'done'
        progress = 1
      } else if (event.type === 'error') {
        logs  = [...logs, { text: 'Error: ' + event.message, level: 'err' }]
        phase = 'done'
      }
    })
  }

  async function onStop() {
    await stopExport()
  }

  async function onOpenFolder() {
    await openOutput(outputFolder)
  }

  async function browseRecordingPath() {
    const selected = await openDialog({ directory: true, multiple: false, title: 'Select Steam Recording Folder' })
    if (selected) {
      recordingPath = selected
      await refreshSessions()
    }
  }

  async function browseOutputFolder() {
    const selected = await openDialog({ directory: true, multiple: false, title: 'Select Output Folder' })
    if (selected) outputFolder = selected
  }

  function onReset() {
    phase    = 'results'
    logs     = []
    progress = 0
    stopped  = false
  }

  // ── Helpers ────────────────────────────────────────────────────────
  function badgeStyle(tag) {
    const map = {
      ACE:   { bg: '#f0c040', fg: '#000' },
      '4K':  { bg: '#e07840', fg: '#000' },
      '3K':  { bg: '#d4a030', fg: '#000' },
      '2K':  { bg: '#4f9eff', fg: '#000' },
      KILL:  { bg: '#ff6b6b', fg: '#000' },
      DEATH: { bg: '#94a3b8', fg: '#000' },
    }
    const s = map[tag] || { bg: '#2a3347', fg: '#e2e8f0' }
    return `background:${s.bg};color:${s.fg}`
  }

  function levelClass(level) {
    return { ok: 'log-ok', warn: 'log-warn', err: 'log-err', info: 'log-info' }[level] || ''
  }

  function fmtDuration(secs) {
    if (!secs) return '--'
    const m = Math.floor(secs / 60)
    const s = Math.round(secs % 60)
    return m > 0 ? `${m}m ${s}s` : `${s}s`
  }
</script>

<!-- ── Header ──────────────────────────────────────────────────────── -->
<header>
  <div class="header-title">
    <span class="header-icon">🎮</span>
    <span>Steam Highlight Extractor</span>
  </div>
  <div class="header-status">
    {#if !serverOk}
      <span class="dot dot-red"></span>
      <span class="status-text">{serverError || 'Connecting…'}</span>
    {:else if !ffmpegFound}
      <span class="dot dot-yellow"></span>
      <span class="status-text">ffmpeg not found — run: winget install Gyan.FFmpeg</span>
    {:else}
      <span class="dot dot-green"></span>
      <span class="status-text">Ready</span>
    {/if}
  </div>
</header>

<!-- ── Body ───────────────────────────────────────────────────────── -->
<div class="body">

  <!-- Sidebar -->
  <aside class="sidebar">

    <div class="section-label">Recording Path</div>
    <div class="sidebar-pad">
      <div class="path-row">
        <input
          type="text"
          bind:value={recordingPath}
          placeholder="Auto-detect…"
          on:change={refreshSessions}
        />
        <button class="browse-btn" on:click={browseRecordingPath} title="Browse folder">📁</button>
      </div>
    </div>

    <div class="section-label" style="margin-top:8px">
      Sessions
      <div class="row-actions">
        <button class="micro" on:click={selectAllSessions}>All</button>
        <button class="micro" on:click={clearSessionSelection}>None</button>
        <button class="micro" on:click={refreshSessions}>↻</button>
      </div>
    </div>
    <div class="session-list">
      {#if sessions.length === 0}
        <div class="empty-text">No sessions found</div>
      {:else}
        {#each sessions as name}
          <div
            class="session-row"
            class:selected={selectedSessions.has(name)}
            on:click={() => toggleSession(name)}
            on:keydown={e => e.key === 'Enter' && toggleSession(name)}
            role="option"
            aria-selected={selectedSessions.has(name)}
            tabindex="0"
          >
            <span class="session-dot" class:active={selectedSessions.has(name)}></span>
            <span class="session-name">{name}</span>
          </div>
        {/each}
      {/if}
    </div>

    <div class="sidebar-sep"></div>

    <div class="section-label">Settings</div>
    <div class="settings-grid">
      <label for="pad-before">Pad before (s)</label>
      <input id="pad-before" type="number" bind:value={padBefore} min="0" max="60" />

      <label for="pad-after">Pad after (s)</label>
      <input id="pad-after" type="number" bind:value={padAfter} min="0" max="60" />

      <label for="pre-shift">Kill pre-shift (s)</label>
      <input id="pre-shift" type="number" bind:value={preShift} min="0" max="10" />

      <label for="merge-win">Merge window (s)</label>
      <input id="merge-win" type="number" bind:value={mergeWindow} min="0" max="120" />
    </div>

    <div class="sidebar-sep"></div>

    <div class="section-label">Extract</div>
    <div class="check-row">
      <input type="checkbox" id="chk-kills" bind:checked={extractKills} />
      <label for="chk-kills">Kills</label>
    </div>
    <div class="check-row">
      <input type="checkbox" id="chk-deaths" bind:checked={extractDeaths} />
      <label for="chk-deaths">Deaths</label>
    </div>

    <div class="sidebar-sep"></div>
    <div class="section-label">Output Folder</div>
    <div class="sidebar-pad">
      <div class="path-row">
        <input type="text" bind:value={outputFolder} placeholder="SteamHighlights/" />
        <button class="browse-btn" on:click={browseOutputFolder} title="Browse folder">📁</button>
      </div>
    </div>

  </aside>

  <!-- Main area -->
  <main class="main">

    {#if phase === 'idle'}
      <div class="empty-state">
        <div class="empty-icon">🎬</div>
        <div class="empty-title">No highlights scanned yet</div>
        <div class="empty-sub">Select sessions on the left, then click <strong>Scan</strong></div>
      </div>

    {:else if phase === 'scanning'}
      <div class="scanning-state">
        <div class="scanning-label">Scanning sessions…</div>
        {#if groups.length > 0}
          <div class="scanning-sub">{groups.length} highlight{groups.length !== 1 ? 's' : ''} found so far</div>
        {/if}
        <div class="scan-logs">
          {#each logs as log}
            <div class="log-line {levelClass(log.level)}">{log.text}</div>
          {/each}
        </div>
      </div>

    {:else if phase === 'results'}
      <div class="results-header">
        <div class="results-title">
          Kill Feed
          {#if groups.length > 0}
            <span class="badge" style="background:var(--surface2);color:var(--muted);margin-left:6px">{groups.length}</span>
          {/if}
          {#if multiKillCount > 0}
            <span class="badge" style="background:var(--blue-dim);color:var(--blue);margin-left:4px">{multiKillCount} multi</span>
          {/if}
        </div>
        <div class="row-actions">
          <span class="muted-label">{selectedCount} selected</span>
          <button class="micro" on:click={selectAllGroups}>All</button>
          <button class="micro" on:click={clearGroupSelection}>None</button>
        </div>
      </div>

      {#if groups.length === 0}
        <div class="empty-state" style="flex:1">
          <div class="empty-icon">🔍</div>
          <div class="empty-title">No highlights found</div>
          <div class="empty-sub">Try enabling Deaths or adjusting settings</div>
        </div>
      {:else}
        <div class="kill-feed">
          {#each groups as group}
            {@const checked = selectedGroups.has(group.out_name)}
            <div
              class="feed-row"
              class:feed-row-checked={checked}
              on:click={() => toggleGroup(group.out_name)}
              on:keydown={e => e.key === 'Enter' && toggleGroup(group.out_name)}
              role="option"
              aria-selected={checked}
              tabindex="0"
            >
              <div class="feed-accent" class:feed-accent-on={checked}></div>
              <input
                type="checkbox"
                checked={checked}
                class="feed-check"
                on:click|stopPropagation={() => toggleGroup(group.out_name)}
              />
              <span class="badge" style={badgeStyle(group.tag)}>{group.tag}</span>
              <div class="feed-info">
                <div class="feed-name">{group.out_name}</div>
                <div class="feed-meta">{group.ts_label} · {fmtDuration(group.clip_duration)}</div>
              </div>
            </div>
          {/each}
        </div>
      {/if}

    {:else if phase === 'exporting' || phase === 'done'}
      <div class="log-header">
        <span>Export Log</span>
        {#if phase === 'done'}
          <button class="micro" on:click={onReset}>← Back to results</button>
        {/if}
      </div>
      <div class="log-panel" id="log-panel">
        {#each logs as log}
          <div class="log-line {levelClass(log.level)}">{log.text}</div>
        {/each}
        {#if phase === 'exporting'}
          <div class="log-line log-muted">…</div>
        {/if}
      </div>
    {/if}

  </main>
</div>

<!-- ── Action bar ─────────────────────────────────────────────────── -->
<footer>
  <div class="footer-left">
    {#if phase === 'idle' || phase === 'results' || phase === 'done'}
      <button
        class="btn-ghost"
        on:click={onScan}
        disabled={busy || selectedSessions.size === 0}
        title="Re-scan selected sessions"
      >
        ↻ Refresh
      </button>
    {/if}

    {#if phase === 'results'}
      <button
        class="btn-success"
        on:click={onExport}
        disabled={busy || selectedCount === 0}
      >
        ▶ Export {selectedCount > 0 ? `(${selectedCount})` : ''}
      </button>
    {/if}

    {#if phase === 'exporting'}
      <button class="btn-danger" on:click={onStop}>⏹ Stop</button>
    {/if}

    {#if phase === 'done'}
      <button
        class="btn-success"
        on:click={onExport}
        disabled={selectedCount === 0}
      >
        ▶ Export again
      </button>
    {/if}

    <button class="btn-ghost" on:click={onOpenFolder}>📂 Open Folder</button>

    {#if phase === 'results' || phase === 'done'}
      <label class="merge-check">
        <input type="checkbox" bind:checked={doMerge} />
        Merge into one clip
      </label>
    {/if}
  </div>

  <div class="footer-right">
    {#if busy}
      <span class="progress-label">{Math.round(progress * 100)}%</span>
    {/if}
    {#if phase === 'done' && stopped}
      <span class="status-warn">Stopped</span>
    {:else if phase === 'done'}
      <span class="status-ok">✓ Done</span>
    {/if}
  </div>

  {#if busy || phase === 'done'}
    <div class="progress-track">
      <div
        class="progress-fill"
        class:progress-done={phase === 'done'}
        style="width:{progress * 100}%"
      ></div>
    </div>
  {/if}
</footer>

<style>
/* ── Layout ─────────────────────────────────────────────────────── */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 16px;
  height: 44px;
  flex-shrink: 0;
}

.header-title {
  font-size: 14px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}

.header-icon { font-size: 16px; }

.header-status {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11px;
  color: var(--muted);
  font-family: var(--font-mono);
}

.status-text { color: var(--muted); }

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-green  { background: var(--green); }
.dot-yellow { background: var(--yellow); }
.dot-red    { background: var(--red); }

.body {
  display: flex;
  flex: 1;
  overflow: hidden;
}

/* ── Sidebar ────────────────────────────────────────────────────── */
.sidebar {
  width: 235px;
  flex-shrink: 0;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: hidden;
}

.sidebar-pad {
  padding: 4px 10px 8px;
}

.path-row {
  display: flex;
  gap: 5px;
  align-items: center;
}

.path-row input {
  flex: 1;
  min-width: 0;
}

.browse-btn {
  flex-shrink: 0;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: 13px;
  padding: 3px 7px;
  cursor: pointer;
  line-height: 1;
}
.browse-btn:hover { background: var(--border); }

.sidebar-sep {
  height: 1px;
  background: var(--border);
  margin: 6px 0;
}

.section-label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  font-family: var(--font-mono);
  padding: 8px 10px 4px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.row-actions {
  display: flex;
  gap: 3px;
  align-items: center;
}

.session-list {
  overflow-y: auto;
  max-height: 200px;
}

.empty-text {
  padding: 10px;
  font-size: 11px;
  color: var(--muted);
  font-family: var(--font-mono);
}

.session-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px;
  cursor: pointer;
  border-left: 2px solid transparent;
  transition: background 0.1s;
  font-size: 11px;
  font-family: var(--font-mono);
}
.session-row:hover { background: var(--surface2); }
.session-row.selected {
  background: var(--blue-dim);
  border-left-color: var(--blue);
}

.session-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--border);
  flex-shrink: 0;
}
.session-dot.active { background: var(--blue); }

.session-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings-grid {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 5px 8px;
  padding: 4px 10px 8px;
  align-items: center;
}

.settings-grid input[type="number"] {
  width: 58px;
  text-align: right;
}

.check-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 3px 10px;
}

/* ── Main ───────────────────────────────────────────────────────── */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg);
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--muted);
}
.empty-icon  { font-size: 40px; }
.empty-title { font-size: 15px; font-weight: 600; color: var(--text); }
.empty-sub   { font-size: 12px; }

.scanning-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 20px;
  gap: 8px;
  overflow: hidden;
}
.scanning-label { font-size: 14px; font-weight: 600; }
.scanning-sub   { font-size: 12px; color: var(--muted); }
.scan-logs {
  flex: 1;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 11px;
}

/* Kill feed */
.results-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px 8px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.results-title {
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
}
.muted-label {
  font-size: 11px;
  color: var(--muted);
  font-family: var(--font-mono);
}

.kill-feed {
  flex: 1;
  overflow-y: auto;
}

.feed-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 14px 7px 0;
  cursor: pointer;
  border-bottom: 1px solid #1a2035;
  transition: background 0.1s;
  position: relative;
}
.feed-row:hover { background: var(--surface); }
.feed-row-checked { background: #0d1520; }

.feed-accent {
  width: 3px;
  align-self: stretch;
  background: transparent;
  transition: background 0.1s;
  flex-shrink: 0;
}
.feed-accent-on { background: var(--blue); }

.feed-check {
  margin-left: 10px;
  flex-shrink: 0;
}

.feed-info {
  flex: 1;
  min-width: 0;
}
.feed-name {
  font-size: 12px;
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.feed-meta {
  font-size: 10px;
  color: var(--muted);
  font-family: var(--font-mono);
  margin-top: 2px;
}

/* Log panel */
.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px 8px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
}

.log-panel {
  flex: 1;
  overflow-y: auto;
  padding: 8px 14px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
}

.log-line  { white-space: pre-wrap; word-break: break-word; }
.log-ok    { color: var(--green); }
.log-warn  { color: var(--yellow); }
.log-err   { color: var(--red); }
.log-info  { color: var(--blue); }
.log-muted { color: var(--muted); }

/* ── Footer ─────────────────────────────────────────────────────── */
footer {
  flex-shrink: 0;
  background: var(--surface);
  border-top: 1px solid var(--border);
  padding: 8px 14px;
  display: flex;
  align-items: center;
  gap: 8px;
  position: relative;
  min-height: 52px;
}

.footer-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  flex-wrap: wrap;
}

.footer-right {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--font-mono);
  font-size: 11px;
}

.progress-label { color: var(--muted); }
.status-ok   { color: var(--green); font-weight: 600; }
.status-warn { color: var(--yellow); font-weight: 600; }

.progress-track {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--surface2);
}
.progress-fill {
  height: 100%;
  background: var(--blue);
  transition: width 0.2s ease;
}
.progress-done { background: var(--green); }

/* ── Micro button ────────────────────────────────────────────────── */
.micro {
  background: var(--surface2);
  color: var(--muted);
  border: 1px solid var(--border);
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 3px;
  cursor: pointer;
  font-family: var(--font-mono);
}
.micro:hover { background: var(--border); color: var(--text); }

.merge-check {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--muted);
  font-family: var(--font-mono);
  cursor: pointer;
  user-select: none;
}
</style>
