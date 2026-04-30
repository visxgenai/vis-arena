export const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

export type Participant = {
  id: string;
  name: string;
  color: string;
};

export type ArenaOverview = {
  total_participants: number;
  total_intervals: number;
  total_submissions: number;
  datasets: string[];
};

export type LeaderboardItem = {
  rank: number;
  id: string;
  name: string;
  color: string;
  score: number;
};

export type FrontierData = {
  participants: Participant[];
  num_intervals: number;
  trajectories: Record<string, number[]>;
  submitted: Record<string, boolean[]>;
  frontiers: Record<string, number[]>;
};

export type ScatterPoint = {
  id: string;
  name: string;
  color: string;
  quality: number;
  alignment: number;
};

export type WinsData = {
  participants: Participant[];
  matrix: Record<string, Record<string, number | null>>;
};

export type AnalyticsData = {
  participants: Participant[];
  num_intervals: number;
  ranks: Record<string, number[]>;
  leniency: Record<string, number[]>;
  consensus: Record<string, number[]>;
  elo: Record<string, number[]>;
};

export type Dataset = {
  id: string;
  name: string;
  visibility: string;
  task_count: number;
};

export type Submission = {
  id: string;
  name: string;
  status: string;
  score: number | null;
};

export type OldLeaderboardEntry = {
  id: string;
  name: string;
  score: number | null;
  owner_name?: string | null;
};

/* ---- API helpers ---- */

export async function fetchArenaOverview(): Promise<ArenaOverview> {
  const res = await fetch(`${API_URL}/v1/arena/overview`);
  return res.json();
}

export async function fetchArenaLeaderboard(): Promise<{ items: LeaderboardItem[] }> {
  const res = await fetch(`${API_URL}/v1/arena/leaderboard`);
  return res.json();
}

export async function fetchFrontier(): Promise<FrontierData> {
  const res = await fetch(`${API_URL}/v1/arena/frontier`);
  return res.json();
}

export async function fetchScatter(): Promise<{ points: ScatterPoint[] }> {
  const res = await fetch(`${API_URL}/v1/arena/scatter`);
  return res.json();
}

export async function fetchWins(): Promise<WinsData> {
  const res = await fetch(`${API_URL}/v1/arena/wins`);
  return res.json();
}

export async function fetchAnalytics(): Promise<AnalyticsData> {
  const res = await fetch(`${API_URL}/v1/arena/analytics`);
  return res.json();
}

export type ApiClient = {
  get<T>(path: string): Promise<T>;
  post<T>(path: string, body?: unknown): Promise<T>;
};

export function makeApi(token: string): ApiClient {
  return {
    async get<T>(path: string) {
      const response = await fetch(`${API_URL}${path}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error(await response.text());
      return response.json() as Promise<T>;
    },
    async post<T>(path: string, body?: unknown) {
      const response = await fetch(`${API_URL}${path}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await response.text());
      return response.json() as Promise<T>;
    },
  };
}

export type PresignedUpload = {
  url: string;
  method: "PUT";
  headers?: Record<string, string>;
};

export async function uploadToS3(upload: PresignedUpload, file: File) {
  const response = await fetch(upload.url, {
    method: upload.method,
    headers: upload.headers,
    body: file,
  });
  if (!response.ok) throw new Error(await response.text());
}
