import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Info, Layers, PencilLine, Repeat2, Send, Workflow, X } from 'lucide-react'
import { api, type DashboardLineage, type DashboardLineageHop } from '../../api/client'
import { apiErrorCode } from '../../api/apiError'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { MEMBER_DASHBOARD_QUERY_PREFIX, subscribeDashboardFields } from '../../hooks/useWebSocket'
import { isEditableElement } from '../../utils/editableTarget'
import {
  ACTIVATE_MESSAGE_TYPE, EDIT_KEY_MESSAGE_TYPE, EDIT_MESSAGE_TYPE, ESCAPE_MESSAGE_TYPE, FOCUS_SEQ, POINT_MESSAGE_TYPE, WHEEL_MESSAGE_TYPE,
  CORRECTION_REASON_LIMIT, HOVER_LEAVE_MS, declaredRoot, manifestFields, parseCorrection, pickedMessage, placePopover, popoverMaxHeight, popoverWidth, shownValue,
  type ChangeRequestOutcome, type FieldSource, type Picked, type Rect,
} from './dashboardEdit'
import { changeRequestMessage } from './dashboardChangeRequest.prompt'

/** The page's readiness beacon. Mirrors `dashboard_frame.READY_MESSAGE_TYPE`. */
const READY_MESSAGE_TYPE = 'kirocrew-dashboard:ready'
/** How long the pointer rests on a value before its lineage opens. */
const HOVER_DWELL_MS = 150
/** Popover size used for placement before it has measured itself. */
const POPOVER_SIZE = { w: 360, h: 380 }

/** `true` while the calling component is mounted: the fence for a completion that
 *  can land after the box that started it is gone. */
function useAlive(): { current: boolean } {
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  return alive
}

/** One value's lineage, keyed under the Bot's dashboard read; no *path* is all of them. */
function lineageQueryKey(slug: string, member: string, path?: string): unknown[] {
  return [MEMBER_DASHBOARD_QUERY_PREFIX, slug, member, 'lineage', ...(path === undefined ? [] : [path])]
}

/** Post to the dashboard frame. `'*'` because its origin is opaque (see CrewDynamicDashboard). */
function postToFrame(frame: HTMLIFrameElement | null, message: Record<string, unknown>): void {
  // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
  frame?.contentWindow?.postMessage(message, '*')
}

/** What the person is looking at: a value from the page, or one picked from Sources. */
interface Target {
  path: string
  rect: Rect
  field: FieldSource | null
  block: string
}

/** A component the person flipped over. */
interface Flip {
  block: string
  rect: Rect
  fields: FieldSource[]
  path: string
}

/**
 * Dashboard edit mode, drawn by the host over the Bot's sandboxed page
 * (preview `dashboard_edit_mode`).
 *
 * The page runs on an opaque origin, so the host never reads its DOM. In Edit mode a
 * capture layer over the frame takes the pointer and posts each point in; the page's
 * bootstrap answers with the field path under it (`dashboard_frame._EDIT_BRIDGE_JS`).
 * The host checks that path against the manifest it already holds and asks the gateway
 * for the lineage. Every word in the popover, the flipped card and Sources is the
 * gateway's or the host's, never the page's.
 *
 * Only the person can send a change request or correct a value: both are host UI, and
 * the page has no message that reaches either.
 */
