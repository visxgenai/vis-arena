import React from "react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
} from "chart.js";
import { Line } from "react-chartjs-2";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend);

type AnalyticsProps = {
  data: any;
};

export function RankChart({ data }: AnalyticsProps) {
  const chartData = {
    labels: Array.from({ length: data.num_intervals }, (_, i) => `Interval ${i + 1}`),
    datasets: data.participants.map((p: any) => ({
      label: p.name,
      data: data.ranks[p.id],
      borderColor: p.color,
      backgroundColor: p.color,
      tension: 0.1,
    })),
  };

  return <Line data={chartData} options={{ responsive: true, maintainAspectRatio: false, scales: { y: { reverse: true, title: { display: true, text: "Rank" } } } }} />;
}

export function LeniencyChart({ data }: AnalyticsProps) {
  const chartData = {
    labels: Array.from({ length: data.num_intervals }, (_, i) => `Interval ${i + 1}`),
    datasets: data.participants.map((p: any) => ({
      label: p.name,
      data: data.leniency[p.id],
      borderColor: p.color,
      backgroundColor: p.color,
      tension: 0.3,
    })),
  };

  return <Line data={chartData} options={{ responsive: true, maintainAspectRatio: false, scales: { y: { title: { display: true, text: "Leniency Drift" } } } }} />;
}

export function ConsensusChart({ data }: AnalyticsProps) {
  const chartData = {
    labels: Array.from({ length: data.num_intervals }, (_, i) => `Interval ${i + 1}`),
    datasets: data.participants.map((p: any) => ({
      label: p.name,
      data: data.consensus[p.id],
      borderColor: p.color,
      backgroundColor: p.color,
      tension: 0.3,
    })),
  };

  return <Line data={chartData} options={{ responsive: true, maintainAspectRatio: false, scales: { y: { title: { display: true, text: "Consensus Agreement" } } } }} />;
}

export function EloChart({ data }: AnalyticsProps) {
  const chartData = {
    labels: Array.from({ length: data.num_intervals }, (_, i) => `Interval ${i + 1}`),
    datasets: data.participants.map((p: any) => ({
      label: p.name,
      data: data.elo[p.id],
      borderColor: p.color,
      backgroundColor: p.color,
      tension: 0.1,
    })),
  };

  return <Line data={chartData} options={{ responsive: true, maintainAspectRatio: false, scales: { y: { title: { display: true, text: "Elo Skill" } } } }} />;
}
