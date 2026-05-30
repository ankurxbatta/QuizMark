"use client";
import { useState, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";
import api from "@/lib/api";
import {
  Upload, File, RotateCcw, Loader2, CheckCircle,
  BookOpen, Library, ArrowRight,
} from "lucide-react";

interface JobStatus {
  job_id: string;
  status: "queued" | "processing" | "done" | "failed";
  total_chapters: number;
  chapters_done: number;
  total_pages: number;
  current_chapter_title: string | null;
  progress_message: string | null;
  error_message?: string | null;
}

function ProgressBar({ pct, label }: { pct: number; label: string }) {
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-gray-500">
        <span>{label}</span><span>{Math.round(pct)}%</span>
      </div>
      <div className="w-full bg-gray-100 rounded-full h-2">
        <div
          className="bg-indigo-500 h-2 rounded-full transition-all duration-700"
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
    </div>
  );
}

export default function GeneratePage() {
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<"idle" | "uploading" | "processing" | "done" | "error">("idle");
  const [job, setJob] = useState<JobStatus | null>(null);
  const [errorMsg, setErrorMsg] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const router = useRouter();

  // Poll job progress
  useEffect(() => {
    if (!job || job.status === "done" || job.status === "failed") return;
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await api.get(`/questions/jobs/${job.job_id}`);
        setJob(data);
        if (data.status === "done") {
          clearInterval(pollRef.current!);
          setStatus("done");
        } else if (data.status === "failed") {
          clearInterval(pollRef.current!);
          setStatus("error");
          setErrorMsg(data.error_message || "Ingestion failed.");
        }
      } catch { /* ignore transient */ }
    }, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [job?.job_id, job?.status]);

  const reset = () => {
    setFile(null);
    setStatus("idle");
    setJob(null);
    setErrorMsg("");
    if (pollRef.current) clearInterval(pollRef.current);
  };

  const handleUpload = async () => {
    if (!file) return;
    setStatus("uploading");
    setErrorMsg("");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const { data } = await api.post("/questions/ingest-book", fd);
      setJob(data);
      setStatus("processing");
    } catch (err: any) {
      setErrorMsg(err.response?.data?.detail || "Upload failed. Check the worker logs.");
      setStatus("error");
    }
  };

  const jobPct = job
    ? job.status === "done" ? 100
    : job.total_chapters > 0 ? Math.min((job.chapters_done / job.total_chapters) * 100, 90)
    : job.total_pages > 0 ? 15
    : 5
    : 0;

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700 flex items-center gap-2">
          <Library size={20} /> Add Book to Library
        </h1>
        <p className="text-xs text-gray-400 mt-0.5">
          Upload a PDF textbook · Chapters, tables, formulas and charts are extracted and stored · Generate questions from Library
        </p>
      </header>

      <main className="max-w-2xl mx-auto px-8 py-12 space-y-6">

        {/* Upload card */}
        {status === "idle" && (
          <div className="bg-white rounded-xl border shadow-sm p-8 space-y-6">
            {/* Drop zone */}
            <div
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-12 text-center cursor-pointer transition-colors ${
                file
                  ? "border-red-400 bg-red-50"
                  : "border-gray-300 hover:border-indigo-300 hover:bg-indigo-50/30"
              }`}
            >
              {file ? (
                <div className="flex flex-col items-center gap-2">
                  <File size={40} className="text-red-500" />
                  <p className="font-semibold text-gray-800">{file.name}</p>
                  <p className="text-xs text-gray-400">{(file.size / 1024 / 1024).toFixed(1)} MB</p>
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); reset(); }}
                    className="text-xs text-gray-400 hover:text-red-500 flex items-center gap-1 mt-1"
                  >
                    <RotateCcw size={11} /> Change file
                  </button>
                </div>
              ) : (
                <div className="flex flex-col items-center gap-3">
                  <div className="w-14 h-14 bg-indigo-50 rounded-full flex items-center justify-center">
                    <Upload size={24} className="text-indigo-400" />
                  </div>
                  <div>
                    <p className="text-gray-700 font-medium">
                      Drop your <span className="text-red-600">.pdf</span> textbook here
                    </p>
                    <p className="text-xs text-gray-400 mt-1">Up to 25 MB · Full textbooks supported (600+ pages)</p>
                  </div>
                </div>
              )}
              <input
                ref={inputRef}
                type="file"
                accept=".pdf"
                className="hidden"
                onChange={(e) => setFile(e.target.files?.[0] || null)}
              />
            </div>

            <button
              onClick={handleUpload}
              disabled={!file}
              className="w-full bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors flex items-center justify-center gap-2 text-sm"
            >
              <BookOpen size={17} /> Add to Library
            </button>

            {/* What happens */}
            <div className="bg-gray-50 rounded-xl p-4 border border-gray-100 space-y-3 text-xs text-gray-500">
              <p className="font-semibold text-gray-700 text-sm">What happens when you add a book</p>
              <div className="grid grid-cols-2 gap-3">
                {[
                  ["📖 Chapter detection", "Automatically finds all chapters and sections"],
                  ["📊 Table extraction", "All data tables are parsed and stored with their structure"],
                  ["🔢 Formula preservation", "Mathematical notation (σ, μ, z-scores) kept intact"],
                  ["🖼️ Image OCR", "Text inside figures and diagrams is read via OCR"],
                  ["📈 Chart descriptions", "Graphs and charts described by Gemini Vision AI"],
                  ["🔍 Vector embeddings", "Each section embedded for semantic search during marking"],
                ].map(([title, desc]) => (
                  <div key={title as string} className="flex gap-2">
                    <span className="text-base leading-none mt-0.5">{(title as string).split(" ")[0]}</span>
                    <div>
                      <p className="font-medium text-gray-600 text-xs">{(title as string).slice(3)}</p>
                      <p className="text-gray-400 text-xs">{desc as string}</p>
                    </div>
                  </div>
                ))}
              </div>
              <p className="text-indigo-500 font-medium">
                Once stored, go to <strong>Library</strong> to generate questions — choose specific chapters and difficulty level.
              </p>
            </div>
          </div>
        )}

        {/* Processing / uploading */}
        {(status === "uploading" || status === "processing") && (
          <div className="bg-white rounded-xl border shadow-sm p-8 space-y-5">
            <div className="flex items-center gap-3">
              <Loader2 size={22} className="animate-spin text-indigo-500 shrink-0" />
              <div>
                <p className="font-semibold text-gray-800">
                  {status === "uploading" ? "Uploading…" : "Processing book…"}
                </p>
                <p className="text-xs text-gray-400">{file?.name}</p>
              </div>
            </div>

            {job && (
              <>
                <ProgressBar
                  pct={jobPct}
                  label={
                    job.total_chapters > 0
                      ? `Chapters stored: ${job.chapters_done} / ${job.total_chapters}`
                      : "Parsing PDF…"
                  }
                />
                {job.progress_message && (
                  <p className="text-sm text-gray-600 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                    {job.progress_message}
                  </p>
                )}
                {job.current_chapter_title && (
                  <p className="text-xs text-gray-500">
                    Current: <span className="text-gray-700">{job.current_chapter_title}</span>
                  </p>
                )}
              </>
            )}
          </div>
        )}

        {/* Done */}
        {status === "done" && job && (
          <div className="bg-white rounded-xl border shadow-sm p-8 space-y-5">
            <div className="flex items-start gap-3 bg-green-50 border border-green-200 rounded-xl px-5 py-4 text-green-700">
              <CheckCircle size={22} className="shrink-0 mt-0.5" />
              <div>
                <p className="font-semibold">Book added to Library</p>
                <p className="text-sm text-green-600 mt-0.5">
                  {job.total_chapters} chapters stored · {job.progress_message?.match(/\d+ chunks/)?.[0] || "chunks stored"}
                </p>
              </div>
            </div>

            <div className="flex gap-3">
              <button
                onClick={() => router.push("/library")}
                className="flex-1 bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 transition-colors flex items-center justify-center gap-2 text-sm"
              >
                Go to Library <ArrowRight size={16} />
              </button>
              <button
                onClick={reset}
                className="px-5 py-3 border border-gray-200 rounded-xl text-sm text-gray-600 hover:bg-gray-50 transition-colors"
              >
                Add another
              </button>
            </div>
          </div>
        )}

        {/* Error */}
        {status === "error" && (
          <div className="bg-white rounded-xl border shadow-sm p-8 space-y-4">
            <div className="bg-red-50 border border-red-200 rounded-xl px-5 py-4 text-red-700 text-sm">
              <p className="font-semibold mb-1">Failed to add book</p>
              <p>{errorMsg}</p>
            </div>
            <button onClick={reset} className="text-sm text-indigo-600 hover:text-indigo-700 flex items-center gap-1">
              <RotateCcw size={13} /> Try again
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
