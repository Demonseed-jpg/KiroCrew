import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import type { RootState } from '../store'
import type { Notification } from '../types'
import '../i18n/all'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

// Every note lands in the "Today" group: the clock sits 30 s after the latest.
const NOW = '2026-07-23T10:01:30Z'

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

const mkN = (over: Partial<Notification>): Notification => ({
  kind: 'system', source: 'system', ts: '2026-07-23T10:00:00Z', title: 'Note', body: 'body', acked: false, ...over,
})

function rowOf(title: string): HTMLElement {
  const row = screen.getByText(title).closest('[data-notif-row]')
  if (!(row instanceof HTMLElement)) throw new Error(`no feed row holds "${title}"`)
  return row
}

describe.each(['panel', 'mac'] as const)('NotificationFeed (%s): a keyed replacement', variant => {
  it('says it was updated, and an unkeyed note does not', () => {
    const store = createTestStore({
      notifications: {
        items: [
          mkN({ ts: '2026-07-23T10:01:00Z', title: 'Build status', key: 'build', replaced: true }),
          mkN({ ts: '2026-07-23T10:00:00Z', title: 'Plain note' }),
        ],
      } as RootState['notifications'],
    })
    renderWithProviders(<NotificationFeed variant={variant} selectedTs={null} onSelect={() => {}} />, { store })
    expect(within(rowOf('Build status')).getByText('updated')).toBeTruthy()
    expect(within(rowOf('Plain note')).queryByText('updated')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open updated notification: Build status' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open notification: Plain note' })).toBeTruthy()
  })
})

describe('NotificationFeed (mac): a keyed replacement heading a collapsed deck', () => {
  it('says it was updated beside the stack count', () => {
    const store = createTestStore({
      notifications: {
        // The store holds notes oldest first, so the replacement is the deck's head.
        items: [
          mkN({ ts: '2026-07-23T10:00:00Z', channel: 'system.agent', title: 'Plain note' }),
          mkN({ ts: '2026-07-23T10:01:00Z', channel: 'system.agent', title: 'Build status', key: 'build', replaced: true }),
        ],
      } as RootState['notifications'],
    })
    renderWithProviders(<NotificationFeed variant="mac" selectedTs={null} onSelect={() => {}} />, { store })
    const head = rowOf('Build status')
    expect(within(head).getByText('2')).toBeTruthy()
    expect(within(head).getByText('updated')).toBeTruthy()
  })
})
