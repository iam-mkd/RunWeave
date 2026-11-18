import React from 'react';

const paths = {
  good: 'M4 8.5l2.5 2.5L12 5.5',
  info: 'M8 4.5a3.5 3.5 0 1 1 0 7a3.5 3.5 0 0 1 0-7z',
  warning: 'M8 3l5.5 10h-11L8 3zM8 7v2.5M8 11.5v.01',
  serious: 'M8 2.5L13.5 8 8 13.5 2.5 8 8 2.5zM8 5.75v3M8 10.75v.01',
  critical: 'M5 5l6 6M11 5l-6 6',
  neutral: 'M4.5 8h7',
  agents: 'M3 4h10v5H3zM5.5 9v3M10.5 9v3M4 12h8',
  runs: 'M3 4h10M3 8h10M3 12h6',
  'load-tests': 'M2.5 12.5l3.5-4 3 2 4.5-6',
  evals: 'M3 3h10v10H3zM5.5 8l1.8 1.8L10.5 6',
  shadow: 'M2.5 4h4.5v8H2.5zM9 4h4.5v8H9z',
  approvals: 'M8 2.5l4.5 2v3.5c0 3-2 4.8-4.5 5.5-2.5-.7-4.5-2.5-4.5-5.5V4.5z',
  settings: 'M8 5.5a2.5 2.5 0 1 1 0 5a2.5 2.5 0 0 1 0-5zM8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4',
  menu: 'M2.5 4.5h11M2.5 8h11M2.5 11.5h11',
  close: 'M4 4l8 8M12 4l-8 8',
  external: 'M6 3.5H3.5v9h9V10M9 3.5h3.5V7M12.5 3.5L7 9',
  sort: 'M5.5 6.5L8 4l2.5 2.5M5.5 9.5L8 12l2.5-2.5',
  up: 'M5 9.5L8 6.5l3 3',
  down: 'M5 6.5L8 9.5l3-3',
};

export function Icon({ name, size = 16, className = 'icon' }) {
  return <svg className={className} aria-hidden="true" focusable="false" width={size} height={size} viewBox="0 0 16 16" fill="none">
    <path d={paths[name] ?? paths.neutral} stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
}
