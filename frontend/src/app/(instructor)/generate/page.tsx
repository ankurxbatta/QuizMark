"use client";
import { useState, useRef, useEffect } from "react";
import api from "@/lib/api";
import {
  Upload, CheckCircle, FileText, File, Zap,
  BookOpen, ChevronDown, RotateCcw, Loader2
} from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────────
interface SyncResult {
  generated: number;
  source_file: string;
  source_pages?: number;
  chunks_processed?: number;
  topics_covered?: string[];
}

interface AsyncJob {
  job_id: string;
  filename: string;
  total_pages: number;
  status: "queued" | "processing" | "done" | "failed";
  chapters_done: number;
  questions_created: number;
  error?: string;
}

// ── Topic chapters from OpenStax Business Statistics ─────────────────────
const TOPICS = [
  "All chapters",
  "Sampling and Data",
  "Descriptive Statistics",
  "Probability",
  "Discrete Random Variables",
  "Continuous Random Variables",
  "Normal Distribution",
  "Central Limit Theorem",
  "Confidence Intervals",
  "Hypothesis Testing (One Sample)",
  "Hypothesis Testing (Two Samples)",
  "Chi-Square Tests",
  "F Distribution and ANOVA",
  "Linear Regression and Correlation",
];

// ── Progress bar component ─────────────────────────────────────────────────
function ProgressBar({ pct, label }: { pct: number; label: string }) {
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-gray-500">
        <span>{label}</span>
        <span>{Math.round(pct)}%</span>
      </div>
      <div className="w-full bg-gray-100 rounded-full h-2">
        <div
          className="bg-indigo-500 h-2 rounded-full transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────
export default function GeneratePage() {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<"quick" | "fullbook">("quick");
  const [qtype, setQtype] = useState("short_answer");
  const [count, setCount] = useState(20);
  const [countPerChapter, setCountPerChapter] = useState(10);
  const [topicFilter, setTopicFilter] = useState("All chapters");

  const [status, setStatus] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [syncResult, setSyncResult] = useState<SyncResult | null>(null);
  const [asyncJob, setAsyncJob] = useState<AsyncJob | null>(null);
  const [errorMsg, setErrorMsg] = useState("");

  const inputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isPDF = !!file?.name.toLowerCase().endsWith(".pdf");

  // ── Poll async job ────────────────────────────────────────────────────────
  useEffect(() => {
    if (asyncJob && (asyncJob.status === "queued" || asyncJob.status === "processing")) {
      pollRef.current = setInterval(async () => {
        try {
          const { data } = await api.get(`/questions/jobs/${asyncJob.job_id}`);
          setAsyncJob(data);
          if (data.status === "done" || data.status === "failed") {
            clearInterval(pollRef.current!);
            setStatus(data.status === "done" ? "done" : "error");
            if (data.status === "failed") setErrorMsg(data.error || "Ingestion failed.");
          }
        } catch {
          clearInterval(pollRef.current!);
        }
      }, 3000);
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [asyncJob?.job_id]);

  const reset = () => {
    setFile(null);
    setStatus("idle");
    setSyncResult(null);
    setAsyncJob(null);
    setErrorMsg("");
    if (pollRef.current) clearInterval(pollRef.current);
  };

  // ── Submit ────────────────────────────────────────────────────────────────
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setStatus("loading");
    setErrorMsg("");

    const fd = new FormData();
    fd.append("file", file);

    try {
      if (mode === "fullbook") {
        // Async full-textbook ingest
        const { data } = await api.post(
          `/questions/generate/async?question_type=${qtype}&count_per_chapter=${countPerChapter}`,
          fd,
          { headers: { "Content-Type": "multipart/form-data" } }
        );
        setAsyncJob({ ...data, chapters_done: 0, questions_created: 0 });
        setStatus("loading"); // will transition to "done" via polling
      } else {
        // Synchronous quick generate
        const topic = topicFilter !== "All chapters" ? `&topic_filter=${encodeURIComponent(topicFilter)}` : "";
        const { data } = await api.post(
          `/questions/generate?question_type=${qtype}&count=${count}${topic}`,
          fd,
          { headers: { "Content-Type": "multipart/form-data" } }
        );
        setSyncResult(data);
        setStatus("done");
      }
    } catch (err: any) {
      setErrorMsg(err.response?.data?.detail || "Generation failed. Check the worker logs.");
      setStatus("error");
    }
  };

  // ── Job progress display ──────────────────────────────────────────────────
  const jobPct = asyncJob
    ? asyncJob.status === "done"
      ? 100
      : asyncJob.total_pages > 0
      ? Math.min((asyncJob.chapters_done / 13) * 100, 95) // 13 chapters
      : 0
    : 0;

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700">Generate Questions from Textbook</h1>
        <p className="text-xs text-gray-400 mt-0.5">
          Deep PDF analysis · Chapter-aware chunking · Formula preservation · Two-stage SLM + LLM generation
        </p>
      </header>

      <main className="max-w-3xl mx-auto px-8 py-10 space-y-6">

        {/* ── Mode selector ── */}
        <div className="bg-white rounded-xl border shadow-sm p-1 flex gap-1">
          {([
            { id: "quick", icon: Zap, label: "Quick Generate", desc: "Single topic · ≤50 questions · ~2 min" },
            { id: "fullbook", icon: BookOpen, label: "Full Textbook", desc: "All 13 chapters · background job · ~30–60 min" },
          ] as const).map(({ id, icon: Icon, label, desc }) => (
            <button
              key={id}
              onClick={() => setMode(id)}
              className={`flex-1 flex items-center gap-3 px-4 py-3 rounded-lg text-left transition-colors ${
                mode === id
                  ? "bg-indigo-50 border border-indigo-200"
                  : "hover:bg-gray-50"
              }`}
            >
              <Icon size={18} className={mode === id ? "text-indigo-600" : "text-gray-400"} />
              <div>
                <p className={`text-sm font-medium ${mode === id ? "text-indigo-700" : "text-gray-600"}`}>
                  {label}
                </p>
                <p className="text-xs text-gray-400">{desc}</p>
              </div>
            </button>
          ))}
        </div>

        {/* ── Main form ── */}
        <div className="bg-white rounded-xl border shadow-sm p-8 space-y-6">
          <form onSubmit={handleSubmit} className="space-y-5">

            {/* Drop zone */}
            <div
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-colors ${
                file
                  ? isPDF
                    ? "border-red-400 bg-red-50"
                    : "border-indigo-400 bg-indigo-50"
                  : "border-gray-300 hover:border-indigo-300 hover:bg-indigo-50/30"
              }`}
            >
              {file ? (
                <div className="flex flex-col items-center gap-2">
                  {isPDF ? (
                    <File size={36} className="text-red-500" />
                  ) : (
                    <FileText size={36} className="text-indigo-500" />
                  )}
                  <p className="font-semibold text-gray-800">{file.name}</p>
                  <p className="text-xs text-gray-400">
                    {(file.size / 1024 / 1024).toFixed(1)} MB ·{" "}
                    {isPDF
                      ? "PDF textbook — full chapter-aware analysis"
                      : "Plain text"}
                  </p>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); reset(); }}
                    className="text-xs text-gray-400 hover:text-red-500 flex items-center gap-1 mt-1"
                  >
                    <RotateCcw size={11} /> Change file
                  </button>
                </div>
              ) : (
                <div className="flex flex-col items-center gap-2">
                  <Upload size={36} className="text-gray-400" />
                  <p className="text-gray-600 font-medium">
                    Drop your <span className="text-red-600">.pdf</span> textbook or{" "}
                    <span className="text-indigo-600">.txt</span> file here
                  </p>
                  <p className="text-xs text-gray-400">Up to 25 MB · Full 600+ page textbooks supported</p>
                </div>
              )}
              <input
                ref={inputRef}
                type="file"
                accept=".pdf,.txt"
                className="hidden"
                onChange={(e) => {
                  setFile(e.target.files?.[0] || null);
                  setStatus("idle");
                  setSyncResult(null);
                  setAsyncJob(null);
                }}
              />
            </div>

            {/* Options */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                  Question Type
                </label>
                <select
                  value={qtype}
                  onChange={(e) => setQtype(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                >
                  <option value="short_answer">Short Answer</option>
                  <option value="mcq">Multiple Choice (MCQ)</option>
                  <option value="true_false">True / False</option>
                </select>
              </div>

              {mode === "quick" ? (
                <div>
                  <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                    Number of Questions (max 50)
                  </label>
                  <input
                    type="number"
                    min={1}
                    max={50}
                    value={count}
                    onChange={(e) => setCount(parseInt(e.target.value))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                  />
                </div>
              ) : (
                <div>
                  <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                    Questions per Chapter
                  </label>
                  <input
                    type="number"
                    min={2}
                    max={50}
                    value={countPerChapter}
                    onChange={(e) => setCountPerChapter(parseInt(e.target.value))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                  />
                </div>
              )}
            </div>

            {/* Topic filter — Quick mode only */}
            {mode === "quick" && isPDF && (
              <div>
                <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                  Focus on Chapter / Topic
                </label>
                <div className="relative">
                  <select
                    value={topicFilter}
                    onChange={(e) => setTopicFilter(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm appearance-none focus:ring-2 focus:ring-indigo-500 focus:outline-none pr-8"
                  >
                    {TOPICS.map((t) => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </select>
                  <ChevronDown size={14} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
                </div>
                <p className="text-xs text-gray-400 mt-1">
                  "All chapters" samples the best content across the whole book.
                </p>
              </div>
            )}

            {/* Full-book info panel */}
            {mode === "fullbook" && (
              <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-800 space-y-1.5">
                <p className="font-semibold">Full Textbook mode</p>
                <p>Processes every chapter in the background via a Celery worker. The page will show live progress — you can navigate away and come back.</p>
                <p className="text-xs text-amber-600">
                  Estimated time: {countPerChapter * 13 * 2}–{countPerChapter * 13 * 4} minutes on CPU ·
                  {" "}{countPerChapter * 13} total questions across 13 chapters
                </p>
              </div>
            )}

            <button
              type="submit"
              disabled={!file || status === "loading"}
              className="w-full bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-60 transition-colors flex items-center justify-center gap-2"
            >
              {status === "loading" ? (
                <>
                  <Loader2 size={18} className="animate-spin" />
                  {mode === "fullbook" ? "Starting background job…" : "Analysing PDF & generating…"}
                </>
              ) : mode === "fullbook" ? (
                <><BookOpen size={18} /> Ingest Full Textbook</>
              ) : (
                <><Zap size={18} /> Generate Questions</>
              )}
            </button>
          </form>

          {/* ── Async job progress ── */}
          {asyncJob && (
            <div className="space-y-4 border-t pt-5">
              <div className="flex items-center justify-between">
                <p className="font-semibold text-gray-700">
                  {asyncJob.status === "done"
                    ? "✅ Complete!"
                    : asyncJob.status === "failed"
                    ? "❌ Failed"
                    : "⏳ Processing…"}
                </p>
                <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                  asyncJob.status === "done" ? "bg-green-100 text-green-700" :
                  asyncJob.status === "failed" ? "bg-red-100 text-red-700" :
                  asyncJob.status === "processing" ? "bg-blue-100 text-blue-700" :
                  "bg-gray-100 text-gray-500"
                }`}>
                  {asyncJob.status}
                </span>
              </div>

              <ProgressBar pct={jobPct} label={`Chapters processed: ${asyncJob.chapters_done} / 13`} />

              <div className="grid grid-cols-3 gap-3 text-center text-sm">
                <div className="bg-gray-50 rounded-lg p-3">
                  <p className="text-2xl font-bold text-indigo-600">{asyncJob.questions_created}</p>
                  <p className="text-xs text-gray-500 mt-0.5">Questions created</p>
                </div>
                <div className="bg-gray-50 rounded-lg p-3">
                  <p className="text-2xl font-bold text-gray-700">{asyncJob.chapters_done}</p>
                  <p className="text-xs text-gray-500 mt-0.5">Chapters done</p>
                </div>
                <div className="bg-gray-50 rounded-lg p-3">
                  <p className="text-2xl font-bold text-gray-400">{asyncJob.total_pages}</p>
                  <p className="text-xs text-gray-500 mt-0.5">Total pages</p>
                </div>
              </div>

              {asyncJob.status === "done" && (
                <div className="flex items-center gap-3 bg-green-50 border border-green-200 rounded-xl px-5 py-4 text-green-700">
                  <CheckCircle size={22} />
                  <div>
                    <p className="font-semibold">{asyncJob.questions_created} questions generated from {asyncJob.filename}</p>
                    <p className="text-sm text-green-600 mt-0.5">All chapters processed. View them in the Q&amp;A bank.</p>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ── Sync result ── */}
          {status === "done" && syncResult && (
            <div className="border-t pt-5 space-y-3">
              <div className="flex items-start gap-3 bg-green-50 border border-green-200 rounded-xl px-5 py-4 text-green-700">
                <CheckCircle size={22} className="mt-0.5 flex-shrink-0" />
                <div>
                  <p className="font-semibold">
                    {syncResult.generated} questions generated from {syncResult.source_file}
                  </p>
                  {syncResult.source_pages && (
                    <p className="text-sm text-green-600 mt-0.5">
                      {syncResult.source_pages} pages · {syncResult.chunks_processed} teaching chunks analysed
                    </p>
                  )}
                </div>
              </div>
              {syncResult.topics_covered && syncResult.topics_covered.length > 0 && (
                <div>
                  <p className="text-xs font-semibold text-gray-500 uppercase mb-2">Topics covered</p>
                  <div className="flex flex-wrap gap-2">
                    {syncResult.topics_covered.map((t) => (
                      <span key={t} className="text-xs bg-indigo-100 text-indigo-700 px-2.5 py-1 rounded-full font-medium">
                        {t}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* ── Error ── */}
          {status === "error" && (
            <div className="border-t pt-5">
              <div className="bg-red-50 border border-red-200 rounded-xl px-5 py-4 text-red-700 text-sm">
                <p className="font-semibold mb-1">Generation failed</p>
                <p>{errorMsg}</p>
              </div>
            </div>
          )}

          {/* ── How it works ── */}
          {status === "idle" && (
            <div className="bg-gray-50 rounded-xl p-4 border border-gray-100 space-y-2 text-xs text-gray-500">
              <p className="font-semibold text-gray-700 text-sm">How deep PDF analysis works</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-2">
                {[
                  ["📖 Chapter detection", "Identifies chapter and section boundaries throughout the book."],
                  ["🔍 Content filtering", "Separates teaching content (definitions, formulas, examples) from exercises and boilerplate."],
                  ["📐 Formula preservation", "Mathematical notation (σ, μ, z-scores, Σ) is kept intact, not stripped."],
                  ["🧠 Two-stage generation", "SLM extracts concept skeletons per section; LLM enriches each into a full exam question with rubric."],
                  ["🗂️ Topic diversity", "Questions are spread across all chapters — not just the first few pages."],
                  ["🔒 Fully private", "All processing on local Ollama. No text leaves your infrastructure."],
                ].map(([title, desc]) => (
                  <div key={title as string} className="flex gap-2">
                    <span className="text-base leading-none mt-0.5">{(title as string).split(" ")[0]}</span>
                    <div>
                      <p className="font-medium text-gray-600">{(title as string).slice(3)}</p>
                      <p className="text-gray-400">{desc as string}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