export default function CrewDashboardEditLayer({ frameRef, frameKey, slug, member, botName, manifest, board, instanceVersion, onSend, onDone }: {
  frameRef: RefObject<HTMLIFrameElement | null>
  /** Changes when the frame mounts a new document, so Edit mode is posted to it again. */
  frameKey: string
  slug: string
  member: string
  botName: string
  manifest: unknown
  /** The page's name in a change request: its template id or package slug. */
  board: string
  instanceVersion: number
  /** Send the person's text as their own chat message to this Bot. Resolves whether it went. */
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onDone: () => void
}) {
  const { t } = useTranslation()
  const reduced = useReducedMotion()
  const fields = useMemo(() => manifestFields(manifest as Record<string, unknown>), [manifest])
  const overlayRef = useRef<HTMLDivElement | null>(null)
  const [box, setBox] = useState<Rect | null>(null)
  const [hover, setHover] = useState<Picked | null>(null)
  const [target, setTarget] = useState<Target | null>(null)
  const [refused, setRefused] = useState<string | null>(null)
  const [flip, setFlip] = useState<Flip | null>(null)
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const seqRef = useRef(0)
  const rafRef = useRef(0)
  /** True while the open popover's Edit is open: hover then leaves it alone. */
  const pinnedRef = useRef(false)
  const escRef = useRef<(() => boolean) | null>(null)
  const editKeyRef = useRef<(() => void) | null>(null)
  /** True while the pointer is inside the open popover. */
  const occupiedRef = useRef(false)
  const onPinned = useCallback((pinned: boolean) => {
    pinnedRef.current = pinned
    // Opening Edit cancels a dismissal already on its way.
    if (pinned) window.clearTimeout(leaveRef.current)
  }, [])
  /** Close the popover after the leave delay, unless by then it is pinned or the
   *  pointer is inside it. A late "nothing under the pointer" reply from the frame
   *  can arrive after the pointer entered the popover, so both are asked when the
   *  timer fires, not only when it is set. */
  const dismissLater = useCallback(() => {
    window.clearTimeout(leaveRef.current)
    leaveRef.current = window.setTimeout(() => {
      if (pinnedRef.current || occupiedRef.current) return
      setTarget(null)
      setRefused(null)
    }, HOVER_LEAVE_MS)
  }, [])
  const escape = useCallback(() => {
    if (escRef.current?.()) return
    occupiedRef.current = false
    setTarget(null)
    setFlip(null)
    setRefused(null)
    setSourcesOpen(false)
  }, [])
  const dwellRef = useRef<number | undefined>(undefined)
  const leaveRef = useRef<number | undefined>(undefined)
  /** Activation as of the last render: of the current hover (the keyboard's), and of
   *  a given pick (a tap's answer). */
  const activateRef = useRef<() => void>(() => {})
  const activateAtRef = useRef<(at: Picked | null) => void>(() => {})
  /** The point request a tap is waiting on, or `null`. */
  const tapSeqRef = useRef<number | null>(null)

  // THE FRAME'S BOX inside the tab, so frame coordinates and host coordinates meet.
  useLayoutEffect(() => {
    const frame = frameRef.current
    if (!frame) return
    const measure = () => setBox({ x: frame.offsetLeft, y: frame.offsetTop, w: frame.offsetWidth, h: frame.offsetHeight })
    measure()
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null
    ro?.observe(frame)
    if (frame.parentElement) ro?.observe(frame.parentElement)
    window.addEventListener('resize', measure)
    return () => { ro?.disconnect(); window.removeEventListener('resize', measure) }
  }, [frameRef, frameKey])

  // EDIT ON for this document, and again for each document the frame mounts.
  useEffect(() => {
    postToFrame(frameRef.current, { type: EDIT_MESSAGE_TYPE, on: true })
    const frame = frameRef.current
    return () => postToFrame(frame, { type: EDIT_MESSAGE_TYPE, on: false })
  }, [frameRef, frameKey])

  const showPicked = useCallback((picked: Picked) => {
    if (pinnedRef.current) return
    window.clearTimeout(dwellRef.current)
    if (!picked.path || !picked.rect) {
      dismissLater()
      return
    }
    window.clearTimeout(leaveRef.current)
    const path = picked.path
    const rect = picked.rect
    dwellRef.current = window.setTimeout(() => {
      const field = declaredRoot(path, fields)
      if (!field) {
        // THE PAGE POINTED AT A FIELD IT DOES NOT DECLARE: said, and nothing traced.
        setTarget(null)
        setRefused(path)
        return
      }
      setRefused(null)
      setTarget({ path, rect, field, block: picked.block })
    }, HOVER_DWELL_MS)
  }, [fields, dismissLater])

  // THE FRAME'S ANSWERS. Only from this frame's window; a stale point's answer is dropped.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const win = frameRef.current?.contentWindow
      if (!win || event.source !== win) return
      const kind = (event.data ?? {}) as { type?: unknown }
      if (kind.type === READY_MESSAGE_TYPE) {
        postToFrame(frameRef.current, { type: EDIT_MESSAGE_TYPE, on: true })
        return
      }
      // A FRAME MESSAGE NEVER DISCARDS A DRAFT. The page's own script posts from the
      // same window as the bridge, so the host cannot tell the two apart; a frame Esc,
      // click or Edit key therefore acts only while nothing typed can be lost: no flip
      // card open and no Edit open in the popover (`pinnedRef`). With a draft open,
      // only the person's own key or click in the host closes or replaces it.
      const draftOpen = pinnedRef.current || flip !== null
      if (kind.type === ESCAPE_MESSAGE_TYPE) { if (!draftOpen) escape(); return }
      // THE KEYBOARD'S CLICK: Enter or Space on the value focus picked opens its
      // component, through the same handler the pointer's click uses.
      if (kind.type === ACTIVATE_MESSAGE_TYPE) { if (!draftOpen) activateRef.current(); return }
      if (kind.type === EDIT_KEY_MESSAGE_TYPE) { if (!draftOpen) editKeyRef.current?.(); return }
      const picked = pickedMessage(event.data)
      if (!picked) return
      if (picked.seq !== FOCUS_SEQ && picked.seq !== seqRef.current) return
      setHover(picked)
      if (picked.seq === tapSeqRef.current) {
        // The answer for a tap: open what is under it.
        tapSeqRef.current = null
        activateAtRef.current(picked)
        return
      }
      if (!flip) showPicked(picked)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [frameRef, escape, showPicked, flip])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') escape() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [escape])

  useEffect(() => () => {
    window.clearTimeout(dwellRef.current)
    window.clearTimeout(leaveRef.current)
    cancelAnimationFrame(rafRef.current)
  }, [])

  const point = (x: number | null, y: number | null) => {
    cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(() => {
      seqRef.current += 1
      postToFrame(frameRef.current, { type: POINT_MESSAGE_TYPE, seq: seqRef.current, x, y })
    })
  }
  const local = (e: { clientX: number; clientY: number }) => {
    const r = frameRef.current?.getBoundingClientRect()
    return r ? { x: e.clientX - r.left, y: e.clientY - r.top } : null
  }

  /** Open the component (or the value) *at* names: the frame's answer for a point. */
  const activate = (at: Picked | null) => {
    if (!at) return
    if (at.block && at.blockRect) {
      const named = at.blockFields.map(name => fields.find(f => f.name === name)).filter((f): f is FieldSource => !!f)
      const picked = at.path ? declaredRoot(at.path, fields) : null
      const list = named.length ? named : picked ? [picked] : []
      setTarget(null)
      setFlip({ block: at.block, rect: at.blockRect, fields: list, path: at.path ?? '' })
      return
    }
    if (at.path && at.rect) {
      const field = declaredRoot(at.path, fields)
      if (field) setFlip({ block: '', rect: { x: at.rect.x - 8, y: at.rect.y - 8, w: Math.max(320, at.rect.w + 16), h: 240 }, fields: [field], path: at.path })
    }
  }

  // A TAP PICKS ITS OWN POINT. A touch, or a click with no move before it, has no
  // hover yet, and a hover answer still in flight names the previous spot. So the
  // tap asks the frame what is under it and opens that once the matching answer
  // arrives (`tapSeqRef`), never the cached hover.
  const onTap = (e: { clientX: number; clientY: number }) => {
    const p = local(e)
    if (!p) return
    cancelAnimationFrame(rafRef.current)
    seqRef.current += 1
    tapSeqRef.current = seqRef.current
    postToFrame(frameRef.current, { type: POINT_MESSAGE_TYPE, seq: seqRef.current, x: p.x, y: p.y })
  }

  // The keyboard activates what focus picked, which is the current hover.
  useLayoutEffect(() => {
    activateRef.current = () => activate(hover)
    activateAtRef.current = activate
  })

  const ctx = { board, version: instanceVersion }

  return (
    <>
      <div className="shrink-0 border-b border-border bg-accent-subtle px-3 py-2 flex items-center gap-2 text-[13px]" data-testid="dashboard-edit-bar" role="status">
        <Info size={14} className="shrink-0 text-accent" aria-hidden />
        <span className="flex-1 min-w-0 text-text">
          <strong className="font-semibold">{t('pages.membersPage.dashboardEdit.mode')}</strong>{' '}
          {t('pages.membersPage.dashboardEdit.bar', { bot: botName })}
        </span>
        <Btn onClick={() => setSourcesOpen(o => !o)} aria-expanded={sourcesOpen} data-testid="dashboard-edit-sources-button" className="shrink-0">
          <Layers size={13} className="lucide-inline" aria-hidden />
          {t('pages.membersPage.dashboardEdit.sources')}
          <span className="ml-1 text-muted tabular-nums">{fields.length}</span>
        </Btn>
        <Btn primary onClick={onDone} data-testid="dashboard-edit-done" className="shrink-0">
          {t('pages.membersPage.dashboardEdit.done')}
        </Btn>
      </div>
      {box && (
        <div ref={overlayRef} className="absolute z-20 pointer-events-none" style={{ left: box.x, top: box.y, width: box.w, height: box.h }} data-testid="dashboard-edit-overlay">
          {/* THE CAPTURE LAYER: the pointer cannot be seen over a cross-origin frame. */}
          {/* The pointer's half only: a keyboard focuses a value inside the frame (the
              bridge makes each one tabbable), and Enter or Space there posts
              ACTIVATE_MESSAGE_TYPE, which opens the same component a tap opens. */}
          <div
            role="presentation"
            className="absolute inset-0 pointer-events-auto cursor-crosshair"
            data-testid="dashboard-edit-capture"
            onPointerMove={e => { const p = local(e); if (p) point(p.x, p.y) }}
            onPointerLeave={() => point(null, null)}
            onWheel={e => {
              const p = local(e)
              postToFrame(frameRef.current, { type: WHEEL_MESSAGE_TYPE, dy: e.deltaY, x: p?.x, y: p?.y, ctrl: e.ctrlKey || e.metaKey })
            }}
            onClick={onTap}
          />
          {hover?.block && hover.blockRect && !flip && (
            <div
              aria-hidden
              className="absolute rounded-lg border-2 border-dashed border-accent/70 bg-accent/5"
              style={{ left: hover.blockRect.x, top: hover.blockRect.y, width: hover.blockRect.w, height: hover.blockRect.h }}
            />
          )}
          {refused && hover?.rect && (
            <div className="absolute pointer-events-auto rounded-md bg-bg-elevated px-2 py-1 shadow" style={{ left: hover.rect.x, top: hover.rect.y + hover.rect.h + 6 }}>
              {/* No hand-off: the page pointed at a field it does not declare, which is
                  the page's own markup, not a failure the agent can recover. */}
              <ErrorNotice variant="inline" message={t('pages.membersPage.dashboardEdit.refused', { path: refused })} testId="dashboard-edit-refused" />
            </div>
          )}
          {target && !flip && (
            <LineagePopover
              key={target.path}
              target={target}
              bounds={box}
              slug={slug}
              member={member}
              botName={botName}
              reduced={reduced}
              ctx={ctx}
              onSend={onSend}
              onEnter={() => { occupiedRef.current = true; window.clearTimeout(leaveRef.current) }}
              onLeave={() => { occupiedRef.current = false; dismissLater() }}
              onClose={() => { occupiedRef.current = false; setTarget(null) }}
              onPinned={onPinned}
              escRef={escRef}
              editKeyRef={editKeyRef}
            />
          )}
          {flip && (
            <FlipCard
              // One card per component: switching components mounts a fresh card,
              // so an earlier submission's lifetime never reaches the new one.
              key={`${flip.block}\u0000${flip.path}`}
              flip={flip}
              bounds={box}
              botName={botName}
              reduced={reduced}
              ctx={ctx}
              onSend={onSend}
              onClose={() => setFlip(null)}
            />
          )}
          {sourcesOpen && (
            <SourcesPanel
              fields={fields}
              botName={botName}
              onPick={(field, rect) => setTarget({ path: field.name, rect, field, block: '' })}
              onClose={() => setSourcesOpen(false)}
            />
          )}
        </div>
      )}
    </>
  )
}

