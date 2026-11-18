import React, { useEffect, useRef, useState } from 'react';

const margin = { top: 12, right: 16, bottom: 28, left: 52 };

export function niceMax(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((candidate) => candidate * magnitude >= value);
  return step * magnitude;
}

function useWidth(fallback) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(240, Math.round(entry.contentRect.width))));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

function linePath(points, x, y) {
  return points.map((point, index) => `${index ? 'L' : 'M'}${x(point.x).toFixed(1)},${y(point.y).toFixed(1)}`).join('');
}

export function LineChart({ title, series, formatX = String, formatY = String, xLabel = 'Time', height = 220 }) {
  const [ref, width] = useWidth(640);
  const [active, setActive] = useState(null);
  const xs = series[0]?.points.map((point) => point.x) ?? [];
  const [minX, maxX] = [Math.min(...xs), Math.max(...xs)];
  const top = niceMax(Math.max(0, ...series.flatMap((line) => line.points.map((point) => point.y))));
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const x = (value) => margin.left + (maxX === minX ? plotWidth / 2 : ((value - minX) / (maxX - minX)) * plotWidth);
  const y = (value) => margin.top + plotHeight - (value / top) * plotHeight;
  const ticks = [0, top / 4, top / 2, (top * 3) / 4, top];
  const xTicks = [...new Set([0, Math.floor((xs.length - 1) / 2), xs.length - 1])].filter((index) => xs[index] !== undefined);
  const last = series.map((line) => line.points.at(-1));
  const summary = `${title}. ${series.map((line, index) => `${line.name} ends at ${formatY(last[index]?.y)}`).join('; ')}.`;

  function moveTo(index) {
    setActive(Math.max(0, Math.min(xs.length - 1, index)));
  }

  function onPointer(event) {
    const box = event.currentTarget.getBoundingClientRect();
    const position = ((event.clientX - box.left) / box.width) * width;
    let nearest = 0;
    xs.forEach((value, index) => { if (Math.abs(x(value) - position) < Math.abs(x(xs[nearest]) - position)) nearest = index; });
    setActive(nearest);
  }

  function onKey(event) {
    if (event.key === 'ArrowRight') { event.preventDefault(); moveTo((active ?? -1) + 1); }
    if (event.key === 'ArrowLeft') { event.preventDefault(); moveTo((active ?? xs.length) - 1); }
    if (event.key === 'Escape') setActive(null);
  }

  return <figure className="chart" ref={ref}>
    <figcaption className="chart-title">{title}</figcaption>
    {series.length > 1 && <ul className="legend">{series.map((line, index) => <li key={line.name}>
      <span className={`series-key series-${index + 1}`} aria-hidden="true" />{line.name}
    </li>)}</ul>}
    <div className="chart-plot">
      <svg role="img" aria-label={summary} width={width} height={height} viewBox={`0 0 ${width} ${height}`}
        tabIndex={0} onPointerMove={onPointer} onPointerLeave={() => setActive(null)} onKeyDown={onKey} onBlur={() => setActive(null)}>
        {ticks.map((tick) => <g key={tick} className="chart-tick">
          <line className={tick === 0 ? 'chart-axis' : 'chart-grid'} x1={margin.left} x2={width - margin.right} y1={y(tick)} y2={y(tick)} />
          <text x={margin.left - 8} y={y(tick)} dy="0.32em" textAnchor="end">{formatY(tick)}</text>
        </g>)}
        {xTicks.map((index) => <text key={index} className="chart-x-tick" x={x(xs[index])} y={height - 8}
          textAnchor={index === 0 ? 'start' : index === xs.length - 1 ? 'end' : 'middle'}>{formatX(xs[index])}</text>)}
        {series.map((line, index) => <path className={`series-line series-${index + 1}`} key={line.name} d={linePath(line.points, x, y)} />)}
        {active !== null && <g className="crosshair">
          <line x1={x(xs[active])} x2={x(xs[active])} y1={margin.top} y2={margin.top + plotHeight} />
          {series.map((line, index) => line.points[active] && <circle key={line.name} className={`series-dot series-${index + 1}`}
            cx={x(line.points[active].x)} cy={y(line.points[active].y)} r="4" />)}
        </g>}
      </svg>
      {active !== null && <div className="chart-tooltip" aria-hidden="true"
        style={{ left: `${Math.min(x(xs[active]) + 12, width - 160)}px` }}>
        <div className="tooltip-x">{formatX(xs[active])}</div>
        {series.map((line, index) => <div className="tooltip-row" key={line.name}>
          <span className={`series-key series-${index + 1}`} /><strong>{formatY(line.points[active]?.y)}</strong><span>{line.name}</span>
        </div>)}
      </div>}
    </div>
    <details className="chart-data">
      <summary>Data table</summary>
      <table className="data-table compact">
        <caption className="visually-hidden">{title}</caption>
        <thead><tr><th scope="col">{xLabel}</th>{series.map((line) => <th scope="col" className="num" key={line.name}>{line.name}</th>)}</tr></thead>
        <tbody>{xs.map((value, index) => <tr key={value}>
          <td>{formatX(value)}</td>
          {series.map((line) => <td className="num" key={line.name}>{formatY(line.points[index]?.y)}</td>)}
        </tr>)}</tbody>
      </table>
    </details>
  </figure>;
}

export function Sparkline({ label, values, width = 96, height = 24 }) {
  const max = Math.max(...values, 1);
  const step = values.length > 1 ? width / (values.length - 1) : 0;
  const d = values.map((value, index) => `${index ? 'L' : 'M'}${(index * step).toFixed(1)},${(height - 2 - (value / max) * (height - 4)).toFixed(1)}`).join('');
  return <svg className="sparkline" role="img" aria-label={label} width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
    <path d={d} />
  </svg>;
}
