// The model chip draws the session's harness mark before the model only when the
// session runs on another harness than this install's, so a Claude Code chat is
// told apart without opening a menu and a default chat looks as it always did.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import { stubStripHeights } from './stripHeights'
import { defaultProps } from './ChatInputTestUtils'

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
})

function renderChip(harnessBackend: string | undefined, acpBackend: string | undefined) {
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { acp_backend: acpBackend } } as never)
  renderWithProviders(
    <ChatInput {...defaultProps} modelName="opus" onModelClick={vi.fn()} harnessBackend={harnessBackend} />,
  )
  return screen.getByTestId('composer-model-chip')
}

describe('ChatInput model chip harness mark', () => {
  it('marks a Claude Code session on a KAS install, named in the tooltip', async () => {
    const chip = renderChip('claude', 'kas')
    const mark = await screen.findByTestId('composer-model-chip-harness')
    expect(mark).toHaveAttribute('title', 'Runs on Claude Code')
    expect(mark.querySelector('[data-testid="harness-logo-claude"]')).not.toBeNull()
    expect(chip).toHaveAttribute('aria-label', expect.stringMatching(/^Runs on Claude Code · /))
    // No new text: the chip still reads just the model.
    expect(chip).toHaveTextContent(/^opus$/)
  })

  it('leaves a session on the install harness unchanged', async () => {
    const chip = renderChip('kas', '')
    await vi.waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    expect(screen.queryByTestId('composer-model-chip-harness')).toBeNull()
    expect(chip.getAttribute('aria-label')).not.toMatch(/Runs on/)
  })

  it('reads Kiro CLI and KAS as the same harness', async () => {
    renderChip('', 'kas')
    await vi.waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalled())
    expect(screen.queryByTestId('composer-model-chip-harness')).toBeNull()
  })

  it('marks a Kiro session on a Claude Code install', async () => {
    renderChip('kas', 'claude')
    const mark = await screen.findByTestId('composer-model-chip-harness')
    expect(mark).toHaveAttribute('title', 'Runs on Kiro Agent')
  })

  it('draws no mark before the session reports its harness', () => {
    renderChip(undefined, 'kas')
    expect(screen.queryByTestId('composer-model-chip-harness')).toBeNull()
    expect(api.kirocrewConfig).not.toHaveBeenCalled()
  })
})
