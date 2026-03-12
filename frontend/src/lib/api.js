const BASE = 'http://127.0.0.1:7847'

export async function getStatus() {
  const r = await fetch(`${BASE}/api/status`)
  return r.json()
}

export async function getDefaults() {
  const r = await fetch(`${BASE}/api/config/defaults`)
  return r.json()
}

export async function getSessions(recordingPath) {
  const url = recordingPath
    ? `${BASE}/api/sessions?recording_path=${encodeURIComponent(recordingPath)}`
    : `${BASE}/api/sessions`
  const r = await fetch(url)
  return r.json()
}

export async function stopExport() {
  await fetch(`${BASE}/api/stop`, { method: 'POST' })
}

export async function openOutput(outputFolder) {
  await fetch(`${BASE}/api/open-output?output_folder=${encodeURIComponent(outputFolder)}`, {
    method: 'POST',
  })
}

/**
 * POST to a streaming SSE endpoint.
 * Calls onEvent(event) for each SSE message.
 * Returns when the stream ends ('done' or 'error' event).
 */
export async function streamPost(path, body, onEvent) {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    const text = await response.text()
    onEvent({ type: 'error', message: `HTTP ${response.status}: ${text}` })
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split('\n')
    buffer = lines.pop() // keep incomplete last line

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const event = JSON.parse(line.slice(6))
          onEvent(event)
          if (event.type === 'done' || event.type === 'error') return
        } catch {
          // skip malformed line
        }
      }
    }
  }
}
