"use client";
import { useState, useEffect, useRef, use } from "react";
import { useRouter } from "next/navigation";
import api from "@/lib/api";
import { useActiveJobs } from "@/lib/useActiveJobs";
import Select from "@/components/Select";
import {
  BookOpen, Database, Layers, Table2, FlaskConical, ImageIcon,
  Loader2, ArrowLeft, Zap, CheckCircle, RefreshCw, CalendarDays,
  ChevronRight, ServerCrash, X,
} from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────────────
interface BookChapter { num: number; title: string }

interface Book {
  book_id: string;
  display_name: string;
  total_chunks: number;
  total_chapters: number;
  chapters: BookChapter[];
  with_tables: number;
  with_math: number;
  with_images: number;
  ingested_at: string | null;
}

interface Job {
  job_id: string;
  filename: string;
  status: "queued" | "processing" | "done" | "failed";
  total_chapters: number;
  chapters_done: number;
  total_pages?: number;
  pages_done?: number;
  progress_percent?: number;
  current_chapter_title: string | null;
  questions_created: number;
  progress_message: string | null;
  error?: string | null;
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function ProgressBar({ pct, label }: { pct: number; label: string }) {
  return (
    <div className="space-y-1.5">
      <div className="flex justify-between text-sm text-gray-500">
        <span>{label}</span><span>{Math.round(pct)}%</span>
      </div>
      <div className="w-full bg-gray-100 rounded-full h-2">
        <div
          className="bg-blue-500 h-2 rounded-full transition-all duration-700"
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
    </div>
  );
}

function Stat({ icon: Icon, value, label, colour }: {
  icon: React.ElementType; value: number; label: string; colour: string;
}) {
  return (
    <div className={`flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-full font-medium ${colour}`}>
      <Icon size={13} />
      <span>{value} {label}</span>
    </div>
  );
}

function JobCard({ job, onDismiss }: { job: Job; onDismiss: (id: string) => void }) {
  const pct =
    job.status === "done"
      ? 100
      : (job.total_pages && job.total_pages > 0 && (job.pages_done ?? 0) > 0)
        ? Math.min(((job.pages_done ?? 0) / job.total_pages) * 100, 99)
        : (job.progress_percent && job.progress_percent > 0)
          ? job.progress_percent
          : (job.total_chapters > 0
              ? Math.min((job.chapters_done / job.total_chapters) * 100, 95)
              : 5);
  const progressLabel = (job.total_pages && job.total_pages > 0)
    ? `Read ${job.pages_done ?? 0} / ${job.total_pages} pages`
    : (job.total_chapters > 0
        ? `Chapters: ${job.chapters_done} / ${job.total_chapters}`
        : "Starting…");

  return (
    <div className="bg-gray-50 border border-gray-200 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {(job.status === "queued" || job.status === "processing") && (
            <Loader2 size={16} className="animate-spin text-blue-500 shrink-0" />
          )}
          {job.status === "done" && <CheckCircle size={16} className="text-green-500 shrink-0" />}
          {job.status === "failed" && <ServerCrash size={16} className="text-red-500 shrink-0" />}
          <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
            job.status === "done"       ? "bg-green-100 text-green-700" :
            job.status === "failed"     ? "bg-red-100 text-red-700"     :
            job.status === "processing" ? "bg-blue-100 text-blue-700"   :
            "bg-gray-200 text-gray-500"
          }`}>{job.status}</span>
          {job.status !== "queued" && job.status !== "processing" && (
            <span className="text-xs text-gray-400">
              {job.questions_created} questions
            </span>
          )}
        </div>
        {(job.status === "done" || job.status === "failed") && (
          <button
            onClick={() => onDismiss(job.job_id)}
            className="text-gray-300 hover:text-gray-500 transition-colors"
            aria-label="Dismiss"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {(job.status === "queued" || job.status === "processing") && (
        <>
          <ProgressBar pct={pct} label={progressLabel} />
          {job.progress_message && (
            <p className="text-xs text-gray-500 truncate">{job.progress_message}</p>
          )}
        </>
      )}

      {job.status === "done" && (
        <>
          <p className="text-xs text-green-600">
            {job.questions_created} questions ready —{" "}
            <a href="/questions" className="underline hover:text-green-800">View Q&amp;A Bank</a>
          </p>
          {job.progress_message?.includes(" of ") && (
            <p className="text-xs text-gray-400">{job.progress_message}</p>
          )}
        </>
      )}

      {job.status === "failed" && (
        <p className="text-xs text-red-600">{job.error || "Generation failed."}</p>
      )}
    </div>
  );
}

// ── Main page ──────────────────────────────────────────────────────────────────
export default function BookDetailPage({ params }: { params: Promise<{ book_id: string }> }) {
  const { book_id } = use(params);
  const bookId = decodeURIComponent(book_id);
  const router = useRouter();

  const [book, setBook]         = useState<Book | null>(null);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState("");

  // Generation state
  const [selectedChapters, setSelectedChapters] = useState<Set<number>>(new Set());
  const [qtype, setQtype]           = useState("short_answer");
  const [difficulty, setDifficulty] = useState("all");
  const [count, setCount]           = useState(10);
  const [requireTable, setRequireTable]   = useState(false);
  const [requireFigure, setRequireFigure] = useState(false);
  const [deepSearch, setDeepSearch]       = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [genError, setGenError]     = useState("");

  // Multiple jobs
  const [jobs, setJobs]     = useState<Job[]>([]);
  const [jobsError, setJobsError] = useState("");
  const jobsRef             = useRef<Job[]>([]);
  const pollRef             = useRef<ReturnType<typeof setInterval> | null>(null);
  const storedIdsRef        = useRef<string[]>([]);
  const { readActiveJobIds, mergeActiveJobIds } = useActiveJobs();

  // Keep ref in sync for interval
  useEffect(() => { jobsRef.current = jobs; }, [jobs]);

  // Load book
  useEffect(() => {
    api.get(`/questions/books/${encodeURIComponent(bookId)}`)
      .then(({ data }) => {
        setBook(data);
        setSelectedChapters(new Set(data.chapters.map((c: BookChapter) => c.num)));
      })
      .catch(() => setError("Book not found or failed to load."))
      .finally(() => setLoading(false));
  }, [bookId]);

  // Recover jobs for this book from localStorage on mount
  useEffect(() => {
    const ids = readActiveJobIds();
    storedIdsRef.current = ids;
    if (ids.length === 0) return;
    Promise.all(ids.map(id =>
      api.get(`/questions/jobs/${id}`)
        .then(r => ({ job: r.data as Job, failed: false }))
        // 404 = job genuinely gone (skip); anything else is a real failure to surface
        .catch(err => ({ job: null, failed: err?.response?.status !== 404 }))
    )).then(results => {
      if (results.some(r => r.failed)) {
        setJobsError("Could not load the status of some generation jobs. Refresh to retry.");
      }
      const valid = results.map(r => r.job).filter(Boolean) as Job[];
      // Only show jobs that belong to this book (filename === bookId)
      const bookJobs = valid.filter(j => j.filename === bookId);
      if (bookJobs.length > 0) setJobs(bookJobs);
    });
  }, [bookId, readActiveJobIds]);

  // Poll active jobs
  useEffect(() => {
    pollRef.current = setInterval(async () => {
      const active = jobsRef.current.filter(j => j.status !== "done" && j.status !== "failed");
      if (active.length === 0) return;
      try {
        const updated = await Promise.all(
          active.map(j => api.get(`/questions/jobs/${j.job_id}`).then(r => r.data))
        );
        setJobsError("");
        setJobs(prev => {
          const next = [...prev];
          updated.forEach(uj => {
            const idx = next.findIndex(x => x.job_id === uj.job_id);
            if (idx !== -1) next[idx] = uj;
          });
          storedIdsRef.current = mergeActiveJobIds(next, storedIdsRef.current);
          return next;
        });
      } catch {
        setJobsError("Failed to refresh job status — retrying…");
      }
    }, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [mergeActiveJobIds]);

  const toggleChapter = (num: number) =>
    setSelectedChapters((prev) => {
      const next = new Set(prev);
      next.has(num) ? next.delete(num) : next.add(num);
      return next;
    });

  const toggleAll = () =>
    setSelectedChapters(
      selectedChapters.size === (book?.chapters.length ?? 0)
        ? new Set()
        : new Set(book!.chapters.map((c) => c.num))
    );

  const handleGenerate = async () => {
    if (!book || selectedChapters.size === 0) {
      setGenError("Select at least one chapter.");
      return;
    }
    setSubmitting(true);
    setGenError("");
    const allSelected = selectedChapters.size === book.chapters.length;
    const chParam = allSelected
      ? ""
      : `&chapter_nums=${[...selectedChapters].sort((a, b) => a - b).join(",")}`;
    try {
      const { data } = await api.post(
        `/questions/generate/from-book?book_id=${encodeURIComponent(bookId)}&question_type=${qtype}&count_per_chapter=${count}&difficulty=${difficulty}${chParam}&require_table=${requireTable}&require_figure=${requireFigure}&deepsearch=${deepSearch}`
      );
      setJobs(prev => {
        const next = [data, ...prev];
        // Persist to shared localStorage key
        storedIdsRef.current = mergeActiveJobIds(next, storedIdsRef.current);
        return next;
      });
    } catch (err: any) {
      setGenError(err.response?.data?.detail || "Generation failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const dismissJob = (jobId: string) => {
    setJobs(prev => {
      const next = prev.filter(j => j.job_id !== jobId);
      storedIdsRef.current = mergeActiveJobIds(next, storedIdsRef.current);
      return next;
    });
  };

  // ── Loading / error ────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center text-gray-400">
        <Loader2 size={28} className="animate-spin mr-3" /> Loading book…
      </div>
    );
  }

  if (error || !book) {
    return (
      <div className="min-h-screen bg-gray-50 p-10">
        <button onClick={() => router.back()} className="flex items-center gap-2 text-sm text-gray-500 hover:text-gray-700 mb-6">
          <ArrowLeft size={15} /> Back to Library
        </button>
        <div className="bg-red-50 border border-red-200 rounded-xl px-6 py-5 text-red-700 text-sm">
          {error || "Book not found."}
        </div>
      </div>
    );
  }

  const ingested = book.ingested_at
    ? new Date(book.ingested_at).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })
    : null;

  const activeChapters = book.chapters.filter((c) => selectedChapters.has(c.num));
  const estimated = activeChapters.length * count;
  const activeJobCount = jobs.filter(j => j.status === "queued" || j.status === "processing").length;

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <button
          onClick={() => router.push("/library")}
          className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-blue-600 mb-2 transition-colors"
        >
          <ArrowLeft size={14} /> Library
        </button>
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 bg-blue-100 rounded-xl flex items-center justify-center shrink-0">
            <BookOpen size={20} className="text-blue-600" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-gray-900">{book.display_name}</h1>
            <p className="text-xs text-gray-400 font-mono mt-0.5">{book.book_id}</p>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-8 py-8 space-y-8">

        {/* Stats row */}
        <div className="flex flex-wrap gap-3">
          <Stat icon={Layers}     value={book.total_chunks}   label="chunks"   colour="bg-blue-50 text-blue-700" />
          <Stat icon={Database}   value={book.total_chapters} label="chapters" colour="bg-slate-100 text-slate-600"  />
          {book.with_tables > 0 && <Stat icon={Table2}       value={book.with_tables} label="tables"   colour="bg-blue-50 text-blue-700"   />}
          {book.with_math   > 0 && <Stat icon={FlaskConical} value={book.with_math}   label="formulas" colour="bg-purple-50 text-purple-700" />}
          {book.with_images > 0 && <Stat icon={ImageIcon}    value={book.with_images} label="charts"   colour="bg-amber-50 text-amber-700"  />}
          {ingested && (
            <div className="flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-full font-medium bg-gray-100 text-gray-500">
              <CalendarDays size={13} /> Ingested {ingested}
            </div>
          )}
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-5 gap-8">

          {/* ── Chapter list (left) ── */}
          <div className="lg:col-span-2 bg-white rounded-xl border shadow-sm p-5 space-y-3 self-start">
            <div className="flex items-center justify-between">
              <h2 className="font-semibold text-gray-800 text-sm">Chapters</h2>
              <button
                onClick={toggleAll}
                className="text-xs text-blue-500 hover:text-blue-700"
              >
                {selectedChapters.size === book.chapters.length ? "Deselect all" : "Select all"}
              </button>
            </div>
            <p className="text-xs text-gray-400">{selectedChapters.size}/{book.chapters.length} selected</p>

            <div className="divide-y divide-gray-50 max-h-[500px] overflow-y-auto -mx-5 px-5">
              {book.chapters.map((ch) => (
                <label
                  key={ch.num}
                  className={`flex items-center gap-3 py-2.5 cursor-pointer hover:bg-gray-50 transition-colors rounded-lg px-1 -mx-1 ${
                    selectedChapters.has(ch.num) ? "bg-blue-50/60" : ""
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={selectedChapters.has(ch.num)}
                    onChange={() => toggleChapter(ch.num)}
                    className="rounded border-gray-300 text-blue-600 focus:ring-blue-500 shrink-0"
                  />
                  <span className="text-xs text-gray-400 w-6 text-right shrink-0">{ch.num}.</span>
                  <span className="text-sm text-gray-700 leading-tight">{ch.title}</span>
                </label>
              ))}
            </div>
          </div>

          {/* ── Right panel ── */}
          <div className="lg:col-span-3 space-y-5">

            {/* Generate form — always visible */}
            <div className="bg-white rounded-xl border shadow-sm p-6 space-y-5">
              <div className="flex items-center justify-between">
                <h2 className="font-semibold text-gray-800">Generate Questions</h2>
                {activeJobCount > 0 && (
                  <span className="flex items-center gap-1.5 text-xs bg-blue-50 text-blue-600 px-2.5 py-1 rounded-full font-medium">
                    <Loader2 size={11} className="animate-spin" />
                    {activeJobCount} job{activeJobCount > 1 ? "s" : ""} running
                  </span>
                )}
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                    Question Type
                  </label>
                  <Select
                    value={qtype}
                    onChange={setQtype}
                    options={[
                      { value: "short_answer", label: "Short Answer" },
                      { value: "mcq",          label: "Multiple Choice (MCQ)" },
                      { value: "true_false",   label: "True / False" },
                    ]}
                  />
                </div>
                <div>
                  <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                    Difficulty
                  </label>
                  <Select
                    value={difficulty}
                    onChange={setDifficulty}
                    options={[
                      { value: "all",    label: "Mixed — AI decides" },
                      { value: "easy",   label: "Easy — recall & definitions" },
                      { value: "medium", label: "Medium — apply & interpret" },
                      { value: "hard",   label: "Hard — multi-step analysis" },
                    ]}
                  />
                </div>
              </div>

              <div>
                <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                  Questions per Chapter
                </label>
                <input
                  type="number" min={1} max={50} value={count}
                  onChange={(e) => setCount(Math.max(1, parseInt(e.target.value) || 1))}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:outline-none"
                />
              </div>

              {/* Asset requirements */}
              <div>
                <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                  Include data-based questions
                </label>
                <div className="space-y-2">
                  <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                    <input
                      type="checkbox" checked={requireTable}
                      onChange={(e) => setRequireTable(e.target.checked)}
                      className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                    />
                    Table-based questions (built around a real chapter table)
                  </label>
                  <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                    <input
                      type="checkbox" checked={requireFigure}
                      onChange={(e) => setRequireFigure(e.target.checked)}
                      className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                    />
                    Graph/figure-based questions (built around a real chapter figure)
                  </label>
                </div>
              </div>

              {/* DeepSearch refine */}
              <div>
                <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
                  Quality
                </label>
                <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                  <input
                    type="checkbox" checked={deepSearch}
                    onChange={(e) => setDeepSearch(e.target.checked)}
                    className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                  />
                  DeepSearch refine (verify &amp; repair each question against the book before validation)
                </label>
              </div>

              {/* Summary */}
              <div className="bg-blue-50 border border-blue-100 rounded-xl px-4 py-3 text-sm text-blue-700 space-y-1">
                <p className="font-medium">
                  ~{estimated} questions from {activeChapters.length} chapter{activeChapters.length !== 1 ? "s" : ""}
                </p>
                {activeChapters.length < book.chapters.length && (
                  <p className="text-xs text-blue-500">
                    Selected: {activeChapters.map((c) => `Ch ${c.num}`).join(", ")}
                  </p>
                )}
                {difficulty !== "all" && (
                  <p className="text-xs text-blue-500 capitalize">Difficulty: {difficulty}</p>
                )}
              </div>

              {genError && (
                <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-4 py-2">
                  {genError}
                </p>
              )}

              <button
                onClick={handleGenerate}
                disabled={submitting || selectedChapters.size === 0}
                className="w-full bg-blue-600 text-white py-3 rounded-xl font-semibold hover:bg-blue-700 disabled:opacity-60 transition-colors flex items-center justify-center gap-2"
              >
                {submitting
                  ? <><Loader2 size={17} className="animate-spin" /> Queuing job…</>
                  : <><Zap size={17} /> Generate {estimated > 0 ? `~${estimated}` : ""} Questions</>}
              </button>
            </div>

            {/* Jobs error */}
            {jobsError && (
              <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-4 py-2">
                {jobsError}
              </p>
            )}

            {/* Jobs list */}
            {jobs.length > 0 && (
              <div className="bg-white rounded-xl border shadow-sm p-5 space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold text-gray-800 text-sm">Generation Jobs</h3>
                  <button
                    onClick={() => {
                      setJobs(prev => {
                        const next = prev.filter(j => j.status === "queued" || j.status === "processing");
                        storedIdsRef.current = mergeActiveJobIds(next, storedIdsRef.current);
                        return next;
                      });
                    }}
                    className="text-xs text-gray-400 hover:text-gray-600 flex items-center gap-1"
                  >
                    <RefreshCw size={11} /> Clear finished
                  </button>
                </div>
                <div className="space-y-2">
                  {jobs.map(job => (
                    <JobCard key={job.job_id} job={job} onDismiss={dismissJob} />
                  ))}
                </div>
              </div>
            )}

            {/* Quick nav to Q&A bank */}
            <a
              href="/questions"
              className="flex items-center justify-between bg-white border rounded-xl px-5 py-4 hover:border-blue-300 hover:shadow-sm transition-all text-sm text-gray-600 hover:text-blue-700"
            >
              <span className="flex items-center gap-2">
                <BookOpen size={16} className="text-blue-400" />
                View Q&amp;A Bank
              </span>
              <ChevronRight size={15} />
            </a>
          </div>
        </div>
      </main>
    </div>
  );
}