/** Which part of the popover's Edit is open. */
type EditMode = 'none' | 'menu' | 'ask' | 'correct' | 'source'

/** One Edit option that does not apply here, with the one-line reason. */
function Greyed({ label, reason, testId }: { label: string; reason: string; testId: string }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-2.5 py-1.5 opacity-70" data-testid={testId} aria-disabled="true">
      {/* Dimmed, never struck through: a struck-out option reads as deleted, and
          this one is only unavailable here, for the reason under it. */}
      <div className="text-[12.5px] text-muted">{label}</div>
      <div className="text-[11.5px] text-muted">{reason}</div>
    </div>
  )
}

/**
 * Edit on one value, by what kind of value it is (the demo's table):
 * - any value: ask the Bot to change it;
 * - a value the Bot writes: correct it (not a whole list or record);
 * - a script's record: the job behind it, Run now (asked first), Open Schedule;
 * - a folded value: ask the Bot to change how it is shown.
 * An option that does not apply is shown greyed with its reason.
 */
function EditPanel({ lineage, mode, setMode, slug, member, botName, ctx, target, onSend, onClose }: {
  lineage: DashboardLineage
  mode: EditMode
  setMode: (m: EditMode) => void
  slug: string
  member: string
  botName: string
  ctx: { board: string; version: number }
  target: Target
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onClose: () => void
}) {
  const { t } = useTranslation()
  const edit = lineage.edit
  const ask = edit.kind === 'fold' ? t('pages.membersPage.dashboardEdit.change_shown', { bot: botName }) : t('pages.membersPage.dashboardEdit.ask', { bot: botName })
  if (mode === 'ask') return <AskBox compact botName={botName} ctx={ctx} block={target.block} path={target.path} onSend={onSend} onSent={onClose} />
  if (mode === 'correct' && edit.kind === 'correct') return <CorrectBox slug={slug} member={member} field={lineage.field} type={edit.type} onDone={() => setMode('none')} />
  if (mode === 'source' && edit.kind === 'source') return <SourceBox job={edit.job} lineage={lineage} />
  return (
    <div className="space-y-1.5" data-testid="lineage-edit-menu">
      <Btn onClick={() => setMode('ask')} data-testid="lineage-ask" className="w-full justify-start">{ask}</Btn>
      {edit.kind === 'correct' && (edit.scalar
        ? <Btn onClick={() => setMode('correct')} data-testid="lineage-correct" className="w-full justify-start">{t('pages.membersPage.dashboardEdit.correct')}</Btn>
        : <Greyed testId="lineage-correct-greyed" label={t('pages.membersPage.dashboardEdit.correct')} reason={t('pages.membersPage.dashboardEdit.no_correct_shape', { bot: botName })} />)}
      {edit.kind === 'correct' && <Greyed testId="lineage-source-greyed" label={t('pages.membersPage.dashboardEdit.edit_source')} reason={t('pages.membersPage.dashboardEdit.no_source_agentic', { bot: botName })} />}
      {edit.kind === 'source' && (edit.job
        ? <Btn onClick={() => setMode('source')} data-testid="lineage-source" className="w-full justify-start">{t('pages.membersPage.dashboardEdit.edit_source')}</Btn>
        : <Greyed testId="lineage-source-greyed" label={t('pages.membersPage.dashboardEdit.edit_source')} reason={t('pages.membersPage.dashboardEdit.no_source_job')} />)}
      {edit.kind === 'source' && <Greyed testId="lineage-correct-greyed" label={t('pages.membersPage.dashboardEdit.correct')} reason={t('pages.membersPage.dashboardEdit.no_edit_cron')} />}
      {edit.kind === 'fold' && <Greyed testId="lineage-open-item-greyed" label={t('pages.membersPage.dashboardEdit.open_item')} reason={t('pages.membersPage.dashboardEdit.needs', { what: 'entry seq on fold rows' })} />}
      {edit.kind === 'fold' && <Greyed testId="lineage-correct-greyed" label={t('pages.membersPage.dashboardEdit.correct')} reason={t('pages.membersPage.dashboardEdit.no_edit_fold', { bot: botName })} />}
    </div>
  )
}

