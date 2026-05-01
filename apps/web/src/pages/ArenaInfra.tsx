import React, { useEffect, useRef, useState } from "react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";
import { Line, Scatter } from "react-chartjs-2";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler);

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

type Participant = { id: string; name: string; color: string };

export default function ArenaInfra() {
  const [frontier, setFrontier] = useState<any>(null);
  const [scatter, setScatter] = useState<any>(null);
  const [wins, setWins] = useState<any>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [isMock, setIsMock] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch(`${API}/v1/arena/frontier`).then((r) => r.json()),
      fetch(`${API}/v1/arena/scatter`).then((r) => r.json()),
      fetch(`${API}/v1/arena/wins`).then((r) => r.json()),
      fetch(`${API}/v1/arena/analytics`).then((r) => r.json()),
    ])
      .then(([f, s, w, a]) => {
        setFrontier(f);
        setScatter(s);
        setWins(w);
        setAnalytics(a);
        setIsMock(f?.is_mock !== false);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="flex items-center justify-center h-96 text-arena-muted">Loading Arena…</div>;

  const participants: Participant[] = frontier?.participants || [];
  const intervals = frontier?.num_intervals || 12;
  const labels = Array.from({ length: intervals }, (_, i) => String(i + 1));
  const dataLabel = isMock ? "MOCK" : "LIVE";

  return (
    <div className="mx-auto max-w-[1400px] px-6 py-8 space-y-10">
      {/* ── Section 1: Frontier ── */}
      <section>
        <SectionHeading
          title={<>Peer-graded <em className="font-serif italic text-arena-orange">frontier</em></>}
          subtitle={`${dataLabel} · ${intervals} INTERVALS · ${participants.length} PARTICIPANTS`}
          isMock={isMock}
        />
        <div className="chart-panel">
          <ParticipantLegend participants={participants} />
          <ExplainBox>
            Each column is one <em className="italic text-arena-ink">time interval</em>; each dot is a participant.{" "}
            <Dot className="bg-arena-muted" /> = <strong className="text-arena-ink">new submission</strong>;{" "}
            <Dot className="border border-arena-muted bg-transparent" /> = <strong className="text-arena-ink">carried over</strong>;{" "}
            <Dot className="bg-arena-orange" /> = <strong className="text-arena-orange">new best score</strong>.
            The frontier <span className="text-arena-orange">----</span> only climbs.
          </ExplainBox>
          <div className="h-[340px] mt-4">
            {frontier && (
              <Line
                data={{
                  labels,
                  datasets: participants.map((p: Participant) => ({
                    label: p.name,
                    data: frontier.frontiers[p.id],
                    borderColor: p.color,
                    backgroundColor: `${p.color}20`,
                    borderWidth: 2,
                    borderDash: [6, 3],
                    pointRadius: (frontier.submitted[p.id] as boolean[]).map((s: boolean) => (s ? 6 : 4)),
                    pointBackgroundColor: (frontier.trajectories[p.id] as number[]).map((score: number, i: number) => {
                      const isBest = i === 0 || score > Math.max(...(frontier.trajectories[p.id] as number[]).slice(0, i));
                      if (isBest && frontier.submitted[p.id][i]) return p.color;
                      if (frontier.submitted[p.id][i]) return "#8b949e";
                      return "transparent";
                    }),
                    pointBorderColor: (frontier.submitted[p.id] as boolean[]).map((s: boolean) => s ? p.color : `${p.color}30`),
                    pointBorderWidth: (frontier.submitted[p.id] as boolean[]).map((s: boolean) => (s ? 2 : 1.5)),
                    tension: 0.1,
                    fill: false,
                  })),
                }}
                options={{
                  responsive: true, maintainAspectRatio: false,
                  plugins: { legend: { display: false } },
                  scales: {
                    x: { title: { display: true, text: "TIME INTERVAL →", color: "#8b949e", font: { size: 10, weight: "bold" as const, family: "JetBrains Mono" } }, ticks: { color: "#8b949e" }, grid: { color: "#30363d40" }, border: { color: "#30363d" } },
                    y: { title: { display: true, text: "↑ PEER-GRADED SCORE", color: "#8b949e", font: { size: 10, weight: "bold" as const, family: "JetBrains Mono" } }, min: 50, max: 100, ticks: { color: "#8b949e", stepSize: 10 }, grid: { color: "#30363d40" }, border: { color: "#30363d" } },
                  },
                }}
              />
            )}
          </div>
        </div>
      </section>

      {/* ── Section 2: Scatter + Win Matrix side by side ── */}
      <section>
        <SectionHeading
          title={<>Who <em className="font-serif italic text-arena-orange">leads</em>, and who <em className="font-serif italic text-arena-orange">beats</em> whom?</>}
          subtitle="TWO COMPLEMENTARY VIEWS · BEST-SO-FAR SUBMISSIONS"
        />
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Scatter */}
          <div className="chart-panel">
            <div className="flex items-start justify-between mb-2">
              <div>
                <h3 className="text-base font-bold text-arena-ink">Peer-graded <em className="font-serif italic text-arena-orange">frontier</em></h3>
                <p className="text-xs text-arena-muted mt-0.5">ALIGNMENT × QUALITY · {participants.length} AGENTS</p>
              </div>
            </div>
            <ParticipantLegend participants={participants} />
            <ExplainBox>
              One dot per participant <Dot className="bg-arena-blue" /> — their <strong className="text-arena-ink">best submission</strong> across all intervals.
              Up = higher quality; right = more aligned with consensus.{" "}
              <strong className="text-arena-orange">Top-right</strong> is ideal.
            </ExplainBox>
            <div className="h-[320px] mt-4">
              {scatter && (
                <Scatter
                  data={{
                    datasets: [{
                      data: scatter.points.map((p: any) => ({ x: p.alignment, y: p.quality })),
                      backgroundColor: scatter.points.map((p: any) => p.color),
                      borderColor: scatter.points.map((p: any) => `${p.color}80`),
                      borderWidth: 2,
                      pointRadius: 12,
                      pointHoverRadius: 16,
                    }],
                  }}
                  options={{
                    responsive: true, maintainAspectRatio: false,
                    plugins: {
                      legend: { display: false },
                      tooltip: {
                        backgroundColor: "#1c2128", borderColor: "#30363d", borderWidth: 1,
                        callbacks: {
                          title: () => "",
                          label: (ctx: any) => {
                            const p = scatter.points[ctx.dataIndex];
                            return [`${p.name}`, `Quality: ${p.quality.toFixed(1)}`, `Alignment: ${p.alignment.toFixed(3)}`];
                          },
                        },
                      },
                    },
                    scales: {
                      x: { title: { display: true, text: "EVALUATOR ALIGNMENT →", color: "#8b949e", font: { size: 10, weight: "bold" as const } }, min: 0.3, max: 1.0, ticks: { color: "#8b949e", stepSize: 0.1 }, grid: { color: "#30363d40" }, border: { color: "#30363d" } },
                      y: { title: { display: true, text: "↑ REPORT QUALITY", color: "#8b949e", font: { size: 10, weight: "bold" as const } }, min: 70, max: 100, ticks: { color: "#8b949e", stepSize: 5 }, grid: { color: "#30363d40" }, border: { color: "#30363d" } },
                    },
                  }}
                />
              )}
            </div>
          </div>

          {/* Win Matrix */}
          <div className="chart-panel">
            <div className="mb-1 rounded border border-arena-teal/40 bg-arena-teal/10 px-2 py-1 text-[10px] font-bold uppercase tracking-widest text-arena-teal inline-block">
              ALTERNATIVE · IF WE GRADE PAIR-WISE INSTEAD OF 0–10 SCORE
            </div>
            <div className="flex items-start justify-between mt-2 mb-2">
              <h3 className="text-base font-bold text-arena-ink">Pairwise <em className="font-serif italic text-arena-orange">win matrix</em></h3>
              <div className="text-right text-[10px] font-bold uppercase tracking-widest text-arena-muted">ROW BEATS<br />COLUMN</div>
            </div>
            <ExplainBox>
              <strong className="text-arena-ink">Cell = preference rate that row beats column</strong>.
              Rows are <strong className="text-arena-ink">ranked by row total</strong> — bigger total = more wins, so <strong className="text-arena-ink">#1 sits at the top</strong>.
            </ExplainBox>
            {wins && <WinMatrixTable wins={wins} />}
            <div className="mt-3 text-center text-[10px] font-bold uppercase tracking-widest text-arena-muted">OPPONENT →</div>
          </div>
        </div>
      </section>

      {/* ── Section 3: Analytics 2×2 grid ── */}
      <section>
        <SectionHeading
          title={<>Who <em className="font-serif italic text-arena-orange">leads</em>, how does judgment <em className="font-serif italic text-arena-orange">drift</em>, and who <em className="font-serif italic text-arena-orange">climbs</em>?</>}
          subtitle={`${dataLabel} · RANK · LENIENCY · CONSENSUS · ELO · ${intervals} INTERVALS · ${participants.length} REVIEWERS`}
          legendParticipants={participants}
        />
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <AnalyticsPanel
            title="Rank" titleEm="ladder" subtitle="PER-INTERVAL RANK · 1 = BEST"
            formula="rank(r, t) = position of r when agents are ordered by score at t"
            explain="Each colored line traces one participant's standing over time. Higher on the chart means better rank, so upward moves indicate overtaking others and downward moves indicate slipping behind."
            chartNode={analytics && <AnalyticsLine data={analytics} field="ranks" yLabel="↑ RANK (1 = BEST)" labels={labels} reverse />}
          />
          <AnalyticsPanel
            title="Leniency" titleEm="drift" subtitle="SCORE GIVEN − CROWD MEAN"
            formula="L(r, t) = mean(scores r gives at t) − crowd mean at t"
            explain="Values above zero mean a reviewer is scoring peers more generously than the group at that interval; values below zero mean they are harsher. The farther a line sits from zero, the stronger that reviewer's bias relative to the crowd."
            chartNode={analytics && <AnalyticsLine data={analytics} field="leniency" yLabel="LENIENCY DRIFT" labels={labels} />}
          />
          <AnalyticsPanel
            title="Consensus" titleEm="agreement" subtitle="RANK-CORR VS. CROWD ORDERING"
            formula="A(r, t) = ρ_spearman(ranks r gives, crowd-mean ranks)"
            explain="Higher values mean a reviewer's ordering of submissions closely matches the crowd's ordering; lower values mean their judgments are more idiosyncratic. This chart is about agreement in relative ranking, not generosity or harshness."
            chartNode={analytics && <AnalyticsLine data={analytics} field="consensus" yLabel="RANK-CORR VS. CROWD" labels={labels} />}
          />
          <AnalyticsPanel
            title="Elo" titleEm="skill curves" subtitle="PER-INTERVAL RATING · START 1500 · K = 32"
            formula="Elo_i ← Elo_i + K · (actual − expected) · E(i,j) = 1 / (1 + 10^((Elo_j − Elo_i) / 400))"
            explain="Elo summarizes repeated pairwise wins and losses into one running skill score. Rising lines indicate a participant is outperforming expectation over time, while falling lines indicate they are losing ground relative to the field."
            chartNode={analytics && <AnalyticsLine data={analytics} field="elo" yLabel="↑ ELO RATING" labels={labels} />}
          />
        </div>
      </section>
    </div>
  );
}

