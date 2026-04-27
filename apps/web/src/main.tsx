import React, { ChangeEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import { BarChart3, BookOpen, Database, FileArchive, KeyRound, Trophy, Upload } from "lucide-react";
import datasetGuide from "../docs/dataset-guide.md?raw";
import gettingStarted from "../docs/getting-started.md?raw";
import sdkGuide from "../docs/sdk-guide.md?raw";
import submissionGuide from "../docs/submission-guide.md?raw";
import "./index.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

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

type LeaderboardEntry = {
  id: string;
  name: string;
  score: number | null;
  owner_name?: string | null;
};

type ApiClient = {
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
};

const DOCS = [
  { id: "getting-started", label: "Getting Started", body: gettingStarted },
  { id: "submissions", label: "Submissions", body: submissionGuide },
  { id: "sdk", label: "SDK & CLI", body: sdkGuide },
  { id: "datasets", label: "Datasets", body: datasetGuide },
];

function App() {
  const [token, setToken] = useState(() => localStorage.getItem("visArenaToken") || "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [message, setMessage] = useState("");
  const [activeDocId, setActiveDocId] = useState(DOCS[0].id);

  const api = useMemo(() => makeApi(token), [token]);
  const activeDoc = DOCS.find((doc) => doc.id === activeDocId) || DOCS[0];

  useEffect(() => {
    if (token) {
      void refreshAll(api, setDatasets, setSubmissions, setLeaderboard, setMessage);
      return;
    }
    void fetch(`${API_URL}/v1/leaderboard`)
      .then((response) => response.json())
      .then((data: { items?: LeaderboardEntry[] }) => setLeaderboard(data.items || []))
      .catch(() => undefined);
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
    await refreshAll(api, setDatasets, setSubmissions, setLeaderboard, setMessage);
  }

  async function uploadSubmission(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const created = await api.post<{ submission: Submission; upload: PresignedUpload }>("/v1/submissions/uploads", {
      name: file.name.replace(/\.zip$/, ""),
    });
    await uploadToS3(created.upload, file);
    await api.post<Submission>(`/v1/submissions/${created.submission.id}/finalize`);
    await refreshAll(api, setDatasets, setSubmissions, setLeaderboard, setMessage);
  }

  return (
    <main className="min-h-screen bg-arena-field">
      <header className="flex h-14 items-center justify-between border-b border-slate-300 bg-white px-5">
        <div className="flex items-center gap-2.5 font-extrabold text-arena-ink">
          <BarChart3 size={24} />
          <span>Vis Arena</span>
        </div>
        {token ? (
          <button className="ghost-button" onClick={() => { localStorage.removeItem("visArenaToken"); setToken(""); }}>
            Sign out
          </button>
        ) : null}
      </header>

      <section className="grid gap-4 p-4 lg:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
        <div className="grid content-start gap-4">
          <section className="panel">
            <h2 className="panel-title"><KeyRound size={18} /> Account</h2>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
              <input className="field" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="email" type="email" />
              <input className="field" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="password" type="password" />
              <button className="primary-button" onClick={() => void login("login")}>Log in</button>
              <button className="secondary-button" onClick={() => void login("register")}>Register</button>
            </div>
            {message ? <p className="mt-3 text-sm text-arena-muted">{message}</p> : null}
          </section>

          <UploadPanel title="Datasets" icon={<Database size={18} />} buttonText="Upload dataset ZIP" onChange={uploadDataset}>
            {datasets.map((dataset) => (
              <div className="list-row" key={dataset.id}>
                <span className="truncate font-medium">{dataset.name}</span>
                <small className="text-arena-muted">{dataset.visibility} · {dataset.task_count} tasks</small>
              </div>
            ))}
            {datasets.length === 0 ? <p className="mt-3 text-sm text-arena-muted">No datasets loaded.</p> : null}
          </UploadPanel>

          <UploadPanel title="Submissions" icon={<FileArchive size={18} />} buttonText="Upload agent ZIP" onChange={uploadSubmission}>
            {submissions.map((submission) => (
              <div className="list-row" key={submission.id}>
                <span className="truncate font-medium">{submission.name}</span>
                <small className="text-arena-muted">{submission.status}{submission.score == null ? "" : ` · ${submission.score.toFixed(2)}`}</small>
              </div>
            ))}
            {submissions.length === 0 ? <p className="mt-3 text-sm text-arena-muted">No submissions yet.</p> : null}
          </UploadPanel>
        </div>

        <div className="grid content-start gap-4">
          <section className="panel min-h-[430px]">
            <h2 className="panel-title"><BarChart3 size={18} /> Artifact Preview</h2>
            <div className="h-[360px] overflow-hidden rounded-lg border border-slate-300 bg-white">
              <iframe className="h-full w-full border-0" title="submission preview" sandbox="allow-scripts" srcDoc={previewHtml()} />
            </div>
          </section>

          <section className="panel">
            <h2 className="panel-title"><Trophy size={18} /> Leaderboard</h2>
            <div className="grid gap-2">
              {leaderboard.map((entry, index) => (
                <div className="grid min-h-11 grid-cols-[32px_minmax(0,1fr)_auto] items-center gap-3 rounded-md border border-slate-100 bg-arena-paper px-3 py-2" key={entry.id}>
                  <strong className="text-arena-muted">{index + 1}</strong>
                  <span className="truncate font-medium">{entry.name}</span>
                  <b>{Number(entry.score || 0).toFixed(2)}</b>
                </div>
              ))}
              {leaderboard.length === 0 ? <p className="text-sm text-arena-muted">No scored submissions yet.</p> : null}
            </div>
          </section>

          <section className="panel">
            <h2 className="panel-title"><BookOpen size={18} /> Arena Docs</h2>
            <div className="mb-4 flex flex-wrap gap-2">
              {DOCS.map((doc) => (
                <button
                  className={doc.id === activeDoc.id ? "primary-button" : "ghost-button"}
                  key={doc.id}
                  onClick={() => setActiveDocId(doc.id)}
                >
                  {doc.label}
                </button>
              ))}
            </div>
            <article className="markdown-doc">
              <ReactMarkdown>{activeDoc.body}</ReactMarkdown>
            </article>
          </section>
        </div>
      </section>
    </main>
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

async function refreshAll(
  api: ApiClient,
  setDatasets: React.Dispatch<React.SetStateAction<Dataset[]>>,
  setSubmissions: React.Dispatch<React.SetStateAction<Submission[]>>,
  setLeaderboard: React.Dispatch<React.SetStateAction<LeaderboardEntry[]>>,
  setMessage: React.Dispatch<React.SetStateAction<string>>,
) {
  try {
    const [datasets, submissions, leaderboard] = await Promise.all([
      api.get<{ items?: Dataset[] }>("/v1/datasets"),
      api.get<{ items?: Submission[] }>("/v1/submissions"),
      fetch(`${API_URL}/v1/leaderboard`).then((response) => response.json() as Promise<{ items?: LeaderboardEntry[] }>),
    ]);
    setDatasets(datasets.items || []);
    setSubmissions(submissions.items || []);
    setLeaderboard(leaderboard.items || []);
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