/** The script job behind a record: what runs it, how long a record holds, how the last
 *  run went, and Run now, which asks first because a run costs one. */
function SourceBox({ job, lineage }: { job: string; lineage: DashboardLineage }) {
  const { t } = useTranslation()
  // Under ['crons'], so every cron refresh frame re-reads the job's last run here too.
  const { data, isError: cronsFailed } = useQuery({ queryKey: ['crons', 'dashboard-edit', job], queryFn: () => api.crons(), staleTime: 15_000 })
  const found = data?.jobs?.find(j => j.id === job)
  const freshness = lineage.hops.find(h => h.kind === 'freshness')
  const script = lineage.hops.find(h => h.kind === 'script')
  const [confirming, setConfirming] = useState(false)
  const [run, setRun] = useState<'idle' | 'running' | 'started' | 'failed'>('idle')
  const runNow = async () => {
    setRun('running')
    try { await api.runCron(job); setRun('started') } catch { setRun('failed') }
    setConfirming(false)
  }
  const row = (label: string, value: ReactNode) => (
    <div className="grid grid-cols-[6.5rem_1fr] gap-2"><dt className="text-muted">{label}</dt><dd className="m-0 min-w-0 break-words font-mono text-text">{value}</dd></div>
  )
  return (
    <div className="space-y-2" data-testid="lineage-source-box">
      {/* A failed read of the schedules, said, so a blank row is not read as "no
          schedule". A read with no draft beside it, so the hand-off is on. */}
      {cronsFailed && <ErrorNotice variant="inline" askAgent message={t('pages.membersPage.schedule_load_failed')} testId="lineage-crons-failed" />}
      <dl className="m-0 space-y-1 rounded-lg border border-border p-2 text-[12px]">
        {row(t('pages.membersPage.dashboardEdit.src_job'), found?.name || job)}
        {row(t('pages.membersPage.dashboardEdit.src_schedule'), found?.schedule || '\u2014')}
        {row(t('pages.membersPage.dashboardEdit.src_max_age'), typeof freshness?.max_age_s === 'number' ? String(freshness.max_age_s) : '\u2014')}
        {row(t('pages.membersPage.dashboardEdit.src_script'), String(script?.script || found?.script || '\u2014'))}
        {row(t('pages.membersPage.dashboardEdit.src_last_run'), found?.last_error ? t('pages.membersPage.schedule_failed') : (found?.last_status || '\u2014'))}
      </dl>
      {/* The job's last run failed. The source panel holds nothing typed (it replaces
          the Ask box rather than sitting beside it), so the hand-off is on. */}
      {found?.last_error && <ErrorNotice variant="inline" askAgent message={found.last_error} testId="lineage-job-failed" />}
      {confirming ? (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-warn bg-warn-subtle px-2 py-1.5 text-[12px]" role="alertdialog" data-testid="lineage-run-confirm">
          <span className="flex-1">{t('pages.membersPage.dashboardEdit.run_confirm')}</span>
          <Btn onClick={() => setConfirming(false)}>{t('pages.membersPage.dashboardEdit.cancel')}</Btn>
          <Btn primary onClick={() => void runNow()} data-testid="lineage-run-yes">{t('pages.membersPage.dashboardEdit.run_now')}</Btn>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Btn onClick={() => setConfirming(true)} disabled={run === 'running'} data-testid="lineage-run">{t('pages.membersPage.dashboardEdit.run_now')}</Btn>
          <a href="/schedule" target="_blank" rel="noopener noreferrer" className="text-[12.5px] text-accent hover:underline">{t('pages.membersPage.dashboardEdit.open_schedule')}</a>
          <span className="text-[11.5px] text-muted" role="status">
            {run === 'started' ? t('pages.membersPage.dashboardEdit.run_started') : ''}
          </span>
          {/* The run request failed. Nothing is typed in this box, so the hand-off is on. */}
          {run === 'failed' && <ErrorNotice variant="inline" askAgent message={t('pages.membersPage.dashboardEdit.run_failed')} testId="lineage-run-failed" />}
        </div>
      )}
    </div>
  )
}

/** The kind tag beside a field: where its value comes from, in one word. */
function KindTag({ kind }: { kind: FieldSource['kind'] | DashboardLineage['kind'] }) {
  const { t } = useTranslation()
  return (
    <span className="shrink-0 rounded-full border border-accent/40 bg-accent-subtle px-2 py-px font-mono text-[11px] text-accent">
      {kind === 'agentic' ? t('pages.membersPage.dashboardEdit.tag_agentic') : kind === 'custom' ? t('pages.membersPage.dashboardEdit.tag_script') : t('pages.membersPage.dashboardEdit.tag_fold')}
    </span>
  )
}

/** A fold field's source as code: `fold → path`. */
function SourceLine({ field }: { field: FieldSource }) {
  return <>{field.fold} {'\u2192'} {field.path}</>
}

