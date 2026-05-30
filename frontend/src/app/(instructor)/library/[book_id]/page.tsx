"use client";
import { useState, useEffect, useRef, use } from "react";
import { useRouter } from "next/navigation";
import api from "@/lib/api";
import Select from "@/components/Select";
import {
  BookOpen, Database, Layers, Table2, FlaskConical, ImageIcon,
  Loader2, ArrowLeft, Zap, CheckCircle, RefreshCw, CalendarDays,
  ChevronRight,
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
  status: "queued" | "processing" | "done" | "failed";
  total_chapters: number;
  chapters_done: number;
  current_chapter_title: string | null;
  questions_created: number;
  progress_message: string | null;
  error_message?: string | null;
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
          className="bg-indigo-500 h-2 rounded-full transition-all duration-700"
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
  const [job, setJob]               = useState<Job | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [genError, setGenError]     = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

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

  // Poll job
  useEffect(() => {
    if (!job || job.status === "done" || job.status === "failed") return;
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await api.get(`/questions/jobs/${job.job_id}`);
        setJob(data);
        if (data.status === "done" || data.status === "failed")
          clearInterval(pollRef.current!);
      } catch { /* ignore */ }
    }, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [job?.job_id, job?.status]);

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
        `/questions/generate/from-book?book_id=${encodeURIComponent(bookId)}&question_type=${qtype}&count_per_chapter=${count}&difficulty=${difficulty}${chParam}`
      );
      setJob(data);
    } catch (err: any) {
      setGenError(err.response?.data?.detail || "Generation failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const resetJob = () => {
    setJob(null);
    setGenError("");
    if (pollRef.current) clearInterval(pollRef.current);
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

  const jobPct = job
    ? job.status === "done" ? 100
    : job.total_chapters > 0 ? Math.min((job.chapters_done / job.total_chapters) * 100, 95)
    : 5
    : 0;

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b px-8 py-4 shadow-sm">
        <button
          onClick={() => router.push("/library")}
          className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-indigo-600 mb-2 transition-colors"
        >
          <ArrowLeft size={14} /> Library
        </button>
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 bg-indigo-100 rounded-xl flex items-center justify-center shrink-0">
            <BookOpen size={20} className="text-indigo-600" />
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
          <Stat icon={Layers}     value={book.total_chunks}   label="chunks"   colour="bg-indigo-50 text-indigo-700" />
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
                className="text-xs text-indigo-500 hover:text-indigo-700"
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
                    selectedChapters.has(ch.num) ? "bg-indigo-50/60" : ""
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={selectedChapters.has(ch.num)}
                    onChange={() => toggleChapter(ch.num)}
                    className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500 shrink-0"
                  />
                  <span className="text-xs text-gray-400 w-6 text-right shrink-0">{ch.num}.</span>
                  <span className="text-sm text-gray-700 leading-tight">{ch.title}</span>
                </label>
              ))}
            </div>
          </div>

          {/* ── Generate panel (right) ── */}
          <div className="lg:col-span-3 space-y-5">
            <div className="bg-white rounded-xl border shadow-sm p-6 space-y-5">
              <h2 className="font-semibold text-gray-800">Generate Questions</h2>

              {!job ? (
                <>
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
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                    />
                  </div>

                  {/* Summary */}
                  <div className="bg-indigo-50 border border-indigo-100 rounded-xl px-4 py-3 text-sm text-indigo-700 space-y-1">
                    <p className="font-medium">
                      ~{estimated} questions from {activeChapters.length} chapter{activeChapters.length !== 1 ? "s" : ""}
                    </p>
                    {activeChapters.length < book.chapters.length && (
                      <p className="text-xs text-indigo-500">
                        Selected: {activeChapters.map((c) => `Ch ${c.num}`).join(", ")}
                      </p>
                    )}
                    {difficulty !== "all" && (
                      <p className="text-xs text-indigo-500 capitalize">Difficulty: {difficulty}</p>
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
                    className="w-full bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-60 transition-colors flex items-center justify-center gap-2"
                  >
                    {submitting
                      ? <><Loader2 size={17} className="animate-spin" /> Starting…</>
                      : <><Zap size={17} /> Generate {estimated > 0 ? `~${estimated}` : ""} Questions</>}
                  </button>
                </>
              ) : (
                /* Job progress */
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <span className={`text-sm px-3 py-1 rounded-full font-medium ${
                      job.status === "done"       ? "bg-green-100 text-green-700" :
                      job.status === "failed"     ? "bg-red-100 text-red-700"     :
                      job.status === "processing" ? "bg-blue-100 text-blue-700"   :
                      "bg-gray-100 text-gray-500"
                    }`}>{job.status}</span>

                    {(job.status === "done" || job.status === "failed") && (
                      <button
                        onClick={resetJob}
                        className="text-sm text-gray-400 hover:text-gray-600 flex items-center gap-1"
                      >
                        <RefreshCw size={13} /> Generate again
                      </button>
                    )}
                  </div>

                  <ProgressBar
                    pct={jobPct}
                    label={`Chapters: ${job.chapters_done} / ${job.total_chapters}`}
                  />

                  {job.current_chapter_title && job.status === "processing" && (
                    <p className="text-sm text-gray-500">
                      Generating: <span className="text-gray-700 font-medium">{job.current_chapter_title}</span>
                    </p>
                  )}

                  {job.progress_message && (
                    <p className="text-sm text-gray-600 bg-gray-50 border border-gray-200 rounded-xl px-4 py-3">
                      {job.progress_message}
                    </p>
                  )}

                  {job.status === "done" && (
                    <div className="flex items-center gap-3 bg-green-50 border border-green-200 rounded-xl px-5 py-4 text-green-700">
                      <CheckCircle size={20} />
                      <div>
                        <p className="font-semibold">{job.questions_created} questions generated</p>
                        <p className="text-sm text-green-600 mt-0.5">
                          Ready in the Q&amp;A Bank —{" "}
                          <a href="/questions" className="underline hover:text-green-800">View now</a>
                        </p>
                      </div>
                    </div>
                  )}

                  {job.status === "failed" && (
                    <div className="bg-red-50 border border-red-200 rounded-xl px-5 py-4 text-red-700 text-sm">
                      <p className="font-semibold mb-1">Generation failed</p>
                      <p>{job.error_message || "An error occurred."}</p>
                    </div>
                  )}

                  {/* Stats while running */}
                  {job.status !== "failed" && (
                    <div className="grid grid-cols-2 gap-3 text-center">
                      <div className="bg-gray-50 rounded-xl p-3">
                        <p className="text-2xl font-bold text-indigo-600">{job.questions_created}</p>
                        <p className="text-xs text-gray-500 mt-0.5">Questions so far</p>
                      </div>
                      <div className="bg-gray-50 rounded-xl p-3">
                        <p className="text-2xl font-bold text-gray-700">{job.chapters_done}</p>
                        <p className="text-xs text-gray-500 mt-0.5">Chapters done</p>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Quick nav to Q&A bank */}
            <a
              href="/questions"
              className="flex items-center justify-between bg-white border rounded-xl px-5 py-4 hover:border-indigo-300 hover:shadow-sm transition-all text-sm text-gray-600 hover:text-indigo-700"
            >
              <span className="flex items-center gap-2">
                <BookOpen size={16} className="text-indigo-400" />
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
