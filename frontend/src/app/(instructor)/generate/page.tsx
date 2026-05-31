"use client";
import { useState, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";
import api from "@/lib/api";
import {
  Upload, File, RotateCcw, Loader2, CheckCircle,
  BookOpen, Library, ArrowRight, ServerCrash
} from "lucide-react";

interface JobStatus {
  job_id: string;
  filename?: string;
  status: "queued" | "processing" | "done" | "failed";
  total_chapters: number;
  chapters_done: number;
  total_pages: number;
  current_chapter_title: string | null;
  progress_message: string | null;
  error_message?: string | null;
  error?: string | null;
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
  const [isUploading, setIsUploading] = useState(false);
  const [jobs, setJobs] = useState<JobStatus[]>([]);
  const [uploadError, setUploadError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const jobsRef = useRef<JobStatus[]>([]);
  const router = useRouter();

  // Keep ref in sync for interval
  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

  // Poll job progress
  useEffect(() => {
    pollRef.current = setInterval(async () => {
      const currentJobs = jobsRef.current;
      const activeJobs = currentJobs.filter(j => j.status !== "done" && j.status !== "failed");
      if (activeJobs.length === 0) return;

      try {
        const updatedJobs = await Promise.all(
          activeJobs.map(j => api.get(`/questions/jobs/${j.job_id}`).then(res => res.data))
        );
        
        setJobs(prev => {
          const newJobs = [...prev];
          updatedJobs.forEach(uj => {
            const idx = newJobs.findIndex(x => x.job_id === uj.job_id);
            if (idx !== -1) newJobs[idx] = uj;
          });
          
          const activeIds = newJobs.filter(x => x.status !== "done" && x.status !== "failed").map(x => x.job_id);
          if (activeIds.length > 0) {
            localStorage.setItem("active_ingest_jobs", JSON.stringify(activeIds));
          } else {
            localStorage.removeItem("active_ingest_jobs");
          }
          return newJobs;
        });
      } catch { /* ignore transient errors */ }
    }, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  // Recover active jobs on page load
  useEffect(() => {
    const saved = localStorage.getItem("active_ingest_jobs");
    if (saved) {
      try {
        const ids = JSON.parse(saved);
        if (Array.isArray(ids) && ids.length > 0) {
          Promise.all(ids.map(id => api.get(`/questions/jobs/${id}`).then(res => res.data)))
            .then(data => {
              setJobs(prev => {
                // merge with any jobs that might have been uploaded in the split second before this resolves
                const combined = [...prev];
                data.forEach(d => {
                  if (!combined.find(x => x.job_id === d.job_id)) combined.push(d);
                });
                return combined;
              });
            })
            .catch(() => {});
        }
      } catch { }
    }
  }, []);

  const reset = () => {
    setFile(null);
    setUploadError("");
  };

  const handleUpload = async () => {
    if (!file) return;
    setIsUploading(true);
    setUploadError("");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const { data } = await api.post("/questions/ingest-book", fd);
      setJobs(prev => {
        const newJobs = [data, ...prev];
        const activeIds = newJobs.filter(x => x.status !== "done" && x.status !== "failed").map(x => x.job_id);
        localStorage.setItem("active_ingest_jobs", JSON.stringify(activeIds));
        return newJobs;
      });
      reset();
    } catch (err: any) {
      setUploadError(err.response?.data?.detail || "Upload failed. Check the worker logs.");
    } finally {
      setIsUploading(false);
    }
  };

  const getJobPct = (job: JobStatus) => {
    if (job.status === "done") return 100;
    if (job.total_chapters > 0) return Math.min((job.chapters_done / job.total_chapters) * 100, 90);
    if (job.total_pages > 0) return 15;
    return 5;
  };

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
        {/* Upload card (Always visible) */}
        <div className="bg-white rounded-xl border shadow-sm p-8 space-y-6">
          {/* Drop zone */}
          <div
            onClick={() => !isUploading && inputRef.current?.click()}
            className={`border-2 border-dashed rounded-xl p-12 text-center transition-colors ${
              isUploading ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
            } ${
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
                {!isUploading && (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); reset(); }}
                    className="text-xs text-gray-400 hover:text-red-500 flex items-center gap-1 mt-1"
                  >
                    <RotateCcw size={11} /> Change file
                  </button>
                )}
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
              disabled={isUploading}
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
          </div>

          <button
            onClick={handleUpload}
            disabled={!file || isUploading}
            className="w-full bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-50 transition-colors flex items-center justify-center gap-2 text-sm"
          >
            {isUploading ? <><Loader2 size={17} className="animate-spin" /> Uploading...</> : <><BookOpen size={17} /> Add to Library</>}
          </button>

          {uploadError && (
            <div className="bg-red-50 border border-red-200 rounded-xl px-4 py-3 text-red-700 text-sm">
              <p className="font-semibold mb-1">Failed to add book</p>
              <p>{uploadError}</p>
            </div>
          )}

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
          </div>
        </div>

        {/* Job List */}
        {jobs.length > 0 && (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-gray-800">Processing Queue</h2>
            {jobs.map(job => (
              <div key={job.job_id} className="bg-white rounded-xl border shadow-sm p-6 space-y-4">
                
                {/* Header */}
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-3">
                    {job.status === "processing" || job.status === "queued" ? (
                      <Loader2 size={22} className="animate-spin text-indigo-500 shrink-0" />
                    ) : job.status === "done" ? (
                      <CheckCircle size={22} className="text-green-500 shrink-0" />
                    ) : (
                      <ServerCrash size={22} className="text-red-500 shrink-0" />
                    )}
                    <div>
                      <p className="font-semibold text-gray-800">
                        {job.status === "queued" ? "Queued…" : job.status === "processing" ? "Processing book…" : job.status === "done" ? "Book added to Library" : "Failed"}
                      </p>
                      <p className="text-xs text-gray-400">{job.filename || job.job_id}</p>
                    </div>
                  </div>
                  {job.status === "done" && (
                    <button
                      onClick={() => router.push("/library")}
                      className="text-indigo-600 hover:text-indigo-700 text-sm font-medium flex items-center gap-1"
                    >
                      Library <ArrowRight size={14} />
                    </button>
                  )}
                </div>

                {/* Body */}
                {(job.status === "processing" || job.status === "queued") && (
                  <>
                    <ProgressBar
                      pct={getJobPct(job)}
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

                {job.status === "done" && (
                  <p className="text-sm text-green-600 bg-green-50 px-3 py-2 rounded-lg inline-block mt-2">
                    {job.total_chapters} chapters stored · {job.progress_message?.match(/\d+ chunks/)?.[0] || "chunks stored"}
                  </p>
                )}

                {job.status === "failed" && (
                  <p className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg border border-red-100">
                    {job.error_message || job.error || "Ingestion failed."}
                  </p>
                )}
                
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
