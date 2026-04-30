import React from "react";
import type { WinsData } from "../lib/api";

type Props = { data: WinsData };

function cellColor(value: number): string {
  // Gradient from dark blue (0.0 = row loses) through grey (0.5) to warm orange (1.0 = row wins)
  if (value >= 0.65) return `rgba(232, 168, 56, ${0.3 + (value - 0.5) * 1.4})`;
  if (value >= 0.5) return `rgba(232, 168, 56, ${0.1 + (value - 0.5) * 0.6})`;
  if (value >= 0.35) return `rgba(74, 158, 234, ${0.1 + (0.5 - value) * 0.6})`;
  return `rgba(74, 158, 234, ${0.3 + (0.5 - value) * 1.4})`;
}

function textColor(value: number): string {
  return value > 0.6 || value < 0.35 ? "#e6edf3" : "#8b949e";
}

export default function WinMatrix({ data }: Props) {
  const ids = data.participants.map((p) => p.id);

  return (
    <div className="chart-panel animate-fade-in">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <div className="mb-1 rounded border border-arena-teal/40 bg-arena-teal/10 px-2 py-1 text-[10px] font-bold uppercase tracking-widest text-arena-teal inline-block">
            ALTERNATIVE · IF WE GRADE PAIR-WISE INSTEAD OF 0–10 SCORE
          </div>
          <h3 className="chart-title mt-2">
            Pairwise <em className="font-display italic text-arena-orange">win matrix</em>
          </h3>
        </div>
        <div className="text-right text-[10px] font-bold uppercase tracking-widest text-arena-muted">
          ROW BEATS
          <br />
          COLUMN
        </div>
      </div>

      {/* Explanation */}
      <div className="mb-4 rounded-lg border border-arena-border bg-arena-card p-4 text-xs leading-5 text-arena-muted">
        <strong className="text-arena-ink">Cell = preference rate that row beats column</strong> (see the color scale beside the matrix).
        Rows are <strong className="text-arena-ink">ranked by row total</strong> — sum of the cells across each row;
        bigger total = more wins overall, so <strong className="text-arena-ink">#1 sits at the top</strong>.
      </div>

      {/* Matrix */}
      <div className="overflow-x-auto">
        <div className="flex items-start gap-3">
          <table className="border-separate border-spacing-[2px]">
            <thead>
              <tr>
                <th className="w-10" />
                <th className="w-8" />
                {ids.map((id) => (
                  <th
                    key={id}
                    className="text-center text-xs font-bold text-arena-ink"
                    style={{ width: 56 }}
                  >
                    {id}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.participants.map((p, rowIdx) => {
                const rowTotal = ids.reduce((sum, qId) => {
                  const v = data.matrix[p.id]?.[qId];
                  return v != null ? sum + v : sum;
                }, 0);
                return (
                  <tr key={p.id}>
                    <td className="text-right pr-2 text-xs font-bold text-arena-muted whitespace-nowrap">
                      {rowIdx < 3 ? (
                        <span className="text-arena-orange">#{rowIdx + 1}</span>
                      ) : (
                        `#${rowIdx + 1}`
                      )}
                    </td>
                    <td className="text-center text-xs font-bold" style={{ color: p.color }}>
                      {p.id}
                    </td>
                    {ids.map((qId) => {
                      const value = data.matrix[p.id]?.[qId];
                      if (value == null) {
                        return (
                          <td key={qId} className="win-cell bg-arena-bg rounded" />
                        );
                      }
                      return (
                        <td
                          key={qId}
                          className="win-cell rounded transition-transform hover:scale-110"
                          style={{
                            backgroundColor: cellColor(value),
                            color: textColor(value),
                          }}
                        >
                          {value.toFixed(2)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>

          {/* Color scale */}
          <div className="flex flex-col items-center justify-between ml-3" style={{ height: 280 }}>
            <div className="text-[10px] font-bold text-arena-orange">1.00</div>
            <div className="text-[9px] text-arena-muted">row wins</div>
            <div
              className="w-4 flex-1 rounded-full my-1"
              style={{
                background: "linear-gradient(to bottom, rgba(232,168,56,0.8), rgba(48,54,61,0.3), rgba(74,158,234,0.8))",
              }}
            />
            <div className="text-[10px] font-bold text-arena-muted">0.50</div>
            <div className="text-[9px] text-arena-muted">coin flip</div>
            <div className="h-2" />
            <div className="text-[10px] font-bold text-arena-blue">0.00</div>
            <div className="text-[9px] text-arena-muted">row loses</div>
          </div>
        </div>
      </div>

      <div className="mt-3 text-center text-[10px] font-bold uppercase tracking-widest text-arena-muted">
        OPPONENT →
      </div>
    </div>
  );
}
