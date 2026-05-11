"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";
import { TrendingUp, Zap, AlertTriangle, BarChart2, RefreshCw } from "lucide-react";

interface RouteRow {
  route: string;
  count: number;
  pct: number;
  avg_confidence: number;
  avg_mark: number;
  avg_keyword_coverage: number;
  avg_semantic_sim: number;
  flagged: number;
  overridden: number;
}

interface PipelineStats {
  total_marked: number;
  overall_avg_confidence: number;
  flagged_rate: number;
  override_rate: number;
  by_route: RouteRow[];
}

interface ConfBin {
  range: string;
  low: number;
  high: number;
  count: number;
}

interface ConfDist {
  bins: ConfBin[];
  total: number;
  thresholds: { confidence_high: number; confidence_mid: number };
}

interface QRow {
  question_id: string;
  question_text: string;
  topic_tag: string;
  difficulty: string;
  submissions: number;
  avg_auto_mark: number;
  avg_confidence: number;
  flagged_rate: number;
  avg_override_delta: number;
}

const ROUTE_COLORS: Record<string, string> = {
  HIGH: "bg-green-500",
  MID: "bg-blue-500",
  LOW: "bg-amber-500",
};
const ROUTE_TEXT: Record<string, string> = {
  HIGH: "text-green-700",
  MID: "text-blue-700",
  LOW: "text-amber-700",
};
const ROUTE_BG: Record<string, string> = {
  HIGH: "bg-green-50 border-green-200",
  MID: "bg-blue-50 border-blue-200",
  LOW: "bg-amber-50 border-amber-200",
};
const ROUTE_LABEL: Record<string, string> = {
  HIGH: "SLM only — no LLM call",
  MID: "RAG + offline LLM",
  LOW: "RAG wide + online/offline LLM + flagged",
};

