import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'

import BrowserLiveCardCanvas, { type BrowserLiveCanvasHandle } from '../pages/chat/BrowserLiveCardCanvas'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * The painter: frames reach the canvas only while visible, the newest frame
 * wins when decodes queue up, password boxes are painted over after the
 * image, replay walks the ring on the clock, and takeover input is mapped to
 * frame pixels and posted (moves coalesced, Esc kept for hand-back).
 */

const S = 'slot:abc'
const frame = (seq: number, over: Partial<DecodedBrowserFrame['header']> = {}): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, seq, w: 640, h: 400, mask: [], ...over },
  jpeg: new Uint8Array([seq]),
  receivedAt: 1000 + seq * 100,
})

let ctx: { drawImage: ReturnType<typeof vi.fn>; fillRect: ReturnType<typeof vi.fn>; fillStyle: string }
let decodes: Array<{ resolve: (v: unknown) => void }>
let writes: { input: ReturnType<typeof vi.fn> }
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

function flushDecodes() { for (const d of decodes.splice(0)) d.resolve({ close: vi.fn() }) }

beforeEach(() => {
  browserLiveStore.__resetForTests()
  browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'live', url: 'https://x.test/' })
  writes = { input: vi.fn(async () => ({ ok: true })) }
  restore = browserLiveStore.__setWritesForTests({ ...writes, ack: vi.fn(async () => ({ ok: true })) })
  ctx = { drawImage: vi.fn(), fillRect: vi.fn(), fillStyle: '' }
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
  decodes = []
  vi.stubGlobal('createImageBitmap', vi.fn(() => new Promise(resolve => { decodes.push({ resolve }) })))
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 320, height: 200, right: 320, bottom: 200, x: 0, y: 0, toJSON: () => ({}) })
})
afterEach(() => {
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

/** Let the ordered sends go out (each waits for the one before). */
const settle = () => act(async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve() })

const mount = (over: Partial<React.ComponentProps<typeof BrowserLiveCardCanvas>> = {}) => {
  const ref = createRef<BrowserLiveCanvasHandle>()
  const utils = render(<BrowserLiveCardCanvas ref={ref} sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" {...over} />)
  return { ref, ...utils }
}

describe('painting', () => {
  it('sizes the canvas to the frame and draws it once decoded', async () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas') as HTMLCanvasElement
    expect(canvas.width).toBe(640)
    expect(canvas.height).toBe(400)
    expect(ctx.drawImage).not.toHaveBeenCalled()
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.drawImage).toHaveBeenCalledTimes(1)
  })

  it('paints the password boxes over the image, in the card colour', async () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1, { mask: [[10, 20, 100, 30], [0, 0, 5, 5]] })))
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.fillRect.mock.calls).toEqual([[10, 20, 100, 30], [0, 0, 5, 5]])
    // drawImage first, then the mask: the order the spy saw them in.
    expect(ctx.drawImage.mock.invocationCallOrder[0]).toBeLessThan(ctx.fillRect.mock.invocationCallOrder[0])
  })

  it('drops frames that arrive while a decode is in flight and paints the newest', async () => {
    mount()
    act(() => {
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyFrame(frame(2))
      browserLiveStore.applyFrame(frame(3))
    })
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    await act(async () => { flushDecodes(); await Promise.resolve(); await Promise.resolve() })
    // The second decode is frame 3; frame 2 was never decoded.
    expect(createImageBitmap).toHaveBeenCalledTimes(2)
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.drawImage).toHaveBeenCalledTimes(2)
  })

  it('paints nothing while off screen, then the latest frame on return', async () => {
    const { rerender } = mount({ visible: false })
    act(() => { browserLiveStore.applyFrame(frame(1)); browserLiveStore.applyFrame(frame(2)) })
    expect(createImageBitmap).not.toHaveBeenCalled()
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" />)
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    const blob = (createImageBitmap as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as Blob
    expect(blob.size).toBe(1)
  })

  it('replay walks the frames on the clock and ends by itself', async () => {
    vi.useFakeTimers()
    const { ref } = mount()
    const frames = [frame(1), frame(2), frame(3)]
    let done = false
    act(() => { void ref.current!.replay(frames).then(() => { done = true }) })
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    await act(async () => { flushDecodes(); await Promise.resolve() })
    // Recorded 100 ms apart.
    await act(async () => { vi.advanceTimersByTime(100); flushDecodes(); await Promise.resolve() })
    expect(createImageBitmap).toHaveBeenCalledTimes(2)
    await act(async () => { vi.advanceTimersByTime(100); flushDecodes(); await Promise.resolve() })
    expect(createImageBitmap).toHaveBeenCalledTimes(3)
    expect(done).toBe(false)
    await act(async () => { vi.advanceTimersByTime(600); flushDecodes(); await Promise.resolve() })
    expect(done).toBe(true)
  })
})

