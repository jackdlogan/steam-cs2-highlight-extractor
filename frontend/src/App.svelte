<script>
  import { onMount } from 'svelte'
  import { open as openDialog } from '@tauri-apps/plugin-dialog'
  import { convertFileSrc } from '@tauri-apps/api/core'
  import { tick } from 'svelte'
  import * as dashjs from 'dashjs'
  import { getStatus, getDefaults, getSessions, stopExport, openOutput, streamPost } from './lib/api.js'
  import killIconRaw  from './assets/kill-icon.svg?raw'
  import deathIconRaw from './assets/death-icon.svg?raw'

  // Build map code → icon URL from all files in assets/map-icon/
  // Filename examples: "32px-De_ancient.png", "32px-Map_icon_de_golden.png"
  const _mapIconModules = import.meta.glob('./assets/map-icon/*.png', { eager: true })
  const mapIcons = {}
  for (const [path, mod] of Object.entries(_mapIconModules)) {
    const filename = path.split('/').pop().replace(/\.png$/i, '')
    const code = filename
      .replace(/^32px-/i, '')
      .replace(/^Map_icon_/i, '')
      .toLowerCase()
    mapIcons[code] = mod.default
  }

  function prepIcon(raw, color, size = 12) {
    return raw
      .replace(/\s+width="[^"]*"/, ` width="${size}"`)
      .replace(/\s+height="[^"]*"/, ` height="${size}"`)
      .replace(/fill="#000000"/g, `fill="${color}"`)
  }

  const killIconGrey   = prepIcon(killIconRaw,  '#94a3b8', 11)
  const deathIconRed   = prepIcon(deathIconRaw, '#ff6b6b', 12)

  const killIconGreen = prepIcon(killIconRaw, '#00e5a0', 12)
  const killIconBlue  = prepIcon(killIconRaw, '#4f9eff', 12)
  const killIconRed   = prepIcon(killIconRaw, '#ff6b6b', 12)
  const killIconGold  = prepIcon(killIconRaw, '#f0c040', 12)

  function killIconForCount(n) {
    if (n >= 5) return killIconGold
    if (n >= 4) return killIconRed
    if (n >= 2) return killIconBlue
    return killIconGreen
  }

  // ── Server / init state ────────────────────────────────────────────
  let serverOk    = false
  let ffmpegFound = false
  let gpuEncoder  = null
  let serverError = ''

  // ── Config ─────────────────────────────────────────────────────────
  let recordingPath = ''
  let outputFolder  = ''
  let padBefore     = 1
  let padAfter      = 5
  let preShift      = 3
  let extractKills  = true
  let extractDeaths = false
  let doMerge       = false
  let workers       = 1
  let lastApplied   = null   // config snapshot from last scan

  // ── Sessions ───────────────────────────────────────────────────────
  let sessions         = []
  let selectedSessions = new Set()
  let groupCache       = {}  // session_name → group[]

  // ── Kill feed ──────────────────────────────────────────────────────
  let groups         = []
  let selectedGroups = new Set()  // by out_name
  let mapFilter      = 'all'
  let multiKillOnly  = false

  // ── Progress / logs ────────────────────────────────────────────────
  let phase          = 'idle'  // idle | scanning | results | exporting | done
  let progress       = 0
  let logs           = []
  let stopped        = false
  let exportTotal     = 0
  let currentClipName = ''
  let clipCards       = []  // [{name, tag, duration, thumbnail_url, status, size_mb, elapsed}]
  let showLog         = false

  // ── Video player ─────────────────────────────────────────────────
  let exportedPaths = new Set()  // out_path strings successfully exported
  let playerCard    = null       // card/group currently open in the player
  let playerSrc     = ''         // asset:// URL (mp4 mode) or manifest URL (dash mode)
  let playerMode    = 'mp4'      // 'mp4' | 'dash'
  let videoEl       = null
  let dashPlayer    = null
  let playerError   = ''
  let playerTrimLeft  = 0        // seconds to extend/trim before clip_start (+extend, -trim)
  let playerTrimRight = 0        // seconds to extend/trim after clip_end (+extend, -trim)
  let dashCurrentTime = 0        // absolute session currentTime for DASH
  let dashPaused      = true

  // ── Derived ────────────────────────────────────────────────────────
  // allMaps: array of { code, display } — code used for filtering, display shown in UI
  $: allMaps = [...new Map(
      groups.filter(g => g.map_name).map(g => [g.map_name, g.map_display || g.map_name])
    ).entries()].map(([code, display]) => ({ code, display })).sort((a, b) => a.display.localeCompare(b.display))
  $: filteredGroups = groups.filter(g => {
    if (mapFilter !== 'all' && g.map_name !== mapFilter) return false
    if (multiKillOnly && (g.kill_count || 0) < 2) return false
    return true
  })

  $: selectedGroupList = filteredGroups.filter(g => selectedGroups.has(g.out_name))
  $: selectedCount     = selectedGroupList.length
  $: multiKillCount    = groups.filter(g => (g.kill_count || 0) >= 2).length
  $: busy              = phase === 'scanning' || phase === 'exporting'
  $: currentClipNum    = exportTotal > 0 ? Math.min(Math.ceil(progress * exportTotal), exportTotal) : 0
  $: scanConfig        = { padBefore, padAfter, preShift, extractKills, extractDeaths }
  $: settingsDirty     = lastApplied !== null && JSON.stringify(scanConfig) !== JSON.stringify(lastApplied)

  // ── Player derived ─────────────────────────────────────────────
  $: pEffStart   = playerCard ? playerCard.clip_start    - playerTrimLeft  : 0
  $: pEffDur     = playerCard ? Math.max(0.5, playerCard.clip_duration + playerTrimLeft + playerTrimRight) : 1
  $: dashClipPct = pEffDur > 0 ? Math.max(0, Math.min(100, (dashCurrentTime - pEffStart) / pEffDur * 100)) : 0
  $: dashClipTime = Math.max(0, dashCurrentTime - pEffStart)

  function config() {
    return {
      recording_path: recordingPath,
      output_folder:  outputFolder,
      pad_before:     padBefore,
      pad_after:      padAfter,
      pre_shift:      preShift,
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
        gpuEncoder  = status.gpu_encoder || null
        workers     = gpuEncoder ? 3 : 1
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
      padBefore     = d.pad_before     ?? 1
      padAfter      = d.pad_after      ?? 5
      preShift      = d.pre_shift      ?? 3
      mergeWindow   = d.merge_window   ?? 45
    } catch { /* ignore */ }
    await refreshSessions()
  }

  async function refreshSessions() {
    // Path changed — cached groups belong to old sessions, discard them
    groupCache     = {}
    groups         = []
    selectedGroups = new Set()
    mapFilter      = 'all'
    multiKillOnly  = false
    try {
      const data = await getSessions(recordingPath)
      if (data.recording_path) recordingPath = data.recording_path
      sessions = data.sessions || []
      if (sessions.length > 0 && selectedSessions.size === 0) {
        selectedSessions = new Set([sessions[sessions.length - 1]])
        await scanNewSessions([sessions[sessions.length - 1]])
      }
    } catch { /* ignore */ }
  }

  // ── Session selection ──────────────────────────────────────────────
  function toggleSession(name) {
    if (selectedSessions.has(name)) {
      // Remove: instant, no backend call
      selectedSessions.delete(name)
      selectedSessions = selectedSessions
      rebuildGroupsFromCache()
    } else {
      selectedSessions.add(name)
      selectedSessions = selectedSessions
      if (groupCache[name] !== undefined) {
        // Already cached — show immediately, no backend call
        rebuildGroupsFromCache([name])
      } else {
        scanNewSessions([name])
      }
    }
  }

  function selectAllSessions() {
    selectedSessions = new Set(sessions)
    const toScan    = sessions.filter(s => groupCache[s] === undefined)
    const fromCache = sessions.filter(s => groupCache[s] !== undefined)
    if (toScan.length === 0) {
      rebuildGroupsFromCache(fromCache)
    } else {
      scanNewSessions(toScan, fromCache)
    }
  }

  function clearSessionSelection() {
    selectedSessions = new Set()
    rebuildGroupsFromCache()
  }

  // ── Group selection ────────────────────────────────────────────────
  function toggleGroup(outName) {
    if (selectedGroups.has(outName)) selectedGroups.delete(outName)
    else selectedGroups.add(outName)
    selectedGroups = selectedGroups
  }

  function selectAllGroups() {
    selectedGroups = new Set([...selectedGroups, ...filteredGroups.map(g => g.out_name)])
  }

  function clearGroupSelection() {
    const filteredNames = new Set(filteredGroups.map(g => g.out_name))
    selectedGroups = new Set([...selectedGroups].filter(n => !filteredNames.has(n)))
  }

  // ── Scan ───────────────────────────────────────────────────────────

  // Rebuild groups[] from cache for all selected sessions.
  // autoSelectSessions: names of sessions whose groups should be auto-selected.
  function rebuildGroupsFromCache(autoSelectSessions = []) {
    const newGroups = []
    for (const name of sessions) {
      if (selectedSessions.has(name) && groupCache[name]) {
        newGroups.push(...groupCache[name])
      }
    }
    groups = newGroups
    const present = new Set(newGroups.map(g => g.out_name))
    const kept    = new Set([...selectedGroups].filter(n => present.has(n)))
    for (const sn of autoSelectSessions) {
      for (const g of (groupCache[sn] || [])) kept.add(g.out_name)
    }
    selectedGroups = kept
    if (phase !== 'scanning') phase = newGroups.length > 0 ? 'results' : 'idle'
  }

  // Scan only the given sessions; autoSelectCached contains names already in cache
  // whose groups should be auto-selected once scan finishes.
  async function scanNewSessions(sessionNamesToScan, autoSelectCached = []) {
    phase    = 'scanning'
    progress = 0
    logs     = []
    stopped  = false

    // Pre-populate display with already-cached sessions
    rebuildGroupsFromCache(autoSelectCached)

    const scanResults = {}
    for (const name of sessionNamesToScan) scanResults[name] = []

    await streamPost('/api/scan', {
      session_names: sessionNamesToScan,
      config:        config(),
    }, (event) => {
      if (event.type === 'log') {
        logs = [...logs, { text: event.text, level: event.level }]
      } else if (event.type === 'group') {
        const g  = event.data
        const sn = g.session_name
        if (sn !== undefined && scanResults[sn] !== undefined) {
          scanResults[sn].push(g)
          groupCache[sn] = scanResults[sn]
          rebuildGroupsFromCache([sn])
        }
      } else if (event.type === 'progress') {
        progress = event.value
      } else if (event.type === 'done') {
        phase       = groups.length > 0 ? 'results' : 'idle'
        progress    = 1
        lastApplied = { ...scanConfig }
      } else if (event.type === 'error') {
        logs  = [...logs, { text: 'Error: ' + event.message, level: 'err' }]
        phase = 'results'
      }
    })
  }

  // Re-scan button: force fresh scan of all selected sessions
  async function onScan() {
    if (selectedSessions.size === 0) return
    for (const name of selectedSessions) delete groupCache[name]
    groups         = []
    selectedGroups = new Set()
    mapFilter      = 'all'
    multiKillOnly  = false
    await scanNewSessions([...selectedSessions])
  }

  // Apply settings: re-scan selected sessions with current config
  async function applySettings() {
    if (selectedSessions.size === 0 || busy) return
    await onScan()
  }

  // ── Export ─────────────────────────────────────────────────────────
  async function onExport() {
    if (selectedCount === 0) return
    phase           = 'exporting'
    progress        = 0
    logs            = []
    stopped         = false
    showLog         = false
    exportTotal     = selectedCount
    currentClipName = ''
    clipCards       = selectedGroupList.map(g => ({
      name:          g.out_name,
      tag:           g.tag,
      duration:      g.clip_duration,
      thumbnail_url: g.thumbnail_url || null,
      out_path:      g.out_path,
      events:        g.events      || [],
      clip_start:    g.clip_start  || 0,
      status:        'pending',
      size_mb:       0,
      elapsed:       0,
    }))

    await streamPost('/api/export', {
      groups:   selectedGroupList,
      config:   config(),
      merge:    doMerge,
      workers:  workers,
    }, (event) => {
      if (event.type === 'log') {
        logs = [...logs, { text: event.text, level: event.level }]
        const m = event.text.match(/\[\d+\/\d+\]\s+(\S+\.mp4)/)
        if (m) currentClipName = m[1]
        const el = document.getElementById('log-panel')
        if (el) requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
      } else if (event.type === 'clip_start') {
        clipCards = clipCards.map((c, i) =>
          i === event.index - 1 ? { ...c, status: 'active' } : c
        )
        currentClipName = event.name
      } else if (event.type === 'clip_done') {
        clipCards = clipCards.map((c, i) => {
          if (i !== event.index - 1) return c
          if ((event.status === 'ok' || event.status === 'skipped') && c.out_path) {
            exportedPaths = new Set([...exportedPaths, c.out_path])
          }
          return { ...c, status: event.status, size_mb: event.size_mb, elapsed: event.elapsed }
        })
        if (event.status === 'failed') showLog = true
      } else if (event.type === 'progress') {
        progress = event.value
      } else if (event.type === 'done') {
        stopped  = event.stopped
        phase    = 'done'
        progress = 1
      } else if (event.type === 'error') {
        logs     = [...logs, { text: 'Error: ' + event.message, level: 'err' }]
        showLog  = true
        phase    = 'done'
      }
    })
  }

  async function onStop() {
    await stopExport()
  }

  async function onOpenFolder() {
    await openOutput(outputFolder)
  }

  async function openRecordingFolder() {
    if (recordingPath) await openOutput(recordingPath)
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
  // ── Badge helpers for kill feed (use full group object) ────────────
  function groupIsDeath(group) {
    return (group.kill_count || 0) === 0
  }

  function groupBadgeStyle(group) {
    if (groupIsDeath(group))
      return `background:rgba(255,107,107,0.12);border:1px solid rgba(255,107,107,0.35);color:#ff6b6b`
    const n = group.kill_count || 1
    if (n >= 5) return `background:rgba(240,192,64,0.15);border:1px solid rgba(240,192,64,0.4);color:#f0c040`
    if (n >= 4) return `background:rgba(255,107,107,0.12);border:1px solid rgba(255,107,107,0.35);color:#ff6b6b`
    if (n >= 2) return `background:rgba(79,158,255,0.12);border:1px solid rgba(79,158,255,0.35);color:#4f9eff`
    return `background:rgba(0,229,160,0.10);border:1px solid rgba(0,229,160,0.3);color:#00e5a0`
  }

  function groupBadgeContent(group) {
    if (groupIsDeath(group))
      return `<span class="badge-icon">${deathIconRed}</span>`
    const n = Math.min(group.kill_count || 1, 5)
    const icon = killIconForCount(n)
    return Array.from({ length: n }, () =>
      `<span class="badge-icon">${icon}</span>`
    ).join('')
  }

  function feedTitle(group) {
    if (!group.events?.length) return group.ts_label
    return group.events.map(e => e.label || '').join(' → ')
  }

  // ── Badge helpers for export clip cards (tag string only) ───────────
  function badgeStyle(tag) {
    const t = (tag || '').toUpperCase()
    if (t === 'DEATH') return `background:rgba(255,107,107,0.12);border:1px solid rgba(255,107,107,0.35);color:#ff6b6b`
    const m = t.match(/^(\d+)K$/)
    if (!m) return `background:#2a3347;border:1px solid #2a3347;color:#e2e8f0`
    const n = parseInt(m[1])
    if (n >= 5) return `background:rgba(240,192,64,0.15);border:1px solid rgba(240,192,64,0.4);color:#f0c040`
    if (n >= 4) return `background:rgba(255,107,107,0.12);border:1px solid rgba(255,107,107,0.35);color:#ff6b6b`
    if (n >= 2) return `background:rgba(79,158,255,0.12);border:1px solid rgba(79,158,255,0.35);color:#4f9eff`
    return `background:rgba(0,229,160,0.10);border:1px solid rgba(0,229,160,0.3);color:#00e5a0`
  }

  function badgeContent(tag) {
    const t = (tag || '').toUpperCase()
    if (t === 'DEATH') return `<span class="badge-icon">${deathIconRed}</span>`
    const m = t.match(/^(\d+)K$/)
    if (m) return `${m[1]}<span class="badge-icon">${killIconGrey}</span>`
    return tag
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

  // ── Video player helpers ────────────────────────────────────────

  // Open exported MP4 via Tauri asset protocol (post-export)
  function openPlayer(card) {
    playerMode      = 'mp4'
    playerCard      = card
    playerSrc       = convertFileSrc(card.out_path)
    playerTrimLeft  = 0
    playerTrimRight = 0
    playerError     = ''
  }

  // Open raw DASH stream for in-place preview (pre-export, kill feed)
  async function openDashPreview(group) {
    playerMode      = 'dash'
    playerError     = ''
    playerCard      = group
    playerTrimLeft  = 0
    playerTrimRight = 0
    dashCurrentTime = 0
    dashPaused      = true

    const mpdUrl = `http://127.0.0.1:7847/api/session-stream/${encodeURIComponent(group.session_name)}/session.mpd?recording_path=${encodeURIComponent(recordingPath)}`
    playerSrc = mpdUrl

    // Pre-validate: verify the MPD endpoint responds before handing to dash.js
    try {
      const r = await fetch(mpdUrl)
      if (!r.ok) {
        playerError = `MPD not found (${r.status}): ${mpdUrl}`
        return
      }
    } catch (err) {
      playerError = `Cannot reach server: ${err.message}`
      return
    }

    await tick()  // wait for videoEl to mount
    _initDashPlayer(group.clip_start ?? 0)
  }

  function _initDashPlayer(seekTo) {
    if (dashPlayer) { dashPlayer.destroy(); dashPlayer = null }
    dashPlayer = dashjs.MediaPlayer().create()
    dashPlayer.updateSettings({ streaming: { buffer: { fastSwitchEnabled: true } } })
    // autoplay=true — don't depend on STREAM_INITIALIZED to trigger play()
    dashPlayer.initialize(videoEl, playerSrc, true)

    // Seek to clip start once the stream timeline is known
    dashPlayer.on(dashjs.MediaPlayer.events.STREAM_INITIALIZED, () => {
      if (seekTo > 0) dashPlayer.seek(seekTo)
    })

    // Surface playback errors in the modal
    dashPlayer.on(dashjs.MediaPlayer.events.ERROR, (e) => {
      const msg = e?.error?.message || e?.event?.message
                  || (e?.error ? JSON.stringify(e.error) : null)
                  || 'Playback error'
      console.error('dash.js error:', e)
      playerError = msg
    })
  }

  function closePlayer() {
    if (dashPlayer) { dashPlayer.destroy(); dashPlayer = null }
    if (videoEl) videoEl.pause()
    playerCard      = null
    playerSrc       = ''
    playerError     = ''
    playerTrimLeft  = 0
    playerTrimRight = 0
    dashCurrentTime = 0
    dashPaused      = true
  }

  function onDashTimeUpdate() {
    if (!videoEl) return
    dashCurrentTime = videoEl.currentTime
    // Auto-pause when clip window ends
    if (dashCurrentTime >= pEffStart + pEffDur + 0.1) {
      videoEl.pause()
    }
  }

  function toggleDashPlay() {
    if (!videoEl) return
    if (videoEl.paused) videoEl.play().catch(() => {})
    else videoEl.pause()
  }

  function onDashProgressClick(e) {
    if (!videoEl || !playerCard) return
    const rect = e.currentTarget.getBoundingClientRect()
    const pct  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
    const seekTo = pEffStart + pct * pEffDur
    if (dashPlayer) dashPlayer.seek(Math.max(0, seekTo))
    else videoEl.currentTime = Math.max(0, seekTo)
  }

  function onTrimChange() {
    if (!playerCard) return
    const newStart = playerCard.clip_start - playerTrimLeft
    if (dashPlayer) dashPlayer.seek(Math.max(0, newStart))
    else if (videoEl) videoEl.currentTime = Math.max(0, playerTrimLeft)
  }

  function applyTrim() {
    if (!playerCard) return
    const newStart    = playerCard.clip_start    - playerTrimLeft
    const newDuration = playerCard.clip_duration + playerTrimLeft + playerTrimRight
    const outName     = playerCard.out_name

    // Update in live groups array
    groups = groups.map(g =>
      g.out_name === outName
        ? { ...g, clip_start: newStart, clip_duration: newDuration, _trimmed: true }
        : g
    )
    // Update in cache so re-scans don't revert the change
    for (const key of Object.keys(groupCache)) {
      groupCache[key] = groupCache[key].map(g =>
        g.out_name === outName
          ? { ...g, clip_start: newStart, clip_duration: newDuration, _trimmed: true }
          : g
      )
    }

    closePlayer()
  }

  function seekToMarker(evt) {
    if (!playerCard) return
    if (dashPlayer) {
      // DASH: seek to absolute session time
      dashPlayer.seek(Math.max(0, evt.time_sec))
    } else if (videoEl) {
      // MP4: seek relative to clip start
      videoEl.currentTime = Math.max(0, evt.time_sec - playerCard.clip_start)
    }
  }
</script>

<!-- ── Header ──────────────────────────────────────────────────────── -->
<header>
  <div class="header-left">
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" style="flex-shrink:0">
      <rect width="18" height="18" rx="4" fill="#4f9eff" fill-opacity="0.15"/>
      <path d="M4 13L7 6L9.5 11L11.5 8L14 13" stroke="#4f9eff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="9" cy="5" r="1.2" fill="#00e5a0"/>
    </svg>
    <span class="header-title">Steam Highlight Extractor</span>
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
      <span class="status-sep">·</span>
      <span class="ffmpeg-ok">ffmpeg ✓</span>
      {#if gpuEncoder}
        <span class="status-sep">·</span>
        <span class="gpu-badge">{gpuEncoder.replace('h264_', '').toUpperCase()}</span>
      {/if}
    {/if}
  </div>
</header>

<!-- ── Body ───────────────────────────────────────────────────────── -->
<div class="body">

  <!-- Sidebar -->
  <aside class="sidebar">

    <div class="section-label">Recording Path</div>
    <div class="sidebar-pad">
      <div class="path-field">
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none" style="flex-shrink:0">
          <path d="M1.5 4C1.5 3.17 2.17 2.5 3 2.5H5.2L6.3 4H10.5C11.33 4 12 4.67 12 5.5V9.5C12 10.33 11.33 11 10.5 11H3C2.17 11 1.5 10.33 1.5 9.5V4Z" stroke="var(--blue)" stroke-width="1.3"/>
        </svg>
        <input
          type="text"
          bind:value={recordingPath}
          placeholder="Auto-detect…"
          readonly
          on:click={browseRecordingPath}
        />
        <button class="field-arrow" on:click={openRecordingFolder} title="Open in Explorer" disabled={!recordingPath}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M4 2L8 6L4 10" stroke="var(--blue)" stroke-width="1.5" stroke-linecap="round"/></svg>
        </button>
      </div>
    </div>

    <div class="sidebar-sep"></div>

    <div class="section-label">
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
          {@const sel = selectedSessions.has(name)}
          <div
            class="session-row"
            class:selected={sel}
            on:click={() => toggleSession(name)}
            on:keydown={e => e.key === 'Enter' && toggleSession(name)}
            role="option"
            aria-selected={sel}
            tabindex="0"
          >
            <div class="session-check" class:session-check-on={sel}>
              {#if sel}
                <svg width="9" height="9" viewBox="0 0 9 9" fill="none"><path d="M1.5 4.5L3.5 6.5L7.5 2.5" stroke="white" stroke-width="1.5" stroke-linecap="round"/></svg>
              {/if}
            </div>
            <span class="session-name">{name}</span>
          </div>
        {/each}
      {/if}
    </div>

    <div class="sidebar-sep"></div>

    <div class="section-label">Settings</div>
    <div class="settings-list">
      <div class="setting-row">
        <span class="setting-label">Padding before</span>
        <div class="setting-control">
          <input type="range" bind:value={padBefore} min="0" max="30" />
          <span class="setting-val">{padBefore}s</span>
        </div>
      </div>
      <div class="setting-row">
        <span class="setting-label">Padding after</span>
        <div class="setting-control">
          <input type="range" bind:value={padAfter} min="0" max="30" />
          <span class="setting-val">{padAfter}s</span>
        </div>
      </div>
      <div class="setting-row">
        <span class="setting-label">Kill pre-shift</span>
        <div class="setting-control">
          <input type="range" bind:value={preShift} min="0" max="10" />
          <span class="setting-val">{preShift}s</span>
        </div>
      </div>
      <div class="setting-row">
        <span class="setting-label">Export workers{gpuEncoder ? ' (GPU)' : ''}</span>
        <div class="setting-control">
          <input type="range" bind:value={workers} min="1" max="4" step="1" />
          <span class="setting-val">{workers}</span>
        </div>
      </div>

      <!-- Rescan button at the bottom of settings -->
      <button
        class="btn-apply"
        class:btn-apply-dirty={settingsDirty}
        disabled={busy || selectedSessions.size === 0}
        on:click={applySettings}
        title={settingsDirty ? 'Re-scan selected sessions with new settings' : 'Settings match current scan'}
      >
        {#if busy && settingsDirty}
          <svg class="apply-spin" width="12" height="12" viewBox="0 0 12 12" fill="none">
            <circle cx="6" cy="6" r="4.5" stroke="currentColor" stroke-width="1.5" stroke-dasharray="18 9" stroke-linecap="round"/>
          </svg>
          Rescanning…
        {:else if settingsDirty}
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M10 2.8A5 5 0 1 0 10.9 6.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M10.5 1.5V4.5H7.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          Rescan with new settings
        {:else}
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M2.5 6L5 8.5L9.5 3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          Settings applied
        {/if}
      </button>
    </div>

    <div class="sidebar-sep"></div>

    <div class="section-label">Extract</div>
    <div class="pill-row">
      <button
        class="pill-toggle"
        class:pill-on={extractKills}
        on:click={() => extractKills = !extractKills}
      >{@html killIconGreen}<span>Kills</span></button>
      <button
        class="pill-toggle pill-toggle-death"
        class:pill-on={extractDeaths}
        on:click={() => extractDeaths = !extractDeaths}
      >{@html deathIconRed}<span>Deaths</span></button>
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
            <span class="muted-label" style="margin-left:10px">
              {filteredGroups.length}{filteredGroups.length !== groups.length ? `/${groups.length}` : ''} clips
            </span>
          {/if}
        </div>
        <div class="row-actions">
          <button class="micro" on:click={selectAllGroups}>Select All</button>
          <button class="micro" on:click={clearGroupSelection}>None</button>
        </div>
      </div>

      {#if groups.length === 0}
        <div class="empty-state" style="flex:1">
          <div class="empty-title">No highlights found</div>
          <div class="empty-sub">Try enabling Deaths or adjusting settings</div>
        </div>
      {:else}
        {#if allMaps.length > 0 || multiKillCount > 0}
          <div class="filter-bar">
            {#if allMaps.length > 0}
              <div class="filter-bar-maps">
                {#if allMaps.length > 1}
                  <button class="map-chip" class:map-chip-active={mapFilter === 'all'} on:click={() => mapFilter = 'all'}>All</button>
                {/if}
                {#each allMaps as map}
                  <button
                    class="map-chip"
                    class:map-chip-active={mapFilter === map.code || allMaps.length === 1}
                    on:click={() => mapFilter = allMaps.length > 1 ? map.code : 'all'}
                  >
                    {#if mapIcons[map.code]}<img src={mapIcons[map.code]} alt="" class="map-chip-icon" />{/if}
                    {map.display}
                  </button>
                {/each}
              </div>
            {/if}
            {#if multiKillCount > 0}
              <button class="multikill-toggle" class:multikill-active={multiKillOnly} on:click={() => multiKillOnly = !multiKillOnly}>
                Multi-kill
              </button>
            {/if}
          </div>
        {/if}
        {#if filteredGroups.length === 0}
          <div class="empty-state" style="flex:1">
            <div class="empty-title">No clips match</div>
            <div class="empty-sub">Try adjusting the filters above</div>
          </div>
        {/if}
        <div class="kill-feed">
          {#each filteredGroups as group}
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
              {#if group.thumbnail_url}
                <img class="feed-thumb" src={group.thumbnail_url} alt="" loading="lazy" />
              {:else}
                <div class="feed-thumb feed-thumb-ph"></div>
              {/if}
              <span class="badge" style={groupBadgeStyle(group)}>{@html groupBadgeContent(group)}</span>
              <div class="feed-info">
                <div class="feed-name">{feedTitle(group)}</div>
                <div class="feed-meta">
                  {#if group.map_name}<span class="feed-map">{#if mapIcons[group.map_name]}<img src={mapIcons[group.map_name]} alt="" class="feed-map-icon" />{/if}{group.map_display || group.map_name}</span> · {/if}{group.ts_label} · {fmtDuration(group.clip_duration)}{#if group._trimmed}<span class="trimmed-badge">trimmed</span>{/if}
                </div>
              </div>
              <button class="feed-preview-btn" on:click|stopPropagation={() => openDashPreview(group)} title="Preview clip">
                <svg width="26" height="26" viewBox="0 0 26 26" fill="none">
                  <circle cx="13" cy="13" r="12" fill="rgba(79,158,255,0.10)" stroke="rgba(79,158,255,0.3)" stroke-width="1.2"/>
                  <path d="M10.5 9L18 13L10.5 17V9Z" fill="#4f9eff"/>
                </svg>
              </button>
            </div>
          {/each}
        </div>
      {/if}

    {:else if phase === 'exporting' || phase === 'done'}
      <div class="log-header">
        <div class="log-header-left">
          <span class="log-title">Exporting</span>
          {#if phase === 'exporting'}
            <div class="live-badge">
              <span class="live-dot"></span>
              <span>LIVE</span>
            </div>
          {/if}
          {#if phase === 'done'}
            <button class="micro" on:click={onReset}>← Back to results</button>
          {/if}
        </div>
        {#if exportTotal > 0}
          <span class="muted-label">Clip {currentClipNum} of {exportTotal}</span>
        {/if}
      </div>

      <!-- Clip cards -->
      <div class="clip-cards">
        {#each clipCards as card}
          <div class="clip-card" class:clip-card-active={card.status === 'active'} class:clip-card-done={card.status === 'ok'}>
            {#if card.thumbnail_url}
              <img class="card-thumb" src={card.thumbnail_url} alt="" />
            {:else}
              <div class="card-thumb card-thumb-ph"></div>
            {/if}
            <span class="badge" style={badgeStyle(card.tag)}>{@html badgeContent(card.tag)}</span>
            <div class="card-info">
              <div class="card-name">{card.name}</div>
              <div class="card-meta">{fmtDuration(card.duration)}</div>
            </div>
            <div class="card-status">
              {#if card.status === 'pending'}
                <span class="status-pending">●</span>
              {:else if card.status === 'active'}
                <span class="status-spinner"></span>
              {:else if card.status === 'ok'}
                <svg class="status-ok-icon" width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <circle cx="8" cy="8" r="7" fill="rgba(0,229,160,0.15)" stroke="rgba(0,229,160,0.4)" stroke-width="1"/>
                  <path d="M5 8L7 10L11 6" stroke="#00e5a0" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
              {:else if card.status === 'skipped'}
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <circle cx="8" cy="8" r="7" stroke="rgba(79,158,255,0.4)" stroke-width="1"/>
                  <path d="M6 8h4M8 6l2 2-2 2" stroke="#4f9eff" stroke-width="1.3" stroke-linecap="round"/>
                </svg>
              {:else if card.status === 'failed'}
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <circle cx="8" cy="8" r="7" fill="rgba(255,107,107,0.15)" stroke="rgba(255,107,107,0.4)" stroke-width="1"/>
                  <path d="M6 6l4 4M10 6l-4 4" stroke="#ff6b6b" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
              {/if}
              {#if card.status === 'ok' || card.status === 'skipped'}
                <div class="card-done-meta">
                  {#if card.size_mb > 0}<span>{card.size_mb} MB</span>{/if}
                  <span class="card-elapsed">{card.elapsed}s</span>
                </div>
                {#if exportedPaths.has(card.out_path)}
                  <button class="card-play-btn" on:click|stopPropagation={() => openPlayer(card)} title="Preview clip">
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                      <circle cx="7" cy="7" r="6" fill="rgba(79,158,255,0.15)" stroke="rgba(79,158,255,0.4)" stroke-width="1"/>
                      <path d="M5.5 4.5L10 7L5.5 9.5Z" fill="#4f9eff"/>
                    </svg>
                  </button>
                {/if}
              {:else if card.status === 'active'}
                <span class="card-encoding">encoding…</span>
              {/if}
            </div>
          </div>
        {/each}
      </div>

      <!-- Collapsible log -->
      <div class="log-toggle-row">
        <button class="micro" on:click={() => showLog = !showLog}>
          {showLog ? '▲' : '▼'} {showLog ? 'Hide' : 'Show'} log
        </button>
      </div>
      {#if showLog}
        <div class="log-panel" id="log-panel">
          {#each logs as log}
            <div class="log-line {levelClass(log.level)}">{log.text}</div>
          {/each}
          {#if phase === 'exporting'}
            <div class="log-line log-muted">…</div>
          {/if}
        </div>
      {/if}
    {/if}

  </main>
</div>

<!-- ── Video player modal ─────────────────────────────────────── -->
{#if playerCard}
  <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-noninteractive-element-interactions -->
  <div class="player-backdrop" on:click={closePlayer} role="dialog" aria-modal="true">
    <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-noninteractive-element-interactions -->
    <div class="player-modal" on:click|stopPropagation role="document">
      <div class="player-header">
        <div class="player-header-left">
          <span class="player-mode-badge" class:badge-preview={playerMode==='dash'} class:badge-exported={playerMode==='mp4'}>
            {playerMode === 'dash' ? 'LIVE PREVIEW' : 'EXPORTED'}
          </span>
          <span class="player-title">{playerCard.out_name || playerCard.name || ''}</span>
        </div>
        <button class="player-close" on:click={closePlayer} aria-label="Close player">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
      {#if playerError}
        <div class="player-error">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" style="flex-shrink:0">
            <circle cx="7" cy="7" r="6" stroke="currentColor" stroke-width="1.4"/>
            <path d="M7 4v3.5M7 9.5v.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
          {playerError}
        </div>
      {/if}
      <!-- svelte-ignore a11y-media-has-caption -->
      {#if playerMode === 'mp4'}
        <video
          bind:this={videoEl}
          class="player-video"
          src={playerSrc}
          controls
          autoplay
          preload="metadata"
        ></video>
      {:else}
        <!-- dash.js attaches to this — no native controls, we render our own -->
        <!-- svelte-ignore a11y-media-has-caption -->
        <video
          bind:this={videoEl}
          class="player-video"
          on:timeupdate={onDashTimeUpdate}
          on:play={() => { dashPaused = false }}
          on:pause={() => { dashPaused = true }}
        ></video>
      {/if}

      <!-- MP4: static marker bar (native controls handle seek) -->
      {#if playerMode === 'mp4' && (playerCard.events?.length > 0) && pEffDur > 0}
        <div class="player-timeline">
          <div class="player-track"></div>
          {#each playerCard.events as evt}
            {@const pct = Math.max(3, Math.min(97, (evt.time_sec - pEffStart) / pEffDur * 100))}
            <button class="player-marker" style="left:{pct}%"
              title="{evt.label} ({fmtDuration(Math.max(0, evt.time_sec - pEffStart))})"
              on:click={() => seekToMarker(evt)}>
              <svg width="10" height="10" viewBox="0 0 10 10"><path d="M5 1L9 9H1Z" fill="currentColor"/></svg>
            </button>
          {/each}
        </div>
      {/if}

      <!-- DASH: custom controls scoped to clip window -->
      {#if playerMode === 'dash'}
        <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-noninteractive-element-interactions -->
        <div class="dash-controls">
          <button class="dash-play-btn" on:click={toggleDashPlay}>
            {#if dashPaused}
              <svg width="16" height="16" viewBox="0 0 16 16"><path d="M4 3l10 5-10 5V3z" fill="currentColor"/></svg>
            {:else}
              <svg width="16" height="16" viewBox="0 0 16 16"><rect x="3" y="2" width="4" height="12" rx="1" fill="currentColor"/><rect x="9" y="2" width="4" height="12" rx="1" fill="currentColor"/></svg>
            {/if}
          </button>

          <!-- Progress bar — represents clip window only -->
          <!-- svelte-ignore a11y-click-events-have-key-events -->
          <div class="dash-progress" on:click={onDashProgressClick}>
            <div class="dash-progress-bg"></div>
            <div class="dash-progress-fill" style="width:{dashClipPct}%"></div>
            <div class="dash-playhead" style="left:{dashClipPct}%"></div>
            <!-- Event markers as ticks on the bar -->
            {#each playerCard.events ?? [] as evt}
              {@const tPct = Math.max(0, Math.min(100, (evt.time_sec - pEffStart) / pEffDur * 100))}
              <button class="dash-tick" style="left:{tPct}%"
                title="{evt.label} ({fmtDuration(Math.max(0, evt.time_sec - pEffStart))})"
                on:click|stopPropagation={() => seekToMarker(evt)}></button>
            {/each}
          </div>

          <span class="dash-time">{fmtDuration(dashClipTime)} / {fmtDuration(pEffDur)}</span>
        </div>

        <!-- Trim controls -->
        <div class="player-trim">
          <div class="trim-section-label">
            <svg width="11" height="11" viewBox="0 0 11 11" fill="none">
              <path d="M1 3.5h9M1 7.5h9M3.5 1L2 5.5l1.5 4.5M7.5 1L9 5.5 7.5 10" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
            </svg>
            TRIM CLIP
          </div>
          <div class="trim-row">
            <span class="trim-label">Start</span>
            <input class="trim-slider" type="range" min="-5" max="20" step="0.5"
              bind:value={playerTrimLeft} on:input={onTrimChange} />
            <span class="trim-val" class:trim-pos={playerTrimLeft>0} class:trim-neg={playerTrimLeft<0}>
              {playerTrimLeft > 0 ? '+' : ''}{playerTrimLeft}s
            </span>
          </div>
          <div class="trim-row">
            <span class="trim-label">End</span>
            <input class="trim-slider" type="range" min="-5" max="20" step="0.5"
              bind:value={playerTrimRight} on:input={onTrimChange} />
            <span class="trim-val" class:trim-pos={playerTrimRight>0} class:trim-neg={playerTrimRight<0}>
              {playerTrimRight > 0 ? '+' : ''}{playerTrimRight}s
            </span>
          </div>
          <div class="trim-footer">
            <span class="trim-duration">{fmtDuration(pEffDur)}</span>
            <button class="btn-primary trim-export-btn" on:click={applyTrim}
              disabled={playerTrimLeft === 0 && playerTrimRight === 0}>
              ✓ Apply
            </button>
          </div>
        </div>
      {/if}
    </div>
  </div>
{/if}

<!-- ── Action bar ─────────────────────────────────────────────────── -->
<footer>

  {#if busy || phase === 'done'}
    <div class="progress-track">
      <div
        class="progress-fill"
        class:progress-done={phase === 'done'}
        style="width:{progress * 100}%"
      ></div>
    </div>
  {/if}

  <div class="footer-main">
    <div class="footer-left">
      {#if phase === 'idle' || phase === 'results' || phase === 'done'}
        <button class="btn-ghost" on:click={onScan} disabled={busy || selectedSessions.size === 0}>
          ↻ Re-scan
        </button>
      {/if}
      {#if phase === 'results'}
        <label class="merge-check">
          <input type="checkbox" bind:checked={doMerge} />
          Merge into one clip
        </label>
        {#if selectedCount > 0}
          <span class="count-pill">{selectedCount} selected</span>
        {/if}
      {/if}
      {#if phase === 'exporting'}
        <button class="btn-danger" on:click={onStop}>
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none"><rect x="1" y="1" width="8" height="8" rx="1.5" fill="currentColor"/></svg>
          Stop
        </button>
      {/if}
      {#if phase === 'done'}
        <button class="btn-ghost" on:click={onExport} disabled={selectedCount === 0}>▶ Export again</button>
      {/if}
      <button class="btn-ghost" on:click={onOpenFolder}>
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none"><path d="M1.5 4C1.5 3.17 2.17 2.5 3 2.5H5.2L6.3 4H10.5C11.33 4 12 4.67 12 5.5V9.5C12 10.33 11.33 11 10.5 11H3C2.17 11 1.5 10.33 1.5 9.5V4Z" stroke="currentColor" stroke-width="1.3"/></svg>
        Open Folder
      </button>
    </div>

    <div class="footer-right">
      {#if phase === 'exporting'}
        {#if currentClipName}
          <span class="current-clip">{currentClipName}</span>
        {/if}
        <span class="progress-pct">{Math.round(progress * 100)}%</span>
      {/if}
      {#if phase === 'results'}
        <button class="btn-primary" on:click={onExport} disabled={busy || selectedCount === 0}>
          Extract Highlights
        </button>
      {/if}
      {#if phase === 'done' && stopped}
        <span class="status-warn">Stopped</span>
      {:else if phase === 'done'}
        <span class="status-ok">✓ Done</span>
      {/if}
    </div>
  </div>

  <!-- Output folder -->
  <div class="output-row">
    <span class="output-label">OUTPUT</span>
    <div class="output-field">
      <svg width="13" height="13" viewBox="0 0 13 13" fill="none" style="flex-shrink:0">
        <path d="M1.5 4C1.5 3.17 2.17 2.5 3 2.5H5.2L6.3 4H10.5C11.33 4 12 4.67 12 5.5V9.5C12 10.33 11.33 11 10.5 11H3C2.17 11 1.5 10.33 1.5 9.5V4Z" stroke="var(--green)" stroke-width="1.3"/>
      </svg>
      <input type="text" class="output-path-input" bind:value={outputFolder} placeholder="SteamHighlights/" />
      <button class="output-browse" on:click={browseOutputFolder}>
        <svg width="11" height="11" viewBox="0 0 11 11" fill="none"><path d="M1 5.5H10M10 5.5L6.5 2M10 5.5L6.5 9" stroke="var(--green)" stroke-width="1.3" stroke-linecap="round"/></svg>
        Browse
      </button>
    </div>
  </div>

</footer>

<style>
/* ── Layout ─────────────────────────────────────────────────────── */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  padding: 0 16px;
  height: 40px;
  flex-shrink: 0;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-title {
  font-size: 13px;
  font-weight: 500;
  letter-spacing: -0.01em;
}

.header-status {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11px;
  color: var(--muted);
  font-family: var(--font-mono);
}

.status-text { color: var(--muted); }
.status-sep  { color: var(--border); }
.ffmpeg-ok   { color: var(--muted); }
:global(.badge-icon) {
  display: inline-flex;
  align-items: center;
  margin-left: 2px;
  vertical-align: middle;
}
:global(.badge-icon:first-child) { margin-left: 0; }

.gpu-badge {
  color: var(--green);
  background: rgba(0,229,160,0.08);
  border: 1px solid rgba(0,229,160,0.25);
  border-radius: 3px;
  padding: 1px 6px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
}

.dot {
  width: 7px;
  height: 7px;
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
  width: 240px;
  flex-shrink: 0;
  background: var(--bg);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: hidden;
}

.sidebar-pad {
  padding: 4px 14px 10px;
}

/* Path field */
.path-field {
  display: flex;
  align-items: center;
  gap: 7px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 6px 10px;
}
.path-field input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  padding: 0;
  font-size: 10px;
  cursor: pointer;
}
.path-field input:focus { border: none; outline: none; }
.field-arrow {
  background: transparent;
  border: none;
  padding: 0;
  cursor: pointer;
  display: flex;
  align-items: center;
  flex-shrink: 0;
}
.field-arrow:disabled { opacity: 0.3; cursor: default; }

.sidebar-sep {
  height: 1px;
  background: var(--border);
  margin: 4px 0;
}

.section-label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  padding: 10px 14px 5px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.row-actions {
  display: flex;
  gap: 3px;
  align-items: center;
}

/* Session list */
.session-list {
  overflow-y: auto;
  max-height: 200px;
}

.empty-text {
  padding: 10px 14px;
  font-size: 11px;
  color: var(--muted);
  font-family: var(--font-mono);
}

.session-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  cursor: pointer;
  transition: background 0.1s;
  font-size: 10px;
  font-family: var(--font-mono);
  border-radius: 0;
}
.session-row:hover { background: var(--surface); }
.session-row.selected { background: var(--surface); }

.session-check {
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid var(--border);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.session-check-on {
  background: var(--blue);
  border-color: var(--blue);
}

.session-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
}
.session-row.selected .session-name { color: var(--text); }

/* Settings sliders */
.settings-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 4px 14px 10px;
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.setting-label {
  font-size: 11px;
  color: var(--muted);
}

.setting-control {
  display: flex;
  align-items: center;
  gap: 7px;
}

.setting-control input[type="range"] {
  width: 72px;
  height: 4px;
  accent-color: var(--blue);
  cursor: pointer;
  background: transparent;
  padding: 0;
  border: none;
}

.setting-val {
  font-size: 10px;
  font-family: var(--font-mono);
  color: var(--text);
  width: 26px;
  text-align: right;
}

/* Pill toggles */
.pill-row {
  display: flex;
  gap: 7px;
  padding: 4px 14px 12px;
}

.pill-toggle {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 5px;
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
  transition: all 0.12s;
}

.pill-toggle:hover { border-color: var(--blue); color: var(--text); }
.pill-on {
  background: rgba(79,158,255,0.08);
  border-color: var(--blue);
  color: var(--text);
}
.pill-toggle-death.pill-on {
  background: rgba(255,107,107,0.08);
  border-color: var(--red);
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
  gap: 8px;
  color: var(--muted);
}
.empty-title { font-size: 14px; font-weight: 600; color: var(--text); }
.empty-sub   { font-size: 12px; }

.scanning-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 20px 24px;
  gap: 8px;
  overflow: hidden;
}
.scanning-label { font-size: 15px; font-weight: 700; letter-spacing: -0.02em; }
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
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.results-title {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.02em;
  display: flex;
  align-items: baseline;
}
.muted-label {
  font-size: 11px;
  color: var(--muted);
  font-family: var(--font-mono);
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  min-height: 0;
}
.filter-bar-maps {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  flex: 1;
  min-width: 0;
}
.map-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  font-size: 11px;
  font-family: var(--font-mono);
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.map-chip:hover { border-color: var(--blue); color: var(--blue); }
.map-chip-active { border-color: var(--blue) !important; background: rgba(79,158,255,0.12) !important; color: var(--blue) !important; }
.map-chip-icon { width: 14px; height: 14px; object-fit: contain; flex-shrink: 0; }
.multikill-toggle {
  flex-shrink: 0;
  padding: 3px 10px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  font-size: 11px;
  font-family: var(--font-mono);
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.multikill-toggle:hover { border-color: var(--green); color: var(--green); }
.multikill-active { border-color: var(--green) !important; background: rgba(0,229,160,0.12) !important; color: var(--green) !important; }

.feed-map {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--blue);
  font-family: var(--font-mono);
}
.feed-map-icon { width: 12px; height: 12px; object-fit: contain; flex-shrink: 0; }

.kill-feed { flex: 1; overflow-y: auto; }

.feed-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 24px 10px 0;
  cursor: pointer;
  border-bottom: 1px solid #131929;
  transition: background 0.1s;
  position: relative;
}
.feed-row:hover { background: var(--surface); }
.feed-row-checked { background: #0b1220; }

.feed-accent {
  width: 3px;
  align-self: stretch;
  background: transparent;
  transition: background 0.1s;
  flex-shrink: 0;
  border-radius: 0 2px 2px 0;
}
.feed-accent-on { background: var(--blue); }

.feed-check {
  margin-left: 10px;
  flex-shrink: 0;
}

/* Thumbnail */
.feed-thumb {
  width: 88px;
  height: 50px;
  border-radius: 5px;
  object-fit: cover;
  flex-shrink: 0;
  border: 1px solid var(--border);
}
.feed-thumb-ph {
  width: 88px;
  height: 50px;
  border-radius: 5px;
  background: var(--surface);
  flex-shrink: 0;
  border: 1px solid var(--border);
}

.feed-info { flex: 1; min-width: 0; }
.feed-name {
  font-size: 11px;
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.feed-meta {
  font-size: 10px;
  color: var(--muted);
  font-family: var(--font-mono);
  margin-top: 3px;
}

/* ── Clip export cards ──────────────────────────────────────────── */
.clip-cards {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  padding: 8px 24px;
  gap: 6px;
}

.clip-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  transition: border-color 0.15s, background 0.15s;
  flex-shrink: 0;
}
.clip-card-active {
  border-color: rgba(79,158,255,0.4);
  background: rgba(79,158,255,0.04);
}
.clip-card-done {
  border-color: rgba(0,229,160,0.2);
}

.card-thumb {
  width: 80px;
  height: 45px;
  border-radius: 5px;
  object-fit: cover;
  flex-shrink: 0;
  border: 1px solid var(--border);
}
.card-thumb-ph {
  width: 80px;
  height: 45px;
  border-radius: 5px;
  background: var(--surface2);
  flex-shrink: 0;
  border: 1px solid var(--border);
}

.card-info {
  flex: 1;
  min-width: 0;
}
.card-name {
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.card-meta {
  font-size: 10px;
  color: var(--muted);
  font-family: var(--font-mono);
  margin-top: 2px;
}

.card-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

/* Pending dot */
.status-pending {
  color: var(--border);
  font-size: 10px;
}

/* Spinning encoder indicator */
.status-spinner {
  display: inline-block;
  width: 16px;
  height: 16px;
  border: 2px solid rgba(79,158,255,0.2);
  border-top-color: var(--blue);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

.status-ok-icon { display: block; }

.card-done-meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--muted);
}
.card-elapsed { color: var(--muted); }

.card-encoding {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--blue);
}

.log-toggle-row {
  padding: 6px 24px 4px;
  flex-shrink: 0;
}

/* Log panel */
.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.log-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.log-title {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.02em;
}

/* LIVE badge */
.live-badge {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 9px;
  background: rgba(255,107,107,0.10);
  border: 1px solid rgba(255,107,107,0.3);
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  color: var(--red);
  letter-spacing: 0.05em;
}
.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--red);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50%       { opacity: 0.4; }
}

.log-panel {
  height: 160px;
  flex-shrink: 0;
  overflow-y: auto;
  padding: 10px 24px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.7;
  border-top: 1px solid var(--border);
  background: var(--bg);
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
  background: var(--bg);
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 24px 14px;
  position: relative;
}

.progress-track {
  width: 100%;
  height: 3px;
  background: var(--surface2);
  border-radius: 2px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--blue), var(--green));
  transition: width 0.25s ease;
  border-radius: 2px;
}
.progress-done { background: var(--green); }

.footer-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.footer-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.footer-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* Progress % during export */
.progress-pct {
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 700;
  color: var(--text);
  letter-spacing: -0.03em;
  line-height: 1;
}

.current-clip {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 300px;
}

/* Merge + count pill */
.merge-check {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}

.count-pill {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--blue);
  background: rgba(79,158,255,0.08);
  border: 1px solid rgba(79,158,255,0.2);
  border-radius: 4px;
  padding: 2px 8px;
}

/* Output row */
.output-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.output-label {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.07em;
  color: var(--muted);
  flex-shrink: 0;
}

.output-field {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 6px 12px;
}

.output-path-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  padding: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text);
}
.output-path-input:focus { border: none; }

.output-browse {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  background: rgba(0,229,160,0.08);
  border: 1px solid rgba(0,229,160,0.25);
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  color: var(--green);
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.12s;
}
.output-browse:hover { background: rgba(0,229,160,0.15); }

/* Status / done */
.status-ok   { color: var(--green); font-weight: 600; font-size: 12px; }
.status-warn { color: var(--yellow); font-weight: 600; font-size: 12px; }

/* ── Apply / Rescan button ───────────────────────────────────────── */
.btn-apply {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: calc(100% - 28px);
  margin: 8px 14px 4px;
  padding: 7px 0;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s, opacity 0.15s;
  background: var(--surface2);
  border: 1px solid var(--border);
  color: var(--muted);
  letter-spacing: 0.02em;
}
.btn-apply:not(:disabled):hover {
  background: var(--border);
  color: var(--text);
}
.btn-apply-dirty {
  background: var(--blue);
  border-color: var(--blue);
  color: #000;
}
.btn-apply-dirty:not(:disabled):hover {
  background: #79b4ff;
  border-color: #79b4ff;
}
@keyframes apply-spin {
  to { transform: rotate(360deg); }
}
.apply-spin {
  animation: apply-spin 0.9s linear infinite;
}

/* ── Micro button ────────────────────────────────────────────────── */
.micro {
  background: var(--surface);
  color: var(--muted);
  border: 1px solid var(--border);
  font-size: 10px;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
}
.micro:hover { background: var(--border); color: var(--text); }

/* ── Kill feed preview button ───────────────────────────────── */
.feed-preview-btn {
  background: none;
  border: none;
  padding: 4px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s;
  flex-shrink: 0;
  display: flex;
  align-items: center;
}
.feed-row:hover .feed-preview-btn { opacity: 1; }

/* ── Card play button ────────────────────────────────────────── */
.card-play-btn {
  background: none;
  border: none;
  padding: 2px;
  cursor: pointer;
  opacity: 0.7;
  transition: opacity 0.15s;
  display: flex;
  align-items: center;
  margin-top: 3px;
}
.card-play-btn:hover { opacity: 1; }

/* ── Video player modal ──────────────────────────────────────── */
/* ── Video player modal ──────────────────────────────────────── */
@keyframes player-in {
  from { opacity: 0; transform: scale(0.97) translateY(10px); }
  to   { opacity: 1; transform: scale(1) translateY(0); }
}

.player-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.88);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.player-modal {
  background: #0d1117;
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 12px;
  width: min(920px, 92vw);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 32px 96px rgba(0,0,0,0.8), 0 0 0 1px rgba(255,255,255,0.04);
  animation: player-in 0.18s ease;
}

/* Header */
.player-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  flex-shrink: 0;
  gap: 10px;
}

.player-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.player-mode-badge {
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.07em;
  padding: 2px 6px;
  border-radius: 3px;
  flex-shrink: 0;
  text-transform: uppercase;
}
.badge-preview {
  background: rgba(79,158,255,0.15);
  color: var(--blue);
  border: 1px solid rgba(79,158,255,0.25);
}
.badge-exported {
  background: rgba(0,229,160,0.12);
  color: var(--green);
  border: 1px solid rgba(0,229,160,0.2);
}

.player-title {
  font-size: 11px;
  font-family: var(--font-mono);
  color: rgba(255,255,255,0.45);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.player-close {
  width: 28px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 6px;
  color: rgba(255,255,255,0.45);
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.player-close:hover {
  background: rgba(255,80,80,0.15);
  border-color: rgba(255,80,80,0.3);
  color: #ff6b6b;
}

/* Error banner */
.player-error {
  display: flex;
  align-items: center;
  gap: 7px;
  background: rgba(255,107,107,0.1);
  border-bottom: 1px solid rgba(255,107,107,0.2);
  color: #ff6b6b;
  font-size: 11px;
  padding: 8px 14px;
  flex-shrink: 0;
}

/* Video */
.player-video {
  width: 100%;
  display: block;
  background: #000;
  aspect-ratio: 16 / 9;
  object-fit: contain;
}

/* MP4 static marker bar */
.player-timeline {
  position: relative;
  height: 36px;
  background: rgba(0,0,0,0.35);
  border-top: 1px solid rgba(255,255,255,0.06);
  flex-shrink: 0;
}
.player-track {
  position: absolute;
  top: 50%; left: 14px; right: 14px;
  height: 2px;
  background: rgba(255,255,255,0.1);
  border-radius: 1px;
  transform: translateY(-50%);
  pointer-events: none;
}
.player-marker {
  position: absolute;
  top: 50%;
  transform: translate(-50%, -50%);
  width: 20px; height: 20px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 4px;
  background: rgba(79,158,255,0.12);
  border: 1px solid rgba(79,158,255,0.35);
  color: var(--blue);
  cursor: pointer; padding: 0;
  transition: background 0.15s, color 0.15s, transform 0.15s, box-shadow 0.15s;
}
.player-marker:hover {
  background: var(--blue); color: #fff;
  transform: translate(-50%, -50%) scale(1.15);
  box-shadow: 0 0 10px rgba(79,158,255,0.5);
}

/* DASH custom controls */
.dash-controls {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  background: rgba(0,0,0,0.3);
  border-top: 1px solid rgba(255,255,255,0.06);
  flex-shrink: 0;
}

.dash-play-btn {
  width: 38px; height: 38px;
  display: flex; align-items: center; justify-content: center;
  background: rgba(79,158,255,0.15);
  border: 1px solid rgba(79,158,255,0.35);
  border-radius: 50%;
  color: var(--blue);
  cursor: pointer; padding: 0; flex-shrink: 0;
  transition: background 0.15s, box-shadow 0.15s, transform 0.12s;
}
.dash-play-btn:hover {
  background: var(--blue);
  color: #fff;
  box-shadow: 0 0 16px rgba(79,158,255,0.45);
  transform: scale(1.06);
}
.dash-play-btn:active { transform: scale(0.96); }

/* Seekable progress track — clip window only */
.dash-progress {
  flex: 1;
  position: relative;
  height: 32px;
  cursor: pointer;
  display: flex;
  align-items: center;
}
.dash-progress-bg {
  position: absolute;
  left: 0; right: 0; top: 50%;
  height: 4px;
  transform: translateY(-50%);
  background: rgba(255,255,255,0.1);
  border-radius: 2px;
  pointer-events: none;
  transition: height 0.15s;
}
.dash-progress-fill {
  position: absolute;
  left: 0; top: 50%;
  height: 4px;
  transform: translateY(-50%);
  background: linear-gradient(to right, #3577d4, var(--blue));
  border-radius: 2px;
  pointer-events: none;
  transition: height 0.15s;
}
.dash-progress:hover .dash-progress-bg,
.dash-progress:hover .dash-progress-fill { height: 6px; }

.dash-playhead {
  position: absolute;
  top: 50%;
  width: 13px; height: 13px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 0 0 2px var(--blue), 0 2px 8px rgba(0,0,0,0.5);
  transform: translate(-50%, -50%) scale(0.6);
  opacity: 0;
  pointer-events: none;
  transition: left 0.08s linear, opacity 0.15s, transform 0.15s;
}
.dash-progress:hover .dash-playhead {
  opacity: 1;
  transform: translate(-50%, -50%) scale(1);
}

/* Kill event tick marks */
.dash-tick {
  position: absolute;
  top: 50%;
  width: 3px; height: 14px;
  transform: translate(-50%, -50%);
  background: var(--yellow);
  border-radius: 1px;
  border: none; padding: 0;
  cursor: pointer;
  opacity: 0.65;
  transition: opacity 0.12s, background 0.12s, height 0.12s, box-shadow 0.12s;
}
.dash-tick:hover {
  opacity: 1;
  height: 18px;
  background: var(--green);
  box-shadow: 0 0 8px rgba(0,229,160,0.5);
}

.dash-time {
  font-size: 11px;
  font-family: var(--font-mono);
  color: rgba(255,255,255,0.5);
  white-space: nowrap;
  flex-shrink: 0;
  min-width: 72px;
  text-align: right;
}

/* Trim controls */
.player-trim {
  background: rgba(0,0,0,0.2);
  border-top: 1px solid rgba(255,255,255,0.06);
  padding: 10px 14px 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  flex-shrink: 0;
}

.trim-section-label {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: rgba(255,255,255,0.25);
  margin-bottom: 2px;
}

.trim-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.trim-label {
  font-size: 10px;
  color: rgba(255,255,255,0.35);
  width: 36px;
  flex-shrink: 0;
  font-family: var(--font-mono);
}

/* Custom range slider */
.trim-slider {
  flex: 1;
  -webkit-appearance: none;
  appearance: none;
  height: 4px;
  border-radius: 2px;
  background: rgba(255,255,255,0.1);
  cursor: pointer;
  outline: none;
}
.trim-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 14px; height: 14px;
  border-radius: 50%;
  background: var(--blue);
  border: 2px solid #0d1117;
  box-shadow: 0 0 0 1px var(--blue);
  cursor: pointer;
  transition: transform 0.12s, box-shadow 0.12s;
}
.trim-slider::-webkit-slider-thumb:hover {
  transform: scale(1.2);
  box-shadow: 0 0 8px rgba(79,158,255,0.5);
}
.trim-slider::-moz-range-thumb {
  width: 14px; height: 14px;
  border-radius: 50%;
  background: var(--blue);
  border: 2px solid #0d1117;
  cursor: pointer;
}

.trim-val {
  font-size: 11px;
  font-family: var(--font-mono);
  color: rgba(255,255,255,0.35);
  width: 42px;
  text-align: right;
  flex-shrink: 0;
  transition: color 0.12s;
}
.trim-val.trim-pos { color: var(--green); }
.trim-val.trim-neg { color: var(--red); }

.trim-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 4px;
}
.trim-duration {
  font-size: 12px;
  font-family: var(--font-mono);
  color: rgba(255,255,255,0.6);
  font-weight: 500;
}
.trim-export-btn {
  font-size: 11px;
  padding: 5px 14px;
  border-radius: 6px;
}

.trimmed-badge {
  margin-left: 5px;
  padding: 1px 5px;
  font-size: 9px;
  border-radius: 3px;
  background: rgba(240,192,64,0.12);
  color: var(--yellow);
  border: 1px solid rgba(240,192,64,0.25);
  vertical-align: middle;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
</style>