/* ─── Shared sub-components ─── */

function SectionHeading({ title, subtitle, legendParticipants, isMock }: { title: React.ReactNode; subtitle: string; legendParticipants?: Participant[]; isMock?: boolean }) {
  return (
    <div className="mb-5">
      <h2 className="text-2xl font-bold text-arena-ink leading-tight">{title}</h2>
      <div className="flex flex-wrap items-center gap-4 mt-1">
        <p className="text-xs font-bold uppercase tracking-widest text-arena-muted">
          {subtitle}
          {isMock && (
            <span className="ml-2 rounded px-1.5 py-0.5 text-[9px] font-bold bg-arena-orange/20 text-arena-orange border border-arena-orange/30">
              DEMO DATA — run /v1/arena/run to populate with real GPT results
            </span>
          )}
        </p>
        {legendParticipants && (
          <div className="flex flex-wrap items-center gap-3">
            {legendParticipants.map((p) => (
              <span key={p.id} className="flex items-center gap-1.5 text-xs font-medium text-arena-ink">
                <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: p.color }} />
                {p.id}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ParticipantLegend({ participants }: { participants: Participant[] }) {
  return (
    <div className="flex flex-wrap items-center gap-3 mb-3">
      {participants.map((p) => (
        <span key={p.id} className="flex items-center gap-1.5 text-xs font-medium text-arena-ink">
          <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: p.color }} />
          {p.id}
        </span>
      ))}
    </div>
  );
}

function Dot({ className }: { className: string }) {
  return <span className={`inline-block h-2 w-2 rounded-full align-middle ${className}`} />;
}

function ExplainBox({ children }: { children: React.ReactNode }) {
  return <div className="rounded-lg border border-arena-border bg-arena-card p-4 text-xs leading-5 text-arena-muted">{children}</div>;
}

function AnalyticsPanel({ title, titleEm, subtitle, formula, explain, chartNode }: {
  title: string; titleEm: string; subtitle: string; formula: string; explain: string; chartNode: React.ReactNode;
}) {
  return (
    <div className="chart-panel flex flex-col">
      <div className="flex items-start justify-between mb-3">
        <h3 className="text-base font-bold text-arena-ink">{title} <em className="font-serif italic text-arena-orange">{titleEm}</em></h3>
        <span className="text-[10px] font-bold uppercase tracking-widest text-arena-muted">{subtitle}</span>
      </div>
      <div className="rounded-lg border border-arena-border bg-arena-bg px-4 py-2 text-xs font-mono text-arena-muted mb-3 overflow-x-auto">{formula}</div>
      <p className="text-xs leading-5 text-arena-muted mb-4">{explain}</p>
      <div className="h-[200px] mt-auto">{chartNode}</div>
    </div>
  );
}

/* ─── Analytics line charts ─── */

function AnalyticsLine({ data, field, yLabel, labels, reverse }: {
  data: any; field: string; yLabel: string; labels: string[]; reverse?: boolean;
}) {
  const participants: Participant[] = data.participants;
  return (
    <Line
      data={{
        labels,
        datasets: participants.map((p: Participant) => ({
          label: p.name,
          data: data[field][p.id],
          borderColor: p.color,
          backgroundColor: p.color,
          borderWidth: 1.5,
          pointRadius: 2,
          tension: 0.2,
        })),
      }}
      options={{
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#8b949e", font: { size: 9 } }, grid: { color: "#30363d40" }, border: { color: "#30363d" } },
          y: { reverse: !!reverse, title: { display: true, text: yLabel, color: "#8b949e", font: { size: 9, weight: "bold" as const } }, ticks: { color: "#8b949e", font: { size: 9 } }, grid: { color: "#30363d40" }, border: { color: "#30363d" } },
        },
      }}
    />
  );
}

/* ─── Win matrix table ─── */

function cellBg(v: number): string {
  if (v >= 0.65) return `rgba(232,168,56,${0.3 + (v - 0.5) * 1.4})`;
  if (v >= 0.5) return `rgba(232,168,56,${0.1 + (v - 0.5) * 0.6})`;
  if (v >= 0.35) return `rgba(74,158,234,${0.1 + (0.5 - v) * 0.6})`;
  return `rgba(74,158,234,${0.3 + (0.5 - v) * 1.4})`;
}

function WinMatrixTable({ wins }: { wins: any }) {
  const parts: Participant[] = wins.participants;
  const ids = parts.map((p: Participant) => p.id);

  return (
    <div className="overflow-x-auto mt-3">
      <div className="flex items-start gap-3">
        <table className="border-separate border-spacing-[2px]" style={{ minWidth: ids.length * 50 + 80 }}>
          <thead>
            <tr>
              <th style={{ width: 32 }} />
              <th style={{ width: 48 }} />
              {ids.map((id: string) => (
                <th key={id} className="text-center text-[10px] font-bold text-arena-ink" style={{ width: 48, maxWidth: 48 }}>{id}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {parts.map((p: Participant, rowIdx: number) => (
              <tr key={p.id}>
                <td className="text-right pr-1 text-[10px] font-bold text-arena-muted whitespace-nowrap">
                  {rowIdx < 3 ? <span className="text-arena-orange">#{rowIdx + 1}</span> : `#${rowIdx + 1}`}
                </td>
                <td className="text-center text-[10px] font-bold whitespace-nowrap" style={{ color: p.color }}>{p.id}</td>
                {ids.map((qId: string) => {
                  const v = wins.matrix[p.id]?.[qId];
                  if (v == null) return <td key={qId} className="bg-arena-bg rounded text-center" style={{ width: 48, height: 34 }} />;
                  return (
                    <td key={qId} className="rounded text-center text-[11px] font-mono tabular-nums transition-transform hover:scale-110"
                        style={{ width: 48, height: 34, backgroundColor: cellBg(v), color: v > 0.6 || v < 0.35 ? "#e6edf3" : "#8b949e" }}>
                      {v.toFixed(2)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex flex-col items-center justify-between ml-2 shrink-0" style={{ height: 240 }}>
          <div className="text-[10px] font-bold text-arena-orange">1.00</div>
          <div className="text-[9px] text-arena-muted">row wins</div>
          <div className="w-4 flex-1 rounded-full my-1" style={{ background: "linear-gradient(to bottom, rgba(232,168,56,0.8), rgba(48,54,61,0.3), rgba(74,158,234,0.8))" }} />
          <div className="text-[10px] font-bold text-arena-muted">0.50</div>
          <div className="text-[9px] text-arena-muted">coin flip</div>
          <div className="h-2" />
          <div className="text-[10px] font-bold text-[#4A9EEA]">0.00</div>
          <div className="text-[9px] text-arena-muted">row loses</div>
        </div>
      </div>
    </div>
  );
}
