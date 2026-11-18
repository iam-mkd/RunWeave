export const sections = [
  { id: 'agents', label: 'Agents', milestone: 19 },
  { id: 'runs', label: 'Runs', milestone: 19 },
  { id: 'load-tests', label: 'Load tests', milestone: 22 },
  { id: 'evals', label: 'Evals', milestone: 22 },
  { id: 'shadow', label: 'Shadow', milestone: 22 },
  { id: 'approvals', label: 'Approvals', milestone: 23 },
  { id: 'settings', label: 'Settings', milestone: 21 },
];

// Only sections that exist in the shipped workspace; the prototype shows all.
export const productionSections = [{ ...sections[1], href: '/' }];
