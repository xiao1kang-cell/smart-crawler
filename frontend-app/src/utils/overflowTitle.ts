const AUTO_TITLE_ATTR = 'data-auto-overflow-title'

function isElement(node: EventTarget | null): node is HTMLElement {
  return node instanceof HTMLElement
}

function isTextCandidate(el: HTMLElement) {
  if (el.closest('[aria-hidden="true"]')) return false
  if (['SCRIPT', 'STYLE', 'SVG', 'PATH'].includes(el.tagName)) return false

  const style = window.getComputedStyle(el)
  const hasClamp = style.webkitLineClamp && style.webkitLineClamp !== 'none'
  const hasEllipsis = style.textOverflow === 'ellipsis'
  const hidesOverflow = ['hidden', 'clip'].includes(style.overflowX) || ['hidden', 'clip'].includes(style.overflowY)
  return hasClamp || hasEllipsis || hidesOverflow
}

function isOverflowing(el: HTMLElement) {
  return el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1
}

function readableText(el: HTMLElement) {
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
    return el.value || el.placeholder || ''
  }
  if (el instanceof HTMLSelectElement) {
    return el.selectedOptions[0]?.textContent?.trim() || ''
  }
  return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()
}

function setAutoTitle(el: HTMLElement, text: string) {
  if (!text) return
  if (el.getAttribute(AUTO_TITLE_ATTR) === 'true' || !el.getAttribute('title')) {
    el.setAttribute('title', text)
    el.setAttribute(AUTO_TITLE_ATTR, 'true')
  }
}

function clearAutoTitle(el: HTMLElement) {
  if (el.getAttribute(AUTO_TITLE_ATTR) !== 'true') return
  el.removeAttribute('title')
  el.removeAttribute(AUTO_TITLE_ATTR)
}

function findOverflowTextElement(path: EventTarget[]) {
  for (const node of path) {
    if (!isElement(node)) continue
    if (node === document.body || node === document.documentElement) break
    if (!isTextCandidate(node)) continue
    const text = readableText(node)
    if (!text) continue
    if (isOverflowing(node)) return { el: node, text }
    clearAutoTitle(node)
  }
  return null
}

export function installOverflowTitle() {
  document.addEventListener('mouseover', (event) => {
    const match = findOverflowTextElement(event.composedPath())
    if (!match) return
    setAutoTitle(match.el, match.text)
  }, true)
}
