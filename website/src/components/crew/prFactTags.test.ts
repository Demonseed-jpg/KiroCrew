import { describe, it, expect } from 'vitest'
import { checkerLine, prClosed, prFactTags } from './prFactTags'

const facts = (reads: Record<string, unknown>) => ({ label: 'a/b#1', url: 'u', reads }) as Parameters<typeof prFactTags>[0]

describe('prFactTags', () => {
  it('draws no state tag from a checker result; it is its own dated line', () => {
    const merged = facts({ accept_eval: { facts: { accept: 'pr_merged', verdict: 'pass' }, observed_at: 1 } })
    expect(prFactTags(merged)).toEqual([])
    expect(checkerLine(merged)).toMatch(/^checker \d\d:\d\d: merged$/)
    const checks = facts({ accept_eval: { facts: { accept: 'pr_checks', verdict: 'fail' }, observed_at: 1 } })
    expect(prFactTags(checks)).toEqual([])
    expect(checkerLine(checks)).toMatch(/^checker \d\d:\d\d: checks failed$/)
    // Tags come only from the welcome's own read.
    const warm = facts({ warm_brief: { facts: { state: 'closed' }, observed_at: 1 } })
    expect(prFactTags(warm).map(t => t.text)).toContain('closed')
    expect(checkerLine(warm)).toBe('')
  })

  it("decides closed from the welcome's own read only; the checker never hides or reopens", () => {
    expect(prClosed(facts({
      warm_brief: { facts: { state: 'merged' }, observed_at: 10 },
      accept_eval: { facts: { accept: 'pr_merged', verdict: 'pending' }, observed_at: 20 },
    }))).toBe(true)
    expect(prClosed(facts({
      warm_brief: { facts: { state: 'open' }, observed_at: 10 },
      accept_eval: { facts: { accept: 'pr_merged', verdict: 'pass' }, observed_at: 20 },
    }))).toBe(false)
    expect(prClosed(facts({ accept_eval: { facts: { accept: 'pr_merged', verdict: 'pass' }, observed_at: 20 } }))).toBe(false)
  })
})
