/**
 * A work item's pull-request facts as short tags, for the Ultra Mode Board and the
 * Crew Board page: what gateway code last read (`pr_facts`, the `objects` fold), bad
 * facts first, then the read time. The words are the warm welcome card's own keys,
 * so the Board and the welcome name a state the same way.
 */
import { i18nT } from '../../i18n/t'
import type { CrewPrFacts } from '../../api/crewBoard'

export interface PrFactTag { text: string; bad: boolean }

const n = (v: unknown) => (Number.isInteger(v) && (v as number) > 0 ? (v as number) : 0)
const lower = (v: unknown) => (typeof v === 'string' ? v.toLowerCase() : '')

function clock(epoch: unknown): string {
  if (typeof epoch !== 'number' || !Number.isFinite(epoch)) return ''
  const d = new Date(epoch * 1000)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** Whether the warm welcome's own newest read shows the pull request merged or
 *  closed. PR state comes only from that read: the checker's verdict
 *  (`accept_eval`) is its own dated line and never hides or reopens a card. */
export function prClosed(facts: CrewPrFacts | undefined): boolean {
  const f = facts?.reads?.warm_brief?.facts ?? {}
  return f.state === 'merged' || f.state === 'closed'
}

export function prFactTags(facts: CrewPrFacts | undefined): PrFactTag[] {
  const tags: PrFactTag[] = []
  const warm = facts?.reads?.warm_brief
  if (warm && typeof warm === 'object') {
    const f = warm.facts ?? {}
    if (f.state === 'merged') tags.push({ text: i18nT('components.warmBriefCard.value_merged'), bad: false })
    else if (f.state === 'closed') tags.push({ text: i18nT('components.warmBriefCard.value_closed'), bad: true })
    if (lower(f.mergeable) === 'conflicting' || lower(f.mergeable) === 'dirty') tags.push({ text: i18nT('components.warmBriefCard.value_conflicts'), bad: true })
    if (lower(f.review) === 'changes_requested') tags.push({ text: i18nT('components.warmBriefCard.value_changes_requested'), bad: true })
    const c = f.checks && typeof f.checks === 'object' ? (f.checks as Record<string, unknown>) : null
    if (c && (n(c.failed) || n(c.passed))) tags.push({ text: i18nT('components.warmBriefCard.checks_counts', { passed: n(c.passed), failed: n(c.failed) }), bad: n(c.failed) > 0 })
    tags.push({ text: i18nT('components.crewPrFacts.read', { time: clock(warm.observed_at) }), bad: false })
  }
  return [...tags.filter(t => t.bad), ...tags.filter(t => !t.bad)]
}

/** The checker's (`accept_eval`) own dated line, "checker HH:MM: <verdict>", or ""
 *  when it has none. Never a state tag: PR state comes only from the warm read. */
export function checkerLine(facts: CrewPrFacts | undefined): string {
  const judged = facts?.reads?.accept_eval
  const v = judged?.facts?.verdict
  if (!judged || typeof v !== 'string') return ''
  const merge = judged.facts.accept === 'pr_merged'
  const verdict = merge
    ? i18nT(v === 'pass' ? 'components.warmBriefCard.value_merged' : v === 'fail' ? 'components.warmBriefCard.value_closed' : 'components.warmBriefCard.value_open')
    : i18nT(v === 'pass' ? 'components.crewPrFacts.verdict_pass' : v === 'fail' ? 'components.crewPrFacts.verdict_fail' : 'components.crewPrFacts.verdict_pending')
  return i18nT('components.crewPrFacts.checker', { time: clock(judged.observed_at), verdict })
}

/** "Conductor wrote at HH:MM" for a record's ISO `written_at`, or "". */
export function conductorSaid(writtenAt: string): string {
  const t = Date.parse(writtenAt)
  return Number.isFinite(t) ? i18nT('components.crewPrFacts.conductor_said', { time: clock(t / 1000) }) : ''
}
