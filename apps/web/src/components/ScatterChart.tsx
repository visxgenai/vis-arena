import React, { useEffect, useRef } from "react";
import {
  Chart,
  LinearScale,
  PointElement,
  Tooltip,
} from "chart.js";
import type { ScatterPoint } from "../lib/api";

Chart.register(LinearScale, PointElement, Tooltip);

type Props = { points: ScatterPoint[] };

export default function ScatterChart({ points }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);

  useEffect(() => {
    if (!canvasRef.current) return;

    if (chartRef.current) {
      chartRef.current.destroy();
    }

    chartRef.current = new Chart(canvasRef.current, {
      type: "scatter",
      data: {
        datasets: [
          {
            data: points.map((p) => ({ x: p.alignment, y: p.quality })),
            backgroundColor: points.map((p) => p.color),
            borderColor: points.map((p) => `${p.color}80`),
            borderWidth: 2,
            pointRadius: 12,
            pointHoverRadius: 16,
            pointHoverBorderWidth: 3,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#1c2128",
            borderColor: "#30363d",
            borderWidth: 1,
            titleColor: "#e6edf3",
            bodyColor: "#8b949e",
            padding: 12,
            cornerRadius: 8,
            callbacks: {
              title: () => "",
              label: (ctx) => {
                const p = points[ctx.dataIndex];
                return [
                  `${p.name} (${p.id})`,
                  `Quality: ${p.quality.toFixed(1)}`,
                  `Alignment: ${p.alignment.toFixed(3)}`,
                ];
              },
            },
          },
        },
        scales: {
          x: {
            title: {
              display: true,
              text: "EVALUATOR ALIGNMENT →",
              color: "#8b949e",
              font: { size: 10, weight: "bold", family: "JetBrains Mono, monospace" },
              padding: 8,
            },
            min: 0.3,
            max: 1.0,
            ticks: {
              color: "#8b949e",
              font: { size: 11, family: "JetBrains Mono, monospace" },
              stepSize: 0.1,
            },
            grid: {
              color: "#30363d40",
              drawTicks: false,
            },
            border: { color: "#30363d" },
          },
          y: {
            title: {
              display: true,
              text: "↑ REPORT QUALITY",
              color: "#8b949e",
              font: { size: 10, weight: "bold", family: "JetBrains Mono, monospace" },
              padding: 8,
            },
            min: 70,
            max: 100,
            ticks: {
              color: "#8b949e",
              font: { size: 11, family: "JetBrains Mono, monospace" },
              stepSize: 5,
            },
            grid: {
              color: "#30363d40",
              drawTicks: false,
            },
            border: { color: "#30363d" },
          },
        },
      },
      plugins: [
        {
          id: "quadrantLabels",
          afterDraw: (chart) => {
            const ctx = chart.ctx;
            const { left, right, top, bottom } = chart.chartArea;
            const midX = (left + right) / 2;
            const midY = (top + bottom) / 2;

            ctx.save();
            ctx.font = "10px 'JetBrains Mono', monospace";

            // Draw quadrant dividers
            ctx.setLineDash([4, 4]);
            ctx.strokeStyle = "#30363d60";
            ctx.beginPath();
            ctx.moveTo(midX, top);
            ctx.lineTo(midX, bottom);
            ctx.moveTo(left, midY);
            ctx.lineTo(right, midY);
            ctx.stroke();
            ctx.setLineDash([]);

            // Quadrant labels
            const pad = 8;
            ctx.fillStyle = "#8b949e";
            ctx.textAlign = "left";
            ctx.fillText("▲ HIGH QUALITY", left + pad, top + 16);
            ctx.fillText("◄ LOW ALIGNMENT", left + pad, top + 30);

            ctx.textAlign = "right";
            ctx.fillStyle = "#e8a838";
            ctx.fillText("▲ HIGH QUALITY", right - pad, top + 16);
            ctx.fillText("► HIGH ALIGNMENT", right - pad, top + 30);

            ctx.fillStyle = "#8b949e";
            ctx.textAlign = "left";
            ctx.fillText("▼ LOW QUALITY", left + pad, bottom - 16);
            ctx.fillText("◄ LOW ALIGNMENT", left + pad, bottom - 4);

            ctx.textAlign = "right";
            ctx.fillText("▼ LOW QUALITY", right - pad, bottom - 16);
            ctx.fillText("► HIGH ALIGNMENT", right - pad, bottom - 4);

            ctx.restore();
          },
        },
      ],
    });

    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, [points]);

  return (
    <div className="chart-panel animate-fade-in">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h3 className="chart-title">
            Peer-graded <em className="font-display italic text-arena-orange">frontier</em>
          </h3>
          <p className="chart-subtitle">ALIGNMENT × QUALITY · {points.length} AGENTS</p>
        </div>
      </div>

      {/* Legend */}
      <div className="mb-3 flex flex-wrap items-center gap-3">
        {points.map((p) => (
          <span key={p.id} className="flex items-center gap-1.5 text-xs font-medium text-arena-ink">
            <span className="participant-dot" style={{ backgroundColor: p.color }} />
            {p.id}
          </span>
        ))}
      </div>

      {/* Explanation */}
      <div className="mb-4 rounded-lg border border-arena-border bg-arena-card p-4 text-xs leading-5 text-arena-muted">
        One dot per participant{" "}
        <span className="inline-block h-2 w-2 rounded-full bg-arena-blue align-middle" /> — their{" "}
        <strong className="text-arena-ink">best submission</strong> across all intervals. Up = higher peer-graded quality;
        right = reviews more aligned with crowd consensus.{" "}
        <strong className="text-arena-orange">Top-right</strong> is ideal: high-quality author{" "}
        <em className="font-display italic text-arena-ink">and</em> well-calibrated reviewer.
      </div>

      <div className="h-[360px]">
        <canvas ref={canvasRef} />
      </div>
    </div>
  );
}
