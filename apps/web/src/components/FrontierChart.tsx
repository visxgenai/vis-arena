import React, { useEffect, useRef, useState } from "react";
import {
  Chart,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";
import type { FrontierData } from "../lib/api";

Chart.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend, Filler);

type Props = { data: FrontierData };

export default function FrontierChart({ data }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);

  useEffect(() => {
    if (!canvasRef.current) return;

    const labels = Array.from({ length: data.num_intervals }, (_, i) => String(i + 1));

    const datasets = data.participants.map((p) => {
      const isActive = !hovered || hovered === p.id;
      return {
        label: p.name,
        data: data.frontiers[p.id],
        borderColor: isActive ? p.color : `${p.color}30`,
        backgroundColor: `${p.color}20`,
        borderWidth: isActive ? 2.5 : 1,
        borderDash: [6, 3],
        pointRadius: data.submitted[p.id].map((s) => (s ? 6 : 4)),
        pointBackgroundColor: data.trajectories[p.id].map((score, i) => {
          const isBest =
            i === 0 ||
            score > Math.max(...data.trajectories[p.id].slice(0, i));
          if (isBest && data.submitted[p.id][i]) return p.color;
          if (data.submitted[p.id][i]) return "#8b949e";
          return "transparent";
        }),
        pointBorderColor: data.submitted[p.id].map((s) =>
          s ? (isActive ? p.color : `${p.color}40`) : `${p.color}30`
        ),
        pointBorderWidth: data.submitted[p.id].map((s) => (s ? 2 : 1.5)),
        pointStyle: data.submitted[p.id].map((s) => (s ? "circle" : "circle")),
        tension: 0.1,
        fill: false,
      };
    });

    if (chartRef.current) {
      chartRef.current.destroy();
    }

    chartRef.current = new Chart(canvasRef.current, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: "nearest",
          intersect: true,
        },
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
            displayColors: true,
            callbacks: {
              title: (items) => {
                if (!items.length) return "";
                return `Interval ${items[0].label}`;
              },
              label: (item) => {
                const pid = data.participants[item.datasetIndex].id;
                const submitted = data.submitted[pid][item.dataIndex];
                const tag = submitted ? "● new" : "○ carried";
                return ` ${item.dataset.label}: ${item.parsed.y!.toFixed(1)} (${tag})`;
              },
            },
          },
        },
        scales: {
          x: {
            title: {
              display: true,
              text: "TIME INTERVAL →",
              color: "#8b949e",
              font: { size: 10, weight: "bold", family: "JetBrains Mono, monospace" },
              padding: 8,
            },
            ticks: { color: "#8b949e", font: { size: 11, family: "JetBrains Mono, monospace" } },
            grid: { color: "#30363d40", drawTicks: false },
            border: { color: "#30363d" },
          },
          y: {
            title: {
              display: true,
              text: "↑ PEER-GRADED SCORE",
              color: "#8b949e",
              font: { size: 10, weight: "bold", family: "JetBrains Mono, monospace" },
              padding: 8,
            },
            min: 50,
            max: 100,
            ticks: { color: "#8b949e", font: { size: 11, family: "JetBrains Mono, monospace" }, stepSize: 10 },
            grid: { color: "#30363d40", drawTicks: false },
            border: { color: "#30363d" },
          },
        },
        onHover: (_event, elements) => {
          if (elements.length > 0) {
            setHovered(data.participants[elements[0].datasetIndex].id);
          } else {
            setHovered(null);
          }
        },
      },
    });

    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, [data, hovered]);

  return (
    <div className="chart-panel animate-fade-in">
      <div className="mb-4 flex items-start justify-between">
        <div>
          <h3 className="chart-title">
            Peer-graded <em className="font-display italic text-arena-orange">frontier</em>
          </h3>
          <p className="chart-subtitle">
            MOCK · {data.num_intervals} INTERVALS · {data.participants.length} PARTICIPANTS
          </p>
        </div>
        <div className="flex items-center gap-4 text-xs text-arena-muted">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-arena-muted" /> SUBMISSION
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full border border-arena-muted bg-transparent" /> CARRIED OVER
          </span>
        </div>
      </div>

      {/* Legend */}
      <div className="mb-3 flex flex-wrap items-center gap-3">
        {data.participants.map((p) => (
          <button
            key={p.id}
            className={`flex items-center gap-1.5 text-xs font-medium transition-opacity ${hovered && hovered !== p.id ? "opacity-30" : "opacity-100"
              }`}
            onMouseEnter={() => setHovered(p.id)}
            onMouseLeave={() => setHovered(null)}
          >
            <span className="participant-dot" style={{ backgroundColor: p.color }} />
            {p.id}
          </button>
        ))}
      </div>

      {/* Explanation box */}
      <div className="mb-4 rounded-lg border border-arena-border bg-arena-card p-4 text-xs leading-5 text-arena-muted">
        Each column is one <em className="font-display italic text-arena-ink">time interval</em>; each dot is a participant.{" "}
        <span className="inline-block h-2 w-2 rounded-full bg-arena-muted align-middle" /> = <strong className="text-arena-ink">new submission</strong> this interval;{" "}
        <span className="inline-block h-2 w-2 rounded-full border border-arena-muted align-middle bg-transparent" /> = <strong className="text-arena-ink">carried over</strong> from an earlier interval.{" "}
        <span className="inline-block h-2 w-2 rounded-full bg-arena-orange align-middle" /> = <strong className="text-arena-orange">new best score</strong>.{" "}
        The frontier <span className="text-arena-orange">----</span> only climbs.{" "}
        <em className="font-display italic text-arena-ink">Hover a dot</em> to highlight that participant&apos;s full trajectory.
      </div>

      <div className="h-[320px]">
        <canvas ref={canvasRef} />
      </div>
    </div>
  );
}
