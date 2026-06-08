import React, { ChangeEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import {
  BarChart3,
  BookOpen,
  ChevronDown,
  Database,
  ExternalLink,
  FileArchive,
  KeyRound,
  Medal,
  TrendingUp,
  Trophy,
  Upload,
  Eye,
  Clock,
} from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
  Legend,
} from "recharts";
import datasetGuide from "../docs/dataset-guide.md?raw";
import gettingStarted from "../docs/getting-started.md?raw";
import sdkGuide from "../docs/sdk-guide.md?raw";
import submissionGuide from "../docs/submission-guide.md?raw";
import "./index.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

type CriterionScore = {
  id: string;
  score: number;
  max_score: number;
};

type Dataset = {
  id: string;
  name: string;
  visibility: string;
  task_count: number;
};

type Submission = {
  id: string;
  name: string;
  status: string;
  score: number | null;
};

type SubmissionSnapshot = {
  id: string;
  score: number | null;
  created_at?: string | null;
  criteria?: CriterionScore[];
  has_preview?: boolean;
};

type AgentEntry = {
  id: string;
  name: string;
  score: number | null;
  created_at?: string | null;
  owner_name?: string | null;
  criteria?: CriterionScore[];
  has_preview?: boolean;
  submissions: SubmissionSnapshot[];
};

type HistoryPoint = {
  name: string;
  score: number;
  created_at: string;
};

type ApiClient = {
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
};

type Tab = "leaderboard" | "dashboard" | "docs";

const DOCS = [
  { id: "getting-started", label: "Getting Started", body: gettingStarted },
  { id: "submissions", label: "Submissions", body: submissionGuide },
  { id: "sdk", label: "SDK & CLI", body: sdkGuide },
  { id: "datasets", label: "Datasets", body: datasetGuide },
];

const RANK_COLORS: Record<number, string> = {
  1: "bg-amber-400 text-white",
  2: "bg-slate-400 text-white",
  3: "bg-amber-700 text-white",
};

const CHART_COLORS = [
  "#1f6feb", "#10a37f", "#e05252", "#9b59b6", "#f39c12",
  "#1abc9c", "#e74c3c", "#3498db", "#2ecc71", "#e67e22",
];

