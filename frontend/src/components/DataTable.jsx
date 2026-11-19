import React from 'react';
import { Icon } from './icons.jsx';
import { useScrollRegion } from './scroll.js';

function nextDirection(sort, key) {
  return sort?.key === key && sort.direction === 'ascending' ? 'descending' : 'ascending';
}

export function DataTable({ caption, captionHidden = false, columns, rows, rowKey, sort, onSort, empty = 'Nothing to show yet.', onRowClick, selectedKey }) {
  const region = useScrollRegion(caption);
  return <div className="table-wrap" {...region}>
    <table className="data-table">
      <caption className={captionHidden ? 'visually-hidden' : undefined}>{caption}</caption>
      <thead>
        <tr>{columns.map((column) => {
          const sorted = sort?.key === column.key ? sort.direction : undefined;
          return <th scope="col" aria-sort={sorted} className={column.numeric ? 'num' : undefined} key={column.key}>
            {column.sortable && onSort
              ? <button type="button" className="sort-button" onClick={() => onSort({ key: column.key, direction: nextDirection(sort, column.key) })}>
                {column.label}
                <Icon name={sorted === 'ascending' ? 'up' : sorted === 'descending' ? 'down' : 'sort'} size={12} />
              </button>
              : column.label}
          </th>;
        })}</tr>
      </thead>
      <tbody>
        {rows.length === 0 && <tr><td colSpan={columns.length} className="table-empty">{empty}</td></tr>}
        {rows.map((row) => {
          const key = rowKey(row);
          return <tr key={key} aria-selected={selectedKey === undefined ? undefined : selectedKey === key}
            className={onRowClick ? 'clickable' : undefined} onClick={onRowClick ? () => onRowClick(row) : undefined}>
            {columns.map((column) => <td className={column.numeric ? 'num' : undefined} key={column.key}>
              {column.render ? column.render(row) : row[column.key]}
            </td>)}
          </tr>;
        })}
      </tbody>
    </table>
  </div>;
}
