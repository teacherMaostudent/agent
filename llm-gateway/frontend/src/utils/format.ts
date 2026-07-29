export function asArray<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? value as T[] : []
}

export function objectSize(value: unknown) {
  return value && typeof value === 'object' ? Object.keys(value as Record<string, unknown>).length : 0
}

export function pretty(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2)
}

export function money(value: unknown) {
  const n = Number(value || 0)
  return `$${n.toFixed(n >= 1 ? 2 : 4)}`
}

export function percent(value: unknown) {
  const n = Number(value || 0)
  return `${n.toFixed(1)}%`
}