describe('takeover input', () => {
  it('posts nothing while not interactive', () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1)))
    fireEvent.pointerDown(screen.getByTestId('browser-live-canvas'), { clientX: 10, clientY: 10, button: 0 })
    fireEvent.keyDown(screen.getByTestId('browser-live-canvas'), { key: 'a' })
    expect(writes.input).not.toHaveBeenCalled()
  })

  it('maps a click to frame pixels through the 320x200 box', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    await settle()
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([
      expect.objectContaining({ type: 'mouse', action: 'down', x: 320, y: 200, button: 'left', clicks: 1 }),
      expect.objectContaining({ type: 'mouse', action: 'up', x: 320, y: 200, button: 'left' }),
    ])
  })

  it('coalesces moves to one per tick and flushes a pending move before a down', async () => {
    vi.useFakeTimers()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerMove(canvas, { clientX: 10, clientY: 10, buttons: 0 })
    fireEvent.pointerMove(canvas, { clientX: 20, clientY: 20, buttons: 0 })
    fireEvent.pointerMove(canvas, { clientX: 30, clientY: 30, buttons: 0 })
    expect(writes.input).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(16) })
    expect(writes.input).toHaveBeenCalledTimes(1)
    expect(writes.input.mock.calls[0][1]).toMatchObject({ action: 'move', x: 60, y: 60 })
    fireEvent.pointerMove(canvas, { clientX: 40, clientY: 40, buttons: 0 })
    fireEvent.pointerDown(canvas, { clientX: 40, clientY: 40, button: 0, pointerId: 1 })
    await settle()
    expect(writes.input.mock.calls.slice(1).map(c => c[1].action)).toEqual(['move', 'down'])
  })

  it('a key goes to the page; Esc goes to hand-back and never to the page', async () => {
    const onHandBack = vi.fn()
    mount({ interactive: true, onHandBack })
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.keyDown(canvas, { key: 'Enter', code: 'Enter' })
    fireEvent.keyUp(canvas, { key: 'Enter', code: 'Enter' })
    fireEvent.keyDown(canvas, { key: 'Escape', code: 'Escape' })
    fireEvent.keyUp(canvas, { key: 'Escape', code: 'Escape' })
    await settle()
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([
      expect.objectContaining({ type: 'key', action: 'down', key: 'Enter' }),
      expect.objectContaining({ type: 'key', action: 'up', key: 'Enter' }),
    ])
    expect(onHandBack).toHaveBeenCalledTimes(1)
  })

  it('a keystroke typed into the page never reaches the window-level shortcut handler', async () => {
    // useKeyboardShortcuts listens on `window`; an Alt+digit that bubbled there
    // would switch session and unmount the card mid-takeover.
    const windowSaw = vi.fn()
    window.addEventListener('keydown', windowSaw)
    window.addEventListener('keyup', windowSaw)
    try {
      mount({ interactive: true, onHandBack: vi.fn() })
      const canvas = screen.getByTestId('browser-live-canvas')
      fireEvent.keyDown(canvas, { key: '1', code: 'Digit1', altKey: true })
      fireEvent.keyUp(canvas, { key: '1', code: 'Digit1', altKey: true })
      fireEvent.keyDown(canvas, { key: 'Escape', code: 'Escape' })
      await settle()
      expect(writes.input.mock.calls.map(c => c[1])).toEqual([
        expect.objectContaining({ type: 'key', action: 'down', key: '1', modifiers: 1 }),
        expect.objectContaining({ type: 'key', action: 'up', key: '1' }),
      ])
      expect(windowSaw).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', windowSaw)
      window.removeEventListener('keyup', windowSaw)
    }
  })

  it('a release past the drawn frame still posts, clamped to the edge', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    fireEvent.pointerUp(canvas, { clientX: 900, clientY: -30, button: 0, pointerId: 1 })
    await settle()
    const actions = writes.input.mock.calls.map(c => c[1])
    expect(actions.at(-1)).toMatchObject({ type: 'mouse', action: 'up', x: 639, y: 0, button: 'left' })
  })

  it('a wheel tick is posted with the pointer position and the deltas', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    // happy-dom's WheelEvent drops the pointer fields, so the event is built by hand.
    const ev = new Event('wheel', { cancelable: true, bubbles: true })
    for (const [k, v] of Object.entries({ clientX: 160, clientY: 100, deltaX: 0, deltaY: 40, deltaMode: 0, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false })) {
      Object.defineProperty(ev, k, { value: v })
    }
    canvas.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
    expect(writes.input.mock.calls[0][1]).toMatchObject({ type: 'wheel', x: 320, y: 200, dx: 0, dy: 40 })
  })

  // happy-dom builds no TouchList, so touch events are built by hand.
  const touch = (el: HTMLElement, type: string, pts: Array<[number, number]>, ended = false) => {
    const ev = new Event(type, { bubbles: true, cancelable: true })
    const list = pts.map(([clientX, clientY], i) => ({ identifier: i, clientX, clientY }))
    Object.defineProperty(ev, 'touches', { value: ended ? [] : list })
    Object.defineProperty(ev, 'changedTouches', { value: list.slice(0, 1) })
    el.dispatchEvent(ev)
    return ev
  }
  const tap = (canvas: HTMLElement, x = 160, y = 100) => {
    touch(canvas, 'touchstart', [[x, y]])
    return touch(canvas, 'touchend', [[x, y]], true)
  }

  it('a tap focuses the keyboard proxy inside the touchend itself, then clicks the page in order', async () => {
    writes.input.mockImplementation(async (_k: string, ev: { action?: string }) =>
      ev.action === 'up' ? { sent: 1, focus: { editable: true, kind: 'number', mode: '' } } : { sent: 1 })
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    let focusedInHandler = false
    canvas.addEventListener('touchend', () => { focusedInHandler = document.activeElement === proxy })
    const end = tap(canvas)
    // Synchronously, in the same task as the tap: iOS opens no keyboard otherwise.
    expect(focusedInHandler).toBe(true)
    // The phone's own emulated mouse click is cancelled: it would focus the canvas.
    expect(end.defaultPrevented).toBe(true)
    expect(proxy.inputMode).toBe('text')
    await settle()
    expect(document.activeElement).toBe(proxy)
    expect(proxy.inputMode).toBe('decimal')
    expect(writes.input.mock.calls.map(c => [c[1].type, c[1].action, c[1].x, c[1].y])).toEqual([['mouse', 'down', 320, 200], ['mouse', 'up', 320, 200]])
  })

  it('a tap on a password field types into a password box', async () => {
    writes.input.mockImplementation(async () => ({ sent: 1, focus: { editable: true, kind: 'password', mode: '' } }))
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    tap(screen.getByTestId('browser-live-canvas'))
    await settle()
    expect((screen.getByTestId('browser-live-type-proxy') as HTMLInputElement).type).toBe('password')
  })

  it('a tap on something that takes no typing closes the keyboard again', async () => {
    writes.input.mockImplementation(async () => ({ sent: 1, focus: { editable: false, kind: '', mode: '' } }))
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    tap(screen.getByTestId('browser-live-canvas'))
    const proxy = screen.getByTestId('browser-live-type-proxy')
    expect(document.activeElement).toBe(proxy)
    await settle()
    expect(document.activeElement).not.toBe(proxy)
  })

  it('a swipe scrolls the page with wheel deltas (finger up, page down) and clicks nothing', async () => {
    vi.useFakeTimers()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    touch(canvas, 'touchstart', [[160, 150]])
    const mv = touch(canvas, 'touchmove', [[160, 120]])
    touch(canvas, 'touchmove', [[160, 100]])
    expect(mv.defaultPrevented).toBe(true)
    touch(canvas, 'touchend', [[160, 100]], true)
    await settle()
    expect(document.activeElement).not.toBe(screen.getByTestId('browser-live-type-proxy'))
    // 50 CSS px up in a 320x200 box drawing a 640x400 frame: 100 frame px down, at the start point.
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([{ type: 'wheel', x: 320, y: 300, dx: 0, dy: 100, modifiers: 0, w: 640 }])
  })

  it('a finger that wanders less than the slop is still a tap', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    touch(canvas, 'touchstart', [[160, 100]])
    touch(canvas, 'touchmove', [[164, 104]])
    touch(canvas, 'touchend', [[164, 104]], true)
    await settle()
    expect(writes.input.mock.calls.map(c => c[1].action)).toEqual(['down', 'up'])
  })

  it('a touch makes no pointer-event clicks of its own (touch events carry it)', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 7, pointerType: 'touch' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 7, pointerType: 'touch' })
    expect(writes.input).not.toHaveBeenCalled()
  })

  it('a mouse click never focuses the keyboard proxy', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    expect(document.activeElement).toBe(canvas)
  })

  /** The keyboard edits the proxy; the browser then fires `input`. */
  const edit = (el: HTMLInputElement, value: string) => {
    el.value = value
    el.dispatchEvent(new Event('input', { bubbles: true }))
  }
  const sent = () => writes.input.mock.calls.map(c => c[1])
  const text = (t: string) => ({ type: 'key', action: 'press', key: '', text: t, modifiers: 0 })
  const bs = [expect.objectContaining({ action: 'down', key: 'Backspace' }), expect.objectContaining({ action: 'up', key: 'Backspace' })]

  it('typed text and Backspace in an empty proxy reach the page; the proxy forgets each', async () => {
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    edit(proxy, fill + '7 ')
    expect(proxy.value).toBe(fill)
    edit(proxy, '')
    expect(proxy.value).toBe(fill)
    edit(proxy, fill + 'ab ')
    expect(proxy.value).toBe(fill)
    await settle()
    expect(sent()).toEqual([text('7 '), ...bs, text('ab ')])
  })

  it('a composing keyboard (Android words, pinyin) types live and its replacement is diffed', async () => {
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    proxy.dispatchEvent(new Event('compositionstart'))
    edit(proxy, fill + 'ni')
    edit(proxy, fill + 'nih')
    expect(proxy.value).toBe(fill + 'nih')
    edit(proxy, fill + '你好')
    proxy.dispatchEvent(new Event('compositionend'))
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    await settle()
    expect(proxy.value).toBe(fill)
    expect(sent()).toEqual([text('ni'), text('h'), ...bs, ...bs, ...bs, text('你好')])
  })

  it('keys on the proxy: named keys go as keys, printable ones and Backspace through the edit, none reach the window', async () => {
    const windowSaw = vi.fn()
    window.addEventListener('keydown', windowSaw)
    try {
      mount({ interactive: true })
      const proxy = screen.getByTestId('browser-live-type-proxy')
      expect(fireEvent.keyDown(proxy, { key: '5', code: 'Digit5', keyCode: 53 })).toBe(true)
      expect(fireEvent.keyDown(proxy, { key: 'Backspace', code: 'Backspace', keyCode: 8 })).toBe(true)
      fireEvent.keyDown(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
      fireEvent.keyUp(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
      await settle()
      expect(writes.input.mock.calls.map(c => [c[1].action, c[1].key])).toEqual([['down', 'Enter'], ['up', 'Enter']])
      expect(windowSaw).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', windowSaw)
    }
  })

  it('a failed send does not stop the ones after it', async () => {
    writes.input.mockImplementationOnce(async () => { throw new Error('net') })
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    edit(proxy, proxy.value + 'a')
    edit(proxy, proxy.value + 'b')
    await settle()
    expect(sent()).toEqual([text('a'), text('b')])
  })

  it('what is still queued when the person hands back never reaches the page', async () => {
    let release!: () => void
    writes.input.mockImplementationOnce(() => new Promise(r => { release = () => r({ sent: 1 }) }))
    const { rerender } = mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    edit(proxy, proxy.value + 'a')
    edit(proxy, proxy.value + 'b')
    await settle()
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" />)
    release()
    await settle()
    expect(sent()).toEqual([text('a')])
  })

  it('autocorrect replacing the word being typed (no composition) is diffed, not appended', async () => {
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    for (const v of ['t', 'te', 'teh']) edit(proxy, fill + v)
    edit(proxy, fill + 'the ')
    await settle()
    expect(proxy.value).toBe(fill)
    expect(sent()).toEqual([text('t'), text('e'), text('h'), ...bs, ...bs, text('he ')])
  })

  it('a password is forgotten at every keystroke, and a blur forgets any word', async () => {
    writes.input.mockImplementation(async (_k: string, ev: { action?: string }) =>
      ev.action === 'up' ? { sent: 1, focus: { editable: true, kind: 'password', mode: '' } } : { sent: 1 })
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    tap(screen.getByTestId('browser-live-canvas'))
    await settle()
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    edit(proxy, fill + 's')
    expect(proxy.value).toBe(fill)
    proxy.type = 'text'
    edit(proxy, fill + 'wor')
    expect(proxy.value).toBe(fill + 'wor')
    act(() => proxy.blur())
    expect(proxy.value).toBe(fill)
  })

  it('typing reaches the page only while in control', async () => {
    mount({ interactive: false })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    edit(proxy, proxy.value + 'x')
    fireEvent.keyDown(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
    await settle()
    expect(writes.input).not.toHaveBeenCalled()
  })

  it('a click on the live (non-interactive) frame claims', () => {
    const onClaim = vi.fn()
    mount({ onClaim })
    fireEvent.click(screen.getByTestId('browser-live-canvas'))
    expect(onClaim).toHaveBeenCalledTimes(1)
  })
})