/** One hop of a lineage: its label, what it says, and who vouches for it. */
function Hop({ hop, botName, index, reduced }: { hop: DashboardLineageHop; botName: string; index: number; reduced: boolean }) {
  const { t } = useTranslation()
  const s = (key: string) => (typeof hop[key] === 'string' ? (hop[key] as string) : '')
  let label = ''
  let body: ReactNode = null
  switch (hop.kind) {
    case 'page':
      label = t('pages.membersPage.dashboardEdit.hop_page')
      body = <><code className="text-accent">{s('path')}</code><Sub>{t('pages.membersPage.dashboardEdit.hop_page_note')}</Sub></>
      break
    case 'field':
      label = t('pages.membersPage.dashboardEdit.hop_field')
      body = <><code className="text-accent">{s('field')} {'\u2190'} {hop.agentic ? '{agentic: true}' : `{fold: "${s('fold')}", path: "${s('path')}"}`}</code><Sub>{t('pages.membersPage.dashboardEdit.hop_type', { type: s('type') })}</Sub></>
      break
    case 'fold':
      label = t('pages.membersPage.dashboardEdit.hop_fold')
      body = <><code className="text-accent">{s('fold')} {'\u2192'} {s('path')}</code>{hop.found === false && <Sub warn>{t('pages.membersPage.dashboardEdit.hop_not_found')}</Sub>}</>
      break
    case 'crew_log':
      label = t('pages.membersPage.dashboardEdit.hop_crew_log')
      body = <Sub>{t('pages.membersPage.dashboardEdit.hop_crew_log_desc', { fold: s('fold') })}</Sub>
      break
    case 'record':
      label = t('pages.membersPage.dashboardEdit.hop_record')
      body = hop.found === false
        ? <Sub warn>{t('pages.membersPage.dashboardEdit.hop_record_missing')}</Sub>
        : <><span><code className="text-accent">{s('writer')}</code> {s('at')}</span>{typeof hop.seq === 'number' && <Sub>{t('pages.membersPage.dashboardEdit.hop_seq', { seq: hop.seq })}</Sub>}</>
      break
    case 'script':
      label = t('pages.membersPage.dashboardEdit.hop_script')
      body = (
        <>
          <code className="text-accent break-all">{s('script') || s('job')}</code>
          <Sub>{[s('job'), s('run_at')].filter(Boolean).join(' \u00b7 ')}</Sub>
          {s('script_sha') && <Sub><span className="font-mono">{s('script_sha').slice(0, 12)}</span></Sub>}
        </>
      )
      break
    case 'source': {
      label = t('pages.membersPage.dashboardEdit.hop_source')
      const points = Array.isArray(hop.points) ? (hop.points as unknown[]).map(p => (Array.isArray(p) ? Number(p[1]) : NaN)).filter(Number.isFinite) : []
      const source = (hop.source && typeof hop.source === 'object' ? hop.source : {}) as Record<string, unknown>
      const url = typeof source.console_url === 'string' && source.console_url.startsWith('https://') ? source.console_url : ''
      body = (
        <>
          <dl className="m-0 mt-0.5 space-y-0.5 font-mono text-[11.5px]">
            {Object.entries(source).filter(([k]) => k !== 'console_url').map(([k, v]) => (
              <div key={k} className="flex gap-2"><dt className="shrink-0 text-muted">{k}</dt><dd className="m-0 min-w-0 break-all">{shownValue(v)}</dd></div>
            ))}
          </dl>
          {url && <a href={url} target="_blank" rel="noopener noreferrer" className="text-[11.5px] text-accent hover:underline">{t('pages.membersPage.dashboardEdit.open_console')}</a>}
          {points.length > 0 && <Spark points={points} />}
          <Sub>{t('pages.membersPage.dashboardEdit.source_unchecked')}</Sub>
        </>
      )
      break
    }
    case 'freshness':
      label = t('pages.membersPage.dashboardEdit.hop_freshness')
      body = hop.needs ? null : (
        <span>
          {s('stale_after') ? t('pages.membersPage.dashboardEdit.stale_after', { at: s('stale_after') }) : t('pages.membersPage.dashboardEdit.no_stale_limit')}
          {typeof hop.max_age_s === 'number' && <Sub>{t('pages.membersPage.dashboardEdit.max_age', { seconds: hop.max_age_s })}</Sub>}
        </span>
      )
      break
    case 'run':
      label = t('pages.membersPage.dashboardEdit.hop_run')
      // No hand-off: this popover can hold an unsent change request under Edit, which
      // the hand-off's navigation would drop.
      body = <ErrorNotice variant="inline" message={s('last_error') || s('failed_at')} title={s('last_error') ? s('failed_at') : undefined} testId="lineage-hop-run-failed" />
      break
    case 'write':
    case 'earlier_write':
      label = hop.kind === 'write' ? t('pages.membersPage.dashboardEdit.hop_write') : t('pages.membersPage.dashboardEdit.hop_earlier')
      body = hop.found === false
        ? <Sub warn>{t('pages.membersPage.dashboardEdit.hop_write_missing')}</Sub>
        : <><span>{hop.actor === 'person' ? t('pages.membersPage.dashboardEdit.by_person', { at: s('at') }) : t('pages.membersPage.dashboardEdit.by_bot', { bot: botName, at: s('at') })}</span>{s('reason') && <Sub>{'\u201c'}{s('reason')}{'\u201d'}</Sub>}</>
      break
    case 'looked_at':
      label = t('pages.membersPage.dashboardEdit.hop_looked_at', { bot: botName })
      break
  }
  return (
    <li
      className={`relative pl-5 pb-3 ${reduced ? '' : 'motion-safe:animate-rise'}`}
      style={reduced ? undefined : { animationDelay: `${index * 250}ms` }}
      data-testid={`lineage-hop-${hop.kind}`}
    >
      <span aria-hidden className="absolute left-0 top-1 h-2.5 w-2.5 rounded-full border-2 border-accent bg-bg-elevated" />
      <div className="flex flex-wrap items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider text-muted">
        <span>{label}</span>
        {hop.needs && <span className="rounded border border-border px-1 normal-case tracking-normal">{t('pages.membersPage.dashboardEdit.needs', { what: hop.needs })}</span>}
        {hop.by && <span className="normal-case tracking-normal">{hop.by === 'script' ? t('pages.membersPage.dashboardEdit.by_script') : t('pages.membersPage.dashboardEdit.by_gateway')}</span>}
      </div>
      {body && <div className="text-[12.5px] text-text">{body}</div>}
    </li>
  )
}

function Sub({ children, warn = false }: { children: ReactNode; warn?: boolean }) {
  return <div className={`text-[11.5px] ${warn ? 'text-warn-text' : 'text-muted'}`}>{children}</div>
}

/** The raw readings a script declared, as a small bar row. */
function Spark({ points }: { points: number[] }) {
  const max = Math.max(...points.map(Math.abs), 1)
  return (
    <div aria-hidden className="mt-1 flex h-6 items-end gap-px">
      {points.slice(-40).map((p, i) => (
        <span key={i} className="w-1 rounded-sm bg-accent/70" style={{ height: `${Math.max(8, (Math.abs(p) / max) * 100)}%` }} />
      ))}
    </div>
  )
}

