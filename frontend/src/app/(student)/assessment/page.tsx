"use client";
import { useEffect, useState } from "react";
import api from "@/lib/api";
import { CheckCircle, Clock } from "lucide-react";

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
    return { stem: text.trim(), options: [] as string[] };
  }

  const firstIndex = matches[0].index ?? 0;
  const stem = text.slice(0, firstIndex).trim();
  const options: string[] = [];

  for (let i = 0; i < matches.length; i += 1) {
    const start = (matches[i].index ?? 0) + matches[i][0].length;
    const end = i + 1 < matches.length ? (matches[i + 1].index ?? text.length) : text.length;
    const optionText = text.slice(start, end).trim();
    if (optionText) options.push(optionText);
  }

  return { stem, options };
}

export default function AssessmentPage() {
  const [questions, setQuestions] = useState<Question[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.get("/questions/").then((r) => setQuestions(r.data.slice(0, 10)));
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    const payloads = Object.entries(answers)
      .map(([question_id, answer_text]) => ({
        question_id,
        answer_text: answer_text.trim(),
      }))
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
          <p className="text-gray-500">Your answers have been submitted for marking. You will be notified when results are available.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b px-8 py-4 flex items-center justify-between shadow-sm">
        <h1 className="text-xl font-bold text-indigo-700">Statistics Assessment</h1>
        <div className="flex items-center gap-2 text-sm text-gray-500">
          <Clock size={16} /> <span>{questions.length} questions</span>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-8 py-10">
        <form onSubmit={handleSubmit} className="space-y-6">
          {questions.map((q, i) => (
            <div key={q.id} className="bg-white rounded-xl border shadow-sm p-6 space-y-3">
              <div className="flex items-start justify-between">
                <span className="text-xs font-bold text-indigo-500 uppercase">Q{i + 1}</span>
                <span className="text-xs text-gray-400">{q.max_marks} marks</span>
              </div>
              {q.question_type === "mcq" ? (() => {
                const { stem, options } = extractMcqParts(q.question_text);
                return (
                  <div className="space-y-3">
                    <p className="text-gray-800 font-medium">{stem || q.question_text}</p>
                    {options.length > 0 ? (
                      <div className="space-y-2">
                        {options.map((opt, idx) => (
                          <label key={idx} className="flex items-start gap-2 text-sm text-gray-700">
                            <input
                              type="radio"
                              name={`q-${q.id}`}
                              value={opt}
                              checked={answers[q.id] === opt}
                              onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                              required
                              className="mt-1"
                            />
                            <span>{String.fromCharCode(65 + idx)}. {opt}</span>
                          </label>
                        ))}
                      </div>
                    ) : (
                      <div className="space-y-2">
                        <p className="text-xs text-amber-600">Options not detected; please answer in text.</p>
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
                );
              })() : q.question_type === "true_false" ? (
                <div className="space-y-3">
                  <p className="text-gray-800 font-medium">{q.question_text}</p>
                  <div className="space-y-2">
                    {(["True", "False"] as const).map((opt) => (
                      <label key={opt} className="flex items-start gap-2 text-sm text-gray-700">
                        <input
                          type="radio"
                          name={`q-${q.id}`}
                          value={opt}
                          checked={answers[q.id] === opt}
                          onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
                          required
                          className="mt-1"
                        />
                        <span>{opt}</span>
                      </label>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <p className="text-gray-800 font-medium">{q.question_text}</p>
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
            <div className="space-y-3">
              <button
                type="submit"
                disabled={submitting}
                className="w-full bg-indigo-600 text-white py-3 rounded-xl font-semibold hover:bg-indigo-700 disabled:opacity-60 transition-colors"
              >
                {submitting ? "Submitting…" : "Submit Assessment"}
              </button>
              {error && (
                <p className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">
                  {error}
                </p>
              )}
            </div>
          )}
        </form>
      </main>
    </div>
  );
}
