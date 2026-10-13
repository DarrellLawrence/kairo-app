export async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  })
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) throw new Error((data && (data.detail?.message || data.detail)) || `HTTP ${res.status}`)
  return data
}

// Server timestamps may or may not carry a time zone; naive ones are UTC.
export function parseTs(ts) {
  if (!ts) return null
  return new Date(/([zZ]|[+-]\d\d:?\d\d)$/.test(ts) ? ts : `${ts}Z`)
}