/** The comment box shared by the flipped card and the popover's "ask" action. */
function AskBox({ botName, ctx, block, path, onSend, onSent, compact = false }: {
  botName: string
  ctx: { board: string; version: number }
  block: string
  path: string
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onSent: () => void
  compact?: boolean
}) {
  const { t } = useTranslation()
  const [text, setText] = useState('')
  const [state, setState] = useState<'idle' | 'sending' | 'sent' | 'failed' | 'unconfirmed'>('idle')
  // THIS BOX'S LIFETIME FENCES ITS SEND. `onSent` closes whatever card is open, so
  // a completion that lands after this box closed would close ANOTHER component's
  // card and drop its draft. Once unmounted, the answer is ignored and the pending
  // close is cancelled.
  const alive = useAlive()
  const closeRef = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(closeRef.current), [])
  const send = async () => {
    if (!onSend || !text.trim() || state === 'sending' || state === 'unconfirmed') return
    setState('sending')
    // A throw is as unknown as a lost answer: the request may have gone out.
    const outcome = await onSend(changeRequestMessage(text, { ...ctx, block, path })).catch(() => 'unconfirmed' as const)
    if (!alive.current) return
    setState(outcome === 'sent' ? 'sent' : outcome === 'refused' ? 'failed' : 'unconfirmed')
    if (outcome === 'sent') closeRef.current = window.setTimeout(() => { if (alive.current) onSent() }, 900)
  }
  return (
    <div className="space-y-1.5">
      <label id={`ask-${block}-${path}-label`} className="block text-[12.5px] font-medium text-text-strong" htmlFor={`ask-${block}-${path}`}>
        {t('pages.membersPage.dashboardEdit.comment_label')}
      </label>
      <div className="flex flex-wrap gap-1 font-mono text-[11px] text-muted" data-testid="dashboard-edit-chip">
        <span className="rounded border border-border px-1">{ctx.board}</span>
        {block && <span className="rounded border border-border px-1">{block}</span>}
        {path && <span className="rounded border border-border px-1">{path}</span>}
        <span className="rounded border border-border px-1">v{ctx.version}</span>
      </div>
      <textarea
        id={`ask-${block}-${path}`}
        aria-labelledby={`ask-${block}-${path}-label`}
        value={text}
        // Editing the text makes it a new request, which may be sent.
        onChange={e => { setText(e.target.value); if (state === 'unconfirmed') setState('idle') }}
        // LOCKED FROM SEND TO CLOSE: the text on screen is the text sent, so the close
        // that follows a sent request can never take an edit nobody sent with it.
        readOnly={state === 'sending' || state === 'sent'}
        aria-readonly={state === 'sending' || state === 'sent' || undefined}
        rows={compact ? 2 : 3}
        className="w-full resize-y rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus:border-accent focus:outline-none"
        data-testid="dashboard-edit-comment"
      />
      <div className="flex items-center gap-2">
        {state === 'failed' ? (
          // No hand-off: the request typed above is unsent local state, which the
          // hand-off's navigation would drop.
          <ErrorNotice variant="inline" className="flex-1" message={t('pages.membersPage.dashboardEdit.send_failed')} testId="dashboard-edit-send-failed" />
        ) : state === 'unconfirmed' ? (
          // No hand-off, for the same unsent draft. Send stays off: the request may
          // already be in the chat, and sending it again would ask twice.
          <ErrorNotice variant="inline" className="flex-1" message={t('pages.membersPage.dashboardEdit.send_unconfirmed', { bot: botName })} testId="dashboard-edit-send-unconfirmed" />
        ) : (
          <span className="flex-1 text-[11.5px] text-muted" role="status">
            {state === 'sent' ? t('pages.membersPage.dashboardEdit.sent', { bot: botName })
              : !onSend ? t('pages.membersPage.dashboardEdit.no_chat')
                : t('pages.membersPage.dashboardEdit.comment_hint', { bot: botName })}
          </span>
        )}
        <Btn primary onClick={() => void send()} disabled={!onSend || !text.trim() || state === 'sending' || state === 'sent' || state === 'unconfirmed'} data-testid="dashboard-edit-send">
          <Send size={13} className="lucide-inline" aria-hidden />
          {t('pages.membersPage.dashboardEdit.send', { bot: botName })}
        </Btn>
      </div>
    </div>
  )
}

/** "Correct the value" for a field the Bot writes. */
function CorrectBox({ slug, member, field, type, onDone }: { slug: string; member: string; field: string; type: string; onDone: () => void }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [reason, setReason] = useState('')
  /** The typed value does not read as the field's type: a check of the input, kept
   *  apart from a request the gateway refused or that failed. */
  const [invalid, setInvalid] = useState(false)
  const [failed, setFailed] = useState(false)
  const [saving, setSaving] = useState(false)
  // Fenced like the Ask box: `onDone` resets the popover's Edit mode, so a save that
  // completes after this box closed must not reset a newer Ask draft opened since.
  const alive = useAlive()
  const save = async () => {
    const parsed = parseCorrection(text, type)
    setFailed(false)
    if (!parsed) { setInvalid(true); return }
    setInvalid(false)
    setSaving(true)
    try {
      await api.memberDashboardCorrect(slug, member, field, parsed.value, reason.trim())
      // The pages re-read either way: the correction is written whether or not
      // this box is still open.
      void qc.invalidateQueries({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, slug] })
      if (!alive.current) return
      onDone()
    } catch (e) {
      if (!alive.current) return
      if (apiErrorCode(e) === 'wrong_type') setInvalid(true)
      else setFailed(true)
    } finally {
      if (alive.current) setSaving(false)
    }
  }
  const hintId = `correct-${field}-hint`
  return (
    <div className="space-y-1.5" data-testid="dashboard-edit-correct">
      <input
        value={text}
        onChange={e => { setText(e.target.value); setInvalid(false) }}
        // Locked while the save is out, for the same reason as the Ask box.
        readOnly={saving}
        aria-label={t('pages.membersPage.dashboardEdit.correct_value')}
        aria-invalid={invalid || undefined}
        aria-describedby={invalid ? hintId : undefined}
        placeholder={t('pages.membersPage.dashboardEdit.correct_value')}
        className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 font-mono text-[13px] text-text focus:border-accent focus:outline-none"
      />
      <input
        value={reason}
        onChange={e => setReason(e.target.value.slice(0, CORRECTION_REASON_LIMIT))}
        readOnly={saving}
        aria-label={t('pages.membersPage.dashboardEdit.correct_reason')}
        placeholder={t('pages.membersPage.dashboardEdit.correct_reason')}
        className="w-full rounded-lg border border-border bg-bg px-2.5 py-1.5 text-[13px] text-text focus:border-accent focus:outline-none"
      />
      {/* No hand-off: the value and reason typed above are unsaved, and the
          hand-off's navigation would drop them. */}
      {failed && <ErrorNotice variant="inline" message={t('pages.membersPage.dashboardEdit.correct_failed')} testId="dashboard-edit-correct-failed" />}
      <div className="flex items-center gap-2">
        <span id={hintId} className="flex-1 text-[11.5px] text-muted" role="status" data-testid="dashboard-edit-correct-hint">
          {invalid ? t('pages.membersPage.dashboardEdit.correct_bad', { type }) : ''}
        </span>
        <Btn primary onClick={() => void save()} disabled={saving || !text.trim()} data-testid="dashboard-edit-correct-save">
          {t('pages.membersPage.dashboardEdit.correct_save')}
        </Btn>
      </div>
    </div>
  )
}

