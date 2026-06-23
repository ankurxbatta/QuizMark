"use client";
import { useEffect, useRef, useState } from "react";
import api, { API_URL } from "@/lib/api";
import {
  CheckCircle,
  Clock,
  LogOut,
  Loader2,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import { useRouter } from "next/navigation";
import Cookies from "js-cookie";
import MathText from "@/components/MathText";

// ─── Types ────────────────────────────────────────────────────────────────────

interface QuestionAsset {
  kind: string;
  caption?: string;
  alt_text?: string;
  table_html?: string;
  image_id?: string;
  source_page?: number;
}

interface Question {
  id: string;
  question_text: string;
  question_type: string;
  max_marks: number;
  topic_tag?: string;
  difficulty?: string;
  assets?: QuestionAsset[];
}

interface SubmissionResult {
  id: string;
  question_id: string;
  question_text: string;
  question_type: string;
  max_marks: number;
  answer_text: string;
  auto_mark: number | null;
  auto_feedback: string | null;
  override_mark: number | null;
  override_feedback: string | null;
  is_flagged: boolean;
  is_marked: boolean;
  assets?: QuestionAsset[];
}

// ─── Asset rendering ──────────────────────────────────────────────────────────

function QuestionAssets({ assets }: { assets?: QuestionAsset[] }) {
  if (!assets || assets.length === 0) return null;
  return (
    <div className="space-y-3">
      {assets.map((asset, idx) => (
        <div key={idx} className="space-y-1.5">
          {asset.table_html ? (
            <div
              className="overflow-x-auto border rounded-lg [&_table]:w-full [&_table]:text-sm [&_th]:border [&_th]:border-gray-200 [&_th]:bg-gray-50 [&_th]:px-3 [&_th]:py-2 [&_th]:text-left [&_th]:font-semibold [&_td]:border [&_td]:border-gray-200 [&_td]:px-3 [&_td]:py-2"
              dangerouslySetInnerHTML={{ __html: asset.table_html }}
            />
          ) : asset.image_id ? (
            <img
              src={`${API_URL}/api/v1/questions/assets/${asset.image_id}?token=${Cookies.get("token") || ""}`}
              alt={asset.alt_text || "Figure"}
              className="rounded border max-h-80"
            />
          ) : null}
          {asset.caption && (
            <p className="text-xs text-gray-400 italic">{asset.caption}</p>
          )}
        </div>
      ))}
    </div>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function extractMcqParts(text: string) {
  const toOptionMatches = (source: string, pattern: RegExp, offset = 0) =>
    Array.from(source.matchAll(pattern)).map((match) => {
      const markerOffset = match[0].search(/(?:Option\s*)?[A-D]/i);
      const index = offset + (match.index ?? 0) + Math.max(markerOffset, 0);
      return {
        letter: match[1].toUpperCase(),
        index,
        end: offset + (match.index ?? 0) + match[0].length,
      };
    });

  let matches = toOptionMatches(text, /^\s*(?:Option\s*)?([A-D])[).:\-]\s*/gim);
  let stemEnd = matches[0]?.index ?? 0;

  if (matches.length === 0) {
    const label = /\b(?:Options|Choices|Answers)\s*[:\-]\s*/i.exec(text);
    if (label) {
      const offset = (label.index ?? 0) + label[0].length;
      matches = toOptionMatches(
        text.slice(offset),
        /(?:^|[^A-Za-z0-9])(?:Option\s*)?([A-D])[).:\-]\s*/gi,
        offset
      );
      stemEnd = label.index ?? 0;
    }
  }

  if (matches.length === 0) {
    return { stem: text.trim(), options: [] as { letter: string; text: string }[] };
  }
  const stem = text.slice(0, stemEnd).trim();
  const options: { letter: string; text: string }[] = [];
  for (let i = 0; i < matches.length; i++) {
    const letter = matches[i].letter;
    const start = matches[i].end;
    const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
    const optionText = text.slice(start, end).trim();
    if (optionText) options.push({ letter, text: optionText });
  }
  return { stem, options };
}

/** Strip the [Route:X|Conf:Y] prefix the backend prepends to auto_feedback. */
function cleanFeedback(feedback: string | null): string {
  if (!feedback) return "";
  return feedback.replace(/^\[Route:[A-Z]+\|Conf:[0-9.]+\]\s*/, "").trim();
}

function markColor(mark: number, max: number): string {
  if (max === 0) return "text-gray-500";
  const pct = mark / max;
  if (pct >= 0.75) return "text-emerald-600";
  if (pct >= 0.5) return "text-amber-600";
  return "text-red-500";
}

// ─── Results view (shown after submission) ────────────────────────────────────

function ResultsView({
  submissionIds,
  questions,
  answers,
  onSignOut,
}: {
  submissionIds: string[];
  questions: Question[];
  answers: Record<string, string>;
  onSignOut: () => void;
}) {
  const [results, setResults] = useState<SubmissionResult[]>([]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const attemptsRef = useRef(0);
  const MAX_POLLS = 30; // give up after ~60 s

  const fetchResults = async () => {
    try {
      const res = await api.get<SubmissionResult[]>("/submissions/my");
      // Only keep the submissions we just made
      const relevant = res.data.filter((s) => submissionIds.includes(s.id));
      setResults(relevant);

      const allMarked = relevant.length === submissionIds.length &&
        relevant.every((s) => s.is_marked);

      attemptsRef.current += 1;
      if (allMarked || attemptsRef.current >= MAX_POLLS) {
        if (pollRef.current) clearInterval(pollRef.current);
      }
    } catch {
      // ignore transient errors while polling
    }
  };

  useEffect(() => {
    fetchResults();
    pollRef.current = setInterval(fetchResults, 2000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const allMarked = results.length === submissionIds.length && results.every((s) => s.is_marked);
  const pendingCount = submissionIds.length - results.filter((s) => s.is_marked).length;

  // Build score summary
  const totalEarned = results.reduce((acc, s) => {
    const mark = s.override_mark ?? s.auto_mark ?? 0;
    return acc + mark;
  }, 0);
  const totalAvailable = results.reduce((acc, s) => acc + (s.max_marks ?? 0), 0);
  const pct = totalAvailable > 0 ? Math.round((totalEarned / totalAvailable) * 100) : 0;

  // Map question_id → question for displaying topic/difficulty
  const questionMap = Object.fromEntries(questions.map((q) => [q.id, q]));

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-indigo-700">Your Results</h1>
          <p className="text-xs text-gray-400 mt-0.5">
            {allMarked
              ? `${results.length} question${results.length !== 1 ? "s" : ""} marked`
              : `Marking ${pendingCount} answer${pendingCount !== 1 ? "s" : ""}…`}
          </p>
        </div>
        <button
          onClick={onSignOut}
          className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-red-500 transition-colors"
        >
          <LogOut size={15} /> Sign out
        </button>
      </header>

      <main className="max-w-3xl mx-auto px-8 py-10 space-y-6">
        {/* Score card */}
        {allMarked && results.length > 0 && (
          <div className="bg-white rounded-2xl border shadow-sm p-8 text-center">
            <p className="text-sm font-medium text-gray-500 mb-1">Total Score</p>
            <p className={`text-5xl font-bold mb-1 ${markColor(totalEarned, totalAvailable)}`}>
              {totalEarned % 1 === 0 ? totalEarned : totalEarned.toFixed(1)}
              <span className="text-2xl text-gray-400 font-normal">
                /{totalAvailable}
              </span>
            </p>
            <p className="text-gray-400 text-sm">{pct}%</p>
          </div>
        )}

        {/* Pending banner */}
        {!allMarked && (
          <div className="flex items-center gap-3 bg-indigo-50 border border-indigo-200 rounded-xl px-5 py-4 text-sm text-indigo-700">
            <Loader2 size={16} className="animate-spin shrink-0" />
            AI is marking your answers — this usually takes 5–20 seconds. Results will appear below automatically.
          </div>
        )}

        {/* Per-question results */}
        {submissionIds.map((sid, i) => {
          const result = results.find((r) => r.id === sid);
          const question = result ? questionMap[result.question_id] : questions[i];
          const isOpen = !!expanded[sid];

          const displayMark = result ? (result.override_mark ?? result.auto_mark) : null;
          const displayFeedback = result
            ? cleanFeedback(result.override_feedback ?? result.auto_feedback)
            : null;
          const studentAnswer = result?.answer_text ?? answers[question?.id ?? ""] ?? "";

          return (
            <div key={sid} className="bg-white rounded-xl border shadow-sm overflow-hidden">
              {/* Header row */}
              <button
                type="button"
                onClick={() => setExpanded((e) => ({ ...e, [sid]: !isOpen }))}
                className="w-full flex items-center justify-between px-6 py-4 text-left hover:bg-gray-50 transition-colors"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <span className="text-xs font-bold text-indigo-500 uppercase shrink-0">
                    Q{i + 1}
                  </span>
                  <MathText
                    text={result?.question_text ?? question?.question_text ?? "—"}
                    className="text-sm text-gray-700 truncate block"
                  />
                </div>
                <div className="flex items-center gap-3 shrink-0 ml-4">
                  {result?.is_flagged && (
                    <span className="flex items-center gap-1 text-xs text-amber-600 bg-amber-50 px-2 py-0.5 rounded-full">
                      <AlertTriangle size={11} /> Under review
                    </span>
                  )}
                  {!result?.is_marked ? (
                    <span className="flex items-center gap-1.5 text-xs text-gray-400">
                      <Loader2 size={12} className="animate-spin" /> Marking…
                    </span>
                  ) : displayMark !== null && result?.max_marks ? (
                    <span className={`text-sm font-semibold ${markColor(displayMark, result.max_marks)}`}>
                      {displayMark % 1 === 0 ? displayMark : displayMark.toFixed(1)}/{result.max_marks}
                    </span>
                  ) : null}
                  {isOpen ? <ChevronUp size={15} className="text-gray-400" /> : <ChevronDown size={15} className="text-gray-400" />}
                </div>
              </button>

              {/* Expanded detail */}
              {isOpen && (
                <div className="border-t px-6 py-5 space-y-4 bg-gray-50">
                  {/* Attached assets (table / figure) */}
                  <QuestionAssets assets={result?.assets ?? question?.assets} />

                  {/* Your answer */}
                  <div>
                    <p className="text-xs font-semibold text-gray-400 uppercase mb-1">Your answer</p>
                    <p className="text-sm text-gray-700 whitespace-pre-wrap">
                      {studentAnswer || <span className="italic text-gray-400">No answer recorded</span>}
                    </p>
                  </div>

                  {/* Mark + feedback */}
                  {result?.is_marked ? (
                    <div className="space-y-3">
                      <div className="flex items-center gap-3">
                        <CheckCircle size={16} className="text-emerald-500 shrink-0" />
                        <div>
                          <p className="text-xs font-semibold text-gray-400 uppercase">Mark</p>
                          <p className={`text-lg font-bold ${markColor(displayMark ?? 0, result.max_marks ?? 1)}`}>
                            {displayMark !== null
                              ? `${displayMark % 1 === 0 ? displayMark : displayMark.toFixed(1)} / ${result.max_marks}`
                              : "—"}
                          </p>
                        </div>
                      </div>
                      {displayFeedback && (
                        <div>
                          <p className="text-xs font-semibold text-gray-400 uppercase mb-1">Feedback</p>
                          <MathText text={displayFeedback} className="text-sm text-gray-700 leading-relaxed block" />
                        </div>
                      )}
                      {result.is_flagged && (
                        <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 text-xs text-amber-700">
                          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                          Your answer has been flagged for instructor review. The mark shown is preliminary and may be updated.
                        </div>
                      )}
                      {result.override_mark !== null && result.override_mark !== undefined && (
                        <div className="text-xs text-indigo-600 bg-indigo-50 rounded-lg px-3 py-2">
                          This mark was reviewed and updated by your instructor.
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 text-sm text-gray-400">
                      <Loader2 size={14} className="animate-spin" /> Marking in progress…
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </main>
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function AssessmentPage() {
  const [questions, setQuestions] = useState<Question[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submissionIds, setSubmissionIds] = useState<string[]>([]);
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const router = useRouter();

  const signOut = () => {
    Cookies.remove("token");
    Cookies.remove("role");
    router.push("/");
  };

  useEffect(() => {
    // Load assigned questions alongside existing submissions so a reload
    // after submitting shows results instead of a blank, resubmittable form.
    Promise.all([
      api.get<Question[]>("/questions/assessment"),
      api.get<SubmissionResult[]>("/submissions/my").catch(() => ({ data: [] as SubmissionResult[] })),
    ])
      .then(([qRes, sRes]) => {
        const assigned = qRes.data;
        const mySubs = sRes.data.filter((s) => assigned.some((q) => q.id === s.question_id));
        const answeredIds = new Set(mySubs.map((s) => s.question_id));
        const unanswered = assigned.filter((q) => !answeredIds.has(q.id));

        if (assigned.length > 0 && unanswered.length === 0) {
          setQuestions(assigned);
          setSubmissionIds(mySubs.map((s) => s.id));
          setSubmitted(true);
        } else {
          setQuestions(unanswered);
        }
      })
      .catch(() => {});
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    const payloads = Object.entries(answers)
      .map(([question_id, answer_text]) => ({ question_id, answer_text: answer_text.trim() }))
      .filter((item) => item.answer_text.length > 0);

    const ids: string[] = [];
    try {
      for (const item of payloads) {
        const res = await api.post("/submissions/", item, { timeout: 20000 });
        ids.push(res.data.id);
      }
      setSubmissionIds(ids);
      setSubmitted(true);
    } catch (err: any) {
      setError(err.response?.data?.detail || "Submission failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <ResultsView
        submissionIds={submissionIds}
        questions={questions}
        answers={answers}
        onSignOut={signOut}
      />
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-indigo-700">Assessment</h1>
          <p className="text-xs text-gray-400 mt-0.5">
            {questions.length} question{questions.length !== 1 ? "s" : ""}
          </p>
        </div>
        <div className="flex items-center gap-4">
          <span className="flex items-center gap-1.5 text-sm text-gray-500">
            <Clock size={15} /> {questions.length} questions
          </span>
          <button
            onClick={signOut}
            className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-red-500 transition-colors"
          >
            <LogOut size={15} /> Sign out
          </button>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-8 py-10">
        {questions.length === 0 && (
          <div className="text-center text-gray-400 py-20">
            No questions assigned yet. Please check back later.
          </div>
        )}
        <form onSubmit={handleSubmit} className="space-y-6">
          {questions.map((q, i) => (
            <div key={q.id} className="bg-white rounded-xl border shadow-sm p-6 space-y-3">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-bold text-indigo-500 uppercase">Q{i + 1}</span>
                  {q.topic_tag && (
                    <span className="text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">
                      {q.topic_tag}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  {q.difficulty && (
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                      q.difficulty === "hard"
                        ? "bg-red-50 text-red-600"
                        : q.difficulty === "medium"
                        ? "bg-amber-50 text-amber-600"
                        : "bg-green-50 text-green-600"
                    }`}>
                      {q.difficulty}
                    </span>
                  )}
                  <span className="text-xs text-gray-400">
                    {q.max_marks} mark{q.max_marks !== 1 ? "s" : ""}
                  </span>
                </div>
              </div>

              <QuestionAssets assets={q.assets} />

              {q.question_type === "mcq" ? (() => {
                const { stem, options } = extractMcqParts(q.question_text);
                return (
                  <div className="space-y-3">
                    <MathText text={stem || q.question_text} className="text-gray-800 font-medium block" />
                    {options.length > 0 ? (
                      <div className="space-y-2">
                        {options.map(({ letter, text: optText }) => (
                          <label
                            key={letter}
                            className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                              answers[q.id] === letter
                                ? "border-indigo-400 bg-indigo-50"
                                : "border-gray-200 hover:border-gray-300"
                            }`}
                          >
                            <input
                              type="radio"
                              name={`q-${q.id}`}
                              value={letter}
                              checked={answers[q.id] === letter}
                              onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                              required
                              className="mt-0.5 accent-indigo-600"
                            />
                            <span className="text-sm text-gray-700">
                              <span className="font-semibold">{letter}.</span>{" "}
                              <MathText text={optText} />
                            </span>
                          </label>
                        ))}
                      </div>
                    ) : (
                      <>
                        <p className="text-xs text-amber-600">
                          Options could not be parsed — please write your answer below.
                        </p>
                        <textarea
                          rows={3}
                          placeholder="Your answer…"
                          value={answers[q.id] || ""}
                          onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                          required
                          className="w-full border border-gray-300 rounded-lg px-4 py-3 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                        />
                      </>
                    )}
                  </div>
                );
              })() : q.question_type === "true_false" ? (
                <div className="space-y-3">
                  <MathText text={q.question_text} className="text-gray-800 font-medium block" />
                  <div className="flex gap-3">
                    {(["True", "False"] as const).map((opt) => (
                      <label
                        key={opt}
                        className={`flex items-center gap-2 flex-1 justify-center py-3 rounded-lg border cursor-pointer transition-colors ${
                          answers[q.id] === opt
                            ? "border-indigo-400 bg-indigo-50 text-indigo-700 font-semibold"
                            : "border-gray-200 hover:border-gray-300 text-gray-700"
                        }`}
                      >
                        <input
                          type="radio"
                          name={`q-${q.id}`}
                          value={opt}
                          checked={answers[q.id] === opt}
                          onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                          required
                          className="sr-only"
                        />
                        {opt}
                      </label>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <MathText text={q.question_text} className="text-gray-800 font-medium block" />
                  <textarea
                    rows={4}
                    placeholder="Write your answer here…"
                    value={answers[q.id] || ""}
                    onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                    required
                    className="w-full border border-gray-300 rounded-lg px-4 py-3 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none"
                  />
                </div>
              )}
            </div>
          ))}

          {questions.length > 0 && (
            <div className="space-y-3 pb-10">
              {error && (
                <p className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">{error}</p>
              )}
              <button
                type="submit"
                disabled={submitting}
                className="w-full bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-60 transition-colors"
              >
                {submitting ? "Submitting…" : "Submit Assessment"}
              </button>
            </div>
          )}
        </form>
      </main>
    </div>
  );
}