function formatCriterionId(id: string): string {
  return id
    .replace(/[_-]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function App() {
  const [token, setToken] = useState(() => localStorage.getItem("visArenaToken") || "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [leaderboard, setLeaderboard] = useState<AgentEntry[]>([]);
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [expandedAgent, setExpandedAgent] = useState<string | null>(null);
  const [selectedSubmission, setSelectedSubmission] = useState<{ agent: AgentEntry; submission: SubmissionSnapshot } | null>(null);
  const [message, setMessage] = useState("");
  const [activeTab, setActiveTab] = useState<Tab>("leaderboard");
  const [activeDocId, setActiveDocId] = useState(DOCS[0].id);

  const api = useMemo(() => makeApi(token), [token]);
  const activeDoc = DOCS.find((doc) => doc.id === activeDocId) || DOCS[0];

  useEffect(() => {
    void fetchLeaderboard(setLeaderboard, setHistory);
    if (token) {
      void refreshDashboard(api, setDatasets, setSubmissions, setMessage);
    }
  }, [api, token]);

  async function login(mode: "login" | "register") {
    setMessage("");
    const path = mode === "register" ? "/v1/auth/register" : "/v1/auth/login";
    const response = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!response.ok) {
      setMessage(await response.text());
      return;
    }
    const payload = await response.json();
    localStorage.setItem("visArenaToken", payload.access_token);
    setToken(payload.access_token);
    setMessage(`Signed in as ${payload.user.email}`);
  }

  async function uploadDataset(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const created = await api.post<{ dataset: Dataset; upload: PresignedUpload }>("/v1/datasets/uploads", {
      name: file.name.replace(/\.zip$/, ""),
      visibility: "private",
    });
    await uploadToS3(created.upload, file);
    await api.post<Dataset>(`/v1/datasets/${created.dataset.id}/finalize`);
    await refreshDashboard(api, setDatasets, setSubmissions, setMessage);
  }

  async function uploadSubmission(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const created = await api.post<{ submission: Submission; upload: PresignedUpload }>("/v1/submissions/uploads", {
      name: file.name.replace(/\.zip$/, ""),
    });
    await uploadToS3(created.upload, file);
    await api.post<Submission>(`/v1/submissions/${created.submission.id}/finalize`);
    await refreshDashboard(api, setDatasets, setSubmissions, setMessage);
  }

  const tabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: "leaderboard", label: "Leaderboard", icon: <Trophy size={16} /> },
    { id: "dashboard", label: "Dashboard", icon: <BarChart3 size={16} /> },
    { id: "docs", label: "Guides", icon: <BookOpen size={16} /> },
  ];

  return (
    <main className="min-h-screen bg-arena-field">
      <header className="flex h-14 items-center justify-between border-b border-slate-300 bg-white px-5">
        <div className="flex items-center gap-2.5 font-extrabold text-arena-ink">
          <BarChart3 size={24} />
          <span>Agentic VIS Challenge</span>
        </div>
        <nav className="flex items-center gap-1">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-semibold transition-colors ${
                activeTab === tab.id
                  ? "bg-arena-blue text-white"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.icon} {tab.label}
            </button>
          ))}
          {token ? (
            <button className="ghost-button ml-3 !min-h-8 !text-xs" onClick={() => { localStorage.removeItem("visArenaToken"); setToken(""); }}>
              Sign out
            </button>
          ) : null}
        </nav>
      </header>

      {activeTab === "leaderboard" && (
        <LeaderboardView
          leaderboard={leaderboard}
          history={history}
          expandedAgent={expandedAgent}
          selectedSubmission={selectedSubmission}
          onToggleAgent={(name) => {
            setExpandedAgent(expandedAgent === name ? null : name);
            if (expandedAgent === name) setSelectedSubmission(null);
          }}
          onSelectSubmission={(agent, sub) => {
            setSelectedSubmission(
              selectedSubmission?.submission.id === sub.id ? null : { agent, submission: sub },
            );
          }}
        />
      )}

      {activeTab === "dashboard" && (
        <DashboardView
          token={token}
          email={email}
          password={password}
          datasets={datasets}
          submissions={submissions}
          message={message}
          onEmailChange={setEmail}
          onPasswordChange={setPassword}
          onLogin={login}
          onUploadDataset={uploadDataset}
          onUploadSubmission={uploadSubmission}
        />
      )}

      {activeTab === "docs" && (
        <DocsView
          activeDoc={activeDoc}
          activeDocId={activeDocId}
          onDocChange={setActiveDocId}
        />
      )}
    </main>
  );
}

