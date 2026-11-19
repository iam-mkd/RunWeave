import { useEffect, useRef, useState } from 'react';

/**
 * Keyboard access to content that scrolls inside its box (WCAG 2.1.1): while the element
 * overflows, it joins the tab order as a named region so arrow keys can scroll it. When it
 * fits, it adds no tab stop.
 */
export function useScrollRegion(label) {
  const ref = useRef(null);
  const [scrollable, setScrollable] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    const measure = () => setScrollable(
      element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1,
    );
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    for (const child of element.children) observer.observe(child);
    return () => observer.disconnect();
  });
  return {
    ref,
    ...(scrollable ? { tabIndex: 0, role: 'region', 'aria-label': label } : {}),
  };
}