function ConfBar({ bins, thresholds }: { bins: ConfBin[]; thresholds: { confidence_high: number; confidence_mid: number } }) {
  const maxCount = Math.max(...bins.map((b) => b.count), 1);
  return (
    <div className="space-y-1">
      <div className="flex items-end gap-0.5 h-28">
        {bins.map((bin, i) => {
          const pct = (bin.count / maxCount) * 100;
          const isHigh = bin.low >= thresholds.confidence_high;
          const isMid = bin.low >= thresholds.confidence_mid && !isHigh;
          const color = isHigh ? "bg-green-400" : isMid ? "bg-blue-400" : "bg-amber-400";
          return (
            <div key={i} className="flex-1 flex flex-col items-center group relative">
              <div
                className={`w-full rounded-sm transition-all ${color}`}
                style={{ height: `${Math.max(pct, 2)}%` }}
              />
              {bin.count > 0 && (
                <div className="absolute -top-6 left-1/2 -translate-x-1/2 bg-gray-800 text-white text-xs px-1.5 py-0.5 rounded opacity-0 group-hover:opacity-100 whitespace-nowrap z-10">
                  {bin.range}: {bin.count}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="flex justify-between text-xs text-gray-400">
        <span>0.0</span>
        <span className="text-amber-600">▲ {thresholds.confidence_mid} MID</span>
        <span className="text-green-600">▲ {thresholds.confidence_high} HIGH</span>
        <span>1.0</span>
      </div>
    </div>
  );
}

export default function AnalyticsPage() {
  const [pipeline, setPipeline] = useState<PipelineStats | null>(null);
  const [confDist, setConfDist] = useState<ConfDist | null>(null);
  const [questions, setQuestions] = useState<QRow[]>([]);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    const [p, c, q] = await Promise.all([
      api.get("/analytics/pipeline"),
      api.get("/analytics/confidence-distribution"),
      api.get("/analytics/questions"),
    ]);
    setPipeline(p.data);
    setConfDist(c.data);
    setQuestions(q.data);
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-indigo-700">Pipeline Analytics</h1>
          <p className="text-xs text-gray-400 mt-0.5">Hybrid SLM + RAG + LLM marking performance</p>
        </div>
        <button
          onClick={load}
          className="flex items-center gap-2 text-sm text-gray-500 hover:text-indigo-600 border rounded-lg px-3 py-1.5 hover:border-indigo-300 transition-colors"
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </header>

      {loading ? (
        <div className="flex items-center justify-center h-64 text-gray-400">Loading analytics…</div>
      ) : (
        <main className="max-w-6xl mx-auto px-8 py-8 space-y-8">

          {/* ── Top-line KPIs ── */}
          {pipeline && (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              {[
                { label: "Total marked", value: pipeline.total_marked, icon: BarChart2, color: "text-indigo-600" },
                { label: "Avg confidence", value: `${(pipeline.overall_avg_confidence * 100).toFixed(1)}%`, icon: TrendingUp, color: "text-green-600" },
                { label: "Flagged rate", value: `${pipeline.flagged_rate}%`, icon: AlertTriangle, color: "text-amber-600" },
                { label: "Override rate", value: `${pipeline.override_rate}%`, icon: Zap, color: "text-blue-600" },
              ].map(({ label, value, icon: Icon, color }) => (
                <div key={label} className="bg-white rounded-xl border shadow-sm p-5">
                  <Icon size={20} className={`${color} mb-2`} />
                  <p className="text-2xl font-bold text-gray-800">{value}</p>
                  <p className="text-sm text-gray-500 mt-0.5">{label}</p>
                </div>
              ))}
            </div>
          )}

          {/* ── Route distribution ── */}
          {pipeline && (
            <section>
              <h2 className="text-base font-semibold text-gray-700 mb-3">Routing tier distribution</h2>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {(["HIGH", "MID", "LOW"] as const).map((route) => {
                  const row = pipeline.by_route.find((r) => r.route === route);
                  if (!row) return (
                    <div key={route} className={`rounded-xl border p-5 ${ROUTE_BG[route]}`}>
                      <p className={`font-bold text-sm ${ROUTE_TEXT[route]}`}>{route}</p>
                      <p className="text-2xl font-bold text-gray-400 mt-1">0</p>
                      <p className="text-xs text-gray-400 mt-1">{ROUTE_LABEL[route]}</p>
                    </div>
                  );
                  return (
                    <div key={route} className={`rounded-xl border p-5 space-y-3 ${ROUTE_BG[route]}`}>
                      <div className="flex items-center justify-between">
                        <p className={`font-bold text-sm ${ROUTE_TEXT[route]}`}>{route}</p>
                        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${ROUTE_TEXT[route]} bg-white/60`}>
                          {row.pct}%
                        </span>
                      </div>
                      <p className="text-3xl font-bold text-gray-800">{row.count}</p>
                      <p className="text-xs text-gray-500">{ROUTE_LABEL[route]}</p>
                      {/* progress bar */}
                      <div className="w-full bg-white/50 rounded-full h-1.5">
                        <div className={`${ROUTE_COLORS[route]} h-1.5 rounded-full`} style={{ width: `${row.pct}%` }} />
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-xs text-gray-600 pt-1">
                        <div><span className="font-medium">Avg conf</span><br />{(row.avg_confidence * 100).toFixed(1)}%</div>
                        <div><span className="font-medium">Avg mark</span><br />{row.avg_mark.toFixed(2)}</div>
                        <div><span className="font-medium">Keyword cov</span><br />{(row.avg_keyword_coverage * 100).toFixed(1)}%</div>
                        <div><span className="font-medium">Semantic sim</span><br />{(row.avg_semantic_sim * 100).toFixed(1)}%</div>
                        <div><span className="font-medium">Flagged</span><br />{row.flagged}</div>
                        <div><span className="font-medium">Overridden</span><br />{row.overridden}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          {/* ── Confidence distribution histogram ── */}
          {confDist && (
            <section>
              <h2 className="text-base font-semibold text-gray-700 mb-1">Confidence score distribution</h2>
              <p className="text-xs text-gray-400 mb-4">
                {confDist.total} marked submissions ·
                amber = LOW (&lt;{confDist.thresholds.confidence_mid}) ·
                blue = MID · green = HIGH (&gt;{confDist.thresholds.confidence_high})
              </p>
              <div className="bg-white rounded-xl border shadow-sm p-6">
                <ConfBar bins={confDist.bins} thresholds={confDist.thresholds} />
              </div>
            </section>
          )}

          {/* ── Per-question table ── */}
          {questions.length > 0 && (
            <section>
              <h2 className="text-base font-semibold text-gray-700 mb-3">Per-question accuracy</h2>
              <div className="bg-white rounded-xl border shadow-sm overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 text-gray-500 uppercase text-xs">
                    <tr>
                      {["Question", "Topic", "Diff", "Subs", "Avg mark", "Avg conf", "Flag %", "Override Δ"].map((h) => (
                        <th key={h} className="px-4 py-3 text-left font-medium whitespace-nowrap">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {questions.map((q) => (
                      <tr key={q.question_id} className="hover:bg-gray-50">
                        <td className="px-4 py-3 max-w-xs truncate text-gray-700">{q.question_text}</td>
                        <td className="px-4 py-3 text-gray-500 whitespace-nowrap">{q.topic_tag || "—"}</td>
                        <td className="px-4 py-3 capitalize text-gray-500">{q.difficulty || "—"}</td>
                        <td className="px-4 py-3 text-gray-700 font-medium">{q.submissions}</td>
                        <td className="px-4 py-3 text-gray-700">{q.avg_auto_mark.toFixed(2)}</td>
                        <td className="px-4 py-3">
                          <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                            q.avg_confidence >= 0.85
                              ? "bg-green-100 text-green-700"
                              : q.avg_confidence >= 0.55
                              ? "bg-blue-100 text-blue-700"
                              : "bg-amber-100 text-amber-700"
                          }`}>
                            {(q.avg_confidence * 100).toFixed(1)}%
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <span className={q.flagged_rate > 30 ? "text-red-600 font-semibold" : "text-gray-600"}>
                            {q.flagged_rate.toFixed(1)}%
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <span className={
                            Math.abs(q.avg_override_delta) > 1
                              ? "text-red-600 font-semibold"
                              : "text-gray-500"
                          }>
                            {q.avg_override_delta > 0 ? "+" : ""}{q.avg_override_delta.toFixed(2)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-xs text-gray-400 mt-2">
                Override Δ = avg (instructor mark − auto mark). Negative = AI over-marked. Large values suggest rubric needs refinement.
              </p>
            </section>
          )}

        </main>
      )}
    </div>
  );
}