function LeaderboardView(props: {
  leaderboard: AgentEntry[];
  history: HistoryPoint[];
  expandedAgent: string | null;
  selectedSubmission: { agent: AgentEntry; submission: SubmissionSnapshot } | null;
  onToggleAgent: (name: string) => void;
  onSelectSubmission: (agent: AgentEntry, sub: SubmissionSnapshot) => void;
}) {
  const { leaderboard, history, expandedAgent, selectedSubmission, onToggleAgent, onSelectSubmission } = props;

  const historyChartData = useMemo(() => {
    const byName: Record<string, { date: string; score: number; ts: number }[]> = {};
    for (const h of history) {
      if (!byName[h.name]) byName[h.name] = [];
      byName[h.name].push({
        date: formatDate(h.created_at),
        score: h.score,
        ts: new Date(h.created_at).getTime(),
      });
    }

    const allTimestamps = [...new Set(history.map((h) => new Date(h.created_at).getTime()))].sort((a, b) => a - b);
    return allTimestamps.map((ts) => {
      const point: Record<string, number | string> = { date: formatDate(new Date(ts).toISOString()) };
      for (const [name, entries] of Object.entries(byName)) {
        const match = entries.find((e) => e.ts === ts);
        if (match) point[name] = match.score;
      }
      return point;
    });
  }, [history]);

  const submissionNames = useMemo(() => {
    return [...new Set(history.map((h) => h.name))];
  }, [history]);

  const activeCriteria = selectedSubmission?.submission.criteria;
  const radarData = useMemo(() => {
    if (!activeCriteria?.length) return [];
    return activeCriteria.map((c) => ({
      criterion: formatCriterionId(c.id),
      score: c.score,
      fullMark: c.max_score,
      percentage: c.max_score > 0 ? Math.round((c.score / c.max_score) * 100) : 0,
    }));
  }, [activeCriteria]);

  const totalAgents = leaderboard.length;
  const totalSubmissions = leaderboard.reduce((sum, a) => sum + a.submissions.length, 0);

  return (
    <section className="mx-auto max-w-7xl p-4">
      <div className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-extrabold text-arena-ink">2026 Leaderboard</h1>
          <p className="mt-1 text-sm text-arena-muted">
            {totalAgents} agent{totalAgents !== 1 ? "s" : ""} · {totalSubmissions} total submission{totalSubmissions !== 1 ? "s" : ""}
          </p>
        </div>
      </div>

      {history.length > 1 && (
        <section className="panel mb-4">
          <h2 className="panel-title"><TrendingUp size={18} /> Score History</h2>
          <div className="h-[280px]">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={historyChartData} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="date" tick={{ fontSize: 12 }} stroke="#94a3b8" />
                <YAxis tick={{ fontSize: 12 }} stroke="#94a3b8" domain={[0, 100]} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "#fff",
                    border: "1px solid #e2e8f0",
                    borderRadius: "8px",
                    fontSize: 13,
                  }}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                {submissionNames.map((name, i) => (
                  <Line
                    key={name}
                    type="monotone"
                    dataKey={name}
                    stroke={CHART_COLORS[i % CHART_COLORS.length]}
                    strokeWidth={2}
                    dot={{ r: 4 }}
                    connectNulls
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </section>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_380px]">
        <section className="panel">
          <h2 className="panel-title"><Medal size={18} /> Rankings</h2>
          <div className="grid gap-2">
            {leaderboard.map((agent, index) => {
              const rank = index + 1;
              const isExpanded = expandedAgent === agent.name;
              return (
                <div key={agent.name} className="overflow-hidden rounded-lg border border-slate-200">
                  <button
                    className={`grid w-full min-h-14 grid-cols-[40px_minmax(0,1fr)_auto_28px] items-center gap-3 px-3 py-3 text-left transition-colors ${
                      isExpanded ? "bg-blue-50" : "bg-arena-paper hover:bg-white"
                    }`}
                    onClick={() => onToggleAgent(agent.name)}
                  >
                    <span className={`flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold ${
                      RANK_COLORS[rank] || "bg-slate-100 text-arena-muted"
                    }`}>
                      {rank}
                    </span>
                    <div className="min-w-0">
                      <div className="truncate font-semibold text-arena-ink">{agent.name}</div>
                      <div className="truncate text-xs text-arena-muted">
                        {agent.owner_name || "Anonymous"} · {agent.submissions.length} submission{agent.submissions.length !== 1 ? "s" : ""}
                      </div>
                    </div>
                    <span className="text-right font-mono text-lg font-bold text-arena-ink">
                      {Number(agent.score || 0).toFixed(2)}
                    </span>
                    <ChevronDown size={16} className={`text-slate-400 transition-transform ${isExpanded ? "rotate-180" : ""}`} />
                  </button>

                  {isExpanded && (
                    <div className="border-t border-slate-100 bg-white">
                      <div className="px-3 py-2">
                        <div className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-arena-muted">
                          <Clock size={12} /> Submission History
                        </div>
                        <div className="grid gap-1">
                          {agent.submissions.map((sub) => {
                            const isActive = selectedSubmission?.submission.id === sub.id;
                            return (
                              <button
                                key={sub.id}
                                className={`grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-3 rounded-md px-3 py-2 text-left text-sm transition-colors ${
                                  isActive
                                    ? "border border-arena-blue bg-blue-50 shadow-sm"
                                    : "border border-transparent hover:bg-slate-50"
                                }`}
                                onClick={() => onSelectSubmission(agent, sub)}
                              >
                                <div className="min-w-0">
                                  <div className="text-xs text-arena-muted">
                                    {formatDateTime(sub.created_at)}
                                  </div>
                                </div>
                                <span className="font-mono text-sm font-semibold text-arena-ink">
                                  {Number(sub.score || 0).toFixed(2)}
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
            {leaderboard.length === 0 && (
              <div className="flex h-32 items-center justify-center text-sm text-arena-muted">
                No scored submissions yet. Submit an agent to see rankings.
              </div>
            )}
          </div>
        </section>

        <div className="grid content-start gap-4">
          {selectedSubmission ? (
            <>
              <section className="panel">
                <h2 className="panel-title"><BarChart3 size={18} /> Score Breakdown</h2>
                <div className="mb-1 text-center text-sm font-medium text-arena-muted">
                  {selectedSubmission.agent.name}
                </div>
                <div className="mb-1 text-center text-xs text-arena-muted">
                  {formatDateTime(selectedSubmission.submission.created_at)}
                </div>
                <div className="mb-3 text-center">
                  <span className="text-3xl font-extrabold text-arena-ink">
                    {Number(selectedSubmission.submission.score || 0).toFixed(2)}
                  </span>
                  <span className="ml-1 text-sm text-arena-muted">/ 100</span>
                </div>
                {radarData.length > 0 ? (
                  <>
                    <div className="h-[260px]">
                      <ResponsiveContainer width="100%" height="100%">
                        <RadarChart data={radarData} cx="50%" cy="50%" outerRadius="70%">
                          <PolarGrid stroke="#e2e8f0" />
                          <PolarAngleAxis dataKey="criterion" tick={{ fontSize: 11 }} />
                          <PolarRadiusAxis angle={90} domain={[0, "auto"]} tick={{ fontSize: 10 }} />
                          <Radar
                            name={selectedSubmission.agent.name}
                            dataKey="score"
                            stroke="#1f6feb"
                            fill="#1f6feb"
                            fillOpacity={0.2}
                          />
                        </RadarChart>
                      </ResponsiveContainer>
                    </div>
                    <div className="mt-2 grid gap-1.5">
                      {radarData.map((d) => (
                        <div key={d.criterion} className="flex items-center justify-between text-sm">
                          <span className="text-arena-muted">{d.criterion}</span>
                          <div className="flex items-center gap-2">
                            <div className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-100">
                              <div
                                className="h-full rounded-full bg-arena-blue"
                                style={{ width: `${d.percentage}%` }}
                              />
                            </div>
                            <span className="w-12 text-right font-mono text-xs font-semibold">
                              {d.score.toFixed(1)}/{d.fullMark.toFixed(0)}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <p className="text-sm text-arena-muted">No criteria breakdown available for this submission.</p>
                )}
              </section>

              {selectedSubmission.submission.has_preview && (
                <section className="panel">
                  <h2 className="panel-title"><Eye size={18} /> Submission Preview</h2>
                  <SubmissionPreview submissionId={selectedSubmission.submission.id} />
                </section>
              )}
            </>
          ) : (
            <section className="panel">
              <div className="flex h-48 flex-col items-center justify-center text-arena-muted">
                <Trophy size={32} className="mb-3 opacity-30" />
                <p className="text-sm font-medium">Select an agent to expand</p>
                <p className="mt-1 text-xs">Then click a submission to see its score breakdown</p>
              </div>
            </section>
          )}
        </div>
      </div>
    </section>
  );
}

function SubmissionPreview(props: { submissionId: string }) {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    setPreviewUrl(null);
    fetch(`${API_URL}/v1/leaderboard/${props.submissionId}/preview`)
      .then((r) => { if (r.ok) return r.json(); throw new Error(); })
      .then((data: { url?: string }) => setPreviewUrl(data.url || null))
      .catch(() => setPreviewUrl(null))
      .finally(() => setLoading(false));
  }, [props.submissionId]);

  if (loading) {
    return <div className="flex h-40 items-center justify-center text-sm text-arena-muted">Loading preview...</div>;
  }

  if (!previewUrl) {
    return <div className="flex h-40 items-center justify-center text-sm text-arena-muted">Preview not available</div>;
  }

  return (
    <div>
      <a
        href={previewUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 text-sm font-semibold text-arena-blue hover:underline"
      >
        <ExternalLink size={14} /> Download artifacts
      </a>
    </div>
  );
}

function DashboardView(props: {
  token: string;
  email: string;
  password: string;
  datasets: Dataset[];
  submissions: Submission[];
  message: string;
  onEmailChange: (v: string) => void;
  onPasswordChange: (v: string) => void;
  onLogin: (mode: "login" | "register") => void;
  onUploadDataset: (e: ChangeEvent<HTMLInputElement>) => void;
  onUploadSubmission: (e: ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <section className="mx-auto max-w-5xl p-4">
      <div className="grid gap-4 lg:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
        <div className="grid content-start gap-4">
          <section className="panel">
            <h2 className="panel-title"><KeyRound size={18} /> Account</h2>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
              <input className="field" value={props.email} onChange={(e) => props.onEmailChange(e.target.value)} placeholder="email" type="email" />
              <input className="field" value={props.password} onChange={(e) => props.onPasswordChange(e.target.value)} placeholder="password" type="password" />
              <button className="primary-button" onClick={() => void props.onLogin("login")}>Log in</button>
              <button className="secondary-button" onClick={() => void props.onLogin("register")}>Register</button>
            </div>
            {props.message ? <p className="mt-3 text-sm text-arena-muted">{props.message}</p> : null}
          </section>

          <UploadPanel title="Datasets" icon={<Database size={18} />} buttonText="Upload dataset ZIP" onChange={props.onUploadDataset}>
            {props.datasets.map((dataset) => (
              <div className="list-row" key={dataset.id}>
                <span className="truncate font-medium">{dataset.name}</span>
                <small className="text-arena-muted">{dataset.visibility} · {dataset.task_count} tasks</small>
              </div>
            ))}
            {props.datasets.length === 0 ? <p className="mt-3 text-sm text-arena-muted">No datasets loaded.</p> : null}
          </UploadPanel>

          <UploadPanel title="Submissions" icon={<FileArchive size={18} />} buttonText="Upload agent ZIP" onChange={props.onUploadSubmission}>
            {props.submissions.map((submission) => (
              <div className="list-row" key={submission.id}>
                <span className="truncate font-medium">{submission.name}</span>
                <small className="text-arena-muted">{submission.status}{submission.score == null ? "" : ` · ${submission.score.toFixed(2)}`}</small>
              </div>
            ))}
            {props.submissions.length === 0 ? <p className="mt-3 text-sm text-arena-muted">No submissions yet.</p> : null}
          </UploadPanel>
        </div>

        <section className="panel min-h-[430px]">
          <h2 className="panel-title"><BarChart3 size={18} /> Artifact Preview</h2>
          <div className="h-[360px] overflow-hidden rounded-lg border border-slate-300 bg-white">
            <iframe className="h-full w-full border-0" title="submission preview" sandbox="allow-scripts" srcDoc={previewHtml()} />
          </div>
        </section>
      </div>
    </section>
  );
}

function DocsView(props: {
  activeDoc: { id: string; label: string; body: string };
  activeDocId: string;
  onDocChange: (id: string) => void;
}) {
  return (
    <section className="mx-auto max-w-4xl p-4">
      <section className="panel">
        <h2 className="panel-title"><BookOpen size={18} /> Guides</h2>
        <div className="mb-4 flex flex-wrap gap-2">
          {DOCS.map((doc) => (
            <button
              className={doc.id === props.activeDocId ? "primary-button" : "ghost-button"}
              key={doc.id}
              onClick={() => props.onDocChange(doc.id)}
            >
              {doc.label}
            </button>
          ))}
        </div>
        <article className="markdown-doc">
          <ReactMarkdown>{props.activeDoc.body}</ReactMarkdown>
        </article>
      </section>
    </section>
  );
}

function UploadPanel(props: {
  title: string;
  icon: React.ReactNode;
  buttonText: string;
  onChange: (event: ChangeEvent<HTMLInputElement>) => void;
  children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <h2 className="panel-title">{props.icon} {props.title}</h2>
      <label className="primary-button">
        <Upload size={16} /> {props.buttonText}
        <input className="hidden" type="file" accept=".zip" onChange={props.onChange} />
      </label>
      <div className="mt-3 grid gap-2">{props.children}</div>
    </section>
  );
}

function makeApi(token: string): ApiClient {
  return {
    async get<T>(path: string) {
      const response = await fetch(`${API_URL}${path}`, { headers: { Authorization: `Bearer ${token}` } });
      if (!response.ok) throw new Error(await response.text());
      return response.json() as Promise<T>;
    },
    async post<T>(path: string, body?: unknown) {
      const response = await fetch(`${API_URL}${path}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await response.text());
      return response.json() as Promise<T>;
    },
  };
}

type PresignedUpload = {
  url: string;
  method: "PUT";
  headers?: Record<string, string>;
};

async function uploadToS3(upload: PresignedUpload, file: File) {
  const response = await fetch(upload.url, { method: upload.method, headers: upload.headers, body: file });
  if (!response.ok) throw new Error(await response.text());
}

async function fetchLeaderboard(
  setLeaderboard: React.Dispatch<React.SetStateAction<AgentEntry[]>>,
  setHistory: React.Dispatch<React.SetStateAction<HistoryPoint[]>>,
) {
  try {
    const [lb, hist] = await Promise.all([
      fetch(`${API_URL}/v1/leaderboard`).then((r) => r.json() as Promise<{ items?: AgentEntry[] }>),
      fetch(`${API_URL}/v1/leaderboard/history`).then((r) => r.json() as Promise<{ items?: HistoryPoint[] }>),
    ]);
    setLeaderboard(lb.items || []);
    setHistory(hist.items || []);
  } catch {
    /* leaderboard fetch failed silently */
  }
}

async function refreshDashboard(
  api: ApiClient,
  setDatasets: React.Dispatch<React.SetStateAction<Dataset[]>>,
  setSubmissions: React.Dispatch<React.SetStateAction<Submission[]>>,
  setMessage: React.Dispatch<React.SetStateAction<string>>,
) {
  try {
    const [datasets, submissions] = await Promise.all([
      api.get<{ items?: Dataset[] }>("/v1/datasets"),
      api.get<{ items?: Submission[] }>("/v1/submissions"),
    ]);
    setDatasets(datasets.items || []);
    setSubmissions(submissions.items || []);
  } catch (error) {
    setMessage(error instanceof Error ? error.message : String(error));
  }
}

function previewHtml() {
  const bars = [60, 90, 110, 80, 140, 170, 150].map((height) => `<div class="bar" style="height:${height}px"></div>`).join("");
  return `<!doctype html><html><head><style>
    body{margin:0;font-family:Inter,system-ui,sans-serif;background:#fbfbf8;color:#25313f}
    main{padding:24px}.chart{height:220px;display:flex;align-items:end;gap:12px;border-left:1px solid #9fb3c8;border-bottom:1px solid #9fb3c8;padding:12px}
    .bar{width:34px;background:#2f80ed;border-radius:4px 4px 0 0}.bar:nth-child(2n){background:#10a37f}
  </style></head><body><main><h1>Preview Surface</h1><div class="chart">${bars}</div></main></body></html>`;
}

createRoot(document.getElementById("root") as HTMLElement).render(<App />);
