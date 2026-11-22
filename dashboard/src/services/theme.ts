import { useEffect, useState } from 'react';

// Light / dark theme: the light palette lives under html.light (index.css); without the class the page is dark.
// The choice is remembered in this browser; ?theme=dark|light overrides it.
export type Theme = 'light' | 'dark';
const KEY = 'gnss-guardian.theme';
const EVENT = 'gnss-guardian-theme';

export function initialTheme(): Theme {
  const q = new URLSearchParams(window.location.search).get('theme');
  if (q === 'light' || q === 'dark') return q;
  try {
    const v = window.localStorage.getItem(KEY);
    if (v === 'light' || v === 'dark') return v;
  } catch {
    /* storage blocked */
  }
  return 'light';
}

export function currentTheme(): Theme {
  return document.documentElement.classList.contains('light') ? 'light' : 'dark';
}

export function applyTheme(t: Theme, remember = true): void {
  document.documentElement.classList.toggle('light', t === 'light');
  document.documentElement.style.colorScheme = t;
  if (remember) {
    try {
      window.localStorage.setItem(KEY, t);
    } catch {
      /* storage blocked: switches for this visit */
    }
  }
  window.dispatchEvent(new Event(EVENT));
}

/** The theme, re-rendering on change (canvas components redraw with it). */
export function useTheme(): [Theme, (t: Theme) => void] {
  const [t, setT] = useState<Theme>(currentTheme);
  useEffect(() => {
    const on = () => setT(currentTheme());
    window.addEventListener(EVENT, on);
    return () => window.removeEventListener(EVENT, on);
  }, []);
  return [t, (x: Theme) => applyTheme(x)];
}

/** A CSS custom property of the current theme (canvas drawing). */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