/** The lineage popover beside one value, with its line to the value and its Edit row. */
function LineagePopover({ target, bounds, slug, member, botName, reduced, ctx, onSend, onEnter, onLeave, onClose, onPinned, escRef, editKeyRef }: {
  target: Target
  bounds: Rect
  slug: string
  member: string
  botName: string
  reduced: boolean
  ctx: { board: string; version: number }
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onEnter: () => void
  onLeave: () => void
  onClose: () => void
  /** Told whether Edit is open: while it is, the popover stays put. */
  onPinned: (pinned: boolean) => void
  /** The layer's Esc asks this first: true means Esc closed Edit and nothing else. */
  escRef: { current: (() => boolean) | null }
  /** The layer calls this when the frame forwards the Edit shortcut. */
  editKeyRef: { current: (() => void) | null }
}) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDivElement | null>(null)
  const [size, setSize] = useState(POPOVER_SIZE)
  const [mode, setMode] = useState<EditMode>('none')
  // EDIT PINS THE POPOVER: moving away or hovering another value leaves it open, and
  // the first Esc closes Edit while a second closes the popover.
  useEffect(() => { onPinned(mode !== 'none') }, [mode, onPinned])
  useEffect(() => () => onPinned(false), [onPinned])
  useEffect(() => {
    escRef.current = () => {
      if (mode === 'none') return false
      setMode('none')
      return true
    }
    return () => { escRef.current = null }
  }, [escRef, mode])
  // The same shortcut pressed on a value focused INSIDE the frame, forwarded by the
  // bridge (EDIT_KEY_MESSAGE_TYPE) because that key never reaches this window.
  useEffect(() => {
    editKeyRef.current = () => { if (mode === 'none') setMode('menu') }
    return () => { editKeyRef.current = null }
  }, [editKeyRef, mode])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Never a letter typed anywhere: not in a field (the chat composer is a
      // contenteditable editor), and not with a modifier held.
      if (isEditableElement(e.target) || e.ctrlKey || e.metaKey || e.altKey) return
      if (mode === 'none' && (e.key === 'e' || e.key === 'E')) { e.preventDefault(); setMode('menu') }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mode])
  const qc = useQueryClient()
  // UNDER THE PAGE'S OWN PREFIX, so every signal that re-reads the page (a value
  // written, the page moved, a refresh frame) re-reads the open lineage with it.
  const lineageKey = lineageQueryKey(slug, member, target.path)
  // A field push refills only the frame, so it re-reads the open lineage itself.
  useEffect(
    () => subscribeDashboardFields(slug, () => { void qc.invalidateQueries({ queryKey: lineageQueryKey(slug, member) }) }),
    [qc, slug, member],
  )
  const { data, isLoading, isError } = useQuery({
    queryKey: lineageKey,
    queryFn: () => api.memberDashboardLineage(slug, member, target.path),
    staleTime: 15_000,
    retry: false,
  })
  useLayoutEffect(() => {
    const el = ref.current
    if (el) setSize({ w: el.offsetWidth || POPOVER_SIZE.w, h: el.offsetHeight || POPOVER_SIZE.h })
  }, [data, mode])
  const width = popoverWidth(bounds.w)
  const place = placePopover(target.rect, { w: width, h: size.h }, bounds)
  const anchorX = place.side === 'left' ? target.rect.x : place.side === 'right' ? target.rect.x + target.rect.w : target.rect.x + target.rect.w / 2
  const anchorY = place.side === 'above' ? target.rect.y : place.side === 'below' ? target.rect.y + target.rect.h : target.rect.y + target.rect.h / 2
  const endX = place.side === 'left' ? place.x + width : place.side === 'right' ? place.x : anchorX
  const endY = place.side === 'above' ? place.y + size.h : place.side === 'below' ? place.y : Math.min(Math.max(anchorY, place.y + 16), place.y + size.h - 16)
  const lineage = data as DashboardLineage | undefined
  return (
    <>
      <svg aria-hidden className="pointer-events-none absolute inset-0 h-full w-full overflow-visible" style={{ zIndex: Z_PICKED }} data-testid="lineage-connector">
        <rect x={target.rect.x - 3} y={target.rect.y - 3} width={target.rect.w + 6} height={target.rect.h + 6} rx={6} className="fill-none stroke-accent" strokeWidth={2} />
        <line x1={anchorX} y1={anchorY} x2={endX} y2={endY} className="stroke-accent" strokeWidth={1.5} strokeDasharray="4 3" />
      </svg>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- the mouse handlers only hold the hover popover open while the pointer is over it; a keyboard opens it pinned and closes it with Escape, and every action inside is a real button */}
      <div
        ref={ref}
        role="dialog"
        aria-label={t('pages.membersPage.dashboardEdit.where_from')}
        className={`absolute pointer-events-auto max-h-[70vh] overflow-y-auto rounded-xl border border-accent/50 bg-bg-elevated p-3 shadow-xl ${reduced ? '' : 'motion-safe:animate-scale-in'}`}
        style={{ left: place.x, top: place.y, width, maxHeight: Math.min(popoverMaxHeight(bounds.h), window.innerHeight * 0.7), zIndex: Z_PICKED }}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
        data-testid="lineage-popover"
        data-side={place.side}
      >
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="truncate text-[22px] font-semibold text-text-strong tabular-nums" data-testid="lineage-value">{lineage ? shownValue(lineage.value) : '\u2026'}</div>
            <div className="truncate text-[12px] text-muted">{target.field?.label || target.path}</div>
          </div>
          {lineage && <KindTag kind={lineage.kind} />}
          {lineage && (
            <button
              type="button"
              onClick={() => setMode(m => (m === 'none' ? 'menu' : 'none'))}
              aria-label={t('pages.membersPage.dashboardEdit.edit_value')}
              aria-pressed={mode !== 'none'}
              className={`rounded p-0.5 bg-transparent border-none cursor-pointer ${mode !== 'none' ? 'text-accent' : 'text-muted hover:text-text'}`}
              data-testid="lineage-pencil"
            >
              <PencilLine size={14} aria-hidden />
            </button>
          )}
          <button type="button" onClick={onClose} aria-label={t('pages.membersPage.dashboardEdit.close')} className="rounded p-0.5 text-muted hover:text-text bg-transparent border-none cursor-pointer">
            <X size={14} aria-hidden />
          </button>
        </div>
        <div className="mt-2 mb-1.5 flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-wider text-accent">
          <Workflow size={12} aria-hidden />{t('pages.membersPage.dashboardEdit.where_from')}
        </div>
        {isLoading && <p className="text-[12px] text-muted" role="status">{t('pages.membersPage.dashboardEdit.tracing')}</p>}
        {/* The lineage read failed, so no Edit is open and nothing typed can be lost:
            the hand-off is on. */}
        {isError && <ErrorNotice variant="inline" askAgent message={t('pages.membersPage.dashboardEdit.trace_failed')} testId="lineage-trace-failed" />}
        {lineage && (
          <ol className="relative list-none m-0 p-0 before:absolute before:left-[4px] before:top-2 before:bottom-3 before:w-px before:bg-accent/40">
            <Hop hop={{ kind: 'page', path: target.path }} botName={botName} index={0} reduced={reduced} key="page" />
            {lineage.hops.map((hop, i) => <Hop key={i} hop={hop} botName={botName} index={i + 1} reduced={reduced} />)}
          </ol>
        )}
        {lineage && (
          <div className="mt-1 border-t border-border pt-2 space-y-2" data-testid="lineage-edit">
            {mode === 'none' ? (
              <Btn onClick={() => setMode('menu')} data-testid="lineage-edit-open">
                <PencilLine size={13} className="lucide-inline" aria-hidden />
                {t('pages.membersPage.dashboardEdit.edit_value')}
                <kbd className="ml-1 rounded border border-border px-1 font-mono text-[10px]">E</kbd>
              </Btn>
            ) : (
              <EditPanel
                lineage={lineage}
                mode={mode}
                setMode={setMode}
                slug={slug}
                member={member}
                botName={botName}
                ctx={ctx}
                target={target}
                onSend={onSend}
                onClose={onClose}
              />
            )}
          </div>
        )}
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-muted">
          <Info size={12} aria-hidden />
          <span className="flex-1">{t('pages.membersPage.dashboardEdit.footer')}</span>
          <kbd className="rounded border border-border px-1 font-mono text-[10px]">Esc</kbd>
        </p>
      </div>
    </>
  )
}

