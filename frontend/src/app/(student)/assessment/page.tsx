"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";
import { CheckCircle, Clock, LogOut } from "lucide-react";
import { useRouter } from "next/navigation";
import Cookies from "js-cookie";

interface Question {
  id: string;
  question_text: string;
  question_type: string;
  max_marks: number;
}

function extractMcqParts(text: string) {
  const pattern = /([A-D])[).:\-]\s*/g;
  const matches = Array.from(text.matchAll(pattern));
  if (matches.length === 0) {
    return { stem: text.trim(), options: [] as { letter: string; text: string }[] };
  }
  const firstIndex = matches[0].index ?? 0;
  const stem = text.slice(0, firstIndex).trim();
  const options: { letter: string; text: string }[] = [];
  for (let i = 0; i < matches.length; i += 1) {
    const letter = matches[i][1].toUpperCase();
    const start = (matches[i].index ?? 0) + matches[i][0].length;
    const end = i + 1 < matches.length ? (matches[i + 1].index ?? text.length) : text.length;
    const optionText = text.slice(start, end).trim();
    if (optionText) options.push({ letter, text: optionText });
  }
  return { stem, options };
}

export default function AssessmentPage() {
  const [questions, setQuestions] = useState<Question[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
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
    api.get("/questions/").then((r) => setQuestions(r.data.slice(0, 10))).catch(() => {});
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    const payloads = Object.entries(answers)
      .map(([question_id, answer_text]) => ({ question_id, answer_text: answer_text.trim() }))
      .filter((item) => item.answer_text.length > 0);
    try {
      for (const item of payloads) {
        await api.post("/submissions/", item, { timeout: 20000 });
      }
      setSubmitted(true);
    } catch (err: any) {
      setError(err.response?.data?.detail || "Submission failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  if (submitted) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <div className="bg-white rounded-2xl shadow-xl p-12 text-center max-w-md">
          <CheckCircle size={48} className="mx-auto text-green-500 mb-4" />
          <h2 className="text-2xl font-bold text-gray-800 mb-2">Submitted!</h2>
          <p className="text-gray-500 mb-6">Your answers have been submitted for marking. Results will be available shortly.</p>
          <button onClick={signOut}
            className="text-sm text-indigo-600 hover:text-indigo-800 underline underline-offset-2">
            Sign out
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center justify-between shadow-sm">
        <div>
          <h1 className="text-xl font-bold text-indigo-700">Assessment</h1>
          <p className="text-xs text-gray-400 mt-0.5">{questions.length} question{questions.length !== 1 ? "s" : ""}</p>
        </div>
        <div className="flex items-center gap-4">
          <span className="flex items-center gap-1.5 text-sm text-gray-500">
            <Clock size={15} /> {questions.length} questions
          </span>
          <button onClick={signOut}
            className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-red-500 transition-colors">
            <LogOut size={15} /> Sign out
          </button>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-8 py-10">
        {questions.length === 0 && (
          <div className="text-center text-gray-400 py-20">No questions available yet. Please check back later.</div>
        )}
        <form onSubmit={handleSubmit} className="space-y-6">
          {questions.map((q, i) => (
            <div key={q.id} className="bg-white rounded-xl border shadow-sm p-6 space-y-3">
              <div className="flex items-start justify-between">
                <span className="text-xs font-bold text-indigo-500 uppercase">Q{i + 1}</span>
                <span className="text-xs text-gray-400">{q.max_marks} mark{q.max_marks !== 1 ? "s" : ""}</span>
              </div>

              {q.question_type === "mcq" ? (() => {
                const { stem, options } = extractMcqParts(q.question_text);
                return (
                  <div className="space-y-3">
                    <p className="text-gray-800 font-medium">{stem || q.question_text}</p>
                    {options.length > 0 ? (
                      <div className="space-y-2">
                        {options.map(({ letter, text: optText }) => (
                          <label key={letter}
                            className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                              answers[q.id] === letter
                                ? "border-indigo-400 bg-indigo-50"
                                : "border-gray-200 hover:border-gray-300"
                            }`}>
                            <input
                              type="radio"
                              name={`q-${q.id}`}
                              // Submit the letter (A/B/C/D) so the backend MCQ parser can read it
                              value={letter}
                              checked={answers[q.id] === letter}
                              onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                              required
                              className="mt-0.5 accent-indigo-600"
                            />
                            <span className="text-sm text-gray-700"><span className="font-semibold">{letter}.</span> {optText}</span>
                          </label>
                        ))}
                      </div>
                    ) : (
                      <>
                        <p className="text-xs text-amber-600">Options could not be parsed — please write your answer below.</p>
                        <textarea rows={3} placeholder="Your answer…"
                          value={answers[q.id] || ""}
                          onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                          required
                          className="w-full border border-gray-300 rounded-lg px-4 py-3 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none" />
                      </>
                    )}
                  </div>
                );
              })() : q.question_type === "true_false" ? (
                <div className="space-y-3">
                  <p className="text-gray-800 font-medium">{q.question_text}</p>
                  <div className="flex gap-3">
                    {(["True", "False"] as const).map((opt) => (
                      <label key={opt}
                        className={`flex items-center gap-2 flex-1 justify-center py-3 rounded-lg border cursor-pointer transition-colors ${
                          answers[q.id] === opt
                            ? "border-indigo-400 bg-indigo-50 text-indigo-700 font-semibold"
                            : "border-gray-200 hover:border-gray-300 text-gray-700"
                        }`}>
                        <input type="radio" name={`q-${q.id}`} value={opt}
                          checked={answers[q.id] === opt}
                          onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                          required className="sr-only" />
                        {opt}
                      </label>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <p className="text-gray-800 font-medium">{q.question_text}</p>
                  <textarea rows={4} placeholder="Write your answer here…"
                    value={answers[q.id] || ""}
                    onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                    required
                    className="w-full border border-gray-300 rounded-lg px-4 py-3 text-sm focus:ring-2 focus:ring-indigo-500 focus:outline-none" />
                </div>
              )}
            </div>
          ))}

          {questions.length > 0 && (
            <div className="space-y-3 pb-10">
              {error && <p className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">{error}</p>}
              <button type="submit" disabled={submitting}
                className="w-full bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-60 transition-colors">
                {submitting ? "Submitting…" : "Submit Assessment"}
              </button>
            </div>
          )}
        </form>
      </main>
    </div>
  );
}