/** The component turned over: its provider, and the box that asks the Bot to change it. */
function FlipCard({ flip, bounds, botName, reduced, ctx, onSend, onClose }: {
  flip: Flip
  bounds: Rect
  botName: string
  reduced: boolean
  ctx: { board: string; version: number }
  onSend?: (text: string) => Promise<ChangeRequestOutcome>
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [turned, setTurned] = useState(reduced)
  useEffect(() => {
    if (reduced) return
    const id = requestAnimationFrame(() => setTurned(true))
    return () => cancelAnimationFrame(id)
  }, [reduced])
  const w = Math.min(Math.max(flip.rect.w, 340), bounds.w - 8)
  const left = Math.max(4, Math.min(flip.rect.x, bounds.w - w - 4))
  const top = Math.max(4, Math.min(flip.rect.y, bounds.h - 300))
  return (
    <div className="absolute pointer-events-auto" style={{ left, top, width: w, perspective: 1200, zIndex: Z_PICKED }} data-testid="flip-card-shell">
      <div
        role="dialog"
        aria-label={t('pages.membersPage.dashboardEdit.flip_title', { block: flip.block || t('pages.membersPage.dashboardEdit.this_value') })}
        className="rounded-xl border-2 border-accent/60 bg-bg-elevated p-3 shadow-2xl"
        style={{ transform: turned ? 'rotateY(0deg)' : 'rotateY(90deg)', transition: reduced ? 'none' : 'transform 260ms cubic-bezier(.16,1,.3,1)' }}
        data-testid="dashboard-edit-flip"
      >
        <div className="mb-2 flex items-center gap-2">
          <Repeat2 size={15} className="text-accent" aria-hidden />
          <span className="flex-1 text-[13.5px] font-semibold text-text-strong">
            {t('pages.membersPage.dashboardEdit.flip_title', { block: flip.block || t('pages.membersPage.dashboardEdit.this_value') })}
          </span>
          <button type="button" onClick={onClose} aria-label={t('pages.membersPage.dashboardEdit.close')} className="rounded border border-border p-1 text-muted hover:text-text bg-transparent cursor-pointer">
            <X size={13} aria-hidden />
          </button>
        </div>
        <dl className="mb-3 space-y-1.5 rounded-lg border border-border p-2.5 text-[12.5px]" data-testid="dashboard-edit-provider">
          {flip.fields.length === 0 && <p className="text-muted">{t('pages.membersPage.dashboardEdit.no_fields')}</p>}
          {flip.fields.map(field => (
            <div key={field.name} className="grid grid-cols-[minmax(0,7rem)_1fr_auto] items-center gap-2">
              <dt className="truncate font-mono text-muted">{field.name}</dt>
              <dd className="m-0 truncate font-mono text-accent">
                {field.kind === 'agentic' ? t('pages.membersPage.dashboardEdit.kind_agentic', { bot: botName }) : <SourceLine field={field} />}
              </dd>
              <KindTag kind={field.kind} />
            </div>
          ))}
        </dl>
        <AskBox botName={botName} ctx={ctx} block={flip.block} path={flip.path} onSend={onSend} onSent={onClose} />
        <div className="mt-2 flex justify-end">
          <Btn onClick={onClose}>{t('pages.membersPage.dashboardEdit.cancel')}</Btn>
        </div>
      </div>
    </div>
  )
}

/** Stacking inside the layer: what the person picked paints over the Sources list, which
 *  stays open beside it, so a narrow pane never hides the lineage under the list. */
const Z_SOURCES = 1
const Z_PICKED = 2

/** Every field the page reads, for a page that draws in script and answers no point. */
function SourcesPanel({ fields, botName, onPick, onClose }: { fields: FieldSource[]; botName: string; onPick: (field: FieldSource, rect: Rect) => void; onClose: () => void }) {
  const { t } = useTranslation()
  const ref = useRef<HTMLDivElement | null>(null)
  const pick = (field: FieldSource, el: HTMLElement) => {
    const host = ref.current?.offsetParent as HTMLElement | null
    const a = el.getBoundingClientRect()
    const b = host?.getBoundingClientRect()
    if (!b) return
    onPick(field, { x: a.left - b.left, y: a.top - b.top, w: a.width, h: a.height })
  }
  return (
    <div ref={ref} style={{ zIndex: Z_SOURCES }} className="absolute right-2 top-2 pointer-events-auto w-[300px] max-h-[80%] overflow-y-auto rounded-xl border border-border bg-bg-elevated p-3 shadow-xl" role="dialog" aria-label={t('pages.membersPage.dashboardEdit.sources')} data-testid="dashboard-edit-sources">
      <div className="mb-1 flex items-center gap-2">
        <Layers size={14} className="text-accent" aria-hidden />
        <span className="flex-1 text-[13px] font-semibold text-text-strong">{t('pages.membersPage.dashboardEdit.sources')}</span>
        <button type="button" onClick={onClose} aria-label={t('pages.membersPage.dashboardEdit.close')} className="rounded p-0.5 text-muted hover:text-text bg-transparent border-none cursor-pointer">
          <X size={13} aria-hidden />
        </button>
      </div>
      <p className="mb-2 text-[11.5px] text-muted">{t('pages.membersPage.dashboardEdit.sources_desc')}</p>
      <ul className="m-0 list-none space-y-1.5 p-0">
        {fields.map(field => (
          <li key={field.name}>
            <button
              type="button"
              onClick={e => pick(field, e.currentTarget)}
              onFocus={e => pick(field, e.currentTarget)}
              className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-left hover:border-accent focus-visible:border-accent cursor-pointer"
              data-testid={`dashboard-edit-source-${field.name}`}
            >
              <span className="flex items-center gap-2">
                <span className="flex-1 truncate font-mono text-[12.5px] text-text-strong">{field.name}</span>
                <KindTag kind={field.kind} />
              </span>
              <span className="block truncate text-[11.5px] text-muted">
                {field.kind === 'agentic' ? t('pages.membersPage.dashboardEdit.kind_agentic', { bot: botName }) : <SourceLine field={field} />}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
